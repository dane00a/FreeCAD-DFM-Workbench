# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for reading the shelf out of FreeCAD's CAM workbench.

Three failures would each be silent in the application, and each is worse
than it looks:

* A CAM bit mapped to the wrong type. A 90-degree V-bit filed as a 6 mm end
  mill makes every pocket corner in the part achievable, and the analysis
  comes back clean because it asked the wrong tool.
* A diameter read without its unit. A tool bit file keeps the unit it was
  typed in, so a 3/8 inch tap read as 0.375 mm puts something on the shelf
  that is not a tool.
* An empty CAM install landing on an empty shelf. Every tool-dependent rule
  stands down when it finds no tool -- with nothing in the report to say so
  -- so the part passes for want of a cutter rather than on its merits.

The live read needs a running FreeCAD with CAM in it, so it sits behind a
seam: a callable handing over libraries of plain bit records. These tests
feed that seam records built by hand, which covers every decision in the
mapping with no FreeCAD in sight. The real thing is verified separately in
FreeCAD itself.
"""

import unittest

from freecad.DFM.core.machining.cam_tools import (
    LATHE_TYPES,
    SHAPE_TYPES,
    UNMAPPED_SHAPES,
    CamBit,
    CamLibrary,
    CamReading,
    cam_tool_library,
    describe,
    entry_from_bit,
    lathe_tooling,
    read_bits,
    read_cam_tools,
)
from freecad.DFM.core.machining.config import (
    TOOL_LIBRARY_PREF_KEY,
    TOOL_SOURCE_CAM,
    TOOL_SOURCE_CATALOGUE,
    TOOL_SOURCE_CUSTOM,
    TOOL_SOURCE_PREF_KEY,
    TOOL_TYPES,
    MachiningConfig,
    ToolEntry,
    default_tool_source,
    encode_tool_library,
    resolve_tool_library,
)


# =============================================================================
# Stand-ins
# =============================================================================


class FakeQuantity:
    """A FreeCAD Quantity reduced to how the reader questions one.

    The real thing is asked to convert rather than to print itself, because
    its printed form follows whatever unit schema the user set and the shelf
    is stored in millimetres regardless.
    """

    def __init__(self, value_mm):
        self.Value = float(value_mm)

    def getValueAs(self, unit):
        if unit != "mm":
            raise ValueError(unit)
        return self.Value

    def __str__(self):
        # Deliberately unhelpful: nothing should be reading this.
        return "<quantity>"


def endmill(diameter="6.0000 mm", **extra):
    properties = {
        "Diameter": diameter,
        "CuttingEdgeHeight": "20.0000 mm",
        "Length": "50.0000 mm",
    }
    properties.update(extra)
    return CamBit(shape="Endmill", label="an end mill", properties=properties)


def library(*bits, label="Shop"):
    return CamLibrary(label=label, bits=list(bits))


def source_of(*libraries):
    return lambda: list(libraries)


# =============================================================================
# Reading one value
# =============================================================================


class TestValueReading(unittest.TestCase):
    def test_stored_millimetres(self):
        tool = entry_from_bit(endmill("5.0000 mm"))
        self.assertAlmostEqual(tool.min_diameter_mm, 5.0)

    def test_stored_inches_are_converted(self):
        """A 3/8 inch tool is 9.525 mm, not 0.375 of anything."""
        tool = entry_from_bit(
            CamBit(
                shape="Tap",
                properties={"Diameter": '0.375 "', "CuttingEdgeLength": '1.063 "'},
            )
        )
        self.assertAlmostEqual(tool.min_diameter_mm, 9.525, places=3)
        self.assertAlmostEqual(tool.max_flute_length_mm, 27.0, places=1)

    def test_inch_spelled_out(self):
        tool = entry_from_bit(endmill("0.5 in"))
        self.assertAlmostEqual(tool.min_diameter_mm, 12.7, places=3)

    def test_quantity_is_asked_to_convert(self):
        tool = entry_from_bit(endmill(FakeQuantity(8.0)))
        self.assertAlmostEqual(tool.min_diameter_mm, 8.0)

    def test_bare_number_is_millimetres(self):
        self.assertAlmostEqual(entry_from_bit(endmill(4.0)).min_diameter_mm, 4.0)

    def test_unknown_unit_is_refused(self):
        """Better no tool than a tool of unknown size."""
        self.assertIsNone(entry_from_bit(endmill("6.0000 furlongs")))


# =============================================================================
# Mapping a bit onto the shelf
# =============================================================================


class TestMapping(unittest.TestCase):
    def test_end_mill(self):
        tool = entry_from_bit(endmill())
        self.assertEqual(tool.type, "end_mill")
        self.assertEqual(tool.corner_radius_mm, 0.0)
        self.assertAlmostEqual(tool.max_flute_length_mm, 20.0)
        self.assertAlmostEqual(tool.max_reach_mm, 50.0)

    def test_fixed_size_not_a_range(self):
        tool = entry_from_bit(endmill("6.0000 mm"))
        self.assertEqual(tool.min_diameter_mm, tool.max_diameter_mm)

    def test_ball_nose_carries_a_full_tip(self):
        """Half the diameter, the way the catalogue marks a ball nose."""
        tool = entry_from_bit(
            CamBit(shape="Ballend", properties={"Diameter": "6.0000 mm"})
        )
        self.assertEqual(tool.type, "ball_nose")
        self.assertAlmostEqual(tool.corner_radius_mm, 3.0)

    def test_tapered_ball_nose_is_still_a_ball_nose(self):
        tool = entry_from_bit(
            CamBit(shape="TaperedBallNose", properties={"Diameter": "2.0000 mm"})
        )
        self.assertEqual(tool.type, "ball_nose")
        self.assertAlmostEqual(tool.corner_radius_mm, 1.0)

    def test_bullnose_is_a_corner_radius_end_mill(self):
        tool = entry_from_bit(
            CamBit(
                shape="Bullnose",
                properties={"Diameter": "6.0000 mm", "CornerRadius": "1.5000 mm"},
            )
        )
        self.assertEqual(tool.type, "end_mill")
        self.assertAlmostEqual(tool.corner_radius_mm, 1.5)

    def test_bullnose_corner_cannot_exceed_the_tip(self):
        """A corner radius past D/2 is a typo; a ball nose is a ball nose."""
        tool = entry_from_bit(
            CamBit(
                shape="Bullnose",
                properties={"Diameter": "6.0000 mm", "CornerRadius": "9.0000 mm"},
            )
        )
        self.assertAlmostEqual(tool.corner_radius_mm, 3.0)

    def test_drill_has_no_flute_length_in_cam(self):
        """CAM records only an overall length, so the flute stays unknown."""
        tool = entry_from_bit(
            CamBit(
                shape="Drill",
                properties={"Diameter": "5.0000 mm", "Length": "50.0000 mm"},
            )
        )
        self.assertEqual(tool.type, "drill")
        self.assertEqual(tool.max_flute_length_mm, 0.0)
        self.assertAlmostEqual(tool.max_reach_mm, 50.0)

    def test_reamer(self):
        tool = entry_from_bit(
            CamBit(
                shape="Reamer",
                properties={"Diameter": "8.0000 mm", "CuttingEdgeHeight": "30.0000 mm"},
            )
        )
        self.assertEqual(tool.type, "reamer")

    def test_shape_name_spelling_does_not_matter(self):
        """A hand-written bit file says "tap"; the class says "Tap"."""
        for spelling in ("Tap", "tap", "TAP"):
            bit = CamBit(shape=spelling, properties={"Diameter": "6.0000 mm"})
            self.assertEqual(entry_from_bit(bit).type, "tap")

    def test_cam_aliases_are_understood(self):
        """Torus is CAM's own other word for a bullnose."""
        bit = CamBit(shape="torus", properties={"Diameter": "6.0000 mm"})
        self.assertEqual(entry_from_bit(bit).type, "end_mill")

    def test_unmapped_shapes_are_left_on_the_bench(self):
        """Never the nearest slot: a V-bit is not a small end mill."""
        for shape in ("VBit", "v-bit", "Chamfer", "Dovetail", "SlittingSaw",
                      "ThreadMill", "Radius", "Probe", "Custom"):
            bit = CamBit(shape=shape, properties={"Diameter": "6.0000 mm"})
            self.assertIsNone(entry_from_bit(bit), shape)

    def test_every_unmapped_shape_is_named(self):
        """A count with no name tells the shop nothing about what it lost."""
        for shape, what in UNMAPPED_SHAPES.items():
            self.assertTrue(what and not what.endswith("."), shape)

    def test_no_shape_is_both_mapped_and_skipped(self):
        self.assertFalse(set(SHAPE_TYPES) & set(UNMAPPED_SHAPES))

    def test_a_bit_with_no_diameter_is_not_a_tool(self):
        """Zero would read as an infinitely fine cutter and pass every corner."""
        self.assertIsNone(entry_from_bit(CamBit(shape="Endmill", properties={})))
        self.assertIsNone(entry_from_bit(endmill("0.0000 mm")))

    def test_cam_tools_are_unit_agnostic(self):
        """A tool on a real shelf cuts whatever the shop points it at.

        The unit field exists to keep a metric shop from being told its bore
        is a quarter-twenty. A cutter somebody actually owns is not a
        catalogue size and must not be filtered out of a size match.
        """
        self.assertEqual(entry_from_bit(endmill('0.25 "')).unit, "")

    def test_mapped_types_are_all_in_the_workbench_vocabulary(self):
        """A type no rule recognises is the same as no tool at all."""
        for tool_type in SHAPE_TYPES.values():
            self.assertIn(tool_type, TOOL_TYPES, tool_type)


