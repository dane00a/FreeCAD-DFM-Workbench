# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the sculpted-surface, turned-profile and ball-pocket rules.

Almost every limit here comes out of the tool library rather than the
material, so the tests are built to show the answer moving when the tooling
does: the same sculpted block is a routine job for a shop with 1 mm cutters
and an impossible one for a shop whose smallest is 12 mm.

The refusals carry as much weight as the findings. A plain block and a plain
shaft have no sculpture on them at all, and a bowl sunk only to its equator
is the widest one a straight tool can still cut.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeRevol,
    BRepPrimAPI_MakeSphere,
)
from OCP.GeomAPI import GeomAPI_PointsToBSpline
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.config import MachiningConfig, ToolEntry
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


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def analyse(shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def rule_check(shape, rule, target="N/A", limit="N/A", severity="WARNING", config=None):
    """Run one rule over a shape, optionally against a different shop.

    Swapping the config after recognition is deliberate: none of these
    recognizers consult the tool library, so the same analysis can be judged
    by any number of shops.
    """
    data = analyse(shape)
    if config is not None:
        list(data.values())[0].config = config
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


def shop_with(*tools) -> MachiningConfig:
    """A shop stocking nothing but the tools given."""
    config = MachiningConfig()
    config.tool_library = list(tools)
    return config


def end_mill(diameter: float) -> ToolEntry:
    return ToolEntry(
        type="end_mill",
        min_diameter_mm=diameter,
        max_diameter_mm=diameter,
        unit="metric",
    )


def ball_nose(diameter: float) -> ToolEntry:
    return ToolEntry(
        type="ball_nose",
        min_diameter_mm=diameter,
        max_diameter_mm=diameter,
        corner_radius_mm=diameter * 0.5,
        unit="metric",
    )


def turning_insert(nose_radius: float) -> ToolEntry:
    return ToolEntry(type="turning_insert", corner_radius_mm=nose_radius)


# -- shapes -------------------------------------------------------------------


def _spline(points) -> object:
    array = TColgp_Array1OfPnt(1, len(points))
    for index, point in enumerate(points, start=1):
        array.SetValue(index, gp_Pnt(*point))
    return GeomAPI_PointsToBSpline(array).Curve()


def block() -> TopoDS_Shape:
    """A plain block: nothing sculpted anywhere on it."""
    return BRepPrimAPI_MakeBox(120.0, 90.0, 50.0).Shape()


def sculpted_block(scale: float = 1.0) -> TopoDS_Shape:
    """A block whose top edge runs along a spline: a wave in plan.

    Extruding the spline gives a genuine freeform face rather than a
    cylinder pretending to be one, and scaling the profile scales its
    curvature with it -- which is how the same shape is made to sit in
    different tiers of the tool library.
    """
    profile = [
        (0.0, 0.0, 0.0),
        (20.0 * scale, 8.0 * scale, 0.0),
        (40.0 * scale, -8.0 * scale, 0.0),
        (60.0 * scale, 0.0, 0.0),
    ]
    far = 60.0 * scale
    back = -40.0 * scale
    wire = BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(_spline(profile)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(far, 0.0, 0.0), gp_Pnt(far, back, 0.0)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(far, back, 0.0), gp_Pnt(0.0, back, 0.0)).Edge(),
    )
    wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(0.0, back, 0.0), gp_Pnt(0.0, 0.0, 0.0)).Edge())
    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, 25.0 * scale)).Shape()


def waisted_shaft() -> TopoDS_Shape:
    """A shaft turned to a spline profile with a hollow waist in the middle."""
    profile = [
        (12.0, 0.0, 0.0),
        (12.0, 0.0, 10.0),
        (6.0, 0.0, 20.0),
        (12.0, 0.0, 30.0),
        (12.0, 0.0, 40.0),
    ]
    wire = BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(_spline(profile)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(12.0, 0.0, 0.0), gp_Pnt(0.0, 0.0, 0.0)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(0.0, 0.0, 0.0), gp_Pnt(0.0, 0.0, 40.0)).Edge(),
    )
    wire.Add(
        BRepBuilderAPI_MakeEdge(gp_Pnt(0.0, 0.0, 40.0), gp_Pnt(12.0, 0.0, 40.0)).Edge()
    )
    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    return BRepPrimAPI_MakeRevol(
        face, gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0))
    ).Shape()


