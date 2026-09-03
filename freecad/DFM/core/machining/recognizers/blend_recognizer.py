# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes fillets and chamfers.

A blend is the transition where two faces would otherwise meet at a sharp
edge: a fillet rolls the corner over an arc, a chamfer cuts it off flat. Both
are nearly free to draw and neither is free to cut. An internal fillet sets
the smallest cutter that can reach into the corner, and a chamfer on an
awkward face buys itself another setup, so both have to be found before any
of that can be judged.

What distinguishes them is the surface and the dihedral. A fillet is a
cylinder or a torus running tangent into a face on *each* side of it -- one
tangent edge is a cylinder that merely touches its neighbour, two is a blend.
A chamfer is a narrow planar strip meeting its neighbours obliquely, square
to neither and flat with neither, and long and thin the way a strip cut in
one pass of a tool is.

The kernel rarely leaves a blend as a single face. A fillet wrapping a part
top comes back as one face per edge it crosses; a chamfer run round a pocket
arrives as strips with mitred corners between them; a chamfer taken across an
arc is split into facets fanning round it. Every one of those is one tool and
one pass to the man running the machine, so sub-faces carrying the same size
are merged before anything is emitted -- by adjacency, and by lying on the
same underlying surface where an intervening face has cut between them.

Only the blend faces themselves are claimed, never the faces they transition
to. A chamfer that absorbed the pocket walls it breaks would overlap that
pocket almost entirely, and one of the two would then be thrown away as a
duplicate of the other.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from OCP.gp import gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer, axes_are_coaxial


# A bounding box side below this is a flat face's own zero thickness rather
# than a real extent, so it is no candidate for the strip width.
_MIN_REAL_DIM_MM = 0.01

# A blend joins two distinct faces and carries a tangent edge into each of
# them. One tangent edge is a cylinder resting against a face it happens to
# touch -- a bore wall, whose only tangent edges are its own seams, has none.
_MIN_BLEND_EDGES = 2

# Two cylinders this close in radius, on a common axis line, are one surface
# the kernel split at a seam rather than two faces blended together.
_SEAM_RADIUS_TOL_MM = 0.01
_AXIS_PARALLEL_DOT = 0.999
_AXIS_COLLINEAR_TOL_MM = 0.05

# Sub-faces of one blend agree on size to within the accuracy of the
# measurement: tighter for a fillet radius, which is read off the surface
# itself, than for a chamfer distance, which is read off the bounding box.
_FILLET_RADIUS_TOL_MM = 0.05
_CHAMFER_DISTANCE_TOL_MM = 0.1

# The oblique dihedral window a chamfer lives in. 110 to 160 degrees of
# interior angle covers chamfer angles from 20 to 70 degrees off the face;
# square corners and tangent blends fall outside it either way.
_CHAMFER_DIHEDRAL_MIN_RAD = math.radians(110.0)
_CHAMFER_DIHEDRAL_MAX_RAD = math.radians(160.0)

# A chamfer breaks an edge, it is not a face in its own right: it stays small
# beside whatever it breaks, and it is long one way and narrow the other.
# Both guards refuse squarish oblique facets, which are draft faces or
# machined reliefs rather than edge breaks.
_CHAMFER_MAX_AREA_SHARE = 0.12
_CHAMFER_MIN_ASPECT = 5.0

# Two chamfer strips lie in one plane when their normals agree and neither
# stands off the other's plane.
_COPLANAR_DOT = 0.999
_COPLANAR_OFFSET_TOL_MM = 0.05

# A conical band wider than this along its slant is a taper or a seat being
# turned, not an edge being broken.
_MAX_CONICAL_CHAMFER_SLANT_MM = 2.0

_CURVED_TYPES = (SurfaceType.CYLINDER, SurfaceType.CONE, SurfaceType.TORUS)


@dataclass
class _Seed:
    """One blend sub-face, with the size two of them must share to merge."""

    node: AagNode
    dim: float
    blend_neighbours: list[int] = field(default_factory=list)