# =============================================================================
# Reading a whole library
# =============================================================================


class TestReadBits(unittest.TestCase):
    def test_counts_what_it_read_and_what_it_passed_over(self):
        reading = read_bits(
            [
                library(
                    endmill("6.0000 mm"),
                    CamBit(shape="VBit", properties={"Diameter": "6.0000 mm"}),
                    CamBit(shape="Probe", properties={"Diameter": "6.0000 mm"}),
                    label="Mill 1",
                )
            ]
        )
        self.assertEqual(len(reading.tools), 1)
        self.assertEqual(reading.bits_seen, 3)
        self.assertEqual(reading.libraries, ["Mill 1"])
        self.assertEqual(reading.skipped, {"V-bit": 1, "probe": 1})
        self.assertTrue(reading.usable)

    def test_the_same_cutter_in_two_libraries_is_one_tool(self):
        """A shop keeps a library per machine and the 6 mm is in all of them."""
        reading = read_bits(
            [library(endmill("6.0000 mm"), label="Mill 1"),
             library(endmill("6.0000 mm"), label="Mill 2")]
        )
        self.assertEqual(len(reading.tools), 1)
        self.assertEqual(reading.bits_seen, 2)
        self.assertEqual(reading.libraries, ["Mill 1", "Mill 2"])

    def test_an_unnamed_library_still_counts(self):
        reading = read_bits([library(endmill(), label="")])
        self.assertEqual(reading.libraries, ["unnamed"])

    def test_nothing_at_all(self):
        reading = read_bits([])
        self.assertFalse(reading.usable)
        self.assertEqual(reading.tools, [])