def plain_shaft() -> TopoDS_Shape:
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 12.0, 40.0
    ).Shape()


def _bowl_block() -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(100.0, 80.0, 50.0).Shape()


def hemispherical_bowl() -> TopoDS_Shape:
    """A 15 mm ball sunk to its equator: the widest bowl with no overhang."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 50), gp_Dir(0, 0, 1)), 15.0)
    return _cut(_bowl_block(), ball.Shape())


def super_hemispherical_bowl() -> TopoDS_Shape:
    """The same ball sunk 6 mm further, so the equator is buried."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 44), gp_Dir(0, 0, 1)), 15.0)
    return _cut(_bowl_block(), ball.Shape())


def shallow_dish() -> TopoDS_Shape:
    """A ball barely dipped in, wide open all the way down."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 58), gp_Dir(0, 0, 1)), 15.0)
    return _cut(_bowl_block(), ball.Shape())


def dome() -> TopoDS_Shape:
    """The same sphere with the material on the other side of it."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 50), gp_Dir(0, 0, 1)), 12.0)
    upper = _cut(ball.Shape(), BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(100, 80, 50)).Shape())
    return _fuse(_bowl_block(), upper)


# =============================================================================


class TestFreeformInternalRadius(unittest.TestCase):
    RULE = Rulebook.FREEFORM_INTERNAL_RADIUS

    def test_a_plain_block_has_no_sculpture(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_an_open_sculpted_surface_is_clean(self):
        # A 10 mm concave radius clears the 1 mm cutter the default library
        # starts at with room to spare.
        self.assertEqual(rule_check(sculpted_block(), self.RULE), [])

    def test_a_small_radius_is_a_note_about_price(self):
        # Two millimetres of concave radius is perfectly machinable, but it
        # forces the bottom of the library.
        findings = rule_check(sculpted_block(0.2), self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        self.assertIn("forces the bottom of the tool library", findings[0].message)

    def test_a_shop_with_big_cutters_cannot_finish_it(self):
        # The same block, judged by a shop whose smallest end mill is 12 mm:
        # that cutter wants 12 mm of concave radius and the part has 10.
        findings = rule_check(sculpted_block(), self.RULE, config=shop_with(end_mill(12.0)))
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertIn("chatter", findings[0].message)

    def test_the_two_tiers_are_configurable(self):
        # Target is the tier that only costs money, limit the tier that
        # cannot be cut.
        self.assertEqual(
            severities(rule_check(sculpted_block(), self.RULE, target="12.0", limit="6.0")),
            [Severity.INFO],
        )
        self.assertEqual(
            severities(rule_check(sculpted_block(), self.RULE, target="20.0", limit="12.0")),
            [Severity.WARNING],
        )

    def test_the_whole_part_is_reported_once(self):
        # One decision about tooling, so one finding -- not one per patch.
        findings = rule_check(sculpted_block(), self.RULE, target="20.0", limit="12.0")
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].failing_geometry)

    def test_a_shop_with_no_mills_says_nothing(self):
        self.assertEqual(
            rule_check(sculpted_block(0.2), self.RULE, config=shop_with(turning_insert(0.4))),
            [],
        )


