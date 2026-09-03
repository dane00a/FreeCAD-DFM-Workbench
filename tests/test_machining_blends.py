# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for fillet and chamfer recognition.

Two things are hard here. The first is refusing the surfaces that look like
blends: a bore wall is a cylinder with a smooth seam running up it, and a
narrow rib top is a strip beside much bigger faces, and neither is a blend.

The second is counting. The kernel hands back one face per edge a blend
crosses, so a radius run round the top of a block arrives as four faces of a
single operation. Reporting four fillets there would mean four radii to a
rule that is trying to say how many tools the job needs.
"""

import unittest

from OCP.BRep import BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Shape

from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.aag import SurfaceType
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.machining.recognizers import BlendRecognizer
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


def _edges(shape: TopoDS_Shape) -> list[TopoDS_Edge]:
    found = []
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        found.append(TopoDS.Edge_s(explorer.Current()))
        explorer.Next()
    return found


def _ends(edge: TopoDS_Edge) -> tuple[gp_Pnt, gp_Pnt]:
    return (
        BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge)),
        BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge)),
    )


def _fillet(shape: TopoDS_Shape, radius: float, wanted) -> TopoDS_Shape:
    """The shape with `radius` rolled onto every edge `wanted` accepts."""
    builder = BRepFilletAPI_MakeFillet(shape)
    for edge in _edges(shape):
        if wanted(*_ends(edge)):
            builder.Add(radius, edge)
    builder.Build()
    return builder.Shape()


def _chamfer(shape: TopoDS_Shape, distance: float, wanted) -> TopoDS_Shape:
    """The shape with `distance` cut off every edge `wanted` accepts."""
    builder = BRepFilletAPI_MakeChamfer(shape)
    for edge in _edges(shape):
        if wanted(*_ends(edge)):
            builder.Add(distance, edge)
    builder.Build()
    return builder.Shape()


def block() -> TopoDS_Shape:
    """The 100 x 80 x 40 block every blend is put on."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def pocket_block() -> TopoDS_Shape:
    """The block with a 70 x 50 pocket 20mm deep in its top."""
    return _cut(block(), BRepPrimAPI_MakeBox(gp_Pnt(15, 15, 20), gp_Pnt(85, 65, 41)).Shape())


def upright(p0: gp_Pnt, p1: gp_Pnt) -> bool:
    """An edge running along Z."""
    return abs(p0.X() - p1.X()) < 1e-6 and abs(p0.Y() - p1.Y()) < 1e-6


def on_plane_z(height: float):
    """An edge lying flat at the given height."""

    def wanted(p0: gp_Pnt, p1: gp_Pnt) -> bool:
        return abs(p0.Z() - height) < 1e-6 and abs(p1.Z() - height) < 1e-6

    return wanted


def circular_at_z(height: float):
    """A closed circular edge at the given height -- a bore rim or a shaft end."""

    def wanted(p0: gp_Pnt, p1: gp_Pnt) -> bool:
        return (
            abs(p0.Z() - height) < 1e-6
            and abs(p1.Z() - height) < 1e-6
            and p0.Distance(p1) < 1e-6
        )

    return wanted


def pocket_rim(p0: gp_Pnt, p1: gp_Pnt) -> bool:
    """An edge on the rim of the pocket, not on the outside of the block."""
    return (
        on_plane_z(40.0)(p0, p1)
        and 10.0 < p0.X() < 90.0
        and 10.0 < p1.X() < 90.0
        and 10.0 < p0.Y() < 70.0
        and 10.0 < p1.Y() < 70.0
    )


def blends_in(shape: TopoDS_Shape):
    graph = AagBuilder(shape, FaceIndex(shape)).build()
    return BlendRecognizer().recognize(graph, shape)


def surface_types(shape: TopoDS_Shape, face_ids) -> set:
    graph = AagBuilder(shape, FaceIndex(shape)).build()
    return {graph.node(face_id).surface_type for face_id in face_ids}


# =============================================================================


