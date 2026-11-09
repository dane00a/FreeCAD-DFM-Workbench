# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes machined steps and shoulders.

A step is a terrace: a flat face that is walled on one side and open on the
other. Drop a shoulder off the end of a bar, mill a rebate along an edge, face
a landing for a cover plate -- all the same thing, and all cut the same way,
with the tool running along the wall and off into fresh air at the other side.

That mix is the whole test. A pocket floor is walled the whole way round, so
every edge is concave. An outer face of the stock is open the whole way round,
so every edge is convex. A terrace has both, and nothing else on a normal part
does.

Two families of impostor look exactly like that mix and are rejected by name.
The first is the host face a feature is cut into: it carries an inner wire, and
an inner wire's rim reads concave, so a plate top with a pocket in it would
otherwise seed a "step" as deep as the pocket. The second is the wall of a
closed opening, which meets its own opposite wall two hops away -- real
terraces have no face staring back at them.
"""

from __future__ import annotations

from typing import Optional, Sequence

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir

from ..aag import AagEdge, AagNode, AttributedAdjacencyGraph, SurfaceType
from ..face_extent import span_of_face
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer


# A wall stands square to the terrace it drops from. Anything less square than
# this is another terrace level or a draft face, not the riser.
_WALL_PERPENDICULAR_MAX_DOT = 0.3

# Two faces this strongly opposed are looking at each other across a void.
# Reaching one through a perpendicular wall means the wall belongs to a closed
# opening -- a milled slot or square cutout -- rather than to a terrace.
_OPPOSED_PARTNER_MAX_DOT = -0.7

# Below this a bounding-box dimension is the flat direction of a planar face,
# not a real extent.
_MIN_MEANINGFUL_DIM_MM = 0.1

# A normal is treated as running along one world axis past this. Used both to
# pick the axis a candidate's footprint is compared on and to decide whether a
# seed lies flat enough for the pad-top width override.
_AXIS_ALIGNED_MIN_DOT = 0.7

# A face covering this much of the part in both in-plane directions is the
# outer silhouette of the stock, not something anyone machined.
_PART_SILHOUETTE_SHARE = 0.8

# ...unless it carries this many concave neighbours, in which case it is a host
# face with several protrusions rising out of it and genuinely does span the
# part while still being a terrace between them.
_SILHOUETTE_CONCAVE_EXEMPTION = 2


class StepRecognizer(FeatureRecognizer):
    """Recognizes machined steps and shoulders."""

    prefix = "st"

    @property
    def name(self) -> str:
        return "Step Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored, as the reference ignores it: no
        # recogniser there is told what the others claimed, so the step pass
        # emits every terrace reading it finds and the resolver sorts them
        # out afterwards. Standing down on claimed faces changed what the
        # pattern pass sees, which runs on the raw readings: the 48 broached
        # slots of turbine_compressor_disk, claimed first by the slit pass,
        # lost the STEP readings the reference groups into its second
        # PATTERN, and the rosette bars of text_neg_decor_groove theirs.
        extent = _part_extent(graph)
        found: list[FeatureInstance] = []
        # (seed, riser, index into found) per candidate, for the mutual-pair
        # pass below.
        candidates: list[tuple[AagNode, AagNode, int]] = []
        self._shape = shape

        for seed in graph.nodes_by_surface_type(SurfaceType.PLANE):
            result = self._recognize_one(graph, seed, extent)
            if result is None:
                continue
            feature, riser = result
            candidates.append((seed, riser, len(found)))
            # Both faces of a shoulder read as a terrace -- the riser is a
            # step with the horizontal face as its wall, seen from the side --
            # and both readings are emitted, as the reference does: the
            # resolver keeps the first of two with the same faces. Collapsing
            # them here instead (the first found owning the pair) changed what
            # the pattern pass sees, which runs on the raw readings: four
            # re-entrant corners of a cross read as four STEPs made a 2 x 2
            # grid, where the reference's eight readings make no pattern.
            found.append(feature)

        # A mutual pair is one step, measured one way. Two perpendicular
        # planes joined by a concave edge, each with a convex edge elsewhere,
        # both qualify as a terrace with the other as its riser, and the two
        # readings measure different numbers (height is the riser's extent,
        # width the terrace's). Which one survived used to depend on
        # traversal order, which a moved seam permutes: the reference
        # measured the same two faces of knurl_neg_flatted_shaft as 0.34 x
        # 0.16 one way and 0.26 x 0.21 the other. So both report the
        # measurement of the preferred terrace (_geometric_less). Faces and
        # types are untouched.
        by_seed = {seed.face_id: i for i, (seed, _, _) in enumerate(candidates)}
        for seed, riser, index in candidates:
            j = by_seed.get(riser.face_id)
            if j is None:
                continue
            other_seed, other_riser, other_index = candidates[j]
            if other_riser.face_id != seed.face_id:
                continue  # not mutual
            if not _geometric_less(other_seed, seed):
                continue  # this one is preferred
            source = found[other_index].parameters
            found[index].parameters["step_height_mm"] = source["step_height_mm"]
            found[index].parameters["step_width_mm"] = source["step_width_mm"]

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- seeding ------------------------------------------------------------

    def _recognize_one(
        self,
        graph: AttributedAdjacencyGraph,
        seed: AagNode,
        extent: tuple[float, float, float],
    ) -> Optional[tuple[FeatureInstance, AagNode]]:
        # A face with an inner wire is the host something was cut into, not a
        # terrace. Seeding one produces a bogus step sized to whatever the
        # feature's depth happens to be.
        if seed.inner_loop_count > 0:
            return None

        normal = seed.outward_normal
        if normal is None:
            return None

        # Inner-wire edges are excluded from the concave/convex tally. The rim
        # of an opening reads concave from the wall's side even where the wall
        # is entirely interior, which makes an all-interior face look mixed
        # and seed a step that is not there. Outer-wire edges are classified
        # honestly and are the only evidence used.
        concave = _outer_wire_only(graph.concave_edges_of(seed.face_id))
        convex = _outer_wire_only(graph.convex_edges_of(seed.face_id))
        if not concave or not convex:
            return None

        concave_neighbours = _sorted_neighbours(graph, seed.face_id, concave)
        if self._is_opening_wall(graph, seed, normal, concave_neighbours):
            return None

        riser = self._riser(graph, normal, concave_neighbours)
        if riser is None:
            return None
        riser_node, step_height = riser

        # The step face's own box, its seam twin included -- and only that.
        # A floor the seam crosses arrives as two faces, and its half-box is
        # not its box. Not is_surface_continuation, which the riser height
        # does use: this box also decides the silhouette gate, and widening
        # it to every coplanar fragment re-gates candidates part-wide (the
        # reference tried it and lost a sheet-metal verdict).
        step_box = Bnd_Box()
        if not seed.bbox.IsVoid():
            step_box.Add(seed.bbox)
        for edge in graph.edges_of(seed.face_id):
            if not edge.is_seam:
                continue
            twin = edge.other_face(seed.face_id)
            if twin == seed.face_id or not graph.has_node(twin):
                continue
            twin_box = graph.node(twin).bbox
            if not twin_box.IsVoid():
                step_box.Add(twin_box)
        if step_box.IsVoid():
            face_dims = seed.bbox_dims()
        else:
            xmin, ymin, zmin, xmax, ymax, zmax = step_box.Get()
            face_dims = (xmax - xmin, ymax - ymin, zmax - zmin)
        width = _narrow_in_plane_dim(face_dims)
        if self._spans_the_part(normal, face_dims, extent, len(concave)):
            return None

        pad_width = self._pad_top_width(graph, seed, normal, concave_neighbours)
        if pad_width is not None:
            width = pad_width

        # Only planar concave neighbours join the face set. A cylinder or
        # sphere across a concave edge belongs to a bore or a boss; pulling it
        # in would bloat the step and confuse anything that dedups features by
        # how far their face sets overlap.
        faces = [seed.face_id] + [
            node.face_id
            for node, _ in concave_neighbours
            if node.surface_type is SurfaceType.PLANE
        ]

        return (
            FeatureInstance(
                instance_id=self.instance_id(0),
                type=FeatureType.STEP,
                faces=faces,
                parameters={
                    "step_height_mm": round(step_height, 6),
                    "step_width_mm": round(width, 6),
                    "normal": (
                        round(normal.X(), 6),
                        round(normal.Y(), 6),
                        round(normal.Z(), 6),
                    ),
                },
            ),
            riser_node,
        )

    # -- guards -------------------------------------------------------------

    @staticmethod
    def _is_opening_wall(
        graph: AttributedAdjacencyGraph,
        seed: AagNode,
        normal: gp_Dir,
        concave_neighbours: Sequence[tuple[AagNode, AagEdge]],
    ) -> bool:
        """Whether the seed is a wall of an opening that closes on itself.

        Walk out through a perpendicular wall and see what is on the far side.
        If a face pointing back at the seed is reachable in that one hop, the
        seed and that face are the two walls of a slot or square cutout. A
        staircase of real terraces has *parallel* siblings, never opposed ones,
        and a terrace open to the outside reaches no sibling at all.
        """
        for wall, _ in concave_neighbours:
            if wall.surface_type is not SurfaceType.PLANE:
                continue
            wall_normal = wall.outward_normal
            if wall_normal is None:
                continue
            if abs(normal.Dot(wall_normal)) > _WALL_PERPENDICULAR_MAX_DOT:
                continue

            for edge in graph.edges_of(wall.face_id):
                if edge.is_inner_wire_edge:
                    continue
                far_id = edge.other_face(wall.face_id)
                if far_id == seed.face_id or not graph.has_node(far_id):
                    continue
                far = graph.node(far_id)
                if far.surface_type is not SurfaceType.PLANE:
                    continue
                far_normal = far.outward_normal
                if far_normal is None:
                    continue
                if normal.Dot(far_normal) < _OPPOSED_PARTNER_MAX_DOT:
                    return True
        return False

    @staticmethod
    def _spans_the_part(
        normal: gp_Dir,
        face_dims: tuple[float, float, float],
        extent: tuple[float, float, float],
        concave_count: int,
    ) -> bool:
        """Whether the candidate is really the outside of the stock.

        A face whose footprint fills the part in both directions across its own
        normal is the top or bottom of the billet. The exemption is a host face
        with several protrusions on it: a pad fused flush with the part edge
        notches into the outer wire instead of forming an inner loop, so the
        host still spans the part yet is a genuine terrace between the pads.
        """
        if min(extent) <= 0.0:
            return False
        components = (abs(normal.X()), abs(normal.Y()), abs(normal.Z()))
        normal_axis = components.index(max(components))
        if components[normal_axis] <= _AXIS_ALIGNED_MIN_DOT:
            return False
        spans = all(
            face_dims[axis] >= extent[axis] * _PART_SILHOUETTE_SHARE
            for axis in range(3)
            if axis != normal_axis
        )
        return spans and concave_count < _SILHOUETTE_CONCAVE_EXEMPTION

    # -- measurement --------------------------------------------------------

    def _riser(
        self,
        graph: AttributedAdjacencyGraph,
        normal: gp_Dir,
        concave_neighbours: Sequence[tuple[AagNode, AagEdge]],
    ) -> Optional[tuple[AagNode, float]]:
        """The wall the terrace drops from and its height, or None.

        Chosen by geometry, not by order: the tallest riser, ties going to
        _geometric_less. The reference took the first wall in edge order
        until a moved seam permuted that order and the reported height with
        it.
        """
        best: Optional[AagNode] = None
        best_height = 0.0
        for wall, _ in concave_neighbours:
            if wall.surface_type is not SurfaceType.PLANE:
                continue
            wall_normal = wall.outward_normal
            if wall_normal is None:
                continue
            if abs(normal.Dot(wall_normal)) >= _WALL_PERPENDICULAR_MAX_DOT:
                continue
            height = _wall_height(graph, self._shape, wall, normal)
            if (
                best is None
                or height > best_height + 1e-9
                or (abs(height - best_height) <= 1e-9 and _geometric_less(wall, best))
            ):
                best = wall
                best_height = height
        if best is None:
            return None
        return best, best_height

    @staticmethod
    def _pad_top_width(
        graph: AttributedAdjacencyGraph,
        seed: AagNode,
        normal: gp_Dir,
        concave_neighbours: Sequence[tuple[AagNode, AagEdge]],
    ) -> Optional[float]:
        """Width taken from the pad standing on the terrace, when there is one.

        For a horizontal host plate carrying a flush pad, the seed's own
        bounding box is the entire plate and says nothing about the step. What
        the machinist cares about is the pad: walk seed -> riser -> pad top and
        measure that instead. A plain single-riser step reaches no such face
        and keeps its own dimension.
        """
        if abs(normal.Z()) <= _AXIS_ALIGNED_MIN_DOT:
            return None

        best: Optional[float] = None
        for wall, _ in concave_neighbours:
            if wall.surface_type is not SurfaceType.PLANE:
                continue
            wall_normal = wall.outward_normal
            if wall_normal is None:
                continue
            if abs(normal.Dot(wall_normal)) >= _WALL_PERPENDICULAR_MAX_DOT:
                continue

            for edge in sorted(
                graph.convex_edges_of(wall.face_id),
                key=lambda e: e.other_face(wall.face_id),
            ):
                pad_id = edge.other_face(wall.face_id)
                if not graph.has_node(pad_id):
                    continue
                pad = graph.node(pad_id)
                if pad.surface_type is not SurfaceType.PLANE:
                    continue
                pad_normal = pad.outward_normal
                if pad_normal is None:
                    continue
                if abs(normal.Dot(pad_normal)) < _AXIS_ALIGNED_MIN_DOT:
                    continue
                candidate = _narrow_in_plane_dim(pad.bbox_dims())
                if candidate > _MIN_MEANINGFUL_DIM_MM and (
                    best is None or candidate < best
                ):
                    best = candidate
        return best


# =============================================================================
# Helpers
# =============================================================================


def _part_extent(graph: AttributedAdjacencyGraph) -> tuple[float, float, float]:
    """Size of the whole part, from the union of the face bounding boxes."""
    box = Bnd_Box()
    found = False
    for node in graph.nodes:
        if not node.bbox.IsVoid():
            box.Add(node.bbox)
            found = True
    if not found or box.IsVoid():
        return (0.0, 0.0, 0.0)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return (xmax - xmin, ymax - ymin, zmax - zmin)


def _geometric_less(a: AagNode, b: AagNode) -> bool:
    """A total order on faces that uses geometry only.

    The more horizontal face first (a terrace is what the part's own up axis
    looks down on), then the larger, then by centroid. Tolerances keep a
    floating-point tie from deciding it.
    """
    za = abs(a.plane_normal.Z()) if a.plane_normal is not None else 0.0
    zb = abs(b.plane_normal.Z()) if b.plane_normal is not None else 0.0
    if abs(za - zb) > 1e-6:
        return za > zb
    scale = max(a.area, b.area, 1.0)
    if abs(a.area - b.area) > 1e-6 * scale:
        return a.area > b.area
    p, q = a.centroid, b.centroid
    if abs(p.X() - q.X()) > 1e-6:
        return p.X() < q.X()
    if abs(p.Y() - q.Y()) > 1e-6:
        return p.Y() < q.Y()
    return p.Z() < q.Z() - 1e-6


def _wall_height(
    graph: AttributedAdjacencyGraph, shape, wall: AagNode, normal: gp_Dir
) -> float:
    """How tall a riser is along the terrace normal, measured from the wall.

    Not from its bbox diagonal: projecting an axis-aligned diagonal onto the
    step's normal answers about the box's corners and swings with the wall's
    orientation (the reference measured 0.34 against 0.07 on one solid with
    the seam moved). And over the whole wall, fragments included: a wall the
    seam crosses can arrive as two faces, and is_surface_continuation says
    which neighbours are the same surface carrying on. The box answer stays
    as the fallback for a wall with no sampleable boundary.
    """
    dx, dy, dz = wall.bbox_dims()
    height = abs(dx * normal.X() + dy * normal.Y() + dz * normal.Z())
    if shape is None:
        return height
    lo, hi = 1.0e300, -1.0e300
    ok, lo, hi = span_of_face(shape, wall.face_id, normal, lo, hi)
    for edge in graph.edges_of(wall.face_id):
        if not edge.is_surface_continuation:
            continue
        twin = edge.other_face(wall.face_id)
        if twin == wall.face_id or not graph.has_node(twin):
            continue
        twin_ok, lo, hi = span_of_face(shape, twin, normal, lo, hi)
        ok = ok or twin_ok
    if ok and hi > lo:
        height = hi - lo
    return height


def _outer_wire_only(edges: Sequence[AagEdge]) -> list[AagEdge]:
    return [edge for edge in edges if not edge.is_inner_wire_edge]


def _sorted_neighbours(
    graph: AttributedAdjacencyGraph, face_id: int, edges: Sequence[AagEdge]
) -> list[tuple[AagNode, AagEdge]]:
    """The far node of each edge, paired with it, in face-id order."""
    found = [
        (graph.node(edge.other_face(face_id)), edge)
        for edge in edges
        if graph.has_node(edge.other_face(face_id))
    ]
    return sorted(found, key=lambda pair: pair[0].face_id)


def _narrow_in_plane_dim(dims: tuple[float, float, float]) -> float:
    """The smaller of a planar face's two real extents.

    The smallest of the three is the flat direction, so it is skipped unless
    the face is genuinely oblique and has no vanishing dimension.
    """
    ordered = sorted(dims)
    return ordered[0] if ordered[0] > _MIN_MEANINGFUL_DIM_MM else ordered[1]
