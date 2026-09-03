# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Builds an :class:`AttributedAdjacencyGraph` from a B-rep solid.

This is the only kernel-heavy module in the machining stack. Every recognizer
downstream reads the graph rather than the shape, so the OpenCascade cost is
paid once per part.

The dihedral computation is the delicate part; see :func:`_dihedral_angle`.
"""

from __future__ import annotations

import math
from typing import Optional

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Pnt2d, gp_Vec, gp_Vec2d
from OCP.ShapeAnalysis import ShapeAnalysis_Surface
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Face, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

from ..utils.geometry import FaceIndex
from .aag import AagEdge, AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType


# An edge is called tangent when its interior angle is within this of flat.
TANGENT_ANGLE_TOLERANCE_RAD = 0.035  # ~2 degrees

# Edges shorter than this in parameter space are treated as degenerate.
MIN_EDGE_PARAM_LENGTH = 1e-6

# Curvature samples per freeform face. The cap bounds the cost on dense
# B-spline faces; the reference uses the same number.
MAX_CURVATURE_SAMPLES = 64

# Below this magnitude a principal curvature counts as flat.
FLAT_CURVATURE_EPS = 1e-6  # 1/mm

_SURFACE_TYPE_MAP = {
    GeomAbs_SurfaceType.GeomAbs_Plane: SurfaceType.PLANE,
    GeomAbs_SurfaceType.GeomAbs_Cylinder: SurfaceType.CYLINDER,
    GeomAbs_SurfaceType.GeomAbs_Cone: SurfaceType.CONE,
    GeomAbs_SurfaceType.GeomAbs_Sphere: SurfaceType.SPHERE,
    GeomAbs_SurfaceType.GeomAbs_Torus: SurfaceType.TORUS,
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: SurfaceType.BSPLINE,
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: SurfaceType.BSPLINE,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: SurfaceType.REVOLVED,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: SurfaceType.EXTRUDED,
}

_CURVE_TYPE_MAP = {
    GeomAbs_CurveType.GeomAbs_Line: "line",
    GeomAbs_CurveType.GeomAbs_Circle: "circle",
    GeomAbs_CurveType.GeomAbs_Ellipse: "ellipse",
    GeomAbs_CurveType.GeomAbs_BezierCurve: "bezier",
    GeomAbs_CurveType.GeomAbs_BSplineCurve: "bspline",
}


class AagBuilder:
    """Constructs the adjacency graph for one shape.

    Usage::

        graph = AagBuilder(shape).build()
    """

    def __init__(self, shape: TopoDS_Shape, face_index: Optional[FaceIndex] = None):
        self.shape = shape
        self.face_index = face_index if face_index is not None else FaceIndex(shape)
        self._graph = AttributedAdjacencyGraph()

    def build(self) -> AttributedAdjacencyGraph:
        self._enumerate_faces()
        self._build_adjacencies()
        self._graph.finalize()
        return self._graph

    # -- nodes ----------------------------------------------------------------

    def _enumerate_faces(self) -> None:
        for face_id in range(1, len(self.face_index) + 1):
            face = self.face_index.face_at(face_id)
            self._graph.add_node(self._build_node(face_id, face))

    def _build_node(self, face_id: int, face: TopoDS_Face) -> AagNode:
        node = AagNode(face_id=face_id)
        node.is_reversed = face.Orientation() == TopAbs_REVERSED

        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        node.area = props.Mass()
        node.centroid = props.CentreOfMass()

        bbox = Bnd_Box()
        BRepBndLib.Add_s(face, bbox)
        node.bbox = bbox

        loops = 0
        explorer = TopExp_Explorer(face, TopAbs_WIRE)
        while explorer.More():
            loops += 1
            explorer.Next()
        node.loop_count = loops
        node.inner_loop_count = max(0, loops - 1)

        self._classify_surface(node, face)

        if node.surface_type.is_freeform:
            self._sample_freeform_curvature(node, face)

        return node

    def _classify_surface(self, node: AagNode, face: TopoDS_Face) -> None:
        adaptor = BRepAdaptor_Surface(face, True)
        surface_type = _SURFACE_TYPE_MAP.get(adaptor.GetType(), SurfaceType.OTHER)
        node.surface_type = surface_type

        if surface_type is SurfaceType.PLANE:
            node.plane_normal = adaptor.Plane().Axis().Direction()

        elif surface_type is SurfaceType.CYLINDER:
            cylinder = adaptor.Cylinder()
            node.cyl_cone_axis = cylinder.Axis()
            node.cyl_radius = cylinder.Radius()
            # For gp_Cylinder the V parameter is the axial coordinate, so the
            # trimmed V range gives the face's true axial extent directly.
            origin = cylinder.Axis().Location()
            direction = cylinder.Axis().Direction()
            node.cyl_p0 = _along(origin, direction, adaptor.FirstVParameter())
            node.cyl_p1 = _along(origin, direction, adaptor.LastVParameter())

        elif surface_type is SurfaceType.CONE:
            cone = adaptor.Cone()
            node.cyl_cone_axis = cone.Axis()
            node.cone_semi_angle = cone.SemiAngle()
            # gp_Cone: r(v) = RefRadius + v*sin(alpha), axial(v) = v*cos(alpha)
            origin = cone.Axis().Location()
            direction = cone.Axis().Direction()
            ref_radius = cone.RefRadius()
            sin_a = math.sin(cone.SemiAngle())
            cos_a = math.cos(cone.SemiAngle())
            v0 = adaptor.FirstVParameter()
            v1 = adaptor.LastVParameter()
            node.cone_r0 = abs(ref_radius + v0 * sin_a)
            node.cone_r1 = abs(ref_radius + v1 * sin_a)
            node.cone_p0 = _along(origin, direction, v0 * cos_a)
            node.cone_p1 = _along(origin, direction, v1 * cos_a)

        elif surface_type is SurfaceType.SPHERE:
            sphere = adaptor.Sphere()
            node.sphere_center = sphere.Location()
            node.sphere_radius = sphere.Radius()
            _detect_sphere_cap(node, face)

        elif surface_type is SurfaceType.TORUS:
            torus = adaptor.Torus()
            node.torus_major_r = torus.MajorRadius()
            node.torus_minor_r = torus.MinorRadius()
            node.torus_axis = torus.Axis()

        elif surface_type is SurfaceType.REVOLVED:
            node.revolved_axis = adaptor.AxeOfRevolution()

        elif surface_type is SurfaceType.EXTRUDED:
            node.extruded_dir = adaptor.Direction()

    def _sample_freeform_curvature(self, node: AagNode, face: TopoDS_Face) -> None:
        """Sample principal curvatures at triangulation vertices.

        Triangulation UVs are guaranteed to lie inside the trimmed domain, so
        no point-in-face classification is needed -- cheaper and more robust
        than walking a UV grid.
        """
        uv_nodes = _triangulation_uvs(face)
        if not uv_nodes:
            return

        adaptor = BRepAdaptor_Surface(face, True)  # named local: SLProps holds a ref
        props = BRepLProp_SLProps(adaptor, 2, 1e-7)

        step = max(1, len(uv_nodes) // MAX_CURVATURE_SAMPLES)
        normals: list[gp_Dir] = []
        min_concave_radius = 0.0
        max_convex_curvature = 0.0

        for uv in uv_nodes[::step]:
            props.SetParameters(uv[0], uv[1])
            if not props.IsNormalDefined() or not props.IsCurvatureDefined():
                continue

            normal = gp_Dir(props.Normal().XYZ())
            k_max = props.MaxCurvature()
            k_min = props.MinCurvature()
            if node.is_reversed:
                normal.Reverse()
                k_max = -k_max
                k_min = -k_min

            normals.append(normal)
            for curvature in (k_max, k_min):
                if curvature > FLAT_CURVATURE_EPS:
                    radius = 1.0 / curvature
                    if min_concave_radius == 0.0 or radius < min_concave_radius:
                        min_concave_radius = radius
                elif curvature < -FLAT_CURVATURE_EPS:
                    max_convex_curvature = max(max_convex_curvature, -curvature)

        if not normals:
            return

        node.has_freeform_curvature = True
        node.freeform_min_concave_radius_mm = min_concave_radius
        node.freeform_max_convex_curvature = max_convex_curvature
        node.freeform_normal_spread_deg = _max_pairwise_angle_deg(normals)

        total = gp_Vec(0.0, 0.0, 0.0)
        for normal in normals:
            total.Add(gp_Vec(normal))
        if total.Magnitude() > 1e-9:
            node.freeform_mean_normal = gp_Dir(total)

    # -- edges ----------------------------------------------------------------

    def _build_adjacencies(self) -> None:
        ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(self.shape, TopAbs_EDGE, TopAbs_FACE, ancestors)

        for i in range(1, ancestors.Extent() + 1):
            edge = TopoDS.Edge_s(ancestors.FindKey(i))
            if BRep_Tool.Degenerated_s(edge):
                continue

            incident = ancestors.FindFromIndex(i)
            if incident.Extent() != 2:
                continue

            face_a = TopoDS.Face_s(incident.First())
            face_b = TopoDS.Face_s(incident.Last())
            raw_a = self.face_index.index_of(face_a)
            raw_b = self.face_index.index_of(face_b)
            if raw_a == 0 or raw_b == 0 or raw_a == raw_b:
                continue  # unmapped, or a seam edge with the same face both sides

            attr = self._build_edge(edge, raw_a, raw_b)
            if attr is not None:
                self._graph.add_edge(attr)

    def _build_edge(self, edge: TopoDS_Edge, raw_a: int, raw_b: int) -> Optional[AagEdge]:
        first, last = BRep_Tool.Range_s(edge)
        if abs(last - first) < MIN_EDGE_PARAM_LENGTH:
            return None

        curve = BRep_Tool.Curve_s(edge, 0.0, 0.0)
        if curve is None:
            return None

        # Canonical form: the lower face index is always A. Take the faces from
        # the index map rather than the ancestor list, so their orientation
        # flags are the ones the traversal established.
        face_id_a, face_id_b = min(raw_a, raw_b), max(raw_a, raw_b)
        face_a = self.face_index.face_at(face_id_a)
        face_b = self.face_index.face_at(face_id_b)

        attr = AagEdge(face_id_a=face_id_a, face_id_b=face_id_b)
        attr.edge_curve_type = _CURVE_TYPE_MAP.get(BRepAdaptor_Curve(edge).GetType(), "other")
        attr.shared_edge_length = _polyline_length(curve, first, last)
        attr.midpoint = curve.Value((first + last) * 0.5)
        attr.is_inner_wire_edge = _is_inner_wire(face_a, edge) or _is_inner_wire(face_b, edge)

        angle = _dihedral_angle(edge, face_a, face_b, curve, first, last)
        if angle is None:
            attr.concavity = Concavity.UNKNOWN
            return attr

        attr.dihedral_angle = angle
        if abs(angle - math.pi) < TANGENT_ANGLE_TOLERANCE_RAD:
            attr.concavity = Concavity.TANGENT
            attr.is_tangent = True
        elif angle > math.pi:
            attr.concavity = Concavity.CONCAVE
        else:
            attr.concavity = Concavity.CONVEX
        return attr


# -- dihedral -----------------------------------------------------------------


def _dihedral_angle(
    edge: TopoDS_Edge,
    face_a: TopoDS_Face,
    face_b: TopoDS_Face,
    curve,
    first: float,
    last: float,
) -> Optional[float]:
    """Interior dihedral angle in radians, in [0, 2*pi). None if undetermined.

        interior = pi - atan2(e . (na x nb), na . nb)

    `e` is the shared edge's tangent taken from face A's *pcurve* -- not from
    the 3D curve, whose direction is arbitrary and would make the sign depend
    on which face happened to win the index tiebreak. The pcurve direction is
    defined relative to face A's own parameterization, so it is consistent.

    The tangent is flipped once, when the edge is REVERSED within its wire.
    Do not also flip on the face's orientation: TopExp_Explorer already
    composes face orientation into the sub-shapes it returns, and flipping
    twice inverts every result.
    """
    mid = (first + last) * 0.5
    mid_point = curve.Value(mid)

    tangent = _edge_tangent_from_pcurve(edge, face_a)
    if tangent is None:
        vec = gp_Vec()
        point = gp_Pnt()
        curve.D1(mid, point, vec)
        if vec.Magnitude() < 1e-12:
            return None
        tangent = gp_Dir(vec)

    normal_a = _outward_normal_at(face_a, edge, mid_point)
    normal_b = _outward_normal_at(face_b, edge, mid_point)
    if normal_a is None or normal_b is None:
        return None

    dot = max(-1.0, min(1.0, normal_a.Dot(normal_b)))
    cross = gp_Vec(normal_a).Crossed(gp_Vec(normal_b))
    signed = gp_Vec(tangent).Dot(cross)
    return math.pi - math.atan2(signed, dot)


def _edge_tangent_from_pcurve(edge: TopoDS_Edge, face: TopoDS_Face) -> Optional[gp_Dir]:
    """Edge tangent at its midpoint, in the sense of `face`'s wire traversal."""
    pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
    if pcurve is None:
        return None

    # The pcurve handed back is untrimmed (a Geom2d_Line reports +/-2e100), so
    # the usable range must come from the edge, not from the curve itself.
    try:
        first, last = BRep_Tool.Range_s(edge, face)
    except Exception:
        first, last = BRep_Tool.Range_s(edge)

    uv = gp_Pnt2d()
    duv = gp_Vec2d()
    pcurve.D1((first + last) * 0.5, uv, duv)

    surface = BRep_Tool.Surface_s(face)
    point = gp_Pnt()
    d_du = gp_Vec()
    d_dv = gp_Vec()
    surface.D1(uv.X(), uv.Y(), point, d_du, d_dv)

    tangent = d_du.Multiplied(duv.X()).Added(d_dv.Multiplied(duv.Y()))
    if tangent.Magnitude() < 1e-12:
        return None

    if _edge_orientation_in_face(face, edge) == TopAbs_REVERSED:
        tangent.Reverse()
    return gp_Dir(tangent)