class TestReadCamTools(unittest.TestCase):
    def test_a_collector_that_throws_is_not_a_traceback(self):
        def broken():
            raise RuntimeError("asset store on fire")

        reading = read_cam_tools(broken)
        self.assertFalse(reading.usable)
        self.assertIn("on fire", reading.error)

    def test_cam_missing_altogether(self):
        def missing():
            raise ImportError("No module named 'Path'")

        reading = read_cam_tools(missing)
        self.assertFalse(reading.usable)
        self.assertIn("not available", reading.error)

    def test_lathe_tooling_rides_along(self):
        """CAM cannot hold a boring bar, so its silence is not a statement."""
        tools = cam_tool_library(source_of(library(endmill("6.0000 mm"))))
        types = {tool.type for tool in tools}
        self.assertIn("end_mill", types)
        for lathe_type in LATHE_TYPES:
            self.assertIn(lathe_type, types)

    def test_lathe_tooling_alone_is_not_a_shelf(self):
        """An empty CAM must report empty, not report three turning inserts."""
        self.assertEqual(cam_tool_library(source_of()), [])
        self.assertTrue(lathe_tooling())


# =============================================================================
# What the page says
# =============================================================================


class TestDescribe(unittest.TestCase):
    def test_an_empty_cam_says_so_and_says_what_happens_next(self):
        text = describe(read_cam_tools(source_of()))
        self.assertIn("No tool libraries", text)
        self.assertIn("catalogue", text)

    def test_a_failure_says_why(self):
        text = describe(CamReading(error="the CAM workbench is not available"))
        self.assertIn("not available", text)
        self.assertIn("catalogue", text)

    def test_a_good_read_names_the_libraries_and_the_leftovers(self):
        text = describe(
            read_cam_tools(
                source_of(
                    library(
                        endmill("6.0000 mm"),
                        CamBit(shape="VBit", properties={"Diameter": "6.0000 mm"}),
                        label="Haas VF2",
                    )
                )
            )
        )
        self.assertIn("Haas VF2", text)
        self.assertIn("1 tool ", text)  # singular, not "1 tools"
        self.assertIn("1 V-bit", text)
        self.assertIn("Boring bars", text)

    def test_libraries_with_nothing_usable(self):
        text = describe(
            read_cam_tools(
                source_of(library(CamBit(shape="Probe", properties={}), label="Router"))
            )
        )
        self.assertIn("nothing in them", text)
        self.assertIn("catalogue", text)


