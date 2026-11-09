# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Base class and shared geometry helpers for feature recognizers.

Recognizers work almost entirely on the adjacency graph rather than on the
shape, which is what keeps them fast: the kernel cost was paid once when the
graph was built. They are stateless between parts -- everything they know
arrives as arguments.

Order matters. Recognizers run in a fixed sequence and later ones are told
what earlier ones already claimed, so a groove does not re-recognize the bore
it sits in.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance


class FeatureRecognizer:
    """Finds one kind of feature in the adjacency graph."""

    #: Short prefix for generated instance ids, e.g. "h" for holes.
    prefix = "f"

    #: The shop's configuration, set by the analyzer before each run. Only a
    #: few recognizers care what tools the shop owns; the rest ignore it.
    config = None

    #: How the analyzer classified the part. A recognizer that reads it is
    #: asking "is this turned", which changes what a surface of revolution
    #: means -- a profile on a lathe, sculpture anywhere else.
    part_process = None

    @property
    def name(self) -> str:
        raise NotImplementedError

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        """Return the features found. Must not mutate the graph."""
        raise NotImplementedError

    def instance_id(self, index: int) -> str:
        return f"{self.prefix}_{index}"

    def threshold(self, name: str, default: float) -> float:
        """A shop-configured limit, or the built-in default.

        Some of what a recognizer treats as a constant is really shop policy
        -- what counts as a rib, which seal standard a gland is cut to, how
        deep the engraver goes. Those are on the settings page, and reading
        them here is what makes editing them do anything.

        The default is passed in rather than looked up so the recognizer
        still works when no configuration has been supplied, which is how
        every unit test calls it.
        """
        config = self.config
        if config is None:
            return default
        return float(getattr(config.thresholds, name, default))


# =============================================================================
# Geometry helpers
# =============================================================================


def axis_direction(node: AagNode) -> Optional[gp_Dir]:
    """The revolution axis direction a face carries, if any."""
    if node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
        return node.cyl_cone_axis.Direction() if node.cyl_cone_axis else None
    if node.surface_type is SurfaceType.REVOLVED:
        return node.revolved_axis.Direction() if node.revolved_axis else None
    return None


def axes_are_coaxial(
    a: gp_Ax1, b: gp_Ax1, direction_dot: float = 0.98, line_distance: float = 0.5
) -> bool:
    """Whether two axes describe the same line, within tolerance."""
    if abs(a.Direction().Dot(b.Direction())) < direction_dot:
        return False
    offset = gp_Vec(a.Location(), b.Location())
    return offset.Crossed(gp_Vec(a.Direction())).Magnitude() < line_distance


def axial_coordinate(point: gp_Pnt, origin: gp_Pnt, direction: gp_Dir) -> float:
    """Distance along an axis from its origin to the point's projection."""
    return gp_Vec(origin, point).Dot(gp_Vec(direction))


def radial_distance(point: gp_Pnt, origin: gp_Pnt, direction: gp_Dir) -> float:
    """Perpendicular distance from an axis to a point."""
    return gp_Vec(origin, point).CrossMagnitude(gp_Vec(direction))


def outward_normal(node: AagNode) -> Optional[gp_Dir]:
    return node.outward_normal


def cylinder_wrap(node: AagNode) -> float:
    """How much of a full revolution a cylindrical face covers, 0 to 1.

    A drilled bore wraps fully. A fillet band running along an edge covers a
    quarter turn or less, which is how the two are told apart.
    """
    if node.surface_type is not SurfaceType.CYLINDER or node.cyl_radius <= 1e-9:
        return 1.0
    if node.cyl_p0 is None or node.cyl_p1 is None:
        return 1.0
    length = node.cyl_p0.Distance(node.cyl_p1)
    full_area = 2.0 * math.pi * node.cyl_radius * length
    return node.area / full_area if full_area > 1e-9 else 1.0