class TestFillets(unittest.TestCase):
    def test_corner_radii_are_found(self):
        # Four upright edges rounded off: four separate radii, because the
        # box sides between them break the run.
        shape = _fillet(block(), 5.0, upright)
        found = blends_in(shape)
        self.assertEqual(len(found), 4)
        self.assertTrue(all(f.type == FeatureType.FILLET for f in found))
        for feature in found:
            self.assertAlmostEqual(feature.number("radius_mm"), 5.0, places=3)

    def test_a_radius_wrapping_the_top_is_one_fillet(self):
        # One pass of one tool round the top of the block. The kernel splits
        # it into a face per edge; reporting four fillets would be four radii
        # to anything counting tools.
        shape = _fillet(block(), 4.0, on_plane_z(40.0))
        found = blends_in(shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].type, FeatureType.FILLET)
        self.assertEqual(len(found[0].faces), 4)
        self.assertAlmostEqual(found[0].number("radius_mm"), 4.0, places=3)

    def test_pocket_floor_radius(self):
        # The internal corner radius: what decides the smallest cutter that
        # can reach the bottom of the cavity.
        shape = _fillet(pocket_block(), 3.0, on_plane_z(20.0))
        found = blends_in(shape)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].number("radius_mm"), 3.0, places=3)

    def test_boss_base_fillet_is_a_torus(self):
        boss = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, 40), gp_Dir(0, 0, 1)), 12.0, 20.0)
        shape = _fillet(_fuse(block(), boss.Shape()), 4.0, circular_at_z(40.0))
        found = blends_in(shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].type, FeatureType.FILLET)
        self.assertAlmostEqual(found[0].number("radius_mm"), 4.0, places=3)
        self.assertEqual(surface_types(shape, found[0].faces), {SurfaceType.TORUS})

    def test_only_the_blend_faces_are_claimed(self):
        # A fillet that swallowed the faces it blends would overlap whatever
        # owns them and one of the two would be dropped as a duplicate.
        shape = _fillet(block(), 4.0, on_plane_z(40.0))
        found = blends_in(shape)
        self.assertEqual(surface_types(shape, found[0].faces), {SurfaceType.CYLINDER})


class TestNotFillets(unittest.TestCase):
    def test_plain_block_has_no_blends(self):
        self.assertEqual(blends_in(block()), [])

    def test_a_drilled_bore_is_not_a_fillet(self):
        # The bore wall comes back as two halves meeting at smooth seams.
        # Those seams are how the surface is written down, not a blend into
        # anything, and without that test every hole in the part is a radius.
        drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 8.0, 50.0)
        self.assertEqual(blends_in(_cut(block(), drill.Shape())), [])

    def test_a_counterbore_is_not_a_fillet(self):
        shape = _cut(
            block(),
            BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 5.0, 60.0).Shape(),
        )
        shape = _cut(
            shape,
            BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, 30), gp_Dir(0, 0, 1)), 10.0, 20.0).Shape(),
        )
        self.assertEqual(blends_in(shape), [])

    def test_a_turned_shaft_is_not_a_fillet(self):
        self.assertEqual(blends_in(BRepPrimAPI_MakeCylinder(20.0, 60.0).Shape()), [])


class TestChamfers(unittest.TestCase):
    def test_edge_breaks_are_found(self):
        shape = _chamfer(block(), 3.0, upright)
        found = blends_in(shape)
        self.assertEqual(len(found), 4)
        self.assertTrue(all(f.type == FeatureType.CHAMFER for f in found))
        for feature in found:
            self.assertAlmostEqual(feature.number("distance_mm"), 3.0, places=3)

    def test_a_chamfer_wrapping_the_top_is_one_chamfer(self):
        shape = _chamfer(block(), 3.0, on_plane_z(40.0))
        found = blends_in(shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0].faces), 4)
        self.assertAlmostEqual(found[0].number("distance_mm"), 3.0, places=3)

    def test_top_and_bottom_chamfers_stay_separate(self):
        # Same distance, and each strip touches the same box sides as its
        # opposite number. They are still two operations, which is why
        # sharing a source face may not merge anything.
        def top_or_bottom(p0, p1):
            return abs(p0.Z() - p1.Z()) < 1e-6 and (
                abs(p0.Z() - 40.0) < 1e-6 or abs(p0.Z()) < 1e-6
            )

        found = blends_in(_chamfer(block(), 3.0, top_or_bottom))
        self.assertEqual(len(found), 2)
        self.assertTrue(all(f.type == FeatureType.CHAMFER for f in found))
        self.assertTrue(all(len(f.faces) == 4 for f in found))

    def test_pocket_rim_chamfer(self):
        shape = _chamfer(pocket_block(), 2.0, pocket_rim)
        found = blends_in(shape)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].number("distance_mm"), 2.0, places=3)

    def test_chamfer_does_not_claim_the_faces_it_breaks(self):
        # If the rim chamfer absorbed the pocket walls it would look like the
        # pocket and be thrown away as a duplicate of it.
        shape = _chamfer(pocket_block(), 2.0, pocket_rim)
        found = blends_in(shape)
        self.assertEqual(len(found[0].faces), 4)
        self.assertEqual(surface_types(shape, found[0].faces), {SurfaceType.PLANE})


