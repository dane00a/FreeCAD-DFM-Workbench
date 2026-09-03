# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the three cavities that have no floor to seed from.

A through cavity, a slit and a spherical bowl are all cut away rather than
milled to a bottom, so none of them presents the floor-and-walls signature the
pocket and slot recognizers look for. Each is instead pinned down by one
physical fact -- a through cavity terminates against the outside of the part at
both ends of an axis, a slit runs clean out at both ends, a bowl is a concave
patch of one sphere -- and most of the work in each recognizer is refusing the
things that satisfy that fact by accident.
"""

import math
import unittest

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeSphere,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape

from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.machining.recognizers import (
    SlitRecognizer,
    SphericalPocketRecognizer,
    ThroughCavityRecognizer,
)
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


def _prism(points, direction) -> TopoDS_Shape:
    """A prism swept from a closed polygon."""
    polygon = BRepBuilderAPI_MakePolygon()
    for point in points:
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(*direction)).Shape()


def _round_vertical_edges(shape: TopoDS_Shape, radius: float) -> TopoDS_Shape:
    """Fillet every edge that runs straight up, corner-radius fashion."""
    fillet = BRepFilletAPI_MakeFillet(shape)
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        adaptor = BRepAdaptor_Curve(edge)
        start = adaptor.Value(adaptor.FirstParameter())
        end = adaptor.Value(adaptor.LastParameter())
        if abs(start.X() - end.X()) < 1e-6 and abs(start.Y() - end.Y()) < 1e-6:
            fillet.Add(radius, edge)
    return fillet.Shape()


def block() -> TopoDS_Shape:
    """The 100 x 80 x 40 block everything here is cut out of."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def _graph(shape: TopoDS_Shape):
    return AagBuilder(shape, FaceIndex(shape)).build()


def cavities_in(shape: TopoDS_Shape, claimed=None):
    return ThroughCavityRecognizer().recognize(_graph(shape), shape, claimed)


def slits_in(shape: TopoDS_Shape, claimed=None):
    return SlitRecognizer().recognize(_graph(shape), shape, claimed)


def bowls_in(shape: TopoDS_Shape, claimed=None):
    return SphericalPocketRecognizer().recognize(_graph(shape), shape, claimed)


# =============================================================================
# Shapes
# =============================================================================


def make_t_slot() -> TopoDS_Shape:
    """A T-slot running the length of the block, open at both ends.

    The neck is 10mm wide and the flange 20, so the two facing pairs give the
    minimum and maximum width directly.
    """
    neck = _cavity((-1, 45, 30), (101, 55, 41))
    flange = _cavity((-1, 40, 20), (101, 60, 30))
    return _cut(block(), _fuse(neck, flange))


def make_rounded_window() -> TopoDS_Shape:
    """A 30mm square window with 6mm corner radii, cut clean through."""
    cutter = _round_vertical_edges(_cavity((35, 25, -1), (65, 55, 41)), 6.0)
    return _cut(block(), cutter)


def make_rectangular_tunnel() -> TopoDS_Shape:
    """A plain rectangular bore running across the block: four walls, four
    corners, nothing a slot reading would lose."""
    return _cut(block(), _cavity((40, -1, 15), (60, 81, 25)))


def make_blind_pocket() -> TopoDS_Shape:
    return _cut(block(), _cavity((15, 15, 20), (85, 65, 41)))


def make_boss() -> TopoDS_Shape:
    """A pad standing on the block: convex all round, and no cavity anywhere."""
    return _fuse(block(), _cavity((40, 30, 40), (60, 50, 55)))


def make_penetrating_slit() -> TopoDS_Shape:
    """A 2.5mm slit sawn right through the block: two walls and nothing else."""
    return _cut(block(), _cavity((40, -1, -1), (42.5, 81, 41)))


def make_floored_slit() -> TopoDS_Shape:
    """A 3mm slit 15mm deep, running out of the block at both ends."""
    return _cut(block(), _cavity((40, -1, 25), (43, 81, 41)))


