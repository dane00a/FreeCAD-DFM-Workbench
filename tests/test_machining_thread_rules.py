# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the tapped-hole rules.

Every fixture here carries a real modelled helix, because that is the only
way a thread reaches these rules: the workbench refuses to infer one from
diameter alone, so a plain bore at a tap-drill size is a bore and nothing
more. The tests hold that line -- a drilled block with a 6.8 mm hole in it
must stay silent under all three rules.

The geometry is deliberately awkward in one respect worth knowing about. A
modelled thread cuts the bore into fragments, so the face carrying the thread
spec no longer touches the surface the hole was drilled through. The shoulder
rule has to find that surface by position rather than by adjacency, and the
raised-pad cases below are what pin that down.
"""

import math
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepLib import BRepLib
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.Geom import Geom_CylindricalSurface
from OCP.Geom2d import Geom2d_Line
from OCP.gp import (
    gp_Ax2,
    gp_Ax2d,
    gp_Ax3,
    gp_Dir,
    gp_Dir2d,
    gp_Pnt,
    gp_Pnt2d,
    gp_Trsf,
    gp_Vec,
)
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# The M8 tap drill, and the coarse pitch that goes with it.
TAP_DRILL_RADIUS = 3.4
PITCH = 1.25


# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def _helix_wire(radius: float, pitch: float, turns: float):
    """A true helix: a straight line in the parameter space of a cylinder."""
    surface = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), radius)
    slope = gp_Dir2d(1.0, pitch / (2.0 * math.pi))
    line = Geom2d_Line(gp_Ax2d(gp_Pnt2d(0.0, 0.0), slope))
    edge = BRepBuilderAPI_MakeEdge(line, surface, 0.0, turns * 2.0 * math.pi).Edge()
    BRepLib.BuildCurves3d_s(edge)
    return BRepBuilderAPI_MakeWire(edge).Wire()


def _swept_thread(radius: float, pitch: float, turns: float, profile):
    pipe = BRepOffsetAPI_MakePipeShell(_helix_wire(radius, pitch, turns))
    pipe.Add(profile, False, False)
    pipe.SetMode(True)
    pipe.Build()
    pipe.MakeSolid()
    return pipe.Shape()


def block(x0=-10.0) -> TopoDS_Shape:
    """The block every tapped hole is cut into, 30 mm tall."""
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, -10, 0), gp_Pnt(10, 10, 30)).Shape()


def tapped_hole(
    solid: TopoDS_Shape,
    x: float = 0.0,
    turns: float = 6,
    hole_depth: float = 20.0,
    groove: float = 0.6,
) -> TopoDS_Shape:
    """An M8 tapped hole entering the underside of a solid.

    The bore starts a millimetre below the block so it opens cleanly, and the
    thread is a swept helix rather than a drawn one, which is what the hole
    recognizer needs before it will call the bore tapped at all.
    """
    bore = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(x, 0.0, -1.0), gp_Dir(0, 0, 1)), TAP_DRILL_RADIUS, hole_depth
    ).Shape()
    profile = BRepBuilderAPI_MakePolygon(
        gp_Pnt(TAP_DRILL_RADIUS - 0.2, 0, 0),
        gp_Pnt(TAP_DRILL_RADIUS + groove, 0, PITCH * 0.35),
        gp_Pnt(TAP_DRILL_RADIUS - 0.2, 0, PITCH * 0.7),
        True,
    ).Wire()
    move = gp_Trsf()
    move.SetTranslation(gp_Vec(x, 0.0, 0.0))
    thread = BRepBuilderAPI_Transform(
        _swept_thread(TAP_DRILL_RADIUS, PITCH, turns, profile), move, True
    ).Shape()
    return _cut(_cut(solid, bore), thread)


def plain_drilled_block() -> TopoDS_Shape:
    """The same 6.8 mm bore with no thread cut in it: an ordinary hole."""
    return _cut(
        block(),
        BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), TAP_DRILL_RADIUS, 20.0
        ).Shape(),
    )


def block_with_pad(offset: float) -> TopoDS_Shape:
    """The block with a 6 mm pad standing proud of the face the tap enters."""
    pad = BRepPrimAPI_MakeBox(gp_Pnt(offset, -8, -6), gp_Pnt(offset + 5, 8, 0)).Shape()
    return _fuse(block(), pad)


def counterbored_tapped_hole(outer_diameter: float) -> TopoDS_Shape:
    """A tapped hole with a coaxial spot face opened over it.

    The seat is a millimetre deep past the block face, which puts its
    shoulder among the first turns of the thread rather than above them. Cut
    deeper than the thread's lead, the seat sits on a plain length of bore
    and the recognizer -- correctly, and as the reference engine does -- reads
    that as a counterbore in its own right with the tapped length below it as
    a second feature. Both readings describe the part; this one is the shape
    the rule under test is about, a head seat bearing straight onto thread.
    """
    mouth = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), outer_diameter / 2.0, 2.0
    ).Shape()
    return _cut(tapped_hole(block()), mouth)


def analyse(shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def rule_check(shape, rule, target="N/A", limit="N/A", severity="WARNING"):
    data = analyse(shape)
    check_class = get_check_class(rule)
    assert check_class is not None
    return check_class().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


def severities(findings):
    return [f.severity for f in findings]


# =============================================================================


class TestThreadRunout(unittest.TestCase):
    RULE = Rulebook.THREAD_RUNOUT

    def test_a_plain_block_has_nothing_to_say(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_an_unthreaded_bore_is_not_judged_as_a_thread(self):
        # 6.8 mm is exactly an M8 tap drill. Without a modelled thread it is
        # still just a hole, and guessing otherwise would light up every
        # clearance bore on a plate.
        self.assertEqual(rule_check(plain_drilled_block(), self.RULE), [])

    def test_a_hole_drilled_well_past_its_thread_is_clean(self):
        self.assertEqual(rule_check(tapped_hole(block()), self.RULE), [])

    def test_a_hole_tapped_almost_to_the_bottom_is_reported(self):
        # Roughly two millimetres of clearance below the last thread against
        # the 3.1 mm an M8 tap wants.
        findings = rule_check(tapped_hole(block(), hole_depth=16.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertLess(findings[0].value, findings[0].limit)

    def test_the_finding_names_the_thread_and_the_basis(self):
        message = rule_check(tapped_hole(block(), hole_depth=16.0), self.RULE)[0].message
        self.assertIn("M8x1.25", message)
        self.assertIn("1.25 mm pitch", message)

    def test_a_configured_minimum_raises_the_bar(self):
        # A shop insisting on six millimetres of run-out turns down a hole
        # the pitch alone would have passed.
        self.assertTrue(rule_check(tapped_hole(block()), self.RULE, limit="6.0"))

    def test_a_configured_minimum_never_lowers_it(self):
        # One millimetre is less than an M8 tap's lead needs, and setting it
        # does not licence tapping closer than the pitch demands.
        shape = tapped_hole(block(), hole_depth=16.0)
        self.assertTrue(rule_check(shape, self.RULE, limit="1.0"))

    def test_a_through_hole_runs_out_into_fresh_air(self):
        # Tapped clean through a 30 mm block: there is no bottom to reach.
        self.assertEqual(rule_check(tapped_hole(block(), hole_depth=40.0), self.RULE), [])


class TestThreadShoulderProximity(unittest.TestCase):
    RULE = Rulebook.THREAD_SHOULDER_PROXIMITY

    def test_a_plain_block_has_nothing_to_say(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_an_unthreaded_bore_is_not_judged_as_a_thread(self):
        self.assertEqual(rule_check(plain_drilled_block(), self.RULE), [])

    def test_an_open_face_around_the_hole_is_clean(self):
        self.assertEqual(rule_check(tapped_hole(block()), self.RULE), [])

    def test_a_pad_beside_the_entry_is_reported(self):
        # The pad wall stands 4.5 mm from the axis, so the holder has 1.1 mm
        # of clearance past the bore -- inside the 1.5 mm minimum.
        findings = rule_check(tapped_hole(block_with_pad(4.5)), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertIn("tap", findings[0].message)
        self.assertLess(findings[0].value, findings[0].limit)

    def test_a_pad_set_back_is_clean(self):
        # Two and a half millimetres of clearance and the holder goes down.
        self.assertEqual(rule_check(tapped_hole(block_with_pad(6.0)), self.RULE), [])

    def test_the_clearance_is_configurable(self):
        self.assertEqual(
            rule_check(tapped_hole(block_with_pad(4.5)), self.RULE, limit="0.5"), []
        )
        self.assertTrue(
            rule_check(tapped_hole(block_with_pad(6.0)), self.RULE, limit="4.0")
        )

    def test_a_tight_counterbore_wall_crowds_the_tap(self):
        # A 10 mm mouth over an M8 thread leaves 1 mm of wall, and the tap's
        # lead chamfer reaches it before the thread is at full form.
        findings = rule_check(counterbored_tapped_hole(10.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertIn("counterbore", findings[0].message)

    def test_a_generous_counterbore_is_clean(self):
        self.assertEqual(rule_check(counterbored_tapped_hole(12.0), self.RULE), [])


class TestThreadWallThickness(unittest.TestCase):
    RULE = Rulebook.THREAD_WALL_THICKNESS

    def test_a_plain_block_has_nothing_to_say(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_an_unthreaded_bore_is_not_judged_as_a_thread(self):
        self.assertEqual(rule_check(plain_drilled_block(), self.RULE), [])

    def test_a_hole_in_the_middle_of_a_block_is_clean(self):
        self.assertEqual(rule_check(tapped_hole(block()), self.RULE), [])

    def test_a_hole_near_a_wall_is_reported(self):
        # The bore sits 5.4 mm from the face, so 2 mm of wall, and the
        # thread root takes 0.68 mm of that.
        findings = rule_check(tapped_hole(block(x0=-6.0), x=-0.6), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 1.32, places=2)

    def test_a_hole_hard_against_a_wall_is_an_error(self):
        # Fourteen tenths of wall before tapping, and seven after: it will
        # split when the fastener is pulled up.
        findings = rule_check(tapped_hole(block(x0=-4.8)), self.RULE)
        self.assertEqual(severities(findings), [Severity.ERROR])

    def test_a_thin_walled_bar_is_measured_against_the_outside(self):
        # A round bar has no flat wall to measure to, so the wall has to come
        # from the outside of the part. A 6.8 mm tapping hole up a 10 mm bar
        # leaves 1.6 mm of it, and less than a millimetre after tapping.
        bar = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 5.0, 30.0
        ).Shape()
        findings = rule_check(tapped_hole(bar), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 0.92, places=2)

    def test_a_thick_walled_bar_is_clean(self):
        bar = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 7.0, 30.0
        ).Shape()
        self.assertEqual(rule_check(tapped_hole(bar), self.RULE), [])

    def test_the_thresholds_are_configurable(self):
        shape = tapped_hole(block(x0=-6.0), x=-0.6)
        self.assertEqual(rule_check(shape, self.RULE, target="1.0", limit="0.5"), [])
        self.assertEqual(
            severities(rule_check(shape, self.RULE, target="3.0", limit="2.0")),
            [Severity.ERROR],
        )

    def test_the_finding_accounts_for_the_thread_root(self):
        message = rule_check(tapped_hole(block(x0=-6.0), x=-0.6), self.RULE)[0].message
        self.assertIn("thread root takes another", message)
        self.assertIn("M8x1.25", message)


if __name__ == "__main__":
    unittest.main()
