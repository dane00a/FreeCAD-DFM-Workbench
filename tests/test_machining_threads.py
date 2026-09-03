# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the thread tables and for modelled-thread recognition.

Threads reach the workbench two quite different ways. An external thread that
someone bothered to model is a real helix and can be measured outright, pitch
and all. A tapped hole is not modelled at all -- it is a plain bore at the tap
drill size -- so it has to be inferred, and the inference is a guess that the
workbench states as one.

The refusals matter here too. A complete circular edge winds exactly one turn
about its own axis, so a threshold of one turn would call every hole a thread.
"""

import math
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepLib import BRepLib
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.Geom import Geom_CylindricalSurface
from OCP.Geom2d import Geom2d_Line
from OCP.gp import gp_Ax2, gp_Ax2d, gp_Ax3, gp_Dir, gp_Dir2d, gp_Pnt, gp_Pnt2d
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.machining.threads import (
    find_by_designation,
    match_major_diameter,
    match_tap_drill,
    threads_for,
)
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _helix_wire(radius: float, pitch: float, turns: float):
    """A true helix, built the way a CAD system builds a thread spine.

    A straight line in the parameter space of a cylinder maps to a helix on
    it, which is both exact and the cheapest way to say it.
    """
    surface = Geom_CylindricalSurface(
        gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), radius
    )
    slope = gp_Dir2d(1.0, pitch / (2.0 * math.pi))
    line = Geom2d_Line(gp_Ax2d(gp_Pnt2d(0.0, 0.0), slope))
    edge = BRepBuilderAPI_MakeEdge(
        line, surface, 0.0, turns * 2.0 * math.pi
    ).Edge()
    BRepLib.BuildCurves3d_s(edge)
    return BRepBuilderAPI_MakeWire(edge).Wire()


def _swept_thread(radius, pitch, turns, profile):
    pipe = BRepOffsetAPI_MakePipeShell(_helix_wire(radius, pitch, turns))
    pipe.Add(profile, False, False)
    pipe.SetMode(True)
    pipe.Build()
    pipe.MakeSolid()
    return pipe.Shape()


def threaded_shaft(radius=4.0, pitch=1.25, turns=6, depth=0.6) -> TopoDS_Shape:
    """A shaft with a helical groove actually cut into it."""
    shaft = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), radius, 30.0
    ).Shape()
    profile = BRepBuilderAPI_MakePolygon(
        gp_Pnt(radius + 0.2, 0, 0),
        gp_Pnt(radius - depth, 0, pitch * 0.35),
        gp_Pnt(radius + 0.2, 0, pitch * 0.7),
        True,
    ).Wire()
    return _cut(shaft, _swept_thread(radius, pitch, turns, profile))


def threaded_bore(radius=4.0, pitch=1.25, turns=6, depth=0.6) -> TopoDS_Shape:
    """A tapped bore with a real modelled helix."""
    block = BRepPrimAPI_MakeBox(gp_Pnt(-10, -10, 0), gp_Pnt(10, 10, 30)).Shape()
    bored = _cut(
        block,
        BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), radius, 32.0
        ).Shape(),
    )
    profile = BRepBuilderAPI_MakePolygon(
        gp_Pnt(radius - 0.2, 0, 0),
        gp_Pnt(radius + depth, 0, pitch * 0.35),
        gp_Pnt(radius - 0.2, 0, pitch * 0.7),
        True,
    ).Wire()
    return _cut(bored, _swept_thread(radius, pitch, turns, profile))


def tapped_blind_hole(drill_radius=3.4, pitch=1.25, turns=6, depth=0.6):
    """A blind hole at the M8 tap drill with a real helix cut in its wall.

    The bore is 6.8 mm because that is what an M8 is tapped from -- a hole
    modelled at 8 mm would be a clearance hole with a thread drawn in it.
    """
    block = BRepPrimAPI_MakeBox(gp_Pnt(-10, -10, 0), gp_Pnt(10, 10, 30)).Shape()
    bored = _cut(
        block,
        BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), drill_radius, 20.0
        ).Shape(),
    )
    profile = BRepBuilderAPI_MakePolygon(
        gp_Pnt(drill_radius - 0.2, 0, 0),
        gp_Pnt(drill_radius + depth, 0, pitch * 0.35),
        gp_Pnt(drill_radius - 0.2, 0, pitch * 0.7),
        True,
    ).Wire()
    return _cut(bored, _swept_thread(drill_radius, pitch, turns, profile))


def plain_tap_drill_hole(radius=3.4) -> TopoDS_Shape:
    """A plain 6.8 mm bore: exactly an M8 tap drill, with no thread in it."""
    block = BRepPrimAPI_MakeBox(gp_Pnt(-10, -10, 0), gp_Pnt(10, 10, 30)).Shape()
    return _cut(
        block,
        BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), radius, 32.0
        ).Shape(),
    )


def holes_in(shape, hole_type):
    context = list(
        MachiningAnalyzer()
        .execute(shape, FaceIndex(shape), EdgeIndex(shape), prefs={})
        .values()
    )[0]
    return context.recognition.of_type(hole_type)


def plain_shaft() -> TopoDS_Shape:
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 4.0, 30.0
    ).Shape()


def drilled_block() -> TopoDS_Shape:
    block = BRepPrimAPI_MakeBox(40.0, 40.0, 20.0).Shape()
    drill = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(20, 20, -1), gp_Dir(0, 0, 1)), 2.5, 22.0
    ).Shape()
    return _cut(block, drill)


def threads_in(shape):
    context = list(
        MachiningAnalyzer()
        .execute(shape, FaceIndex(shape), EdgeIndex(shape), prefs={})
        .values()
    )[0]
    return context.recognition.of_type(FeatureType.EXTERNAL_THREAD)


# =============================================================================


class TestThreadTables(unittest.TestCase):
    def test_tap_drill_resolves_a_standard_size(self):
        self.assertEqual(match_tap_drill(5.0).designation, "M6x1.0")
        self.assertEqual(match_tap_drill(4.2).designation, "M5x0.8")

    def test_a_diameter_off_every_tap_drill_resolves_to_nothing(self):
        # 7.5 mm sits between the M8 and M10 tap drills. Guessing here would
        # put a thread callout on every clearance bore on the part.
        self.assertIsNone(match_tap_drill(7.5))

    def test_a_metric_shop_is_not_told_about_unc(self):
        # 5.06 mm is within tolerance of both the M6 tap drill and the
        # 1/4-20 one. Which thread it means depends entirely on the shop.
        self.assertEqual(match_tap_drill(5.06, "metric").designation, "M6x1.0")
        self.assertEqual(
            match_tap_drill(5.06, "imperial").designation, "1/4-20 UNC"
        )

    def test_major_diameter_resolves_an_outside_thread(self):
        self.assertEqual(match_major_diameter(8.0).designation, "M8x1.25")

    def test_designations_are_matched_however_they_are_written(self):
        for written in ("M6x1.0", "M6x1", "M6 x 1", "m6X1.0", "M6"):
            self.assertEqual(
                find_by_designation(written).designation, "M6x1.0", written
            )

    def test_imperial_designations_tolerate_the_suffix(self):
        for written in ("1/4-20 UNC", "1/4-20", "1/4-20unc"):
            self.assertEqual(
                find_by_designation(written).designation, "1/4-20 UNC", written
            )

    def test_an_unknown_designation_resolves_to_nothing(self):
        self.assertIsNone(find_by_designation("M6.5x9"))
        self.assertIsNone(find_by_designation(""))

    def test_the_tables_are_selected_by_unit_system(self):
        self.assertTrue(all(s.system == "metric" for s in threads_for("metric")))
        self.assertTrue(
            all(s.system == "imperial" for s in threads_for("imperial"))
        )
        self.assertGreater(len(threads_for("both")), len(threads_for("metric")))


class TestExternalThreadRecognition(unittest.TestCase):
    def test_a_modelled_thread_is_found(self):
        self.assertEqual(len(threads_in(threaded_shaft())), 1)

    def test_the_pitch_is_measured_from_the_helix(self):
        thread = threads_in(threaded_shaft(pitch=1.25))[0]
        self.assertAlmostEqual(thread.number("thread_pitch_mm"), 1.25, places=2)

    def test_the_major_diameter_comes_from_the_crest(self):
        # The helix runs at the root. The diameter a thread is named for is
        # the crest, a thread depth further out.
        thread = threads_in(threaded_shaft(radius=4.0))[0]
        self.assertAlmostEqual(thread.number("major_diameter_mm"), 8.0, places=2)

    def test_a_coarse_thread_is_named(self):
        self.assertEqual(
            threads_in(threaded_shaft(radius=4.0, pitch=1.25))[0].param(
                "thread_designation"
            ),
            "M8x1.25",
        )
        self.assertEqual(
            threads_in(threaded_shaft(radius=3.0, pitch=1.0, depth=0.5))[0].param(
                "thread_designation"
            ),
            "M6x1.0",
        )

    def test_a_fine_pitch_is_measured_but_not_named(self):
        # 8 mm at 1.0 mm pitch is M8x1, a fine thread. The tables are coarse,
        # so the honest answer is the measurement without a name -- not the
        # coarse thread of the same diameter.
        thread = threads_in(threaded_shaft(radius=4.0, pitch=1.0))[0]
        self.assertAlmostEqual(thread.number("thread_pitch_mm"), 1.0, places=2)
        self.assertIsNone(thread.param("thread_designation"))

    def test_the_evidence_is_recorded(self):
        self.assertEqual(
            threads_in(threaded_shaft())[0].param("thread_evidence"),
            "modelled_helix",
        )


class TestTappedHoles(unittest.TestCase):
    """Internal threads, which are promoted from bores by the hole pass."""

    def test_a_modelled_tapped_hole_is_promoted(self):
        found = holes_in(tapped_blind_hole(), FeatureType.THREADED_HOLE)
        self.assertEqual(len(found), 1)

    def test_the_tap_drill_names_the_thread(self):
        # The bore diameter is the tap drill by definition, so once a helix
        # has confirmed a thread the table can name it.
        thread = holes_in(tapped_blind_hole(), FeatureType.THREADED_HOLE)[0]
        self.assertEqual(thread.param("thread_designation"), "M8x1.25")
        self.assertAlmostEqual(thread.number("diameter_mm"), 6.8, places=2)

    def test_the_tapped_length_is_measured_not_assumed(self):
        # Worst-casing to the full hole depth would fire the run-out rule on
        # every part that took the trouble to model its thread.
        thread = holes_in(tapped_blind_hole(), FeatureType.THREADED_HOLE)[0]
        depth = thread.number("thread_depth_mm")
        self.assertIsNotNone(depth)
        self.assertLess(depth, thread.number("depth_mm"))

    def test_the_evidence_is_recorded(self):
        self.assertEqual(
            holes_in(tapped_blind_hole(), FeatureType.THREADED_HOLE)[0].param(
                "thread_evidence"
            ),
            "modelled_helix",
        )

    def test_a_bare_tap_drill_hole_is_not_tapped(self):
        # The diameter-only heuristic is deliberately absent. Most bores at a
        # tap drill size are clearance, reamed, dowel or pilot holes, and
        # guessing would light up a plate of standard drill sizes as fully
        # tapped.
        self.assertEqual(
            holes_in(plain_tap_drill_hole(), FeatureType.THREADED_HOLE), []
        )
        self.assertTrue(holes_in(plain_tap_drill_hole(), FeatureType.THROUGH_HOLE))


class TestThreadRefusals(unittest.TestCase):
    def test_a_plain_shaft_has_no_thread(self):
        self.assertEqual(threads_in(plain_shaft()), [])

    def test_a_plain_block_has_no_thread(self):
        self.assertEqual(
            threads_in(BRepPrimAPI_MakeBox(40.0, 40.0, 20.0).Shape()), []
        )

    def test_a_drilled_hole_is_not_a_thread(self):
        # Every complete circular edge winds exactly one turn about its own
        # axis, so a one-turn threshold would call every hole a thread.
        self.assertEqual(threads_in(drilled_block()), [])

    def test_a_modelled_internal_thread_is_not_an_external_one(self):
        # The flanks of a thread groove look into that groove whichever side
        # the material is on, so the crest is what settles it.
        self.assertEqual(threads_in(threaded_bore()), [])


if __name__ == "__main__":
    unittest.main()