def make_wide_channel() -> TopoDS_Shape:
    """20mm wide and 15 deep: open at both ends, but nothing bends here."""
    return _cut(block(), _cavity((-1, 30, 25), (101, 50, 41)))


def make_dovetail() -> TopoDS_Shape:
    """A slot wider at the bottom than at its opening: no endmill cuts this."""
    profile = [
        (-1, 35, 41),
        (-1, 45, 41),
        (-1, 48, 25),
        (-1, 32, 25),
    ]
    return _cut(block(), _prism(profile, (102, 0, 0)))


def make_v_groove() -> TopoDS_Shape:
    """A 60 degree V running the length of the block, 5mm deep."""
    half_width = 6.0 * math.tan(math.radians(30.0))
    profile = [
        (-1, 40.0 - half_width, 41.0),
        (-1, 40.0 + half_width, 41.0),
        (-1, 40.0, 35.0),
    ]
    return _cut(block(), _prism(profile, (102, 0, 0)))


def make_closed_slot() -> TopoDS_Shape:
    """A 4mm slot blind at both ends: the slot recognizer's work, not a slit."""
    return _cut(block(), _cavity((30, 38, 25), (70, 42, 41)))


def make_blind_shaft() -> TopoDS_Shape:
    """A 10 x 10 square shaft sunk into the top.

    Each of its four walls presents an opposing pair and a run that is open
    both ways. The face opposite each one roofs the supposed opening over.
    """
    return _cut(block(), _cavity((45, 35, 20), (55, 45, 41)))


def make_hemispherical_bowl() -> TopoDS_Shape:
    """A 15mm ball sunk to its equator: the widest bowl with no overhang."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 40), gp_Dir(0, 0, 1)), 15.0)
    return _cut(block(), ball.Shape())


def make_super_hemispherical_bowl() -> TopoDS_Shape:
    """The same ball sunk 6mm further, so the equator is buried."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 34), gp_Dir(0, 0, 1)), 15.0)
    return _cut(block(), ball.Shape())


