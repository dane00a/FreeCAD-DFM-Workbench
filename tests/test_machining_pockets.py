# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for pocket recognition.

The hard part of recognizing a pocket is not finding floors, it is refusing
the things that look like floors. An open corner between two webs, the top of
a plate carrying a boss, and the outside of the part all present a flat face
with other faces around it, and none of them is a cavity.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.machining.recognizers import PocketRecognizer
from freecad.DFM.core.utils.geometry import FaceIndex


# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def _cavity(p0, p1) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def block() -> TopoDS_Shape:
    """The 100 x 80 x 40 block every pocket is cut into."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def pockets_in(shape: TopoDS_Shape):
    graph = AagBuilder(shape, FaceIndex(shape)).build()
    return PocketRecognizer().recognize(graph, shape)


def make_simple_pocket() -> TopoDS_Shape:
    """A 70 x 50 pocket, 20mm deep, open at the top only."""
    return _cut(block(), _cavity((15, 15, 20), (85, 65, 41)))


def make_deep_narrow_cavity() -> TopoDS_Shape:
    """10mm wide, 40mm long and 30mm deep, closed at both ends.

    Well enclosed, so the pocket seed accepts it -- but four times longer
    than it is wide, which makes it a channel to mill along rather than a
    pocket to clear out.
    """
    return _cut(block(), _cavity((45, 20, 10), (55, 60, 41)))


def make_through_slot() -> TopoDS_Shape:
    """A channel running right across the block, open at both ends."""
    return _cut(block(), _cavity((40, -1, 20), (60, 81, 41)))


def make_stepped_pocket() -> TopoDS_Shape:
    """A pocket with a deeper pocket inside it: two floors, two cavities."""
    return _cut(_cut(block(), _cavity((15, 15, 25), (85, 65, 41))), _cavity((30, 25, 12), (70, 55, 41)))


def make_angle_plate() -> TopoDS_Shape:
    """Two perpendicular webs meeting in an L.

    The open corner between them has walls on about half its boundary and
    reads as a deep pocket if enclosure is not checked.
    """
    upright = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(100, 12, 80)).Shape()
    base = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(100, 80, 12)).Shape()
    return _fuse(upright, base)


def make_pocket_with_bored_floor() -> TopoDS_Shape:
    """A pocket with a hole drilled through its floor."""
    pocket = _cut(block(), _cavity((20, 20, 20), (80, 60, 41)))
    drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 6.0, 50.0)
    return _cut(pocket, drill.Shape())


# =============================================================================


class TestPocketRecognition(unittest.TestCase):
    def test_simple_pocket(self):
        pockets = pockets_in(make_simple_pocket())
        self.assertEqual(len(pockets), 1)
        self.assertEqual(pockets[0].type, FeatureType.POCKET)

    def test_pocket_depth_and_width(self):
        pocket = pockets_in(make_simple_pocket())[0]
        self.assertAlmostEqual(pocket.number("depth_mm"), 20.0, places=3)
        self.assertAlmostEqual(pocket.number("min_width_mm"), 50.0, places=3)
        self.assertAlmostEqual(pocket.number("max_width_mm"), 70.0, places=3)

    def test_floor_normal_points_out_of_the_cavity(self):
        pocket = pockets_in(make_simple_pocket())[0]
        normal = pocket.param("floor_normal")
        self.assertAlmostEqual(normal[2], 1.0, places=3)

    def test_floor_leads_the_face_list(self):
        # Later passes rely on the floor being first.
        pocket = pockets_in(make_simple_pocket())[0]
        self.assertEqual(len(pocket.faces), 5)  # floor plus four walls

    def test_long_narrow_cavity_is_a_channel_not_a_pocket(self):
        # Milling a channel is a different job from clearing a pocket -- one
        # pass along its length against a spiral clear-out -- so a cavity
        # much longer than it is wide is reported as a slot even though its
        # floor is fully enclosed.
        found = pockets_in(make_deep_narrow_cavity())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].type, FeatureType.SLOT)
        self.assertAlmostEqual(found[0].number("width_mm"), 10.0, places=3)
        self.assertAlmostEqual(found[0].number("length_mm"), 40.0, places=3)
        self.assertAlmostEqual(found[0].number("depth_mm"), 30.0, places=3)

    def test_a_squarer_cavity_stays_a_pocket(self):
        # 50 x 70 is not elongated enough to be a channel.
        found = pockets_in(make_simple_pocket())
        self.assertEqual(found[0].type, FeatureType.POCKET)

    def test_two_pockets_are_reported_separately(self):
        shape = _cut(
            _cut(block(), _cavity((10, 10, 25), (45, 70, 41))),
            _cavity((55, 10, 25), (90, 70, 41)),
        )
        self.assertEqual(len(pockets_in(shape)), 2)

    def test_stepped_pocket_gives_a_cavity_per_level(self):
        self.assertEqual(len(pockets_in(make_stepped_pocket())), 2)

    def test_a_hole_in_the_floor_is_not_absorbed(self):
        # The bore belongs to the hole recognizer. Absorbing it would make
        # one feature of the cavity and everything drilled into it.
        pockets = pockets_in(make_pocket_with_bored_floor())
        self.assertEqual(len(pockets), 1)
        self.assertAlmostEqual(pockets[0].number("depth_mm"), 20.0, places=3)


class TestNotPockets(unittest.TestCase):
    def test_plain_block_has_no_pocket(self):
        self.assertEqual(pockets_in(block()), [])

    def test_boss_on_a_plate_is_not_a_pocket(self):
        boss = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, 40), gp_Dir(0, 0, 1)), 12.0, 20.0)
        self.assertEqual(pockets_in(_fuse(block(), boss.Shape())), [])

    def test_a_hole_is_not_a_pocket(self):
        drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 8.0, 50.0)
        self.assertEqual(pockets_in(_cut(block(), drill.Shape())), [])

    def test_open_corner_of_an_angle_plate_is_not_a_pocket(self):
        # The enclosure test exists for this: the corner is walled on about
        # half its boundary, and without the check it reads as a pocket 80mm
        # deep that nobody machined.
        self.assertEqual(pockets_in(make_angle_plate()), [])

    def test_through_slot_is_not_a_pocket(self):
        # Open at both ends, so it is a slot. Its floor is rimmed on only
        # half its boundary and the slot recognizer owns it instead.
        self.assertEqual(pockets_in(make_through_slot()), [])


if __name__ == "__main__":
    unittest.main()


# =============================================================================


from freecad.DFM.core.machining.recognizers import SlotRecognizer


def slots_in(shape: TopoDS_Shape):
    """Cavities the pocket recognizer declined, plus channels it reclassified."""
    graph = AagBuilder(shape, FaceIndex(shape)).build()
    pockets = PocketRecognizer().recognize(graph, shape)
    claimed = {face for feature in pockets for face in feature.faces}
    from_pockets = [f for f in pockets if f.type == FeatureType.SLOT]
    return from_pockets + SlotRecognizer().recognize(graph, shape, claimed)


class TestSlotRecognition(unittest.TestCase):
    def test_through_slot_is_recognized_and_open(self):
        slots = slots_in(make_through_slot())
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].type, FeatureType.SLOT)
        self.assertTrue(slots[0].param("is_open"))

    def test_through_slot_dimensions(self):
        slot = slots_in(make_through_slot())[0]
        self.assertAlmostEqual(slot.number("width_mm"), 20.0, places=3)
        self.assertAlmostEqual(slot.number("length_mm"), 80.0, places=3)
        self.assertAlmostEqual(slot.number("depth_mm"), 20.0, places=3)

    def test_narrow_deep_channel(self):
        shape = _cut(block(), _cavity((48, -1, 10), (52, 81, 41)))
        slots = slots_in(shape)
        self.assertEqual(len(slots), 1)
        self.assertAlmostEqual(slots[0].number("width_mm"), 4.0, places=3)
        self.assertAlmostEqual(slots[0].number("depth_mm"), 30.0, places=3)

    def test_closed_channel_is_not_open(self):
        slot = slots_in(make_deep_narrow_cavity())[0]
        self.assertFalse(slot.param("is_open"))

    def test_square_pocket_is_not_a_slot(self):
        self.assertEqual(slots_in(make_simple_pocket()), [])

    def test_plain_block_has_no_slot(self):
        self.assertEqual(slots_in(block()), [])

    def test_a_hole_is_not_a_slot(self):
        drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 8.0, 50.0)
        self.assertEqual(slots_in(_cut(block(), drill.Shape())), [])

    def test_a_boss_is_not_a_slot(self):
        boss = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, 40), gp_Dir(0, 0, 1)), 12.0, 20.0)
        self.assertEqual(slots_in(_fuse(block(), boss.Shape())), [])

    def test_square_corners_record_no_radius(self):
        # Absent, not zero: a rule must be able to tell an unmeasured radius
        # from a measured radius of nothing.
        slot = slots_in(make_through_slot())[0]
        self.assertFalse(slot.has("corner_radius_mm"))
