# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the protrusion recognizers: steps, bosses and ribs.

Protrusions are harder to refuse than cavities, because the outside of the raw
billet has the same local signature as the thing standing on it. A billet top
is a flat face with nothing above it, exactly like a boss top; a billet side is
a flat face opposed to another one, exactly like a rib web. Most of what these
tests check is the guards that tell the two apart.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.machining.recognizers import (
    BossRecognizer,
    RibRecognizer,
    StepRecognizer,
)
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


def block() -> TopoDS_Shape:
    """The 100 x 80 x 40 billet."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def make_shouldered_block() -> TopoDS_Shape:
    """One shoulder milled off the top: a 50 x 80 terrace 20 mm down."""
    return _cut(block(), _box((50, -1, 20), (101, 81, 41)))


def make_staircase() -> TopoDS_Shape:
    """Two terraces, one below the other."""
    first = _cut(block(), _box((50, -1, 20), (101, 81, 41)))
    return _cut(first, _box((70, -1, 10), (101, 81, 41)))


def make_cylindrical_boss() -> TopoDS_Shape:
    """A 24 mm spigot standing 20 mm off the billet top."""
    spigot = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(50, 40, 40), gp_Dir(0, 0, 1)), 12.0, 20.0
    )
    return _fuse(block(), spigot.Shape())


def make_rectangular_boss() -> TopoDS_Shape:
    """A 20 x 20 mounting pad standing 20 mm off the billet top."""
    return _fuse(block(), _box((40, 30, 40), (60, 50, 60)))


def make_through_hole() -> TopoDS_Shape:
    """A 16 mm bore straight through the billet."""
    drill = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 8.0, 50.0
    )
    return _cut(block(), drill.Shape())


def plate() -> TopoDS_Shape:
    """The 120 x 80 x 10 plate the ribs stand on."""
    return BRepPrimAPI_MakeBox(120.0, 80.0, 10.0).Shape()


def make_rib_on_plate() -> TopoDS_Shape:
    """One web: 4 mm thick, 60 mm long, standing 20 mm off the plate."""
    return _fuse(plate(), _box((58, 10, 10), (62, 70, 30)))


def make_rib_field() -> TopoDS_Shape:
    """Two parallel webs on the same plate, well apart."""
    one = _fuse(plate(), _box((30, 10, 10), (34, 70, 30)))
    return _fuse(one, _box((86, 10, 10), (90, 70, 30)))


def make_thin_slot() -> TopoDS_Shape:
    """A 4 mm slot right through the billet.

    Its two walls are 4 mm apart and face each other, which is a rib in every
    respect except the one that matters: there is a void between them.
    """
    return _cut(block(), _box((48, -1, 20), (52, 81, 41)))


# =============================================================================


def _graph(shape: TopoDS_Shape):
    return AagBuilder(shape, FaceIndex(shape)).build()


def steps_in(shape: TopoDS_Shape):
    return StepRecognizer().recognize(_graph(shape), shape)


def bosses_in(shape: TopoDS_Shape):
    return BossRecognizer().recognize(_graph(shape), shape)


def ribs_in(shape: TopoDS_Shape):
    return RibRecognizer().recognize(_graph(shape), shape)


def pipeline_features(shape: TopoDS_Shape, feature_type: str):
    """Features of one type as the whole pipeline reports them.

    Some of what a protrusion pass has to refuse is refused for it by an
    earlier pass: a channel floor is a terrace by every local test, and it is
    the slot already owning it that settles the matter.
    """
    data = MachiningAnalyzer().execute(
        shape, FaceIndex(shape), EdgeIndex(shape), prefs={}
    )
    context = list(data.values())[0]
    return context.recognition.of_type(feature_type)


# =============================================================================


class TestStepRecognition(unittest.TestCase):
    def test_shoulder_is_one_step(self):
        steps = steps_in(make_shouldered_block())
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].type, FeatureType.STEP)

    def test_step_height_and_width(self):
        step = steps_in(make_shouldered_block())[0]
        self.assertAlmostEqual(step.number("step_height_mm"), 20.0, places=3)
        self.assertAlmostEqual(step.number("step_width_mm"), 50.0, places=3)

    def test_step_normal_points_out_of_the_terrace(self):
        step = steps_in(make_shouldered_block())[0]
        normal = step.direction("normal")
        self.assertIsNotNone(normal)
        self.assertAlmostEqual(normal.Z(), 1.0, places=3)

    def test_step_owns_its_terrace_and_riser(self):
        step = steps_in(make_shouldered_block())[0]
        self.assertEqual(len(step.faces), 2)

    def test_staircase_gives_a_step_per_terrace(self):
        steps = steps_in(make_staircase())
        self.assertEqual(len(steps), 2)
        heights = sorted(round(s.number("step_height_mm"), 3) for s in steps)
        self.assertEqual(heights, [10.0, 20.0])


class TestNotSteps(unittest.TestCase):
    def test_plain_billet_has_no_step(self):
        # Every face is open on all sides, so nothing has the mixed edge
        # signature a terrace needs.
        self.assertEqual(steps_in(block()), [])

    def test_pocket_does_not_seed_a_step(self):
        # The host face carries an inner wire and the pocket walls reach the
        # outside only across that wire, so neither can seed. Without the
        # inner-wire filter the block top reports a step as deep as the pocket.
        pocket = _cut(block(), _box((15, 15, 20), (85, 65, 41)))
        self.assertEqual(steps_in(pocket), [])

    def test_slot_walls_are_not_terraces(self):
        # Each wall of a slot can see its opposite number one hop away through
        # the floor. Real terraces have nothing staring back at them, so no
        # candidate with a sideways normal survives.
        for step in steps_in(make_thin_slot()):
            normal = step.direction("normal")
            self.assertAlmostEqual(abs(normal.Z()), 1.0, places=3)

    def test_slot_floor_is_not_a_terrace(self):
        # Walled on two sides and open at both ends, which is exactly a
        # terrace locally. The slot pass runs first and claims it.
        self.assertEqual(pipeline_features(make_thin_slot(), FeatureType.STEP), [])

    def test_bored_billet_has_no_step(self):
        self.assertEqual(steps_in(make_through_hole()), [])


class TestBossRecognition(unittest.TestCase):
    def test_cylindrical_boss(self):
        bosses = bosses_in(make_cylindrical_boss())
        self.assertEqual(len(bosses), 1)
        self.assertEqual(bosses[0].type, FeatureType.BOSS)
        self.assertEqual(bosses[0].param("boss_type"), "cylindrical")

    def test_cylindrical_boss_size(self):
        boss = bosses_in(make_cylindrical_boss())[0]
        self.assertAlmostEqual(boss.number("diameter_mm"), 24.0, places=3)
        self.assertAlmostEqual(boss.number("height_mm"), 20.0, places=3)

    def test_cylindrical_boss_records_its_axis(self):
        # The undercut rules need the axis to tell a leaning spigot from an
        # upright one, and the viewer needs a point on the axis line because a
        # tilted cylinder does not sit centred in its bounding box.
        boss = bosses_in(make_cylindrical_boss())[0]
        axis = boss.direction("axis")
        self.assertIsNotNone(axis)
        self.assertAlmostEqual(abs(axis.Z()), 1.0, places=3)
        self.assertTrue(boss.has("axis_location"))

    def test_cylindrical_boss_owns_top_and_wall(self):
        boss = bosses_in(make_cylindrical_boss())[0]
        self.assertEqual(len(boss.faces), 2)

    def test_rectangular_boss(self):
        bosses = bosses_in(make_rectangular_boss())
        self.assertEqual(len(bosses), 1)
        self.assertEqual(bosses[0].param("boss_type"), "rectangular")
        self.assertAlmostEqual(bosses[0].number("diameter_mm"), 0.0, places=3)

    def test_rectangular_boss_size(self):
        boss = bosses_in(make_rectangular_boss())[0]
        self.assertAlmostEqual(boss.number("height_mm"), 20.0, places=3)
        self.assertAlmostEqual(boss.number("width_mm"), 20.0, places=3)
        self.assertAlmostEqual(boss.number("length_mm"), 20.0, places=3)

    def test_rectangular_boss_owns_top_and_four_walls(self):
        boss = bosses_in(make_rectangular_boss())[0]
        self.assertEqual(len(boss.faces), 5)


class TestNotBosses(unittest.TestCase):
    def test_plain_billet_has_no_boss(self):
        # The billet top is a flat face with every edge convex, which is the
        # boss seed exactly. Its walls sit on the stock silhouette, and that
        # is what refuses it.
        self.assertEqual(bosses_in(block()), [])

    def test_bored_billet_has_no_boss(self):
        # A bore leaves the top face all-convex too: the rim of an opening is
        # the edge you deburr, not an interior corner.
        self.assertEqual(bosses_in(make_through_hole()), [])

    def test_shoulder_is_not_a_boss(self):
        self.assertEqual(bosses_in(make_shouldered_block()), [])

    def test_rib_top_is_not_a_boss(self):
        # A rib top passes every downstream guard -- it has walls, it is
        # enclosed, and the plate below is a base. Only its aspect ratio says
        # it is a web rather than a pad.
        self.assertEqual(bosses_in(make_rib_on_plate()), [])

    def test_pocket_floor_is_not_a_boss(self):
        pocket = _cut(block(), _box((15, 15, 20), (85, 65, 41)))
        self.assertEqual(bosses_in(pocket), [])


class TestRibRecognition(unittest.TestCase):
    def test_web_on_a_plate_is_a_rib(self):
        ribs = ribs_in(make_rib_on_plate())
        self.assertEqual(len(ribs), 1)
        self.assertEqual(ribs[0].type, FeatureType.RIB)

    def test_rib_dimensions(self):
        # Height must come from the standing direction, not from sorting the
        # bounding box: this rib runs 60 mm and stands 20 mm, and it is the
        # 20 mm that the aspect rule reads.
        rib = ribs_in(make_rib_on_plate())[0]
        self.assertAlmostEqual(rib.number("thickness_mm"), 4.0, places=3)
        self.assertAlmostEqual(rib.number("height_mm"), 20.0, places=3)
        self.assertAlmostEqual(rib.number("length_mm"), 60.0, places=3)

    def test_rib_owns_its_webs_top_and_ends(self):
        # Two webs, the top strip and two end walls -- and not the plate,
        # which is a convex neighbour of both webs and would otherwise be
        # highlighted as part of the rib.
        rib = ribs_in(make_rib_on_plate())[0]
        self.assertEqual(len(rib.faces), 5)

    def test_rib_field_reports_each_web_separately(self):
        # A part that is mostly ribs is the case membership has to be right
        # on, because the thin-feature findings are suppressed per rib.
        ribs = ribs_in(make_rib_field())
        self.assertEqual(len(ribs), 2)
        for rib in ribs:
            self.assertAlmostEqual(rib.number("thickness_mm"), 4.0, places=3)

    def test_webs_of_different_ribs_are_not_paired(self):
        # The two far webs are opposed and thin, but the material between
        # them is air. Only the sign test rules that out.
        faces = [set(rib.faces) for rib in ribs_in(make_rib_field())]
        self.assertTrue(faces[0].isdisjoint(faces[1]))


class TestNotRibs(unittest.TestCase):
    def test_plain_billet_has_no_rib(self):
        self.assertEqual(ribs_in(block()), [])

    def test_bare_plate_has_no_rib(self):
        self.assertEqual(ribs_in(plate()), [])

    def test_slot_walls_are_not_a_rib(self):
        # Four millimetres apart and facing each other, but the offset points
        # along the outward normal rather than against it, so there is a void
        # between them and not a web.
        self.assertEqual(ribs_in(make_thin_slot()), [])

    def test_boss_walls_are_not_a_rib(self):
        # A 20 mm pad has material between its opposed walls, so only the
        # thickness cap separates it from a rib. Past 5 mm a wall is structure
        # and none of the rib rules have anything to say about it.
        self.assertEqual(ribs_in(make_rectangular_boss()), [])


if __name__ == "__main__":
    unittest.main()
