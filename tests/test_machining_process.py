# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for part-process classification and the machining configuration.

The classification decides which rules run and against which limits, so the
cases here are chosen to pin the discriminators that carry real weight: the
convex-exterior veto that keeps a bored block from reading as turned, and the
end-face size gate that keeps a milled box's top face from counting toward the
turned fraction.
"""

import math
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.config import (
    IMPERIAL_DRILL_SIZES_MM,
    MachiningConfig,
    RuleThresholds,
    ToolEntry,
    default_tool_library,
)
from freecad.DFM.core.machining.process_classifier import (
    PartProcessType,
    axes_colinear,
    classify_part_process,
)


# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def make_prismatic_block() -> TopoDS_Shape:
    """A plain block. No axis of revolution anywhere."""
    return BRepPrimAPI_MakeBox(80.0, 50.0, 30.0).Shape()


def make_bored_block() -> TopoDS_Shape:
    """A block with one long bore through it.

    The bore is the largest cylindrical system on the part, but it is
    internal, so the exterior is entirely prismatic. This must read MILLED --
    it is the case the convex-exterior veto exists for.
    """
    axis = gp_Ax2(gp_Pnt(40, 25, -1), gp_Dir(0, 0, 1))
    return _cut(make_prismatic_block(), BRepPrimAPI_MakeCylinder(axis, 12.0, 40.0).Shape())


def make_shaft() -> TopoDS_Shape:
    """A plain round shaft: a body of revolution."""
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 15.0, 90.0).Shape()


def make_stepped_shaft() -> TopoDS_Shape:
    """A shaft with a smaller coaxial spigot: still all lathe work."""
    body = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 20.0, 60.0)
    spigot = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 60), gp_Dir(0, 0, 1)), 10.0, 25.0)
    return _fuse(body.Shape(), spigot.Shape())


def make_shaft_with_flats() -> TopoDS_Shape:
    """A turned shaft with two deep milled flats and a cross hole.

    Enough milled area to pull the turned fraction out of the TURNED band
    without losing the dominant axis: live-tool territory.

    The flats stop short of both ends deliberately. Run end to end they
    would make the shaft a constant cross-section -- which is drawn or
    broached bar, not turned work, and the classifier is right to say so.
    A wrench flat is local, and that is what makes this a turned part with
    milled features rather than a length of profile.
    """
    shaft = make_shaft()
    left = BRepPrimAPI_MakeBox(gp_Pnt(4, -20, 25), gp_Pnt(20, 20, 65)).Shape()
    right = BRepPrimAPI_MakeBox(gp_Pnt(-20, -20, 25), gp_Pnt(-4, 20, 65)).Shape()
    cross = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-20, 0, 45), gp_Dir(1, 0, 0)), 4.0, 40.0)
    return _cut(_cut(_cut(shaft, left), right), cross.Shape())


def _classify(shape: TopoDS_Shape, thresholds=None):
    return classify_part_process(AagBuilder(shape).build(), thresholds)


# =============================================================================


class TestAxisGeometry(unittest.TestCase):
    def test_identical_axes_are_colinear(self):
        a = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
        self.assertTrue(axes_colinear(a, gp_Ax1(gp_Pnt(0, 0, 50), gp_Dir(0, 0, 1))))

    def test_opposite_direction_is_still_the_same_axis(self):
        a = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
        self.assertTrue(axes_colinear(a, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, -1))))

    def test_parallel_but_offset_axes_are_not_colinear(self):
        a = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
        self.assertFalse(axes_colinear(a, gp_Ax1(gp_Pnt(10, 0, 0), gp_Dir(0, 0, 1))))

    def test_skewed_axes_are_not_colinear(self):
        a = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
        self.assertFalse(axes_colinear(a, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 1))))

    def test_tolerance_admits_a_small_angular_error(self):
        a = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
        nearly = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(math.sin(math.radians(2)), 0, math.cos(math.radians(2))))
        self.assertTrue(axes_colinear(a, nearly))


# =============================================================================


class TestClassification(unittest.TestCase):
    def test_plain_block_is_milled(self):
        self.assertIs(_classify(make_prismatic_block()).type, PartProcessType.MILLED)

    def test_block_has_no_axis(self):
        self.assertIsNone(_classify(make_prismatic_block()).axis_of_revolution)

    def test_bored_block_is_milled_despite_a_dominant_bore(self):
        # The convex-exterior veto: a turned part must have a turned exterior.
        self.assertIs(_classify(make_bored_block()).type, PartProcessType.MILLED)

    def test_shaft_is_turned(self):
        result = _classify(make_shaft())
        self.assertIs(result.type, PartProcessType.TURNED)
        self.assertIsNotNone(result.axis_of_revolution)
        self.assertGreaterEqual(result.turned_surface_fraction, 0.8)

    def test_turned_axis_matches_the_shaft_axis(self):
        axis = _classify(make_shaft()).axis_of_revolution
        self.assertIsNotNone(axis)
        self.assertAlmostEqual(abs(axis.Direction().Z()), 1.0, places=6)

    def test_stepped_shaft_is_turned(self):
        self.assertIs(_classify(make_stepped_shaft()).type, PartProcessType.TURNED)

    def test_shaft_with_milled_features_is_mill_turn(self):
        result = _classify(make_shaft_with_flats())
        self.assertIs(result.type, PartProcessType.MILL_TURN)
        self.assertIsNotNone(result.axis_of_revolution)

    def test_turned_fraction_is_a_fraction(self):
        for builder in (make_prismatic_block, make_shaft, make_shaft_with_flats):
            with self.subTest(shape=builder.__name__):
                fraction = _classify(builder()).turned_surface_fraction
                self.assertGreaterEqual(fraction, 0.0)
                self.assertLessEqual(fraction, 1.0)

    def test_bands_are_configurable(self):
        # Widening the milled band far enough must reclassify a real shaft.
        thresholds = RuleThresholds()
        thresholds.turned_fraction_turned_min = 1.5  # unreachable
        thresholds.turned_fraction_milled_max = 1.0  # everything is milled
        self.assertIs(_classify(make_shaft(), thresholds).type, PartProcessType.MILLED)

    def test_turning_family_covers_turned_and_mill_turn(self):
        self.assertTrue(PartProcessType.TURNED.is_turning_family)
        self.assertTrue(PartProcessType.MILL_TURN.is_turning_family)
        self.assertFalse(PartProcessType.MILLED.is_turning_family)
        self.assertFalse(PartProcessType.SHEET_METAL.is_turning_family)


# =============================================================================


class TestMachiningConfig(unittest.TestCase):
    def test_default_library_covers_the_expected_tool_types(self):
        types = {t.type for t in default_tool_library()}
        self.assertEqual(
            types,
            {"end_mill", "ball_nose", "drill", "tap", "reamer", "boring_bar", "turning_insert"},
        )

    def test_smallest_internal_corner_is_half_the_smallest_end_mill(self):
        config = MachiningConfig()
        self.assertAlmostEqual(config.smallest_end_mill_diameter(unit_filtered=False), 1.0)
        self.assertAlmostEqual(config.smallest_internal_corner_radius(), 0.5)

    def test_internal_corner_is_not_taken_from_the_corner_radius_field(self):
        # A sharp end mill has a zero corner radius but still leaves its own
        # radius in an inside corner; reading the field would say 0.0.
        config = MachiningConfig()
        self.assertGreater(config.smallest_internal_corner_radius(), 0.0)

    def test_standard_corner_radii_come_from_sharp_end_mills_only(self):
        radii = MachiningConfig().standard_corner_radii()
        self.assertIn(0.5, radii)  # the 1mm sharp end mill
        self.assertEqual(radii, sorted(radii))

    def test_smallest_turning_nose_radius(self):
        self.assertAlmostEqual(MachiningConfig().smallest_turning_nose_radius(), 0.2)

    def test_unit_system_filters_size_matching_tools(self):
        metric = MachiningConfig(unit_system="metric")
        imperial_mills = [
            t for t in metric.tools_of_type("end_mill", unit_filtered=True) if t.unit == "imperial"
        ]
        self.assertEqual(imperial_mills, [])

    def test_unit_agnostic_tools_survive_any_unit_system(self):
        metric = MachiningConfig(unit_system="metric")
        self.assertTrue(metric.tool_unit_enabled(ToolEntry(type="boring_bar")))

    def test_drill_catalog_merges_and_deduplicates(self):
        both = MachiningConfig(unit_system="both").all_drill_sizes_mm()
        metric_only = MachiningConfig(unit_system="metric").all_drill_sizes_mm()
        self.assertGreater(len(both), len(metric_only))
        self.assertEqual(both, sorted(both))
        for first, second in zip(both, both[1:]):
            self.assertGreaterEqual(second - first, 0.001)

    def test_metric_catalog_includes_the_iso_tap_drills(self):
        sizes = MachiningConfig(unit_system="metric").all_drill_sizes_mm()
        for tap_drill in (3.3, 4.2, 6.8, 10.2, 17.5):  # M4 M5 M8 M12 M20
            self.assertIn(tap_drill, sizes)

    def test_imperial_catalog_includes_number_and_letter_drills(self):
        self.assertIn(5.105, IMPERIAL_DRILL_SIZES_MM)  # #7, the 1/4-20 tap drill
        self.assertIn(6.528, IMPERIAL_DRILL_SIZES_MM)  # F, the 5/16-18 tap drill

    def test_rule_and_category_disabling(self):
        config = MachiningConfig(
            disabled_rules=["hole_deep_risk"], disabled_categories=["setup"]
        )
        self.assertTrue(config.is_rule_disabled("hole_deep_risk"))
        self.assertFalse(config.is_rule_disabled("pocket_deep_risk"))
        self.assertTrue(config.is_category_disabled("setup"))


# =============================================================================


class TestThresholdOverrides(unittest.TestCase):
    def test_a_missing_key_keeps_the_default(self):
        thresholds = RuleThresholds()
        thresholds.apply_overrides({"hole_deep_warn_ratio": 5.0})
        self.assertEqual(thresholds.hole_deep_warn_ratio, 5.0)
        self.assertEqual(thresholds.hole_deep_error_ratio, 10.0)

    def test_unknown_keys_are_ignored(self):
        thresholds = RuleThresholds()
        thresholds.apply_overrides({"not_a_threshold": 1.0})
        self.assertFalse(hasattr(thresholds, "not_a_threshold"))

    def test_integer_fields_stay_integers(self):
        # Rounding these through a float is a mistake worth guarding against:
        # a setup count of 3.0 is not a valid comparison operand.
        thresholds = RuleThresholds()
        thresholds.apply_overrides({"setup_count_warn": "4"})
        self.assertIsInstance(thresholds.setup_count_warn, int)
        self.assertEqual(thresholds.setup_count_warn, 4)

    def test_none_values_are_ignored(self):
        thresholds = RuleThresholds()
        thresholds.apply_overrides({"hole_deep_warn_ratio": None})
        self.assertEqual(thresholds.hole_deep_warn_ratio, 6.0)

    def test_unparseable_values_are_ignored(self):
        thresholds = RuleThresholds()
        thresholds.apply_overrides({"hole_deep_warn_ratio": "wide"})
        self.assertEqual(thresholds.hole_deep_warn_ratio, 6.0)

    def test_preferences_round_trip(self):
        config = MachiningConfig.from_preferences(
            {
                "MachiningMachineMode": "5axis",
                "MachiningUnitSystem": "metric",
                "MachiningBlankForm": "as_cast",
                "MachiningPrecisionMode": True,
                "MachiningThresholdhole_deep_warn_ratio": 7.5,
            }
        )
        self.assertEqual(config.machine_mode, "5axis")
        self.assertEqual(config.unit_system, "metric")
        self.assertEqual(config.blank_form, "as_cast")
        self.assertTrue(config.precision_mode)
        self.assertEqual(config.thresholds.hole_deep_warn_ratio, 7.5)

    def test_preferences_reject_invalid_enumerations(self):
        config = MachiningConfig.from_preferences({"MachiningMachineMode": "7axis"})
        self.assertEqual(config.machine_mode, "3axis")

    def test_empty_preferences_give_defaults(self):
        self.assertEqual(MachiningConfig.from_preferences({}).machine_mode, "3axis")
        self.assertEqual(MachiningConfig.from_preferences(None).unit_system, "both")


if __name__ == "__main__":
    unittest.main()