def cylinder_wrap_radians(graph: AttributedAdjacencyGraph, face_map, node: AagNode) -> float:
    """Radians of its axis a cylinder and its seam partners cover together.

    This one question separates features otherwise identical in every
    measurable respect: a counterbore's outer wall (a bore, wraps) from the
    rounded end of an obround slot coaxial with a hole drilled at its centre;
    a cylindrical fillet, which spans only the dihedral of the straight edge
    it rounds, from a bore or a boss, which closes on itself.

    Summed over the whole seam group -- every coaxial face of the same
    radius -- because the kernel habitually splits a full bore into two
    half-turn faces, and one half alone is indistinguishable from a slot end.
    The centroid's distance from the axis would say the same more cheaply,
    but it also moves when a bore is trimmed obliquely, which is not a change
    in wrap. ``face_map`` is the shape's indexed face map (face ids are its
    indices); without one there is nothing to measure and this returns 0.
    """
    if node.surface_type is not SurfaceType.CYLINDER or node.cyl_radius <= 1.0e-9:
        return 0.0
    if node.cyl_cone_axis is None or face_map is None:
        return 0.0
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    from freecad.DFM.core.utils.occt import TopoDS

    origin = node.cyl_cone_axis.Location()
    direction = node.cyl_cone_axis.Direction()
    # U spans are a property of the face alone; cache them on the graph so a
    # part with many cylinders is not re-adapted face by face per question.
    spans = getattr(graph, "_cylinder_u_spans", None)
    if spans is None:
        spans = {}
        graph._cylinder_u_spans = spans
    total = 0.0
    for other in graph.nodes_by_surface_type(SurfaceType.CYLINDER):
        if other.cyl_cone_axis is None:
            continue
        if abs(other.cyl_radius - node.cyl_radius) > 0.05:
            continue
        if abs(direction.Dot(other.cyl_cone_axis.Direction())) < 0.98:
            continue
        offset = gp_Vec(origin, other.cyl_cone_axis.Location())
        if gp_Vec(direction).Crossed(offset).Magnitude() > 0.1:
            continue
        span = spans.get(other.face_id)
        if span is None:
            span = 0.0
            if 1 <= other.face_id <= face_map.Extent():
                surface = BRepAdaptor_Surface(
                    TopoDS.Face_s(face_map.FindKey(other.face_id)), True
                )
                if surface.GetType() == GeomAbs_Cylinder:
                    span = abs(surface.LastUParameter() - surface.FirstUParameter())
            spans[other.face_id] = span
        total += span
    return total


def cylinder_wraps_axis(graph: AttributedAdjacencyGraph, face_map, node: AagNode) -> bool:
    """Whether a cylinder closes on itself. 300 degrees leaves room for a
    bore another feature has taken a bite out of, and is nowhere near a slot
    end's 180."""
    return cylinder_wrap_radians(graph, face_map, node) >= math.radians(300.0)


def cone_rim_mean_radii(graph: AttributedAdjacencyGraph, face_map, node: AagNode):
    """A cone's radii as the reference's graph states them, (r0, r1).

    Our graph takes a cone's radii at the face's V bounds, the true extremes
    of the face. The reference's takes them at the MEAN axial position of the
    rim samples on each side of the axial midpoint: every non-line bounding
    edge sampled 32 times, samples outside the planar faces' box dropped,
    split at the midpoint, each half averaged. On a seam-split cone the
    seams are B-splines running the full slant, so their samples pull both
    means toward the middle (a 7 -> 10 bevel reads 7.4 -> 9.6). Gates the
    reference tunes on those numbers are ported on these. Falls back to the
    graph's pair when the face cannot be sampled.
    """
    fallback = (node.cone_r0, node.cone_r1)
    if node.surface_type is not SurfaceType.CONE or face_map is None:
        return fallback
    cache = getattr(graph, "_cone_rim_means", None)
    if cache is None:
        cache = {}
        graph._cone_rim_means = cache
    if node.face_id in cache:
        return cache[node.face_id]
    if not 1 <= node.face_id <= face_map.Extent():
        return fallback
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.Bnd import Bnd_Box
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Line
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_WIRE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from freecad.DFM.core.utils.occt import TopoDS
    from freecad.DFM.core.utils.occt import TopTools_IndexedMapOfShape

    face = TopoDS.Face_s(face_map.FindKey(node.face_id))
    surface = BRepAdaptor_Surface(face)
    if surface.GetType() != GeomAbs_Cone:
        return fallback
    cone = surface.Cone()
    loc = cone.Location()
    ad = cone.Axis().Direction()
    ref_r = cone.RefRadius()
    slope = math.tan(cone.SemiAngle())

    planes = getattr(graph, "_plane_box", None)
    if planes is None:
        planes = Bnd_Box()
        for plane in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if not plane.bbox.IsVoid():
                planes.Add(plane.bbox)
        graph._plane_box = planes
    if planes.IsVoid():
        lo = (-1e30,) * 3
        hi = (1e30,) * 3
    else:
        x0, y0, z0, x1, y1, z1 = planes.Get()
        lo, hi = (x0, y0, z0), (x1, y1, z1)
    margin = 0.01

    def axial(p) -> float:
        return (
            (p.X() - loc.X()) * ad.X()
            + (p.Y() - loc.Y()) * ad.Y()
            + (p.Z() - loc.Z()) * ad.Z()
        )

    samples: list[float] = []
    wires = TopExp_Explorer(face, TopAbs_WIRE)
    while wires.More():
        edge_map = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(wires.Current(), TopAbs_EDGE, edge_map)
        for i in range(1, edge_map.Extent() + 1):
            curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge_map.FindKey(i)))
            t0, t1 = curve.FirstParameter(), curve.LastParameter()
            if t1 - t0 < 1e-9 or curve.GetType() == GeomAbs_Line:
                continue
            for j in range(32):
                p = curve.Value(t0 + (t1 - t0) * (j / 31.0))
                if not all(
                    lo[k] - margin <= c <= hi[k] + margin
                    for k, c in enumerate((p.X(), p.Y(), p.Z()))
                ):
                    continue
                samples.append(axial(p))
        wires.Next()
    if not samples:
        cache[node.face_id] = fallback
        return fallback
    a_min, a_max = min(samples), max(samples)
    a_mid = (a_min + a_max) * 0.5
    low = [a for a in samples if a < a_mid]
    high = [a for a in samples if a >= a_mid]
    a0 = sum(low) / len(low) if low else a_min
    a1 = sum(high) / len(high) if high else a_max
    if abs(slope) > 1e-6:
        apex = -ref_r / slope
        if a_min - 0.1 <= apex <= a_max + 0.1:
            if not low:
                a0 = apex
            if not high:
                a1 = apex
    result = (max(0.0, ref_r + a0 * slope), max(0.0, ref_r + a1 * slope))
    cache[node.face_id] = result
    return result


