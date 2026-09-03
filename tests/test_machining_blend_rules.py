# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the blend rules: corner radii, chamfer angles and seal edges.

The corner-radius limits come from the tool library rather than the material,
so most of what is checked here is that the same corner changes verdict when
the shop's cutters do.

The other half of the work is refusing radii that are not corners at all. A
floor-to-wall blend and a plan-view corner are the same surface locally, and
only the second one is set by the cutter's diameter -- flag both and every
pocket with a radiused floor collects an error it does not deserve.
"""

import unittest

from OCP.BRep import BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.config import MachiningConfig, ToolEntry
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# =============================================================================
# Shapes
# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def _box(p0, p1) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def _edges(shape: TopoDS_Shape) -> list[TopoDS_Edge]:
    found = []
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        found.append(TopoDS.Edge_s(explorer.Current()))
        explorer.Next()
    return found


def _ends(edge: TopoDS_Edge) -> tuple[gp_Pnt, gp_Pnt]:
    return (
        BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge)),
        BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge)),
    )


def _fillet(shape: TopoDS_Shape, radius: float, wanted) -> TopoDS_Shape:
    builder = BRepFilletAPI_MakeFillet(shape)
    for edge in _edges(shape):
        if wanted(*_ends(edge)):
            builder.Add(radius, edge)
    builder.Build()
    return builder.Shape()


def _chamfer(shape: TopoDS_Shape, distance: float, wanted) -> TopoDS_Shape:
    builder = BRepFilletAPI_MakeChamfer(shape)
    for edge in _edges(shape):
        if wanted(*_ends(edge)):
            builder.Add(distance, edge)
    builder.Build()
    return builder.Shape()


def block() -> TopoDS_Shape:
    """The 100 x 80 x 40 block every blend is put on."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def pocket_block() -> TopoDS_Shape:
    """The block with a 70 x 50 pocket 20 mm deep in its top."""
    return _cut(block(), _box((15, 15, 20), (85, 65, 41)))


def upright(p0: gp_Pnt, p1: gp_Pnt) -> bool:
    return abs(p0.X() - p1.X()) < 1e-6 and abs(p0.Y() - p1.Y()) < 1e-6


def on_plane_z(height: float):
    def wanted(p0: gp_Pnt, p1: gp_Pnt) -> bool:
        return abs(p0.Z() - height) < 1e-6 and abs(p1.Z() - height) < 1e-6

    return wanted


def circular_at_z(height: float):
    def wanted(p0: gp_Pnt, p1: gp_Pnt) -> bool:
        return (
            abs(p0.Z() - height) < 1e-6
            and abs(p1.Z() - height) < 1e-6
            and p0.Distance(p1) < 1e-6
        )

    return wanted


def pocket_corner(p0: gp_Pnt, p1: gp_Pnt) -> bool:
    """An upright edge inside the pocket, so its fillet is a plan-view corner."""
    if not upright(p0, p1):
        return False
    return 10.0 < p0.X() < 90.0 and 10.0 < p0.Y() < 70.0


def make_pocket_with_corner_radius(radius: float) -> TopoDS_Shape:
    return _fillet(pocket_block(), radius, pocket_corner)


def make_pocket_with_floor_radius(radius: float) -> TopoDS_Shape:
    """The same pocket blended where its walls meet the floor.

    A bull-nose or ball mill leaves this radius, and those come in far smaller
    sizes than an end mill's diameter allows in a corner.
    """
    return _fillet(pocket_block(), radius, on_plane_z(20.0))


def make_bore_with_chamfer(distance: float) -> TopoDS_Shape:
    """A 16 mm bore with its rim broken at 45 degrees."""
    drill = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 8.0, 50.0
    )
    return _chamfer(_cut(block(), drill.Shape()), distance, circular_at_z(40.0))