def make_shallow_dish() -> TopoDS_Shape:
    """A ball barely dipped in: a dish 7mm deep, wide open."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 48), gp_Dir(0, 0, 1)), 15.0)
    return _cut(block(), ball.Shape())


def make_dome() -> TopoDS_Shape:
    """A hemispherical dome standing proud of the block."""
    ball = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(50, 40, 40), gp_Dir(0, 0, 1)), 12.0)
    upper = _cut(ball.Shape(), _cavity((0, 0, -20), (100, 80, 40)))
    return _fuse(block(), upper)


def make_filleted_pocket() -> TopoDS_Shape:
    """A pocket with every internal edge blended at one radius.

    Where two wall fillets meet the floor fillet the kernel produces a
    spherical patch of that same radius -- concave, rimmed by cylinders, and
    indistinguishable from a bowl on local geometry alone.
    """
    shape = _cut(block(), _cavity((20, 20, 20), (80, 60, 41)))
    fillet = BRepFilletAPI_MakeFillet(shape)
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        adaptor = BRepAdaptor_Curve(edge)
        start = adaptor.Value(adaptor.FirstParameter())
        end = adaptor.Value(adaptor.LastParameter())
        inside = all(
            19.9 <= point.X() <= 80.1
            and 19.9 <= point.Y() <= 60.1
            and 19.9 <= point.Z() <= 40.1
            for point in (start, end)
        )
        vertical = abs(start.X() - end.X()) < 1e-6 and abs(start.Y() - end.Y()) < 1e-6
        on_floor = abs(start.Z() - 20.0) < 1e-6 and abs(end.Z() - 20.0) < 1e-6
        if inside and (vertical or on_floor):
            fillet.Add(5.0, edge)
    return fillet.Shape()


# =============================================================================


class TestThroughCavityRecognition(unittest.TestCase):
    """A window is a window because it comes out the other side."""

    def test_t_slot_is_one_cavity(self):
        cavities = cavities_in(make_t_slot())
        self.assertEqual(len(cavities), 1)
        self.assertEqual(cavities[0].type, FeatureType.THROUGH_CAVITY)

    def test_t_slot_owns_every_wall_of_the_profile(self):
        """Two neck walls, two steps, two flange walls and the bottom."""
        self.assertEqual(len(cavities_in(make_t_slot())[0].faces), 7)

    def test_t_slot_runs_along_the_block(self):
        cavity = cavities_in(make_t_slot())[0]
        axis = cavity.direction("through_axis")
        self.assertAlmostEqual(abs(axis.X()), 1.0, places=6)
        self.assertAlmostEqual(cavity.number("depth_mm"), 100.0, places=3)

    def test_t_slot_reports_both_sections(self):
        """The narrow width is what the tool has to fit through; the wide one
        is what the tee-nut sits in."""
        cavity = cavities_in(make_t_slot())[0]
        self.assertAlmostEqual(cavity.number("min_width_mm"), 10.0, places=3)
        self.assertAlmostEqual(cavity.number("max_width_mm"), 20.0, places=3)

    def test_t_slot_profile_turns_six_times(self):
        """Two of the six corners are external -- where each step meets the
        neck wall above it -- so counting only inward turns leaves four and
        reads a T as a plain rectangle."""
        self.assertEqual(cavities_in(make_t_slot())[0].param("corner_count"), 6)

    def test_rounded_window_is_claimed_for_its_radii(self):
        """A floorless rounded window has four corners and no other recognizer
        to serve it, so its corner cylinders alone earn the claim."""
        cavities = cavities_in(make_rounded_window())
        self.assertEqual(len(cavities), 1)
        self.assertAlmostEqual(cavities[0].number("corner_radius_mm"), 6.0, places=3)
        self.assertAlmostEqual(
            abs(cavities[0].direction("through_axis").Z()), 1.0, places=6
        )

    def test_rectangular_tunnel_is_left_alone(self):
        """Four walls and four corners: the slot recognizer reads it without
        losing anything, so this one stands down."""
        self.assertEqual(cavities_in(make_rectangular_tunnel()), [])

    def test_blind_pocket_is_not_a_through_cavity(self):
        """Its walls terminate against the part on one side only."""
        self.assertEqual(cavities_in(make_blind_pocket()), [])

    def test_a_boss_grows_no_phantom_cavity(self):
        """A pad's walls are convex nearly all round: no interior corners to
        seed on."""
        self.assertEqual(cavities_in(make_boss()), [])


class TestSlitRecognition(unittest.TestCase):
    """A slit is a cut that never ends against material."""

    def test_penetrating_slit_is_found_without_a_floor(self):
        slits = slits_in(make_penetrating_slit())
        self.assertEqual(len(slits), 1)
        self.assertEqual(slits[0].type, FeatureType.FLEXURE_SLIT)
        self.assertTrue(slits[0].param("full_penetration"))
        self.assertEqual(len(slits[0].faces), 2)
        self.assertAlmostEqual(slits[0].number("width_mm"), 2.5, places=3)

    def test_floored_slit_is_a_flexure(self):
        slits = slits_in(make_floored_slit())
        self.assertEqual(len(slits), 1)
        self.assertEqual(slits[0].type, FeatureType.FLEXURE_SLIT)
        self.assertAlmostEqual(slits[0].number("width_mm"), 3.0, places=3)
        self.assertAlmostEqual(slits[0].number("depth_mm"), 15.0, places=3)
        self.assertTrue(slits[0].param("through_both_ends"))

    def test_floored_slit_length_spans_the_block(self):
        self.assertAlmostEqual(
            slits_in(make_floored_slit())[0].number("length_mm"), 80.0, places=3
        )

    def test_wide_channel_is_not_a_flexure(self):
        """Nothing 20mm wide and 15 deep bends; it keeps the plain slot name."""
        slits = slits_in(make_wide_channel())
        self.assertEqual(len(slits), 1)
        self.assertEqual(slits[0].type, FeatureType.SLOT)

    def test_dovetail_is_broached(self):
        """Both walls lean back over the floor, so no endmill reaches the
        undercut and the profile has to be broached or wire-cut."""
        slits = slits_in(make_dovetail())
        self.assertEqual(len(slits), 1)
        self.assertEqual(slits[0].type, FeatureType.BROACHED_SLOT)
        self.assertEqual(slits[0].param("slit_profile"), "reentrant")

    def test_v_groove_is_read_from_its_apex(self):
        grooves = slits_in(make_v_groove())
        self.assertEqual(len(grooves), 1)
        self.assertEqual(grooves[0].type, FeatureType.V_GROOVE)
        self.assertAlmostEqual(grooves[0].number("apex_angle_deg"), 60.0, places=2)
        self.assertAlmostEqual(grooves[0].number("depth_mm"), 5.0, places=3)

    def test_closed_slot_is_not_a_slit(self):
        """It stops against material at both ends: the slot recognizer's."""
        self.assertEqual(slits_in(make_closed_slot()), [])

    def test_blind_shaft_is_not_a_slit(self):
        """Each wall pair opposes correctly, and the wall opposite roofs the
        opening over."""
        self.assertEqual(slits_in(make_blind_shaft()), [])

    def test_a_claimed_floor_is_left_alone(self):
        """What an earlier recognizer spoke for is settled geometry."""
        shape = make_floored_slit()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        floor = slits_in(shape)[0].faces[0]
        self.assertEqual(SlitRecognizer().recognize(graph, shape, {floor}), [])