# =============================================================================
# Which shelf ends up in force
# =============================================================================

_HAND_EDITED = encode_tool_library(
    [
        ToolEntry(type="end_mill", min_diameter_mm=2.0, max_diameter_mm=2.0),
        ToolEntry(type="end_mill", min_diameter_mm=10.0, max_diameter_mm=10.0),
    ]
)


class TestResolveToolLibrary(unittest.TestCase):
    def test_catalogue_ignores_a_stored_list(self):
        tools = resolve_tool_library(TOOL_SOURCE_CATALOGUE, _HAND_EDITED)
        self.assertGreater(len(tools), 50)

    def test_custom_uses_the_stored_list(self):
        tools = resolve_tool_library(TOOL_SOURCE_CUSTOM, _HAND_EDITED)
        self.assertEqual(len(tools), 2)

    def test_custom_with_nothing_stored_falls_back(self):
        """An emptied list is not an instruction to analyse against no tools."""
        self.assertGreater(len(resolve_tool_library(TOOL_SOURCE_CUSTOM, "")), 50)

    def test_cam_with_tools(self):
        tools = resolve_tool_library(
            TOOL_SOURCE_CAM, "", reader=source_of(library(endmill("3.0000 mm")))
        )
        diameters = [t.min_diameter_mm for t in tools if t.type == "end_mill"]
        self.assertEqual(diameters, [3.0])

    def test_cam_with_nothing_falls_back_to_the_catalogue(self):
        tools = resolve_tool_library(TOOL_SOURCE_CAM, "", reader=source_of())
        self.assertGreater(len(tools), 50)

    def test_cam_never_falls_back_to_a_stored_list(self):
        """A shop that chose CAM did not ask for the shelf it typed in 2024."""
        tools = resolve_tool_library(TOOL_SOURCE_CAM, _HAND_EDITED, reader=source_of())
        self.assertGreater(len(tools), 50)

    def test_an_unknown_source_behaves_the_way_it_always_did(self):
        tools = resolve_tool_library("something from a newer version", _HAND_EDITED)
        self.assertEqual(len(tools), 2)


class TestDefaultToolSource(unittest.TestCase):
    def test_a_stored_shelf_means_it_was_hand_edited(self):
        self.assertEqual(default_tool_source(_HAND_EDITED), TOOL_SOURCE_CUSTOM)

    def test_nothing_stored_means_the_catalogue(self):
        self.assertEqual(default_tool_source(""), TOOL_SOURCE_CATALOGUE)

    def test_a_garbled_shelf_is_not_a_hand_edited_one(self):
        self.assertEqual(default_tool_source(";;;"), TOOL_SOURCE_CATALOGUE)