def _edge_orientation_in_face(face: TopoDS_Face, edge: TopoDS_Edge):
    """The edge's orientation as traversed within this face's wires."""
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        candidate = explorer.Current()
        if candidate.IsSame(edge):
            return candidate.Orientation()
        explorer.Next()
    return None


def _outward_normal_at(
    face: TopoDS_Face, edge: TopoDS_Edge, point: gp_Pnt
) -> Optional[gp_Dir]:
    """Face normal at a 3D point on one of its edges, pointing out of material.

    The UV is taken from the face's own pcurve for the edge when one exists --
    exact, and far cheaper than a global inverse projection. Falls back to
    ShapeAnalysis_Surface for faces that carry no pcurve for this edge.
    """
    uv = _uv_from_pcurve(edge, face)
    if uv is None:
        surface = BRep_Tool.Surface_s(face)
        if surface is None:
            return None
        projected = ShapeAnalysis_Surface(surface).ValueOfUV(point, 1e-4)
        uv = (projected.X(), projected.Y())

    adaptor = BRepAdaptor_Surface(face, True)  # named local: SLProps holds a ref
    props = BRepLProp_SLProps(adaptor, uv[0], uv[1], 1, 1e-6)
    if not props.IsNormalDefined():
        return None

    normal = gp_Dir(props.Normal().XYZ())
    if face.Orientation() == TopAbs_REVERSED:
        normal.Reverse()
    return normal