def make_knife_edge_ring() -> TopoDS_Shape:
    """A metal-seal ridge: two coaxial cones meeting at a sharp circle.

    The profile rises from 10 mm radius to 12 and falls straight back, so the
    material left at the ridge is a 53 degree wedge with no flat on it at all
    -- the knife edge on a vacuum flange.
    """
    lower = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 10.0, 12.0, 1.0
    ).Shape()
    upper = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(0, 0, 1), gp_Dir(0, 0, 1)), 12.0, 10.0, 1.0
    ).Shape()
    return _fuse(lower, upper)


def make_stacked_tapers() -> TopoDS_Shape:
    """Two coaxial cones meeting at an ordinary corner rather than a knife.

    Same construction as the seal ring, but the upper taper leans the other
    way and leaves a blunt 122 degree wedge. Nothing about it needs
    protecting from the deburring bench.
    """
    lower = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 10.0, 12.0, 1.0
    ).Shape()
    upper = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(0, 0, 1), gp_Dir(0, 0, 1)), 12.0, 12.5, 5.0
    ).Shape()
    return _fuse(lower, upper)


# =============================================================================
# Harness
# =============================================================================


def analyse(shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def _run(data, rule, target="N/A", limit="N/A", severity="WARNING"):
    check_class = get_check_class(rule)
    assert check_class is not None
    return check_class().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


def rule_check(shape, rule, target="N/A", limit="N/A", severity="WARNING"):
    return _run(analyse(shape), rule, target, limit, severity)


def rule_check_with_tools(shape, rule, diameters, severity="WARNING"):
    """The same rule asked of a shop whose end mills are the given sizes."""
    data = analyse(shape)
    config = MachiningConfig()
    config.tool_library = [
        ToolEntry(
            type="end_mill",
            min_diameter_mm=diameter,
            max_diameter_mm=diameter,
            unit="metric",
        )
        for diameter in diameters
    ]
    list(data.values())[0].config = config
    return _run(data, rule, severity=severity)


def severities(findings):
    return [f.severity for f in findings]


# =============================================================================


class TestCutterRadiusInfeasible(unittest.TestCase):
    RULE = Rulebook.CUTTER_RADIUS_INFEASIBLE

    def test_plain_block_is_clean(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_square_cornered_pocket_is_clean(self):
        # No radius at all is the cavity rules' problem, not this one: there
        # is nothing here to compare against a cutter.
        self.assertEqual(rule_check(pocket_block(), self.RULE), [])

    def test_generous_corner_radius_is_clean(self):
        # R3 is a 6 mm cutter, which every shop has.
        self.assertEqual(rule_check(make_pocket_with_corner_radius(3.0), self.RULE), [])

    def test_corner_tighter_than_any_cutter_is_an_error(self):
        # The smallest end mill in the default library is 1 mm, so R0.5 is the
        # tightest corner it can leave and R0.4 cannot be milled at all.
        findings = rule_check(make_pocket_with_corner_radius(0.4), self.RULE)
        self.assertEqual(severities(findings), [Severity.ERROR])
        self.assertAlmostEqual(findings[0].value, 0.4, places=3)
        self.assertAlmostEqual(findings[0].limit, 0.5, places=3)

    def test_the_finding_names_the_process_that_would_cut_it(self):
        finding = rule_check(make_pocket_with_corner_radius(0.4), self.RULE)[0]
        self.assertIn("sinker EDM", finding.message)

    def test_a_floor_blend_is_not_a_corner(self):
        # The same 0.4 mm radius where the wall meets the floor is cut by a
        # bull-nose, whose corner radius has nothing to do with its diameter.
        self.assertEqual(rule_check(make_pocket_with_floor_radius(0.4), self.RULE), [])

    def test_an_outside_radius_is_not_a_corner(self):
        # Rolled onto the outside of the block, the tool cuts with its flank
        # and no cutter size limits the radius.
        shape = _fillet(block(), 0.4, on_plane_z(40.0))
        self.assertEqual(rule_check(shape, self.RULE), [])

    def test_the_answer_depends_on_the_tool_library(self):
        # R3 is comfortable for a shop with 1 mm cutters and impossible for
        # one whose smallest end mill is 8 mm.
        shape = make_pocket_with_corner_radius(3.0)
        self.assertEqual(rule_check(shape, self.RULE), [])
        findings = rule_check_with_tools(shape, self.RULE, [8.0, 12.0])
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].limit, 4.0, places=3)

    def test_the_limit_is_configurable(self):
        shape = make_pocket_with_corner_radius(0.8)
        self.assertEqual(rule_check(shape, self.RULE), [])
        self.assertEqual(len(rule_check(shape, self.RULE, limit="1.5")), 1)

    def test_one_finding_carries_every_corner_it_covers(self):
        """A pocket has four corners and one corner radius.

        The designer chose it once and will change it once, so saying it four
        times is four readings of the same decision -- and on a plate of
        pockets it is the difference between a page and a line. Every corner
        still has to be named, or selecting the finding lights one wall of
        four and the machinist goes looking for the others.
        """
        findings = rule_check(make_pocket_with_corner_radius(0.4), self.RULE)
        self.assertEqual(len(findings), 1)
        faces = {index for _, index in findings[0].failing_geometry}
        self.assertEqual(len(faces), 4)

    def test_the_finding_says_how_many_corners_it_speaks_for(self):
        message = rule_check(make_pocket_with_corner_radius(0.4), self.RULE)[0].message
        self.assertIn("4 corners", message)