class TestConicalChamfers(unittest.TestCase):
    def test_a_bore_rim_chamfer_is_reported(self):
        drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 8.0, 50.0)
        shape = _chamfer(_cut(block(), drill.Shape()), 1.0, circular_at_z(40.0))
        found = blends_in(shape)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].type, FeatureType.CHAMFER)
        self.assertTrue(found[0].param("conical"))
        self.assertAlmostEqual(found[0].number("width_mm"), 1.0, places=3)
        self.assertAlmostEqual(found[0].number("chamfer_angle_deg"), 45.0, places=1)
        self.assertEqual(surface_types(shape, found[0].faces), {SurfaceType.CONE})

    def test_a_shaft_end_chamfer_is_reported(self):
        shape = _chamfer(BRepPrimAPI_MakeCylinder(20.0, 60.0).Shape(), 1.0, circular_at_z(60.0))
        found = blends_in(shape)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].param("conical"))
        self.assertAlmostEqual(found[0].number("width_mm"), 1.0, places=3)

    def test_a_taper_is_not_a_chamfer(self):
        # A cone the length of the part is a turned taper. Only a thin band
        # is an edge being broken.
        cone = BRepPrimAPI_MakeCone(20.0, 10.0, 30.0)
        self.assertEqual(blends_in(cone.Shape()), [])


class TestNotChamfers(unittest.TestCase):
    def test_a_rib_top_is_not_a_chamfer(self):
        # Narrow, long, and far smaller than the faces around it -- but it
        # stands square to them, so it is a rib and not an edge break.
        rib = BRepPrimAPI_MakeBox(gp_Pnt(40, 0, 40), gp_Pnt(44, 80, 70)).Shape()
        self.assertEqual(blends_in(_fuse(block(), rib)), [])

    def test_a_stepped_face_is_not_a_chamfer(self):
        # Every dihedral square, nothing oblique anywhere on the part.
        step = _cut(block(), BRepPrimAPI_MakeBox(gp_Pnt(60, -1, 25), gp_Pnt(101, 81, 41)).Shape())
        self.assertEqual(blends_in(step), [])


class TestReporting(unittest.TestCase):
    def test_instance_ids_are_unique_and_prefixed(self):
        shape = _chamfer(block(), 3.0, upright)
        found = blends_in(shape)
        ids = [f.instance_id for f in found]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(i.startswith("b_") for i in ids))

    def test_recognition_is_repeatable(self):
        # Merging walks a dictionary of components; the order it settles on
        # must not decide which face list a blend gets.
        shape = _fillet(block(), 4.0, on_plane_z(40.0))
        first = [(f.type, tuple(f.faces), f.number("radius_mm")) for f in blends_in(shape)]
        second = [(f.type, tuple(f.faces), f.number("radius_mm")) for f in blends_in(shape)]
        self.assertEqual(first, second)

    def test_faces_are_reported_in_ascending_order(self):
        shape = _fillet(block(), 4.0, on_plane_z(40.0))
        for feature in blends_in(shape):
            self.assertEqual(feature.faces, sorted(feature.faces))


if __name__ == "__main__":
    unittest.main()