def _uv_from_pcurve(edge: TopoDS_Edge, face: TopoDS_Face) -> Optional[tuple[float, float]]:
    pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
    if pcurve is None:
        return None
    try:
        first, last = BRep_Tool.Range_s(edge, face)
    except Exception:
        first, last = BRep_Tool.Range_s(edge)
    uv = pcurve.Value((first + last) * 0.5)
    return (uv.X(), uv.Y())


# -- helpers ------------------------------------------------------------------


def _along(origin: gp_Pnt, direction: gp_Dir, distance: float) -> gp_Pnt:
    return gp_Pnt(
        origin.X() + direction.X() * distance,
        origin.Y() + direction.Y() * distance,
        origin.Z() + direction.Z() * distance,
    )


def _polyline_length(curve, first: float, last: float, segments: int = 5) -> float:
    """Chord-sum approximation of arc length, matching the reference's 5 steps."""
    total = 0.0
    previous = curve.Value(first)
    for i in range(1, segments + 1):
        current = curve.Value(first + (last - first) * i / segments)
        total += previous.Distance(current)
        previous = current
    return total


def _is_inner_wire(face: TopoDS_Face, edge: TopoDS_Edge) -> bool:
    """True when the edge lies on a non-outer wire of this face."""
    try:
        outer = BRepTools.OuterWire_s(face)
    except Exception:
        return False

    wire_explorer = TopExp_Explorer(face, TopAbs_WIRE)
    while wire_explorer.More():
        wire = TopoDS.Wire_s(wire_explorer.Current())
        wire_explorer.Next()
        if not outer.IsNull() and wire.IsSame(outer):
            continue
        edge_explorer = TopExp_Explorer(wire, TopAbs_EDGE)
        while edge_explorer.More():
            if edge_explorer.Current().IsSame(edge):
                return True
            edge_explorer.Next()
    return False