class TestFromPreferences(unittest.TestCase):
    def test_no_preferences_at_all(self):
        config = MachiningConfig.from_preferences({})
        self.assertEqual(config.tool_source, TOOL_SOURCE_CATALOGUE)
        self.assertGreater(len(config.tool_library), 50)

    def test_an_upgrade_keeps_a_hand_edited_shelf(self):
        """The source key is new; the shelf under the old key is not."""
        config = MachiningConfig.from_preferences({TOOL_LIBRARY_PREF_KEY: _HAND_EDITED})
        self.assertEqual(config.tool_source, TOOL_SOURCE_CUSTOM)
        self.assertEqual(len(config.tool_library), 2)
        self.assertAlmostEqual(config.smallest_end_mill_diameter(), 2.0)

    def test_choosing_the_catalogue_over_a_stored_shelf(self):
        config = MachiningConfig.from_preferences(
            {
                TOOL_SOURCE_PREF_KEY: TOOL_SOURCE_CATALOGUE,
                TOOL_LIBRARY_PREF_KEY: _HAND_EDITED,
            }
        )
        self.assertGreater(len(config.tool_library), 50)

    def test_a_stale_source_value_does_not_empty_the_shelf(self):
        config = MachiningConfig.from_preferences({TOOL_SOURCE_PREF_KEY: "nonsense"})
        self.assertGreater(len(config.tool_library), 50)

    def test_cam_asked_for_but_not_installed(self):
        """The whole point: never an empty shelf. The headless run has no CAM."""
        config = MachiningConfig.from_preferences({TOOL_SOURCE_PREF_KEY: TOOL_SOURCE_CAM})
        self.assertEqual(config.tool_source, TOOL_SOURCE_CAM)
        self.assertGreater(len(config.tool_library), 50)
        self.assertIsNotNone(config.smallest_internal_corner_radius())


class TestResolvedShelfChangesVerdicts(unittest.TestCase):
    """The number that decides most verdicts has to follow the source.

    Half the smallest end-mill diameter is the tightest inside corner the
    shop can cut, and it is what the pocket and blend rules compare every
    modelled corner against. If it does not move when the shelf does, the
    whole exercise bought nothing.
    """

    @staticmethod
    def _corner(**prefs):
        return MachiningConfig.from_preferences(prefs).smallest_internal_corner_radius()

    def test_the_catalogue_corner(self):
        self.assertAlmostEqual(self._corner(), 0.5)  # a 1 mm end mill

    def test_a_cam_shop_with_nothing_smaller_than_a_quarter_inch(self):
        config = MachiningConfig()
        config.tool_library = cam_tool_library(
            source_of(library(endmill('0.25 "'), endmill("12.0000 mm")))
        )
        config.tool_source = TOOL_SOURCE_CAM
        self.assertAlmostEqual(config.smallest_end_mill_diameter(), 6.35, places=2)
        self.assertAlmostEqual(config.smallest_internal_corner_radius(), 3.175, places=3)

    def test_a_cam_shop_that_owns_a_one_millimetre_cutter(self):
        config = MachiningConfig()
        config.tool_library = cam_tool_library(
            source_of(library(endmill("1.0000 mm"), endmill("12.0000 mm")))
        )
        self.assertAlmostEqual(config.smallest_internal_corner_radius(), 0.5)

    def test_a_cam_shelf_of_ball_noses_alone_cuts_no_inside_corner(self):
        """No end mill on the shelf: the corner rules stand down honestly."""
        config = MachiningConfig()
        config.tool_library = cam_tool_library(
            source_of(library(CamBit(shape="Ballend", properties={"Diameter": "6 mm"})))
        )
        self.assertIsNone(config.smallest_internal_corner_radius())

    def test_the_turning_nose_survives_a_cam_read(self):
        """CAM holds no inserts, so the lathe rules must not go quiet."""
        config = MachiningConfig()
        config.tool_library = cam_tool_library(source_of(library(endmill())))
        self.assertIsNotNone(config.smallest_turning_nose_radius())

    def test_standard_corner_radii_come_from_the_cam_shelf(self):
        config = MachiningConfig()
        config.tool_library = cam_tool_library(
            source_of(library(endmill("6.0000 mm"), endmill("10.0000 mm")))
        )
        self.assertEqual(config.standard_corner_radii(), [3.0, 5.0])


if __name__ == "__main__":
    unittest.main()
