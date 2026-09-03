# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the cavity rules about tool reach, slot width and process.

Two of these rules answer from the tool library rather than from the material,
so the tests pin that the answer moves when the tooling does: a pocket a
25mm cutter can reach becomes unreachable in a shop that only owns small ones.

The other two fire on feature type alone, which makes their negative cases the
interesting ones -- a plain straight-walled channel must not be called a
flexure slit, and must not be called a dovetail either.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
from OCP.gp import gp_Pnt, gp_Vec
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.config import MachiningConfig, ToolEntry
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _cavity(p0, p1) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def _prism(points, direction) -> TopoDS_Shape:
    """A prism swept from a closed polygon."""
    polygon = BRepBuilderAPI_MakePolygon()
    for point in points:
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(*direction)).Shape()


def block() -> TopoDS_Shape:
    """The 100 x 80 x 40 block every cavity here is cut into."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def analyse(shape: TopoDS_Shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def check(shape, rule, target="N/A", limit="N/A", severity="WARNING", prefs=None):
    data = analyse(shape, prefs)
    check_class = get_check_class(rule)
    assert check_class is not None, f"no check registered for {rule.name}"
    return check_class().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


def check_with_tools(shape, rule, tools, severity="WARNING"):
    """Run a rule against a shop that owns only the given tools."""
    data = analyse(shape)
    config = MachiningConfig()
    config.tool_library = list(tools)
    list(data.values())[0].config = config
    return get_check_class(rule)().run_check(
        data,
        RuleLimit(target="N/A", limit="N/A", binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


def types_in(shape) -> list[str]:
    context = list(analyse(shape).values())[0]
    return sorted(f.type for f in context.recognition.features)


def severities(findings) -> list:
    return [f.severity for f in findings]


# -- shapes -------------------------------------------------------------------


def make_pocket(width: float, length: float, depth: float) -> TopoDS_Shape:
    """A rectangular pocket sunk from the top face."""
    return _cut(
        block(),
        _cavity(
            (50 - width / 2.0, 40 - length / 2.0, 40.0 - depth),
            (50 + width / 2.0, 40 + length / 2.0, 41.0),
        ),
    )


def make_channel(width: float, depth: float = 20.0) -> TopoDS_Shape:
    """A channel of the given width run right across the block."""
    half = width / 2.0
    return _cut(block(), _cavity((50 - half, -1, 40.0 - depth), (50 + half, 81, 41)))


def make_flexure_slit(width: float = 2.5) -> TopoDS_Shape:
    """A slit sawn clean through the block: two walls and nothing else."""
    return _cut(block(), _cavity((40, -1, -1), (40 + width, 81, 41)))


def make_dovetail() -> TopoDS_Shape:
    """A slot 10mm at the mouth and 22mm at the root: nothing rotating cuts it."""
    profile = [(-1, 35, 41), (-1, 45, 41), (-1, 51, 25), (-1, 29, 25)]
    return _cut(block(), _prism(profile, (102, 0, 0)))


# =============================================================================


class TestPocketAspectRatio(unittest.TestCase):
    RULE = Rulebook.POCKET_ASPECT_RATIO

    def test_a_pocket_a_big_cutter_fits_is_clean(self):
        # 30mm across takes a 25mm cutter, which carries 75mm of flute.
        self.assertEqual(check(make_pocket(30.0, 30.0, 35.0), self.RULE), [])

    def test_a_deep_narrow_pocket_is_out_of_reach(self):
        findings = check(make_pocket(8.0, 12.0, 35.0), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(severities(findings), [Severity.ERROR])

    def test_the_finding_names_the_flute_length_that_ran_out(self):
        # The 8mm end mill is the biggest that fits, and carries 24mm.
        finding = check(make_pocket(8.0, 12.0, 35.0), self.RULE)[0]
        self.assertIn("24 mm", finding.message)
        self.assertAlmostEqual(finding.value, 35.0 / 24.0, places=3)

    def test_a_shallow_narrow_pocket_is_clean(self):
        # Same 8mm width, well inside the tool's reach.
        self.assertEqual(check(make_pocket(8.0, 12.0, 15.0), self.RULE), [])

    def test_the_material_cannot_move_the_flute_length(self):
        # The limit is the tool, not a policy number, so the material's
        # figures are ignored either way round. A pocket 22mm deep with 24mm
        # of flute is reachable however the numbers are set; one 35mm deep
        # is not.
        reachable = make_pocket(8.0, 12.0, 22.0)
        self.assertEqual(check(reachable, self.RULE, target="0.5", limit="0.6"), [])
        self.assertEqual(
            len(check(make_pocket(8.0, 12.0, 35.0), self.RULE, target="8.0", limit="15.0")),
            1,
        )

    def test_the_answer_depends_on_the_tool_library(self):
        # A shop whose longest 8mm cutter is a stubby one cannot reach a
        # floor that the default library gets to comfortably.
        shape = make_pocket(8.0, 12.0, 15.0)
        self.assertEqual(check(shape, self.RULE), [])
        stubby = [
            ToolEntry(
                type="end_mill",
                min_diameter_mm=6.0,
                max_diameter_mm=6.0,
                max_flute_length_mm=10.0,
                unit="metric",
            )
        ]
        self.assertEqual(len(check_with_tools(shape, self.RULE, stubby)), 1)


class TestSlotNonstandardWidth(unittest.TestCase):
    RULE = Rulebook.SLOT_NONSTANDARD_WIDTH

    def test_a_slot_cut_by_a_stock_cutter_is_clean(self):
        self.assertEqual(check(make_channel(6.0), self.RULE), [])

    def test_an_odd_width_is_reported(self):
        findings = check(make_channel(5.3), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 5.3, places=3)

    def test_the_finding_names_the_nearest_cutter(self):
        finding = check(make_channel(5.3), self.RULE)[0]
        self.assertAlmostEqual(finding.limit, 5.0, places=3)
        self.assertIn("5.00 mm", finding.message)

    def test_wide_slots_are_exempt(self):
        # Past the cap a slot is roughed and finished with a smaller cutter
        # as a matter of course, so matching the width to a tool is moot.
        self.assertEqual(check(make_channel(20.3), self.RULE), [])

    def test_the_answer_depends_on_the_tool_library(self):
        # 6mm is a stock size in the default library and not in a shop whose
        # end mills come in 5 and 8.
        shape = make_channel(6.0)
        self.assertEqual(check(shape, self.RULE), [])
        odd = [
            ToolEntry(
                type="end_mill",
                min_diameter_mm=size,
                max_diameter_mm=size,
                unit="metric",
            )
            for size in (5.0, 8.0)
        ]
        findings = check_with_tools(shape, self.RULE, odd)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].limit, 5.0, places=3)

    def test_the_slit_family_is_not_judged_on_width(self):
        # A flexure slit is sawn or wired, not milled, so asking whether an
        # end mill matches its width is the wrong question entirely.
        self.assertIn(FeatureType.FLEXURE_SLIT, types_in(make_flexure_slit()))
        self.assertEqual(check(make_flexure_slit(), self.RULE), [])


class TestFlexureSlitProcess(unittest.TestCase):
    RULE = Rulebook.FLEXURE_SLIT_PROCESS

    def test_a_slit_is_reported_as_a_sawing_job(self):
        findings = check(make_flexure_slit(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.INFO)

    def test_the_finding_names_the_processes_that_do_cut_it(self):
        message = check(make_flexure_slit(), self.RULE)[0].message
        self.assertIn("slitting-saw", message)
        self.assertIn("wire-EDM", message)

    def test_the_finding_carries_the_slit_width(self):
        finding = check(make_flexure_slit(width=2.5), self.RULE)[0]
        self.assertAlmostEqual(finding.value, 2.5, places=3)

    def test_a_wide_channel_is_ordinary_milling(self):
        # 20mm wide and 15 deep: nothing here flexes, and an end mill cuts
        # it without complaint.
        self.assertEqual(check(make_channel(20.0, depth=15.0), self.RULE), [])

    def test_a_dovetail_is_not_a_flexure_slit(self):
        self.assertEqual(check(make_dovetail(), self.RULE), [])


class TestBroachedSlotProcess(unittest.TestCase):
    RULE = Rulebook.BROACHED_SLOT_PROCESS

    def test_a_dovetail_is_reported(self):
        self.assertIn(FeatureType.BROACHED_SLOT, types_in(make_dovetail()))
        findings = check(make_dovetail(), self.RULE)
        self.assertEqual(len(findings), 1)

    def test_the_finding_names_the_processes_that_do_cut_it(self):
        message = check(make_dovetail(), self.RULE)[0].message
        self.assertIn("broaching", message)
        self.assertIn("wire EDM", message)

    def test_the_severity_comes_from_the_material(self):
        self.assertEqual(
            severities(check(make_dovetail(), self.RULE, severity="ERROR")),
            [Severity.ERROR],
        )

    def test_a_straight_walled_channel_is_clean(self):
        self.assertEqual(check(make_channel(10.0), self.RULE), [])

    def test_a_flexure_slit_is_not_a_dovetail(self):
        self.assertEqual(check(make_flexure_slit(), self.RULE), [])


if __name__ == "__main__":
    unittest.main()
