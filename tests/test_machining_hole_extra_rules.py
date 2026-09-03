# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the hole rules about size, entry and what a bore runs into.

Every one of these rules has an obvious way to be wrong that would make it
useless: flag every diameter that is not a round number, flag every hole in a
part that has a slope somewhere on it, flag every bore that passes through a
pocket. So each rule gets its positive case and, next to it, the ordinary
geometry it must stay silent on -- a stock-size hole drilled square into a
flat face has to produce nothing at all.
"""

import math
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
)
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
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


def _cylinder(x, y, z, dx, dy, dz, radius, height) -> TopoDS_Shape:
    axis = gp_Ax2(gp_Pnt(x, y, z), gp_Dir(dx, dy, dz))
    return BRepPrimAPI_MakeCylinder(axis, radius, height).Shape()


def block() -> TopoDS_Shape:
    """The 100 x 80 x 40 block most of these are cut into."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def analyse(shape: TopoDS_Shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def check(shape, rule, target="N/A", limit="N/A", severity="WARNING", prefs=None):
    """Run one rule over one shape and return its findings."""
    data = analyse(shape, prefs)
    check_class = get_check_class(rule)
    assert check_class is not None, f"no check registered for {rule.name}"
    return check_class().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


def features_of(shape, *types):
    context = list(analyse(shape).values())[0]
    return context.recognition.of_type(*types)


def severities(findings) -> list:
    return [f.severity for f in findings]


# -- shapes -------------------------------------------------------------------


def make_bore(diameter: float) -> TopoDS_Shape:
    """A through bore of the given diameter, square through the block."""
    return _cut(block(), _cylinder(50, 40, -1, 0, 0, 1, diameter / 2.0, 60.0))


def make_sloped_block(tilt_deg: float = 40.0) -> TopoDS_Shape:
    """A 30 x 60 x 40 block whose whole top face is planed off at an angle.

    Built by rotating the cutting half-space rather than the block, so the
    part still sits square to the machine axes: it is the face that is
    sloped, not the setup.
    """
    base = BRepPrimAPI_MakeBox(30.0, 60.0, 40.0).Shape()
    cutter = _cavity((-100, -50, 20), (200, 150, 200))
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(15, 0, 20), gp_Dir(0, 1, 0)), math.radians(tilt_deg))
    return _cut(base, BRepBuilderAPI_Transform(cutter, turn, True).Shape())


def make_hole_into_slope(start_z: float, through: bool = False) -> TopoDS_Shape:
    """A vertical bore entering the sloped face of that block."""
    bore = _cylinder(15, 30, -1.0 if through else start_z, 0, 0, 1, 3.0, 60.0)
    return _cut(make_sloped_block(), bore)


def make_flat_deep_hole() -> TopoDS_Shape:
    """The same bore, same slenderness, entering a face square to it."""
    base = BRepPrimAPI_MakeBox(30.0, 60.0, 40.0).Shape()
    return _cut(base, _cylinder(15, 30, 15, 0, 0, 1, 3.0, 40.0))


def make_countersink(cone_height: float) -> TopoDS_Shape:
    """A through bore with a conical entry of the given depth.

    The cone opens one millimetre of radius per millimetre of height at 90
    degrees included, so the height alone sets the angle.
    """
    bore = _cylinder(50, 40, -1, 0, 0, 1, 4.0, 60.0)
    cone = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(50, 40, 40.0 - cone_height), gp_Dir(0, 0, 1)),
        4.0,
        4.0 + cone_height,
        cone_height,
    ).Shape()
    return _cut(_cut(block(), bore), cone)


def make_shallow_countersink() -> TopoDS_Shape:
    """A 53 degree cone: no countersink is ground anywhere near it."""
    bore = _cylinder(50, 40, -1, 0, 0, 1, 4.0, 60.0)
    cone = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(50, 40, 24.0), gp_Dir(0, 0, 1)), 4.0, 12.0, 16.0
    ).Shape()
    return _cut(_cut(block(), bore), cone)