class TestSphericalPocketRecognition(unittest.TestCase):
    """A bowl is measured by its rim, not by its radius."""

    def test_hemisphere_is_a_pocket(self):
        bowls = bowls_in(make_hemispherical_bowl())
        self.assertEqual(len(bowls), 1)
        self.assertEqual(bowls[0].type, FeatureType.SPHERICAL_POCKET)
        self.assertAlmostEqual(bowls[0].number("radius_mm"), 15.0, places=3)

    def test_hemisphere_opens_to_its_full_diameter(self):
        bowl = bowls_in(make_hemispherical_bowl())[0]
        self.assertAlmostEqual(bowl.number("opening_diameter_mm"), 30.0, places=3)
        self.assertAlmostEqual(
            abs(bowl.direction("opening_normal").Z()), 1.0, places=6
        )

    def test_hemisphere_overhangs_nothing(self):
        """The widest bowl a straight tool can still leave: exactly at the
        equator, nothing hanging over."""
        bowl = bowls_in(make_hemispherical_bowl())[0]
        self.assertFalse(bowl.param("is_super_hemispherical"))
        self.assertAlmostEqual(bowl.number("overhang_mm"), 0.0, places=6)

    def test_buried_equator_is_an_undercut(self):
        bowl = bowls_in(make_super_hemispherical_bowl())[0]
        self.assertTrue(bowl.param("is_super_hemispherical"))
        self.assertAlmostEqual(bowl.number("embedment_depth_mm"), 6.0, places=3)
        self.assertGreater(bowl.number("overhang_mm"), 1.0)
        self.assertLess(bowl.number("opening_diameter_mm"), 30.0)

    def test_shallow_dish_is_not_an_undercut(self):
        """Sunk short of the equator, so the opening is the widest part."""
        bowl = bowls_in(make_shallow_dish())[0]
        self.assertFalse(bowl.param("is_super_hemispherical"))
        self.assertAlmostEqual(bowl.number("overhang_mm"), 0.0, places=6)

    def test_dome_is_not_a_pocket(self):
        """Same sphere, material on the other side of it."""
        self.assertEqual(bowls_in(make_dome()), [])

    def test_filleted_pocket_corners_are_not_bowls(self):
        """Four concave spherical patches, every one of them the blend where
        three same-radius fillets meet. A ball endmill of that radius rolls
        straight through, so none is an undercut."""
        shape = make_filleted_pocket()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        corners = SphericalPocketRecognizer._sphere_clusters(graph)
        self.assertEqual(len(corners), 4)  # the geometry really is there
        self.assertEqual(bowls_in(shape), [])


if __name__ == "__main__":
    unittest.main()