class TestFreeformFinishing(unittest.TestCase):
    RULE = Rulebook.FREEFORM_FINISHING

    def test_a_plain_block_needs_no_finishing_pass(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_sculpted_area_is_priced(self):
        findings = rule_check(sculpted_block(), self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        message = findings[0].message
        self.assertIn("stepover", message)
        self.assertIn("metres of finishing path", message)

    def test_the_ball_is_chosen_to_fit_the_tightest_hollow(self):
        # A 10 mm concave radius admits the 20 mm ball but not the next one
        # up, and the stepover follows from whichever ball fits.
        message = rule_check(sculpted_block(), self.RULE)[0].message
        self.assertIn("20.0 mm ball", message)

    def test_a_small_patch_of_sculpture_is_not_worth_saying(self):
        # Seventy-odd square millimetres is a detail of the job, not a
        # driver of its price.
        self.assertEqual(rule_check(sculpted_block(0.2), self.RULE), [])

    def test_a_hollow_below_the_smallest_ball_cannot_be_finished(self):
        # A shop whose smallest ball is 25 mm has nothing that reaches the
        # bottom of a 10 mm hollow, so the burden estimate is beside the
        # point and the finding says so instead.
        findings = rule_check(
            sculpted_block(), self.RULE, config=shop_with(ball_nose(25.0))
        )
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertIn("micron", findings[0].message)
        self.assertNotIn("metres of finishing path", findings[0].message)

    def test_a_shop_with_no_ball_noses_says_nothing(self):
        self.assertEqual(
            rule_check(sculpted_block(), self.RULE, config=shop_with(end_mill(1.0))), []
        )


class TestTurnedProfileRadius(unittest.TestCase):
    RULE = Rulebook.TURNED_PROFILE_RADIUS

    def test_a_plain_shaft_has_no_profile(self):
        self.assertEqual(rule_check(plain_shaft(), self.RULE), [])

    def test_a_plain_block_has_no_profile(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_a_generous_waist_is_clean(self):
        # Six millimetres of valley against the R0.2 nose the default
        # library starts at.
        self.assertEqual(rule_check(waisted_shaft(), self.RULE), [])

    def test_a_valley_tighter_than_the_nose_is_reported(self):
        # The same shaft at a shop whose smallest insert is R4: the nose
        # would sit in the valley whole.
        findings = rule_check(
            waisted_shaft(), self.RULE, config=shop_with(turning_insert(4.0))
        )
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertIn("grooving pass", findings[0].message)

    def test_the_required_radius_is_configurable(self):
        self.assertTrue(rule_check(waisted_shaft(), self.RULE, limit="10.0"))
        self.assertEqual(rule_check(waisted_shaft(), self.RULE, limit="2.0"), [])

    def test_a_shop_with_no_inserts_says_nothing(self):
        self.assertEqual(
            rule_check(waisted_shaft(), self.RULE, config=shop_with(end_mill(1.0))), []
        )


class TestSphericalPocketUndercut(unittest.TestCase):
    RULE = Rulebook.SPHERICAL_POCKET_UNDERCUT

    def test_a_plain_block_has_no_bowl(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_a_hemisphere_is_the_widest_bowl_that_still_mills(self):
        # Sunk exactly to the equator, nothing hangs over the opening.
        self.assertEqual(rule_check(hemispherical_bowl(), self.RULE), [])

    def test_a_shallow_dish_is_open_all_the_way_down(self):
        self.assertEqual(rule_check(shallow_dish(), self.RULE), [])

    def test_a_dome_is_not_a_pocket(self):
        self.assertEqual(rule_check(dome(), self.RULE), [])

    def test_a_buried_equator_is_reported(self):
        findings = rule_check(super_hemispherical_bowl(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertGreater(findings[0].value, 1.0)
        self.assertIn("sinker EDM", findings[0].message)

    def test_the_severity_follows_the_rule_configuration(self):
        self.assertEqual(
            severities(rule_check(super_hemispherical_bowl(), self.RULE, severity="ERROR")),
            [Severity.ERROR],
        )

    def test_a_generous_threshold_silences_it(self):
        # Below the threshold the rim is inside machining tolerance and a
        # small enough ball reaches essentially all of the surface.
        self.assertEqual(
            rule_check(super_hemispherical_bowl(), self.RULE, limit="20.0"), []
        )


if __name__ == "__main__":
    unittest.main()