def face_map_of(shape):
    """The shape's indexed face map, or None without a shape."""
    if shape is None:
        return None
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from freecad.DFM.core.utils.occt import TopTools_IndexedMapOfShape

    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)
    return face_map


def cylinder_length(node: AagNode) -> float:
    """Axial extent of a cylindrical face."""
    if node.cyl_p0 is None or node.cyl_p1 is None:
        return 0.0
    return node.cyl_p0.Distance(node.cyl_p1)


def cross_section_area(node: AagNode) -> float:
    return math.pi * node.cyl_radius * node.cyl_radius


def neighbours(
    graph: AttributedAdjacencyGraph, face_id: int
) -> list[tuple[AagNode, "object"]]:
    """Adjacent nodes paired with the edge that reaches them.

    Sorted by face id so a recognizer's first-match-wins decisions are
    reproducible; dictionary or set ordering must never decide which of two
    equally valid candidates is chosen.
    """
    found = []
    for edge in graph.edges_of(face_id):
        other = edge.other_face(face_id)
        if graph.has_node(other):
            found.append((graph.node(other), edge))
    return sorted(found, key=lambda pair: pair[0].face_id)


def shares_inner_wire_with(
    graph: AttributedAdjacencyGraph, face_id: int, other_face_id: int
) -> bool:
    """Whether the two faces meet along an edge on an inner wire.

    For a plain bore this means the bore passes *through* the other face: the
    cylinder itself has no inner wires, so the inner wire must belong to the
    face it pierces.
    """
    for edge in graph.edges_of(face_id):
        if edge.is_inner_wire_edge and edge.other_face(face_id) == other_face_id:
            return True
    return False


def normal_bucket_up_to_sign(direction: gp_Dir) -> tuple[int, int, int]:
    """A key under which a normal and its opposite collide, and nothing else.

    Pocket and slot recognisers pick a cavity's floor as the minority
    normal: walls come in anti-parallel pairs, the floor stands alone. The
    key this replaces was |x|,|y|,|z|, which also makes every mirror image
    collide -- (0.7, 0, 0.7) and (0.7, 0, -0.7) are perpendicular yet both
    quantise to 7,0,7. On a slot sunk at 45 degrees (the mirror seats of
    optical_periscope_housing) that put the slanted floor in the big walls'
    bucket and left a side wall as the "floor".

    Quantise first, then flip so the first non-zero component is positive.
    In the other order a near-zero leading component decides the sign on
    noise.
    """

    def lround(value: float) -> int:
        # C's lround: half away from zero, not Python's half to even.
        return int(math.copysign(math.floor(abs(value) + 0.5), value))

    q = [lround(direction.X() * 10.0), lround(direction.Y() * 10.0), lround(direction.Z() * 10.0)]
    for component in q:
        if component == 0:
            continue
        if component < 0:
            q = [-c for c in q]
        break
    return (q[0] + 0, q[1] + 0, q[2] + 0)
