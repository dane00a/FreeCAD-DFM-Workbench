# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the machining settings pages and what they persist.

Three failures are worth catching here, and each has bitten a real port:

* A threshold added to `RuleThresholds` and never given a label. It works
  perfectly and no shop can reach it, which is invisible until someone asks
  why changing the number in the preferences did nothing.
* A float written to the parameter store as an integer. FreeCAD keeps the two
  in different tables and truncates silently, so a 1.5 mm wall limit comes
  back as 1 mm and every part suddenly passes.
* A tool library or drill index that is editable but not read. The shelf is
  what half the rules are judged against, and a shop can fill the page in
  perfectly while the analysis still uses the catalogue.

PySide6 is not installed in the headless environment, so the widgets are not
built here. What is tested is everything the widgets are generated from --
the grouping, the labels, the defaults and the types -- plus the parameter
round trip against a store that truncates the way FreeCAD's does.
"""

import unittest
from collections import Counter

from freecad.DFM.core.machining.config import (
    IMPERIAL_DRILL_PREF_KEY,
    IMPERIAL_DRILL_SIZES_MM,
    METRIC_DRILL_PREF_KEY,
    METRIC_DRILL_SIZES_MM,
    THRESHOLD_PREF_PREFIX,
    TOOL_LIBRARY_PREF_KEY,
    TOOL_TYPES,
    MachiningConfig,
    RuleThresholds,
    ToolEntry,
    decode_size_list,
    decode_tool_library,
    default_tool_library,
    encode_size_list,
    encode_tool_library,
)
from freecad.DFM.gui.machining_thresholds import (
    THRESHOLD_GROUPS,
    _UNIT_STYLES,
    all_specs,
    common_field_names,
    default_for,
    is_integer_field,
    preference_defaults,
    preference_key,
    spec_for,
    threshold_field_names,
    unreachable_fields,
)


class _TypedParams:
    """Stands in for a FreeCAD parameter group, typed the way FreeCAD is.

    The truncation in `SetInt` is the point of the class. FreeCAD keeps
    integers and floats in separate tables, so a float written through the
    wrong setter loses its decimals with nothing said about it. Reproducing
    that here is what makes the round-trip test mean something.
    """

    def __init__(self):
        self._values: dict = {}

    def SetInt(self, key, value):
        self._values[key] = int(value)

    def SetFloat(self, key, value):
        self._values[key] = float(value)

    def SetBool(self, key, value):
        self._values[key] = bool(value)

    def SetString(self, key, value):
        self._values[key] = str(value)

    def GetInt(self, key, default=0):
        return self._values.get(key, default)

    def GetFloat(self, key, default=0.0):
        return self._values.get(key, default)

    def GetBool(self, key, default=False):
        return self._values.get(key, default)

    def GetString(self, key, default=""):
        return self._values.get(key, default)

    def GetContents(self):
        return [(type(value).__name__, key, value) for key, value in self._values.items()]

    def as_prefs(self) -> dict:
        """The flat dictionary the analysis runner builds from a store."""
        return {key: value for _, key, value in self.GetContents()}


def _write_threshold(params, field, value) -> None:
    """Write one threshold the way the page's widgets do.

    The dispatch is on the field's own type, which is the same question
    `is_integer_field` answers for the widget choice. Anything that makes
    these two disagree is the truncation bug.
    """
    key = preference_key(field)
    if is_integer_field(field):
        params.SetInt(key, int(value))
    else:
        params.SetFloat(key, float(value))


# =============================================================================
# Coverage: can a shop reach every number?
# =============================================================================


class ThresholdCoverageTests(unittest.TestCase):
    def test_every_threshold_is_reachable(self):
        self.assertEqual(unreachable_fields(), ())

    def test_no_spec_names_a_field_that_does_not_exist(self):
        known = set(threshold_field_names())
        self.assertEqual([s.field for s in all_specs() if s.field not in known], [])

    def test_no_threshold_appears_twice(self):
        """Two widgets on one key would fight: whichever saves last wins."""
        counts = Counter(spec.field for spec in all_specs())
        self.assertEqual([f for f, c in counts.items() if c > 1], [])

    def test_every_group_is_navigable(self):
        """A group nobody can scan is the flat list the page exists to avoid."""
        for group in THRESHOLD_GROUPS:
            self.assertTrue(group.title)
            self.assertTrue(group.blurb, f"{group.title} has nothing to say for itself")
            self.assertTrue(group.specs)
            self.assertLessEqual(len(group.specs), 20, group.title)

    def test_a_handful_is_marked_as_everyday(self):
        common = common_field_names()
        self.assertGreater(len(common), 4)
        self.assertLess(len(common), 20)
        self.assertIn("thin_wall_warn_mm", common)


class ThresholdLabelTests(unittest.TestCase):
    def test_labels_are_prose_not_identifiers(self):
        for spec in all_specs():
            self.assertTrue(spec.label, spec.field)
            self.assertNotIn("_", spec.label, spec.field)
            self.assertEqual(spec.label, spec.label.strip(), spec.field)

    def test_labels_are_unique_within_a_group(self):
        for group in THRESHOLD_GROUPS:
            labels = [spec.label for spec in group.specs]
            self.assertEqual(len(labels), len(set(labels)), group.title)

    def test_every_spec_names_a_known_unit(self):
        for spec in all_specs():
            self.assertIn(spec.unit, _UNIT_STYLES, spec.field)

    def test_defaults_sit_inside_the_range_offered(self):
        """A default outside its own spinbox range is silently clamped."""
        for spec in all_specs():
            style = spec.style
            value = default_for(spec.field)
            self.assertGreaterEqual(value, style.minimum, spec.field)
            self.assertLessEqual(value, style.maximum, spec.field)

    def test_defaults_come_from_the_rules_themselves(self):
        thresholds = RuleThresholds()
        for spec in all_specs():
            self.assertEqual(default_for(spec.field), getattr(thresholds, spec.field))


# =============================================================================
# Types: does a float stay a float?
# =============================================================================


class ThresholdTypeTests(unittest.TestCase):
    def test_only_the_counting_fields_are_integers(self):
        integers = {spec.field for spec in all_specs() if is_integer_field(spec.field)}
        self.assertEqual(
            integers,
            {
                "hole_intersecting_network_threshold",
                "setup_count_info_min",
                "setup_count_warn",
                "setup_count_error",
            },
        )

    def test_a_count_expressed_as_a_float_stays_a_float(self):
        """`feature_complexity_warn` counts features and is still a float.

        Which is exactly why the widget type is derived from the default
        rather than guessed from the label.
        """
        self.assertFalse(is_integer_field("feature_complexity_warn"))

    def test_preference_defaults_are_typed_to_match(self):
        for spec in all_specs():
            value = preference_defaults()[preference_key(spec.field)]
            if is_integer_field(spec.field):
                self.assertIsInstance(value, int, spec.field)
            else:
                self.assertIsInstance(value, float, spec.field)
                self.assertNotIsInstance(value, int, spec.field)

    def test_every_threshold_survives_the_parameter_store(self):
        """Write every default out, read it back, and compare exactly."""
        params = _TypedParams()
        for spec in all_specs():
            _write_threshold(params, spec.field, default_for(spec.field))

        config = MachiningConfig.from_preferences(params.as_prefs())
        for spec in all_specs():
            self.assertEqual(
                getattr(config.thresholds, spec.field),
                default_for(spec.field),
                spec.field,
            )

    def test_a_float_threshold_stays_a_float(self):
        params = _TypedParams()
        _write_threshold(params, "thin_wall_warn_mm", 0.9)
        _write_threshold(params, "sheet_louver_max_height_factor", 3.3474)

        thresholds = MachiningConfig.from_preferences(params.as_prefs()).thresholds
        self.assertIsInstance(thresholds.thin_wall_warn_mm, float)
        self.assertEqual(thresholds.thin_wall_warn_mm, 0.9)
        self.assertEqual(thresholds.sheet_louver_max_height_factor, 3.3474)

    def test_the_wrong_setter_would_have_truncated(self):
        """The failure the derived widget type exists to prevent.

        Not a test of the page -- a test that the trap is real, so the guard
        above is not mistaken for ceremony.
        """
        params = _TypedParams()
        params.SetInt(preference_key("thin_wall_warn_mm"), 1.5)
        thresholds = MachiningConfig.from_preferences(params.as_prefs()).thresholds
        self.assertEqual(thresholds.thin_wall_warn_mm, 1.0)

    def test_an_integer_threshold_stays_whole(self):
        params = _TypedParams()
        _write_threshold(params, "setup_count_warn", 5)
        thresholds = MachiningConfig.from_preferences(params.as_prefs()).thresholds
        self.assertEqual(thresholds.setup_count_warn, 5)
        self.assertIsInstance(thresholds.setup_count_warn, int)

    def test_an_edited_value_actually_reaches_the_rules(self):
        params = _TypedParams()
        _write_threshold(params, "hole_deep_warn_ratio", 4.25)
        config = MachiningConfig.from_preferences(params.as_prefs())
        self.assertEqual(config.thresholds.hole_deep_warn_ratio, 4.25)
        self.assertNotEqual(config.thresholds.hole_deep_warn_ratio, 6.0)

    def test_the_key_prefix_is_the_one_the_config_looks_for(self):
        self.assertTrue(preference_key("thin_wall_warn_mm").startswith(THRESHOLD_PREF_PREFIX))
        self.assertEqual(
            preference_key("thin_wall_warn_mm"),
            THRESHOLD_PREF_PREFIX + "thin_wall_warn_mm",
        )


# =============================================================================
# Promoted constants
# =============================================================================


class PromotedConstantTests(unittest.TestCase):
    """The recognizer constants lifted into `RuleThresholds`.

    Every default is pinned to the value the recognizer already used, so
    promoting them changes no verdict until each recognizer is wired to read
    its threshold instead of its own module constant.
    """

    PROMOTED = {
        "draft_min_deg": 0.5,
        "draft_max_deg": 8.0,
        "rib_recognized_max_thickness_mm": 5.0,
        "rib_recognized_min_height_aspect": 3.0,
        "sheet_classify_max_gauge_mm": 8.0,
        "sheet_fold_min_gauge_mm": 0.5,
        "sheet_fold_max_gauge_mm": 6.0,
        "marking_max_depth_mm": 2.8,
        "marking_max_stroke_width_mm": 3.5,
        "marking_max_glyph_size_mm": 20.0,
        "oring_gland_min_width_mm": 2.0,
        "oring_gland_max_width_mm": 10.0,
        "oring_gland_min_width_depth_ratio": 1.4,
        "oring_gland_max_width_depth_ratio": 2.2,
    }

    def test_promoted_defaults_match_the_recognizers(self):
        thresholds = RuleThresholds()
        for field, value in self.PROMOTED.items():
            self.assertEqual(getattr(thresholds, field), value, field)

    def test_promoted_fields_are_reachable_from_the_editor(self):
        for field in self.PROMOTED:
            self.assertIsNotNone(spec_for(field), field)

    def test_promoted_fields_can_be_overridden(self):
        params = _TypedParams()
        for field in self.PROMOTED:
            _write_threshold(params, field, 7.0)
        thresholds = MachiningConfig.from_preferences(params.as_prefs()).thresholds
        for field in self.PROMOTED:
            self.assertEqual(getattr(thresholds, field), 7.0, field)


# =============================================================================
# The shelf
# =============================================================================


class ToolLibraryCodecTests(unittest.TestCase):
    def test_one_tool_round_trips(self):
        tool = ToolEntry(
            type="end_mill",
            min_diameter_mm=1.588,
            max_diameter_mm=1.588,
            corner_radius_mm=0.0,
            max_flute_length_mm=4.762,
            max_reach_mm=9.525,
            unit="imperial",
        )
        self.assertEqual(ToolEntry.from_spec(tool.to_spec()), tool)

    def test_the_default_shelf_round_trips(self):
        """To six significant figures, which is the store's precision.

        The defaults are computed -- a corner radius is five percent of the
        diameter -- so a few arrive carrying float noise in the sixteenth
        digit. A micron on a 100 mm tool is not a difference a shop has.
        """
        original = default_tool_library()
        restored = decode_tool_library(encode_tool_library(original))
        self.assertEqual(len(restored), len(original))
        for before, after in zip(original, restored):
            self.assertEqual(before.type, after.type)
            self.assertEqual(before.unit, after.unit)
            for attribute in (
                "min_diameter_mm",
                "max_diameter_mm",
                "corner_radius_mm",
                "max_flute_length_mm",
                "max_reach_mm",
            ):
                self.assertAlmostEqual(
                    getattr(before, attribute), getattr(after, attribute), places=5
                )

    def test_a_garbled_entry_costs_one_tool_not_the_shelf(self):
        text = "end_mill,1,1,0,3,6,metric;this is not a tool;drill,3,3,0,30,60,metric"
        tools = decode_tool_library(text)
        self.assertEqual([tool.type for tool in tools], ["end_mill", "drill"])

    def test_a_short_entry_is_dropped(self):
        self.assertIsNone(ToolEntry.from_spec("end_mill,6,6"))

    def test_an_unknown_unit_becomes_unit_agnostic(self):
        tool = ToolEntry.from_spec("end_mill,6,6,0,18,36,cubits")
        self.assertEqual(tool.unit, "")

    def test_the_unit_is_optional(self):
        tool = ToolEntry.from_spec("boring_bar,10,100,0.4,80,150")
        self.assertEqual(tool.unit, "")
        self.assertEqual(tool.max_diameter_mm, 100.0)

    def test_nothing_stored_reads_as_an_empty_shelf(self):
        self.assertEqual(decode_tool_library(""), [])

    def test_the_default_types_are_all_offered_by_the_page(self):
        stocked = {tool.type for tool in default_tool_library()}
        self.assertTrue(stocked.issubset(set(TOOL_TYPES)), stocked - set(TOOL_TYPES))


class ToolLibraryPreferenceTests(unittest.TestCase):
    def test_an_edited_shelf_reaches_the_config(self):
        params = _TypedParams()
        params.SetString(TOOL_LIBRARY_PREF_KEY, "end_mill,6,6,0,18,36,metric")

        config = MachiningConfig.from_preferences(params.as_prefs())
        self.assertEqual(len(config.tool_library), 1)
        self.assertEqual(config.smallest_end_mill_diameter(), 6.0)

    def test_the_shelf_changes_what_the_rules_answer(self):
        """The whole reason the library is worth editing.

        Half the corner-radius verdicts turn on this one number, and it is
        half the smallest end-mill diameter rather than the smallest corner
        radius on the shelf.
        """
        catalogue = MachiningConfig.from_preferences({})
        self.assertEqual(catalogue.smallest_internal_corner_radius(), 0.5)

        coarse = MachiningConfig.from_preferences(
            {TOOL_LIBRARY_PREF_KEY: "end_mill,6,6,0,18,36,metric"}
        )
        self.assertEqual(coarse.smallest_internal_corner_radius(), 3.0)

    def test_an_unset_shelf_falls_back_to_the_catalogue(self):
        for prefs in ({}, {TOOL_LIBRARY_PREF_KEY: ""}, {TOOL_LIBRARY_PREF_KEY: "rubbish"}):
            config = MachiningConfig.from_preferences(prefs)
            self.assertEqual(len(config.tool_library), len(default_tool_library()))

    def test_a_one_millimetre_shop_and_a_six_millimetre_shop_differ(self):
        fine = MachiningConfig.from_preferences(
            {TOOL_LIBRARY_PREF_KEY: "end_mill,1,1,0,3,6,metric;end_mill,6,6,0,18,36,metric"}
        )
        self.assertEqual(fine.smallest_end_mill_diameter(), 1.0)
        self.assertEqual(fine.standard_corner_radii(), [0.5, 3.0])


# =============================================================================
# The drill indexes
# =============================================================================


class DrillIndexCodecTests(unittest.TestCase):
    def test_sizes_round_trip_in_order(self):
        self.assertEqual(
            decode_size_list(encode_size_list((6.0, 3.0, 10.0))), [3.0, 6.0, 10.0]
        )

    def test_a_shop_may_type_them_in_any_order(self):
        self.assertEqual(decode_size_list("10, 3, 6"), [3.0, 6.0, 10.0])

    def test_duplicates_and_rubbish_are_dropped(self):
        self.assertEqual(decode_size_list("3, 3.0, oops, , -1, 0, 6"), [3.0, 6.0])

    def test_newlines_and_semicolons_are_tolerated(self):
        self.assertEqual(decode_size_list("3;6\n10"), [3.0, 6.0, 10.0])

    def test_the_catalogues_round_trip(self):
        for catalogue in (METRIC_DRILL_SIZES_MM, IMPERIAL_DRILL_SIZES_MM):
            restored = decode_size_list(encode_size_list(catalogue))
            self.assertEqual(restored, sorted(set(round(s, 6) for s in catalogue)))


class DrillIndexPreferenceTests(unittest.TestCase):
    def test_an_edited_metric_index_reaches_the_config(self):
        config = MachiningConfig.from_preferences(
            {"MachiningUnitSystem": "metric", METRIC_DRILL_PREF_KEY: "3, 6, 6.8"}
        )
        self.assertEqual(config.drill_sizes_mm, [3.0, 6.0, 6.8])
        self.assertEqual(config.all_drill_sizes_mm(), [3.0, 6.0, 6.8])

    def test_an_edited_imperial_index_reaches_the_config(self):
        config = MachiningConfig.from_preferences(
            {"MachiningUnitSystem": "imperial", IMPERIAL_DRILL_PREF_KEY: "3.175, 6.35"}
        )
        self.assertEqual(config.imperial_drill_sizes_mm, [3.175, 6.35])
        self.assertEqual(config.all_drill_sizes_mm(), [3.175, 6.35])

    def test_both_indexes_are_consulted_under_both(self):
        config = MachiningConfig.from_preferences(
            {
                "MachiningUnitSystem": "both",
                METRIC_DRILL_PREF_KEY: "6",
                IMPERIAL_DRILL_PREF_KEY: "6.35",
            }
        )
        self.assertEqual(config.all_drill_sizes_mm(), [6.0, 6.35])

    def test_an_unset_index_falls_back_to_the_catalogue(self):
        config = MachiningConfig.from_preferences({METRIC_DRILL_PREF_KEY: ""})
        self.assertEqual(config.drill_sizes_mm, list(METRIC_DRILL_SIZES_MM))
        self.assertEqual(config.imperial_drill_sizes_mm, list(IMPERIAL_DRILL_SIZES_MM))

    def test_an_odd_index_makes_a_hole_nonstandard(self):
        """A shop with no 5 mm drill should not be told a 5 mm hole is stock."""
        config = MachiningConfig.from_preferences(
            {"MachiningUnitSystem": "metric", METRIC_DRILL_PREF_KEY: "3, 4, 6"}
        )
        self.assertNotIn(5.0, config.all_drill_sizes_mm())
        self.assertIn(4.0, config.all_drill_sizes_mm())


if __name__ == "__main__":
    unittest.main()
