# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Attributed Adjacency Graph over a B-rep solid.

Nodes are faces carrying surface type, analytic parameters and neighbourhood
statistics. Edges are the topological edges shared by exactly two faces,
carrying the interior dihedral angle and its concavity classification.

Every machining feature recognizer and most machining DFM rules read this graph
rather than raw geometry, so the kernel cost is paid once per part.

Faces are identified by their 1-based index in ``TopExp::MapShapes`` order,
matching :class:`freecad.DFM.core.utils.geometry.FaceIndex`. A check can
therefore emit ``("Face", node.face_id)`` directly as failing geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterator, Optional

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt


class SurfaceType(Enum):
    """Classification of a face's underlying surface."""

    PLANE = auto()
    CYLINDER = auto()
    CONE = auto()
    SPHERE = auto()
    TORUS = auto()
    BSPLINE = auto()
    REVOLVED = auto()
    EXTRUDED = auto()
    OTHER = auto()

    @property
    def is_freeform(self) -> bool:
        """True for the family that carries sampled curvature instead of
        analytic parameters."""
        return self in _FREEFORM_TYPES


_FREEFORM_TYPES = frozenset(
    {SurfaceType.BSPLINE, SurfaceType.REVOLVED, SurfaceType.EXTRUDED, SurfaceType.OTHER}
)


class Concavity(Enum):
    """Concavity of a shared edge, relative to the solid's interior.

    CONCAVE — interior dihedral angle > pi (a pocket wall meeting its floor)
    CONVEX  — interior dihedral angle < pi (an outside box edge)
    TANGENT — interior dihedral angle ~ pi (a fillet blend)
    UNKNOWN — could not be determined (degenerate or non-manifold)

    Ground truth worth remembering, because it reads backwards at first: the
    opening rim of a hole or pocket is CONVEX -- it is the edge you deburr --
    while the base junction of a boss or rib is CONCAVE.
    """

    CONCAVE = auto()
    CONVEX = auto()
    TANGENT = auto()
    UNKNOWN = auto()


@dataclass
class AagNode:
    """One B-rep face and everything the recognizers need to know about it."""

    face_id: int  # 1-based index in TopExp::MapShapes order
    surface_type: SurfaceType = SurfaceType.OTHER

    # Always populated
    area: float = 0.0
    centroid: gp_Pnt = field(default_factory=gp_Pnt)
    bbox: Bnd_Box = field(default_factory=Bnd_Box)
    loop_count: int = 0
    inner_loop_count: int = 0

    # OpenCascade's stored orientation flag. Combined with the surface's own
    # normal it yields the true outward direction, which is why
    # `outward_normal` is reliable. The flag *alone* is not portable: the same
    # solid built in FreeCAD and in OpenCascade carries opposite flags with
    # correspondingly opposite surface parameterisations. Use `is_internal`
    # to ask whether a face bounds a void.
    is_reversed: bool = False

    # True when the face looks into a void rather than out into space: the
    # wall of a bore rather than of a boss. Derived from geometry -- which
    # side of the surface the material is on -- so it holds whatever produced
    # the solid.
    is_internal: bool = False

    # PLANE. Held in the surface's own sense, NOT corrected for orientation --
    # use `outward_normal` when you want the direction pointing out of material.
    plane_normal: Optional[gp_Dir] = None

    # CYLINDER / CONE
    cyl_cone_axis: Optional[gp_Ax1] = None
    cyl_radius: float = 0.0
    cyl_p0: Optional[gp_Pnt] = None  # axial endpoints on the centreline
    cyl_p1: Optional[gp_Pnt] = None
    cone_semi_angle: float = 0.0  # radians, signed
    cone_r0: float = 0.0
    cone_r1: float = 0.0
    cone_p0: Optional[gp_Pnt] = None
    cone_p1: Optional[gp_Pnt] = None

    # SPHERE
    sphere_center: Optional[gp_Pnt] = None
    sphere_radius: float = 0.0
    # Set only when the face has exactly one small analytic circular cap: the
    # spherical-pocket recognizer reads this as "unique opening to the outside".
    sphere_has_clip: bool = False
    sphere_clip_normal: Optional[gp_Dir] = None
    sphere_clip_offset: float = 0.0

    # TORUS
    torus_major_r: float = 0.0
    torus_minor_r: float = 0.0
    torus_axis: Optional[gp_Ax1] = None

    # REVOLVED / EXTRUDED
    revolved_axis: Optional[gp_Ax1] = None
    extruded_dir: Optional[gp_Dir] = None

    # Freeform curvature, sampled at triangulation vertices. Signs are taken
    # with respect to the outward normal: k > 0 is concave (hollow -- the tool
    # must fit inside this radius), k < 0 is convex.
    has_freeform_curvature: bool = False
    freeform_min_concave_radius_mm: float = 0.0
    freeform_max_convex_curvature: float = 0.0
    freeform_normal_spread_deg: float = 0.0
    freeform_mean_normal: Optional[gp_Dir] = None

    # Filled by AttributedAdjacencyGraph.finalize()
    concave_neighbor_count: int = 0
    convex_neighbor_count: int = 0
    tangent_neighbor_count: int = 0

    @property
    def outward_normal(self) -> Optional[gp_Dir]:
        """`plane_normal` corrected for face orientation, so it points out of
        material. None for non-planar faces."""
        if self.plane_normal is None:
            return None
        normal = gp_Dir(self.plane_normal.XYZ())
        if self.is_reversed:
            normal.Reverse()
        return normal

    @property
    def key(self) -> tuple[str, int]:
        """Geometry reference usable directly as a check's failing geometry."""
        return ("Face", self.face_id)

    def bbox_dims(self) -> tuple[float, float, float]:
        """(dx, dy, dz) of the face bounding box; zeros when the box is void."""
        if self.bbox.IsVoid():
            return (0.0, 0.0, 0.0)
        xmin, ymin, zmin, xmax, ymax, zmax = self.bbox.Get()
        return (xmax - xmin, ymax - ymin, zmax - zmin)