def make_interrupted_bore(slot_width: float) -> TopoDS_Shape:
    """A 6mm bore run the length of the block, crossed by an open channel."""
    left = 50.0 - slot_width / 2.0
    slot = _cavity((left, -1, 10), (left + slot_width, 81, 41))
    bore = _cylinder(-1, 40, 20, 1, 0, 0, 3.0, 110.0)
    return _cut(_cut(block(), slot), bore)


def make_bore_into_bowl() -> TopoDS_Shape:
    """A bore sunk from the top until it breaks into a spherical chamber."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 15), gp_Dir(0, 0, 1)), 12.0)
    return _cut(_cut(block(), ball.Shape()), _cylinder(50, 40, 20, 0, 0, 1, 3.0, 40.0))


def make_linked_tunnels(bore: bool = True) -> TopoDS_Shape:
    """Two channels through the block, optionally joined by a cross passage.

    With the passage, neither of its mouths reaches the outside of the part:
    both open onto a channel wall.
    """
    shape = _cut(block(), _cavity((20, -1, 15), (40, 81, 25)))
    shape = _cut(shape, _cavity((60, -1, 15), (80, 81, 25)))
    if bore:
        shape = _cut(shape, _cylinder(39, 40, 20, 1, 0, 0, 3.0, 22.0))
    return shape


# =============================================================================


class TestHoleNonstandardDiameter(unittest.TestCase):
    RULE = Rulebook.HOLE_NONSTANDARD_DIAMETER

    def test_stock_size_is_clean(self):
        self.assertEqual(check(make_bore(10.0), self.RULE), [])

    def test_tap_drill_size_is_clean(self):
        # 6.8mm is the M8 coarse tap drill: not a round number, and stocked
        # in every shop that taps M8.
        self.assertEqual(check(make_bore(6.8), self.RULE), [])

    def test_fractional_inch_is_clean(self):
        # 3/8" exactly. A metric shop does not stock it, but the default
        # unit system is both, and this one does.
        self.assertEqual(check(make_bore(9.525), self.RULE), [])

    def test_off_catalogue_size_is_reported(self):
        findings = check(make_bore(9.25), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 9.25, places=3)

    def test_the_finding_names_a_size_to_move_to(self):
        finding = check(make_bore(9.25), self.RULE)[0]
        self.assertAlmostEqual(finding.limit, 9.2, places=3)
        self.assertIn("9.20 mm", finding.message)

    def test_large_bores_are_exempt(self):
        # Past the cap a hole is bored or interpolated to size, so the drill
        # catalogue has nothing to say about it.
        self.assertEqual(check(make_bore(29.25), self.RULE), [])

    def test_the_severity_comes_from_the_material(self):
        findings = check(make_bore(9.25), self.RULE, severity="ERROR")
        self.assertEqual(severities(findings), [Severity.ERROR])


class TestHolePartialEntry(unittest.TestCase):
    RULE = Rulebook.HOLE_PARTIAL_ENTRY

    def test_deep_hole_started_on_a_slope_is_reported(self):
        findings = check(make_hole_into_slope(start_z=1.0), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 40.0, delta=0.5)

    def test_the_finding_points_at_the_face_it_starts_on(self):
        finding = check(make_hole_into_slope(start_z=1.0), self.RULE)[0]
        self.assertTrue(all(ref[0] == "Face" for ref in finding.failing_geometry))
        self.assertGreater(len(finding.failing_geometry), 1)

    def test_the_same_bore_in_a_flat_face_is_clean(self):
        self.assertEqual(check(make_flat_deep_hole(), self.RULE), [])

    def test_a_shallow_hole_on_the_slope_is_clean(self):
        # Started on the slope but only a couple of diameters deep: the
        # drill is straight by the time it is at depth.
        self.assertEqual(check(make_hole_into_slope(start_z=12.0), self.RULE), [])

    def test_one_square_end_is_enough(self):
        # The same slope, but the bore now goes right through and comes out
        # on the flat underside. A machinist drills it from there.
        self.assertEqual(check(make_hole_into_slope(0.0, through=True), self.RULE), [])

    def test_a_gentler_slope_stays_clean(self):
        # 20 degrees off square is inside the threshold, so this is the
        # negative half of the pair the sloped case makes.
        gentle = _cut(
            make_sloped_block(tilt_deg=20.0), _cylinder(15, 30, 1, 0, 0, 1, 3.0, 60.0)
        )
        holes = features_of(gentle, FeatureType.BLIND_HOLE)
        self.assertEqual(len(holes), 1, "the bore should still be recognized")
        self.assertGreater(holes[0].number("depth_mm") / 6.0, 3.0)
        self.assertEqual(check(gentle, self.RULE), [])


class TestHoleCountersinkAngle(unittest.TestCase):
    RULE = Rulebook.HOLE_COUNTERSINK_ANGLE

    def test_ninety_degree_countersink_is_clean(self):
        self.assertEqual(check(make_countersink(8.0), self.RULE), [])

    def test_off_list_angle_is_reported(self):
        findings = check(make_shallow_countersink(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 53.13, places=1)

    def test_the_finding_names_the_nearest_stock_angle(self):
        finding = check(make_shallow_countersink(), self.RULE)[0]
        self.assertAlmostEqual(finding.limit, 60.0, places=3)
        self.assertIn("60", finding.message)

    def test_a_plain_bore_has_no_angle_to_judge(self):
        self.assertEqual(check(make_bore(10.0), self.RULE), [])


class TestHoleMultiPass(unittest.TestCase):
    RULE = Rulebook.HOLE_MULTI_PASS

    def test_wide_void_means_two_drillings(self):
        findings = check(make_interrupted_bore(slot_width=20.0), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].value, 2.0)

    def test_it_is_information_rather_than_a_fault(self):
        finding = check(make_interrupted_bore(slot_width=20.0), self.RULE)[0]
        self.assertEqual(finding.severity, Severity.INFO)

    def test_the_finding_reports_the_longest_solid_run(self):
        message = check(make_interrupted_bore(slot_width=20.0), self.RULE)[0].message
        self.assertIn("40.0 mm", message)

    def test_a_narrow_void_is_crossed_in_one_pass(self):
        # 10mm on a 6mm drill: the margins pick the far side up, so this is
        # one drilling and the rule stays quiet.
        shape = make_interrupted_bore(slot_width=10.0)
        hole = features_of(shape, FeatureType.THROUGH_HOLE)
        self.assertEqual(len(hole), 1, "the bore should still read as one hole")
        self.assertEqual(check(shape, self.RULE), [])

    def test_an_uninterrupted_bore_is_clean(self):
        self.assertEqual(check(make_bore(10.0), self.RULE), [])


class TestHoleIntersectsCavity(unittest.TestCase):
    RULE = Rulebook.HOLE_INTERSECTS_CAVITY

    def test_bore_ending_in_a_chamber_is_reported(self):
        findings = check(make_bore_into_bowl(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].value, 1.0)
        self.assertEqual(findings[0].severity, Severity.INFO)

    def test_cross_passage_between_two_channels_is_reported(self):
        findings = check(make_linked_tunnels(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].value, 1.0)

    def test_an_ordinary_through_hole_is_clean(self):
        self.assertEqual(check(make_bore(10.0), self.RULE), [])

    def test_a_plain_blind_hole_is_clean(self):
        self.assertEqual(check(make_flat_deep_hole(), self.RULE), [])

    def test_a_bore_that_reaches_daylight_is_clean(self):
        # It passes clean through a channel, but both its ends open on the
        # outside of the part. That is ordinary drilling, and reporting it
        # would flag half the holes on a typical part.
        self.assertEqual(check(make_interrupted_bore(slot_width=20.0), self.RULE), [])

    def test_channels_with_no_passage_between_them_are_clean(self):
        self.assertEqual(check(make_linked_tunnels(bore=False), self.RULE), [])

    def test_bore_into_bore_is_left_to_the_other_rule(self):
        # A cross-drilled pair is the intersecting-holes rule's territory.
        # Counting it here as well would report one breakthrough twice.
        vertical = _cylinder(50, 40, -1, 0, 0, 1, 5.0, 60.0)
        horizontal = _cylinder(-1, 40, 20, 1, 0, 0, 4.0, 110.0)
        shape = _cut(_cut(block(), vertical), horizontal)
        self.assertEqual(check(shape, self.RULE), [])


if __name__ == "__main__":
    unittest.main()
