# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the machining adjacency graph.

The concavity convention is the thing most likely to be subtly wrong, and a
sign error produces plausible output rather than a crash. So it is pinned two
ways here: named ground-truth cases that state what a machinist would say about
each edge, and a census that cross-checks every edge against an independent
physical probe of the solid.
"""

import math
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.machining import AagBuilder, Concavity, SurfaceType
from freecad.DFM.core.machining.oracle import concavity_census
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


def make_box() -> TopoDS_Shape:
    """A 50x50x25 block. Six planar faces, twelve edges, every one convex."""
    return BRepPrimAPI_MakeBox(50.0, 50.0, 25.0).Shape()


def make_pocket() -> TopoDS_Shape:
    """The block with a 30x30x15 open-topped cavity.

    Eight concave edges: four wall-to-floor junctions and four wall-to-wall
    corners. The four edges of the opening rim are convex -- they are the ones
    you deburr.
    """
    cavity = BRepPrimAPI_MakeBox(gp_Pnt(10, 10, 10), gp_Pnt(40, 40, 26)).Shape()
    return _cut(make_box(), cavity)


def make_through_hole() -> TopoDS_Shape:
    """The block with a 12mm bore straight through. Both rims are convex."""
    axis = gp_Ax2(gp_Pnt(25, 25, -1), gp_Dir(0, 0, 1))
    drill = BRepPrimAPI_MakeCylinder(axis, 6.0, 30.0).Shape()
    return _cut(make_box(), drill)


def make_blind_hole() -> TopoDS_Shape:
    """A 10mm bore stopping short of the far side, leaving a flat floor.

    The floor-to-wall junction is concave; the entry rim is convex.
    """
    axis = gp_Ax2(gp_Pnt(25, 25, 10), gp_Dir(0, 0, 1))
    drill = BRepPrimAPI_MakeCylinder(axis, 5.0, 20.0).Shape()
    return _cut(make_box(), drill)


def make_boss() -> TopoDS_Shape:
    """A 16mm cylindrical boss standing on the block.

    The base junction is concave -- material rises out of the host face -- and
    the boss cylinder is not reversed, because it is an external surface.
    """
    axis = gp_Ax2(gp_Pnt(25, 25, 25), gp_Dir(0, 0, 1))
    boss = BRepPrimAPI_MakeCylinder(axis, 8.0, 10.0).Shape()
    return _fuse(make_box(), boss)


def make_slot() -> TopoDS_Shape:
    """A 10mm wide channel run right across the top face."""
    cutter = BRepPrimAPI_MakeBox(gp_Pnt(-1, 20, 15), gp_Pnt(51, 30, 26)).Shape()
    return _cut(make_box(), cutter)


def make_cross_holes() -> TopoDS_Shape:
    """A vertical bore intersected by a horizontal one."""
    vertical = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(25, 25, -1), gp_Dir(0, 0, 1)), 5.0, 30.0)
    horizontal = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-1, 25, 12), gp_Dir(1, 0, 0)), 4.0, 60.0)
    return _cut(_cut(make_box(), vertical.Shape()), horizontal.Shape())


def make_spherical_pocket() -> TopoDS_Shape:
    """A hemispherical bowl cut into the top face, opening upward."""
    ball = BRepPrimAPI_MakeSphere(gp_Pnt(25, 25, 25), 10.0).Shape()
    return _cut(make_box(), ball)


def make_countersink() -> TopoDS_Shape:
    """A through bore with a 90-degree conical entry."""
    bore = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(25, 25, -1), gp_Dir(0, 0, 1)), 4.0, 30.0)
    cone = BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(25, 25, 21.0), gp_Dir(0, 0, 1)), 4.0, 8.0, 4.0)
    return _cut(_cut(make_box(), bore.Shape()), cone.Shape())


ALL_SHAPES = {
    "box": make_box,
    "pocket": make_pocket,
    "through_hole": make_through_hole,
    "blind_hole": make_blind_hole,
    "boss": make_boss,
    "slot": make_slot,
    "cross_holes": make_cross_holes,
    "spherical_pocket": make_spherical_pocket,
    "countersink": make_countersink,
}


def _build(shape: TopoDS_Shape):
    return AagBuilder(shape).build()


def _concavities(graph) -> dict[str, int]:
    tally: dict[str, int] = {}
    for edge in graph.edges:
        tally[edge.concavity.name] = tally.get(edge.concavity.name, 0) + 1
    return tally


# =============================================================================


class TestGraphStructure(unittest.TestCase):
    def test_box_topology(self):
        graph = _build(make_box())
        self.assertEqual(graph.face_count, 6)
        self.assertEqual(len(graph.edges), 12)

    def test_box_faces_are_all_planar(self):
        graph = _build(make_box())
        for node in graph.nodes:
            self.assertIs(node.surface_type, SurfaceType.PLANE)
            self.assertIsNotNone(node.plane_normal)

    def test_face_ids_are_one_based_and_contiguous(self):
        graph = _build(make_pocket())
        self.assertEqual([n.face_id for n in graph.nodes], list(range(1, graph.face_count + 1)))

    def test_face_key_is_usable_as_geometry_reference(self):
        graph = _build(make_box())
        node = graph.nodes[0]
        self.assertEqual(node.key, ("Face", 1))

    def test_edges_are_canonically_ordered(self):
        graph = _build(make_pocket())
        for edge in graph.edges:
            self.assertLess(edge.face_id_a, edge.face_id_b)

    def test_neighbour_counts_match_incident_edges(self):
        graph = _build(make_pocket())
        for node in graph.nodes:
            incident = graph.edges_of(node.face_id)
            expected_concave = sum(1 for e in incident if e.concavity is Concavity.CONCAVE)
            expected_convex = sum(1 for e in incident if e.concavity is Concavity.CONVEX)
            self.assertEqual(node.concave_neighbor_count, expected_concave)
            self.assertEqual(node.convex_neighbor_count, expected_convex)

    def test_other_face_resolves_both_directions(self):
        graph = _build(make_box())
        edge = graph.edges[0]
        self.assertEqual(edge.other_face(edge.face_id_a), edge.face_id_b)
        self.assertEqual(edge.other_face(edge.face_id_b), edge.face_id_a)
        with self.assertRaises(KeyError):
            edge.other_face(9999)

    def test_area_and_total_area(self):
        graph = _build(make_box())
        # 2*(50*50) + 4*(50*25)
        self.assertAlmostEqual(graph.total_area(), 10000.0, places=3)


# =============================================================================


class TestConcavityGroundTruth(unittest.TestCase):
    """Each case states what a machinist would say about the edge in question."""

    def test_box_edges_are_all_convex(self):
        self.assertEqual(_concavities(_build(make_box())), {"CONVEX": 12})

    def test_pocket_has_eight_concave_edges(self):
        # four wall-to-floor junctions plus four wall-to-wall corners
        tally = _concavities(_build(make_pocket()))
        self.assertEqual(tally.get("CONCAVE"), 8)

    def test_pocket_opening_rim_is_convex(self):
        graph = _build(make_pocket())
        # The host top face keeps its four outer box edges and gains the rim;
        # every edge on it must read convex.
        top = max(
            (n for n in graph.nodes if n.surface_type is SurfaceType.PLANE),
            key=lambda n: n.centroid.Z(),
        )
        for edge in graph.edges_of(top.face_id):
            self.assertIs(edge.concavity, Concavity.CONVEX)

    def test_through_hole_rims_are_convex(self):
        tally = _concavities(_build(make_through_hole()))
        self.assertEqual(tally.get("CONCAVE"), None)
        self.assertEqual(tally.get("CONVEX"), 14)

    def test_blind_hole_floor_junction_is_concave(self):
        graph = _build(make_blind_hole())
        cylinders = graph.nodes_by_surface_type(SurfaceType.CYLINDER)
        self.assertEqual(len(cylinders), 1)
        bore = cylinders[0]
        self.assertEqual(bore.concave_neighbor_count, 1)  # the floor
        self.assertEqual(bore.convex_neighbor_count, 1)  # the entry rim

    def test_boss_base_junction_is_concave(self):
        graph = _build(make_boss())
        cylinders = graph.nodes_by_surface_type(SurfaceType.CYLINDER)
        self.assertEqual(len(cylinders), 1)
        boss = cylinders[0]
        self.assertEqual(boss.concave_neighbor_count, 1)  # where it meets the block
        self.assertEqual(boss.convex_neighbor_count, 1)  # its top rim

    def test_slot_has_concave_floor_junctions(self):
        graph = _build(make_slot())
        self.assertGreaterEqual(_concavities(graph).get("CONCAVE", 0), 2)


# =============================================================================


class TestOrientation(unittest.TestCase):
    def test_bore_is_reversed_and_boss_is_not(self):
        bore = _build(make_through_hole()).nodes_by_surface_type(SurfaceType.CYLINDER)[0]
        boss = _build(make_boss()).nodes_by_surface_type(SurfaceType.CYLINDER)[0]
        self.assertTrue(bore.is_reversed, "an internal bore must be reversed")
        self.assertFalse(boss.is_reversed, "an external boss must not be reversed")

    def test_outward_normal_points_out_of_material(self):
        graph = _build(make_box())
        for node in graph.nodes:
            normal = node.outward_normal
            self.assertIsNotNone(normal)
            # The box spans (0,0,0)-(50,50,25), so its centre is inside. A true
            # outward normal must point away from that centre.
            to_centre = (
                25.0 - node.centroid.X(),
                25.0 - node.centroid.Y(),
                12.5 - node.centroid.Z(),
            )
            dot = normal.X() * to_centre[0] + normal.Y() * to_centre[1] + normal.Z() * to_centre[2]
            self.assertLess(dot, 0.0)

    def test_outward_normal_is_none_for_curved_faces(self):
        bore = _build(make_through_hole()).nodes_by_surface_type(SurfaceType.CYLINDER)[0]
        self.assertIsNone(bore.outward_normal)


# =============================================================================


class TestSurfaceClassification(unittest.TestCase):
    def test_cylinder_parameters(self):
        bore = _build(make_through_hole()).nodes_by_surface_type(SurfaceType.CYLINDER)[0]
        self.assertAlmostEqual(bore.cyl_radius, 6.0, places=6)
        self.assertIsNotNone(bore.cyl_cone_axis)
        axis = bore.cyl_cone_axis.Direction()
        self.assertAlmostEqual(abs(axis.Z()), 1.0, places=6)
        # The bore runs the full 25mm height of the block.
        self.assertAlmostEqual(bore.cyl_p0.Distance(bore.cyl_p1), 25.0, places=3)

    def test_sphere_parameters_and_single_opening(self):
        graph = _build(make_spherical_pocket())
        spheres = graph.nodes_by_surface_type(SurfaceType.SPHERE)
        self.assertEqual(len(spheres), 1)
        bowl = spheres[0]
        self.assertAlmostEqual(bowl.sphere_radius, 10.0, places=6)
        self.assertTrue(bowl.is_reversed)

    def test_cone_parameters(self):
        graph = _build(make_countersink())
        cones = graph.nodes_by_surface_type(SurfaceType.CONE)
        self.assertEqual(len(cones), 1)
        cone = cones[0]
        # A 4mm rise over a 4mm radius change is a 45 degree half angle.
        self.assertAlmostEqual(abs(math.degrees(cone.cone_semi_angle)), 45.0, places=3)

    def test_torus_parameters(self):
        torus = BRepPrimAPI_MakeTorus(20.0, 5.0).Shape()
        nodes = _build(torus).nodes_by_surface_type(SurfaceType.TORUS)
        self.assertEqual(len(nodes), 1)
        self.assertAlmostEqual(nodes[0].torus_major_r, 20.0, places=6)
        self.assertAlmostEqual(nodes[0].torus_minor_r, 5.0, places=6)

    def test_inner_wire_detected_on_pocket_host_face(self):
        graph = _build(make_through_hole())
        self.assertTrue(
            any(e.is_inner_wire_edge for e in graph.edges),
            "the bore rim sits on an inner wire of the face it pierces",
        )

    def test_host_face_reports_an_inner_loop(self):
        graph = _build(make_through_hole())
        self.assertTrue(any(n.inner_loop_count > 0 for n in graph.nodes))

    def test_edge_curve_types(self):
        graph = _build(make_through_hole())
        kinds = {e.edge_curve_type for e in graph.edges}
        self.assertIn("line", kinds)
        self.assertIn("circle", kinds)

    def test_shared_edge_length_of_a_box_edge(self):
        graph = _build(make_box())
        lengths = sorted({round(e.shared_edge_length, 3) for e in graph.edges})
        self.assertEqual(lengths, [25.0, 50.0])


# =============================================================================


class TestConcavityCensus(unittest.TestCase):
    """Cross-check every edge against an independent probe of the solid.

    The graph decides concavity analytically from normals and the edge tangent;
    the oracle walks into the material and asks the kernel. They agree only if
    the analytic sign convention is right.
    """

    def test_every_shape_agrees_with_the_physical_oracle(self):
        for name, builder in ALL_SHAPES.items():
            with self.subTest(shape=name):
                shape = builder()
                face_index = FaceIndex(shape)
                graph = AagBuilder(shape, face_index).build()
                result = concavity_census(shape, graph, face_index)

                self.assertEqual(
                    result.disagreed,
                    0,
                    f"{name}: analytic concavity disagrees with the physical "
                    f"probe on {result.disagreements}",
                )
                self.assertGreater(result.compared, 0, f"{name}: nothing was compared")


if __name__ == "__main__":
    unittest.main()