@dataclass
class AagEdge:
    """One topological edge shared by exactly two faces.

    `face_id_a` is always the lower of the two face indices, so an edge has one
    canonical form regardless of traversal order.
    """

    face_id_a: int
    face_id_b: int

    shared_edge_length: float = 0.0
    edge_curve_type: str = "other"  # line | circle | ellipse | bspline | bezier | other
    midpoint: Optional[gp_Pnt] = None

    dihedral_angle: float = 0.0  # interior angle in radians, [0, 2*pi)
    concavity: Concavity = Concavity.UNKNOWN
    is_tangent: bool = False

    # True when the edge lies on an inner (non-outer) wire of either face --
    # for example the rim of a pocket opening cut into a host face. Used for
    # topological questions only; it carries no sign correction.
    is_inner_wire_edge: bool = False

    def other_face(self, face_id: int) -> int:
        """The face at the far end of this edge."""
        if face_id == self.face_id_a:
            return self.face_id_b
        if face_id == self.face_id_b:
            return self.face_id_a
        raise KeyError(f"Face {face_id} is not an endpoint of this edge")


class AttributedAdjacencyGraph:
    """Faces and their adjacency relationships for one solid."""

    def __init__(self) -> None:
        self._nodes: dict[int, AagNode] = {}
        self._edges: list[AagEdge] = []
        self._incident: dict[int, list[int]] = {}  # face_id -> indices into _edges

    # -- construction ---------------------------------------------------------

    def add_node(self, node: AagNode) -> None:
        self._nodes[node.face_id] = node
        self._incident.setdefault(node.face_id, [])

    def add_edge(self, edge: AagEdge) -> None:
        index = len(self._edges)
        self._edges.append(edge)
        self._incident.setdefault(edge.face_id_a, []).append(index)
        self._incident.setdefault(edge.face_id_b, []).append(index)

    def finalize(self) -> None:
        """Compute per-node neighbour statistics. Call once, after all edges."""
        for node in self._nodes.values():
            node.concave_neighbor_count = 0
            node.convex_neighbor_count = 0
            node.tangent_neighbor_count = 0

        for edge in self._edges:
            for face_id in (edge.face_id_a, edge.face_id_b):
                node = self._nodes.get(face_id)
                if node is None:
                    continue
                if edge.concavity is Concavity.CONCAVE:
                    node.concave_neighbor_count += 1
                elif edge.concavity is Concavity.CONVEX:
                    node.convex_neighbor_count += 1
                elif edge.concavity is Concavity.TANGENT:
                    node.tangent_neighbor_count += 1

    # -- queries --------------------------------------------------------------

    def has_node(self, face_id: int) -> bool:
        return face_id in self._nodes

    def node(self, face_id: int) -> AagNode:
        return self._nodes[face_id]

    @property
    def nodes(self) -> list[AagNode]:
        """Nodes in ascending face_id order, so iteration is deterministic."""
        return [self._nodes[k] for k in sorted(self._nodes)]

    @property
    def edges(self) -> list[AagEdge]:
        return list(self._edges)

    def edges_of(self, face_id: int) -> list[AagEdge]:
        return [self._edges[i] for i in self._incident.get(face_id, [])]

    def neighbors_of(self, face_id: int) -> list[int]:
        """Adjacent face ids, ascending and deduplicated."""
        return sorted({e.other_face(face_id) for e in self.edges_of(face_id)})

    def edges_by_concavity(self, face_id: int, concavity: Concavity) -> list[AagEdge]:
        return [e for e in self.edges_of(face_id) if e.concavity is concavity]

    def concave_edges_of(self, face_id: int) -> list[AagEdge]:
        return self.edges_by_concavity(face_id, Concavity.CONCAVE)

    def convex_edges_of(self, face_id: int) -> list[AagEdge]:
        return self.edges_by_concavity(face_id, Concavity.CONVEX)

    def tangent_edges_of(self, face_id: int) -> list[AagEdge]:
        return self.edges_by_concavity(face_id, Concavity.TANGENT)

    def nodes_by_surface_type(self, surface_type: SurfaceType) -> list[AagNode]:
        """Seed set for a recognizer, in ascending face_id order."""
        return [n for n in self.nodes if n.surface_type is surface_type]

    @property
    def face_count(self) -> int:
        return len(self._nodes)

    def total_area(self) -> float:
        return sum(n.area for n in self._nodes.values())

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self) -> Iterator[AagNode]:
        return iter(self.nodes)

    def __repr__(self) -> str:
        return (
            f"<AttributedAdjacencyGraph {len(self._nodes)} faces, {len(self._edges)} edges>"
        )