def _triangulation_uvs(face: TopoDS_Face) -> list[tuple[float, float]]:
    """UV coordinates of the face's triangulation vertices, meshing if needed."""
    from OCP.TopLoc import TopLoc_Location

    location = TopLoc_Location()
    triangulation = BRep_Tool.Triangulation_s(face, location)
    if triangulation is None:
        BRepMesh_IncrementalMesh(face, 0.5, False, 0.5, False)
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
    if triangulation is None or not triangulation.HasUVNodes():
        return []

    uvs = []
    for i in range(1, triangulation.NbNodes() + 1):
        uv = triangulation.UVNode(i)
        uvs.append((uv.X(), uv.Y()))
    return uvs


def _max_pairwise_angle_deg(normals: list[gp_Dir]) -> float:
    """Largest angle between any two sampled normals, in degrees."""
    min_dot = 1.0
    for i in range(len(normals)):
        for j in range(i + 1, len(normals)):
            min_dot = min(min_dot, normals[i].Dot(normals[j]))
    return math.degrees(math.acos(max(-1.0, min(1.0, min_dot))))


def _detect_sphere_cap(node: AagNode, face: TopoDS_Face) -> None:
    """Find the single small circular boundary that opens a spherical pocket.

    Set only when the face has exactly one small analytic circular cap, which
    is what distinguishes a bowl with one opening from a corner blend patch
    bounded by several circles.
    """
    center = node.sphere_center
    radius = node.sphere_radius
    if center is None or radius <= 0.0:
        return

    caps: list[tuple[gp_Pnt, gp_Dir, float]] = []
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        if BRep_Tool.IsClosed_s(edge, face):
            continue  # parametric seam, not a real boundary
        adaptor = BRepAdaptor_Curve(edge)
        if adaptor.GetType() != GeomAbs_CurveType.GeomAbs_Circle:
            continue
        circle = adaptor.Circle()
        offset = circle.Location().Distance(center)
        # Pythagoras: a circle on the sphere satisfies r^2 + offset^2 = R^2.
        if abs(circle.Radius() ** 2 + offset**2 - radius**2) > 0.1:
            continue
        if offset <= 1e-3:
            continue  # great circle: not a small cap
        caps.append((circle.Location(), circle.Axis().Direction(), offset))

    if len(caps) != 1:
        return

    cap_center, axis_dir, offset = caps[0]
    normal = gp_Dir(axis_dir.XYZ())
    to_cap = gp_Vec(center, cap_center)
    if gp_Vec(normal).Dot(to_cap) < 0.0:
        normal.Reverse()

    node.sphere_has_clip = True
    node.sphere_clip_normal = normal
    node.sphere_clip_offset = offset