class _UnionFind:
    """Joins blend sub-faces back into the operation they came from."""

    def __init__(self, count: int) -> None:
        self._parent = list(range(count))

    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def unite(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self._parent[a] = b


class BlendRecognizer(FeatureRecognizer):
    """Recognizes fillets and chamfers."""

    prefix = "b"

    @property
    def name(self) -> str:
        return "Blend Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored. A fillet in the corner of a
        # pocket belongs to that pocket's geometry as well as being a radius
        # in its own right, and the pocket pass has already taken it. Skipping
        # claimed faces would lose exactly the internal corners the rules most
        # need to see.
        found: list[FeatureInstance] = []
        found.extend(self._fillets(graph))
        found.extend(self._chamfers(graph))
        found.extend(self._conical_chamfers(graph))

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- fillets ------------------------------------------------------------

    def _fillets(self, graph: AttributedAdjacencyGraph) -> list[FeatureInstance]:
        """Cylinders and tori running tangent into a face on either side."""
        seeds: list[_Seed] = []
        for node in graph.nodes_by_surface_type(SurfaceType.CYLINDER):
            seed = self._fillet_seed(graph, node, node.cyl_radius)
            if seed is not None:
                seeds.append(seed)
        for node in graph.nodes_by_surface_type(SurfaceType.TORUS):
            seed = self._fillet_seed(graph, node, node.torus_minor_r)
            if seed is not None:
                seeds.append(seed)

        return _emit_merged(
            graph,
            seeds,
            _FILLET_RADIUS_TOL_MM,
            _coaxial_seeds,
            FeatureType.FILLET,
            "radius_mm",
        )

    @staticmethod
    def _fillet_seed(
        graph: AttributedAdjacencyGraph, node: AagNode, radius: float
    ) -> Optional[_Seed]:
        tangent = graph.tangent_edges_of(node.face_id)

        # Count only the tangent edges reaching a genuinely different surface.
        # A full-turn bore wall comes back from the kernel as two coaxial
        # halves meeting at axis-parallel seams; those seams are smooth, but
        # they are an artefact of the parametrisation and not a blend into
        # anything. Requiring two real blend edges is what keeps bores,
        # counterbores and drilled holes out of the fillet list.
        blend_edges = 0
        neighbours: list[int] = []
        for edge in tangent:
            other_id = edge.other_face(node.face_id)
            neighbours.append(other_id)
            if not graph.has_node(other_id):
                continue
            if _is_seam_partner(node, graph.node(other_id)):
                continue
            blend_edges += 1
        if blend_edges < _MIN_BLEND_EDGES:
            return None

        return _Seed(node=node, dim=radius, blend_neighbours=neighbours)

    # -- chamfers -----------------------------------------------------------

    def _chamfers(self, graph: AttributedAdjacencyGraph) -> list[FeatureInstance]:
        """Narrow planar strips sitting at an oblique dihedral."""
        seeds: list[_Seed] = []

        for node in graph.nodes_by_surface_type(SurfaceType.PLANE):
            edges = graph.edges_of(node.face_id)
            if not edges:
                continue

            oblique = 0
            largest_neighbour_area = 0.0
            neighbours: list[int] = []
            for edge in edges:
                other_id = edge.other_face(node.face_id)
                if (
                    _CHAMFER_DIHEDRAL_MIN_RAD
                    <= edge.dihedral_angle
                    <= _CHAMFER_DIHEDRAL_MAX_RAD
                ):
                    oblique += 1
                    neighbours.append(other_id)
                if graph.has_node(other_id):
                    largest_neighbour_area = max(
                        largest_neighbour_area, graph.node(other_id).area
                    )

            if oblique < 1:
                continue
            if node.area >= largest_neighbour_area * _CHAMFER_MAX_AREA_SHARE:
                continue

            distance = _shortest_real_dim(node)
            if distance <= 0.0:
                continue
            if _longest_dim(node) < distance * _CHAMFER_MIN_ASPECT:
                continue

            seeds.append(_Seed(node=node, dim=distance, blend_neighbours=neighbours))

        def same_surface(a: _Seed, b: _Seed) -> bool:
            return _chamfer_same_surface(graph, a, b)

        return _emit_merged(
            graph,
            seeds,
            _CHAMFER_DISTANCE_TOL_MM,
            same_surface,
            FeatureType.CHAMFER,
            "distance_mm",
        )

    # -- conical chamfers ---------------------------------------------------

    def _conical_chamfers(
        self, graph: AttributedAdjacencyGraph
    ) -> list[FeatureInstance]:
        """Thin conical bands bevelling a circular edge.

        The knife edge on a vacuum flange is one of these: a half-millimetre
        band at 30 degrees off the sealing face, a chamfer in every sense that
        matters even though it is a cone rather than a strip.

        Reported liberally. A cone that really belongs to a hole -- a
        countersink mouth, a drill point -- is already claimed by that hole
        and the resolver drops the duplicate on overlap, so there is nothing
        to be gained by working out ownership here.
        """
        found: list[FeatureInstance] = []

        for node in graph.nodes_by_surface_type(SurfaceType.CONE):
            slant = _shortest_real_dim(node)
            if slant <= 0.0 or slant > _MAX_CONICAL_CHAMFER_SLANT_MM:
                continue

            touches_plane = False
            for edge in graph.edges_of(node.face_id):
                other_id = edge.other_face(node.face_id)
                if not graph.has_node(other_id):
                    continue
                if graph.node(other_id).surface_type is SurfaceType.PLANE:
                    touches_plane = True
                    break
            if not touches_plane:
                continue

            # The machinist's convention: measured from the face the chamfer
            # bevels, not from the axis. A 45 degree chamfer has a semi-angle
            # of 45; a knife edge 30 degrees off its sealing face has a
            # semi-angle of 60.
            semi_deg = abs(math.degrees(node.cone_semi_angle))
            found.append(
                FeatureInstance(
                    instance_id="",
                    type=FeatureType.CHAMFER,
                    faces=[node.face_id],
                    parameters={
                        "chamfer_angle_deg": round(90.0 - semi_deg, 1),
                        "width_mm": round(slant, 6),
                        "conical": True,
                    },
                )
            )

        return found


# -- merging ------------------------------------------------------------------


def _emit_merged(
    graph: AttributedAdjacencyGraph,
    seeds: list[_Seed],
    dim_tol: float,
    same_surface: Callable[[_Seed, _Seed], bool],
    feature_type: str,
    dim_param: str,
) -> list[FeatureInstance]:
    """One feature per connected group of same-sized blend sub-faces."""
    if not seeds:
        return []

    index_of = {seed.node.face_id: i for i, seed in enumerate(seeds)}
    groups = _UnionFind(len(seeds))

    def same_dim(a: int, b: int) -> bool:
        return abs(seeds[a].dim - seeds[b].dim) <= dim_tol

    # Adjacency first. This is what joins the mitred corners of a chamfer run
    # round a pocket perimeter to the straight strips either side of them.
    for i, seed in enumerate(seeds):
        for edge in graph.edges_of(seed.node.face_id):
            other = index_of.get(edge.other_face(seed.node.face_id))
            if other is not None and same_dim(i, other):
                groups.unite(i, other)

    # Then the same underlying surface, which catches the pieces of one
    # operation that an intervening face has separated -- a chamfer strip
    # broken in two by the corner fillet it runs into. Note what this is
    # *not*: a shared-neighbour test. Sharing a source face is far too
    # permissive, since the top and bottom chamfers of a block both touch its
    # sides and are plainly two separate chamfers.
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            if same_dim(i, j) and same_surface(seeds[i], seeds[j]):
                groups.unite(i, j)

    # The size is reported from the largest member. A small corner patch
    # misreports it, because its bounding box does not line up with the
    # direction the chamfer was offset along; a long strip is reliable.
    canonical: dict[int, int] = {}
    for i, seed in enumerate(seeds):
        root = groups.find(i)
        best = canonical.get(root)
        if best is None or seed.node.area > seeds[best].node.area:
            canonical[root] = i

    found: list[FeatureInstance] = []
    feature_of_root: dict[int, FeatureInstance] = {}
    for i, seed in enumerate(seeds):
        root = groups.find(i)
        feature = feature_of_root.get(root)
        if feature is None:
            feature = FeatureInstance(
                instance_id="",
                type=feature_type,
                parameters={dim_param: round(seeds[canonical[root]].dim, 6)},
            )
            feature_of_root[root] = feature
            found.append(feature)
        if seed.node.face_id not in feature.faces:
            feature.faces.append(seed.node.face_id)

    for feature in found:
        feature.faces.sort()
    return found


# -- shared geometry ----------------------------------------------------------


def _shortest_real_dim(node: AagNode) -> float:
    """Shortest bounding box side that is not the face's own zero thickness.

    For a chamfer this is the offset the tool was set to, and it is the
    canonical width of any blend strip.
    """
    for dim in sorted(node.bbox_dims()):
        if dim > _MIN_REAL_DIM_MM:
            return dim
    return 0.0


def _longest_dim(node: AagNode) -> float:
    return max(node.bbox_dims())


def _is_seam_partner(node: AagNode, other: AagNode) -> bool:
    """Whether two cylinders are one surface the kernel split at a seam.

    Same axis line and same radius means a bore wall arriving as two halves,
    and the smooth edge between them is a parametrisation seam rather than a
    blend into another face.
    """
    if node.surface_type is not SurfaceType.CYLINDER:
        return False
    if other.surface_type is not SurfaceType.CYLINDER:
        return False
    if abs(node.cyl_radius - other.cyl_radius) > _SEAM_RADIUS_TOL_MM:
        return False
    return _coaxial(node, other)


def _coaxial(a: AagNode, b: AagNode) -> bool:
    """Whether two cylinders share an axis line, to blend tolerance."""
    if a.cyl_cone_axis is None or b.cyl_cone_axis is None:
        return False
    return axes_are_coaxial(
        a.cyl_cone_axis,
        b.cyl_cone_axis,
        direction_dot=_AXIS_PARALLEL_DOT,
        line_distance=_AXIS_COLLINEAR_TOL_MM,
    )


def _coaxial_seeds(a: _Seed, b: _Seed) -> bool:
    """Same-surface test for fillets.

    Only cylinders carry an axis, so two tori never merge this way. That is
    no loss: a torus corner blend already touches the cylindrical fillets
    either side of it across tangent edges, and merges by adjacency instead.
    """
    if a.node.surface_type is not SurfaceType.CYLINDER:
        return False
    if b.node.surface_type is not SurfaceType.CYLINDER:
        return False
    return _coaxial(a.node, b.node)


def _chamfer_same_surface(
    graph: AttributedAdjacencyGraph, a: _Seed, b: _Seed
) -> bool:
    """Same-surface test for chamfers.

    Two strips are pieces of one chamfer when they are coplanar -- a single
    flat chamfer the kernel cut in two, whose pieces stayed in its plane --
    or when both touch the same curved face. The second case is a chamfer
    taken across an arc: the kernel returns two or more facets with different
    normals fanning round the corner, and what they have in common is the
    curved feature they wrapped around. A block chamfered top and bottom has
    no curved faces at all, so that rule never fires there and its two
    chamfers stay apart even though both touch the same sides.
    """
    normal_a = a.node.outward_normal
    normal_b = b.node.outward_normal
    if normal_a is not None and normal_b is not None:
        if abs(normal_a.Dot(normal_b)) >= _COPLANAR_DOT:
            offset = gp_Vec(a.node.centroid, b.node.centroid)
            if abs(offset.Dot(gp_Vec(normal_a))) < _COPLANAR_OFFSET_TOL_MM:
                return True

    curved = _curved_neighbours(graph, a)
    if not curved:
        return False
    return bool(curved & _curved_neighbours(graph, b))


def _curved_neighbours(graph: AttributedAdjacencyGraph, seed: _Seed) -> set[int]:
    """Every curved face a strip touches, across a blend edge or any other."""
    found: set[int] = set()
    candidates = list(seed.blend_neighbours)
    candidates.extend(
        edge.other_face(seed.node.face_id)
        for edge in graph.edges_of(seed.node.face_id)
    )
    for face_id in candidates:
        if graph.has_node(face_id) and graph.node(face_id).surface_type in _CURVED_TYPES:
            found.add(face_id)
    return found