class TestCutterRadiusSuboptimal(unittest.TestCase):
    RULE = Rulebook.CUTTER_RADIUS_SUBOPTIMAL

    def test_plain_block_is_clean(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_a_stocked_size_is_clean(self):
        # R3 is exactly half a 6 mm end mill, so a cutter rides the corner.
        self.assertEqual(rule_check(make_pocket_with_corner_radius(3.0), self.RULE), [])

    def test_an_odd_radius_is_a_note(self):
        findings = rule_check(make_pocket_with_corner_radius(1.3), self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        self.assertAlmostEqual(findings[0].value, 1.3, places=3)

    def test_the_note_offers_the_sizes_either_side(self):
        message = rule_check(make_pocket_with_corner_radius(1.3), self.RULE)[0].message
        self.assertIn("R1.25", message)
        self.assertIn("R1.50", message)

    def test_an_unmillable_corner_belongs_to_the_other_rule(self):
        # Telling someone to round R0.4 up to the nearest stocked size would
        # be the wrong advice about a corner that needs EDM.
        self.assertEqual(rule_check(make_pocket_with_corner_radius(0.4), self.RULE), [])

    def test_a_floor_blend_is_not_a_corner(self):
        self.assertEqual(rule_check(make_pocket_with_floor_radius(1.3), self.RULE), [])

    def test_the_answer_depends_on_the_tool_library(self):
        # A shop with only 6 mm cutters stocks one corner radius, R3, so a
        # 5 mm corner no longer matches anything.
        shape = make_pocket_with_corner_radius(5.0)
        self.assertEqual(rule_check(shape, self.RULE), [])
        findings = rule_check_with_tools(shape, self.RULE, [6.0])
        self.assertEqual(len(findings), 1)
        self.assertIn("R3.00", findings[0].message)


class TestChamferNonstandardAngle(unittest.TestCase):
    RULE = Rulebook.CHAMFER_NONSTANDARD_ANGLE

    def test_plain_block_is_clean(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_a_forty_five_degree_chamfer_is_clean(self):
        self.assertEqual(rule_check(make_bore_with_chamfer(1.0), self.RULE), [])

    def test_a_flat_chamfer_strip_is_not_judged(self):
        # A planar strip is reported by the distance it takes off the edge,
        # which says nothing about the angle it was cut at.
        self.assertEqual(rule_check(_chamfer(block(), 3.0, upright), self.RULE), [])

    def test_a_sealing_bevel_is_a_note(self):
        findings = rule_check(make_knife_edge_ring(), self.RULE, severity="INFO")
        self.assertEqual(severities(findings), [Severity.INFO] * 2)
        self.assertAlmostEqual(findings[0].value, 26.6, places=1)

    def test_it_stays_a_note_when_nothing_is_configured(self):
        # An unconfigured binary rule falls back to ERROR, and a deliberate
        # sealing bevel is not an error.
        findings = rule_check(make_knife_edge_ring(), self.RULE, severity="")
        self.assertEqual(severities(findings), [Severity.INFO] * 2)

    def test_the_note_says_what_would_cut_it(self):
        message = rule_check(make_knife_edge_ring(), self.RULE)[0].message
        self.assertIn("angle cutter", message)


class TestMetalSealWitness(unittest.TestCase):
    RULE = Rulebook.METAL_SEAL_WITNESS

    def test_plain_block_is_clean(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_a_bore_rim_chamfer_is_not_a_seal(self):
        # A cone meeting a plane is an edge break. A seal edge needs a cone on
        # both sides of it.
        self.assertEqual(rule_check(make_bore_with_chamfer(1.0), self.RULE), [])

    def test_two_tapers_meeting_bluntly_are_not_a_seal(self):
        self.assertEqual(rule_check(make_stacked_tapers(), self.RULE), [])

    def test_a_knife_edge_is_reported_once(self):
        findings = rule_check(make_knife_edge_ring(), self.RULE, severity="INFO")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.INFO)
        self.assertEqual(findings[0].value, 1.0)

    def test_it_stays_a_note_when_nothing_is_configured(self):
        # Nothing is wrong with the part. The finding exists so the edge
        # survives the deburring bench, so it must not read as a defect.
        findings = rule_check(make_knife_edge_ring(), self.RULE, severity="")
        self.assertEqual(severities(findings), [Severity.INFO])

    def test_the_finding_highlights_both_cones(self):
        finding = rule_check(make_knife_edge_ring(), self.RULE)[0]
        self.assertEqual(len(finding.failing_geometry), 2)

    def test_the_finding_measures_the_seal_circle(self):
        # Taken off the axis rather than the edge length, which is a polyline
        # approximation and understates a circle by a few percent.
        message = rule_check(make_knife_edge_ring(), self.RULE)[0].message
        self.assertIn("24.0 mm", message)

    def test_the_finding_warns_against_deburring_it(self):
        message = rule_check(make_knife_edge_ring(), self.RULE)[0].message
        self.assertIn("tumbler", message)


class TestReporting(unittest.TestCase):
    def test_findings_are_repeatable(self):
        shape = make_pocket_with_corner_radius(0.4)
        first = [
            (f.severity, f.value, tuple(f.failing_geometry))
            for f in rule_check(shape, Rulebook.CUTTER_RADIUS_INFEASIBLE)
        ]
        second = [
            (f.severity, f.value, tuple(f.failing_geometry))
            for f in rule_check(shape, Rulebook.CUTTER_RADIUS_INFEASIBLE)
        ]
        self.assertEqual(first, second)

    def test_a_generously_radiused_pocket_trips_no_blend_rule(self):
        shape = make_pocket_with_corner_radius(3.0)
        for rule in (
            Rulebook.CUTTER_RADIUS_INFEASIBLE,
            Rulebook.CUTTER_RADIUS_SUBOPTIMAL,
            Rulebook.CHAMFER_NONSTANDARD_ANGLE,
            Rulebook.METAL_SEAL_WITNESS,
        ):
            self.assertEqual(rule_check(shape, rule), [], rule.name)

    def test_a_plain_block_trips_no_blend_rule(self):
        for rule in (
            Rulebook.CUTTER_RADIUS_INFEASIBLE,
            Rulebook.CUTTER_RADIUS_SUBOPTIMAL,
            Rulebook.CHAMFER_NONSTANDARD_ANGLE,
            Rulebook.METAL_SEAL_WITNESS,
        ):
            self.assertEqual(rule_check(block(), rule), [], rule.name)


if __name__ == "__main__":
    unittest.main()
