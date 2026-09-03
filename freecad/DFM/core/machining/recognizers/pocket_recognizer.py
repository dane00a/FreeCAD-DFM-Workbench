# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes milled pockets.

A pocket is a floor with walls round it. Saying that precisely is the whole
difficulty, because plenty of things are a flat face with other faces nearby.

The seed test is enclosure: a pocket floor is *rimmed* by its walls, all the
way round or nearly so. The open corner between two webs on an angle plate has
walls on about a third of its boundary and is not a pocket, however much it
looks like one from the right angle.

Growth is along concave edges only. The face a pocket is cut into meets the
cavity across convex rims -- those are the edges you deburr -- so it can never
be absorbed by construction, which is what keeps a pocket from swallowing the
part.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional, Sequence

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer, neighbours


# A floor must be walled around at least this much of its outer boundary.
# Two thirds, not a half: an L-shaped corner between two webs is rimmed on
# exactly half its perimeter and is not a pocket.
_FLOOR_ENCLOSURE_MIN = 0.65

# A wall stands roughly square to its floor.
_WALL_PERPENDICULAR_MAX_DOT = 0.35

# A face this close to the part's outer envelope is on the outside of it.
_PART_OUTER_TOLERANCE_MM = 0.5

# A neighbour bigger than this share of the part is not a pocket wall.
_MAX_WALL_AREA_SHARE = 0.15

# Total cavity area may not exceed this share of the part: a circuit breaker
# against the search running away through an unusual graph.
_MAX_POCKET_AREA_SHARE = 0.70

# Tori bigger than this are passage surfaces, not wall fillets.
_MAX_BLEND_TORUS_MINOR_R = 8.0


class PocketRecognizer(FeatureRecognizer):
    """Finds pockets: a floor, its walls, and the fillets between them."""

    prefix = "p"

    @property
    def name(self) -> str:
        return "Pocket Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        total_area = graph.total_area()
        if total_area <= 1e-9:
            return []

        outer = _PartEnvelope(graph)
        candidates = self._floor_candidates(graph, outer)
        candidate_ids = {node.face_id for node in candidates}

        taken: set[int] = set(claimed or ())
        found: list[FeatureInstance] = []

        for floor in candidates:
            if floor.face_id in taken:
                continue
            faces = self._grow(graph, floor, candidate_ids, taken, total_area)
            if len(faces) < 3:
                continue  # a floor and one wall is not a cavity

            feature = self._describe(graph, floor, faces)
            if feature is None:
                continue
            found.append(feature)
            taken.update(feature.faces)

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- seeding ------------------------------------------------------------

    def _floor_candidates(
        self, graph: AttributedAdjacencyGraph, outer: "_PartEnvelope"
    ) -> list[AagNode]:
        """Planar faces that are rimmed by walls, largest first."""
        candidates: list[AagNode] = []

        for node in graph.nodes:
            if node.surface_type is not SurfaceType.PLANE:
                continue
            floor_normal = node.outward_normal
            if floor_normal is None:
                continue
            edges = graph.edges_of(node.face_id)
            if not edges:
                continue

            walls: set[int] = set()
            boundary_length = 0.0
            wall_length = 0.0
            has_planar_neighbour = False
            disqualified = False

            for edge in edges:
                other = graph.node(edge.other_face(node.face_id))
                # Inner-wire edges rim a recess *in* the floor or the base of
                # something standing on it. They belong to neither side of the
                # enclosure sum, so a floor with a trough in it is not
                # penalised for having one.
                if not edge.is_inner_wire_edge:
                    boundary_length += edge.shared_edge_length
                if other.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
                    continue
                has_planar_neighbour = True

                if edge.concavity is Concavity.CONCAVE:
                    if self._is_wall_of(node, floor_normal, other):
                        walls.add(other.face_id)
                        if not edge.is_inner_wire_edge:
                            wall_length += edge.shared_edge_length
                    continue

                # A recessed floor never rims onto the outside of the part.
                # A host face carrying bosses has the same concave-base
                # signature, but its convex edges reach the part's outer
                # sides -- and a wall's top rim meets the large host face.
                # Either disqualifies.
                if outer.is_outer(other) or other.area >= node.area * 0.8:
                    disqualified = True
                    break

            if disqualified or not has_planar_neighbour or len(walls) < 2:
                continue
            if boundary_length <= 1e-9:
                continue
            if wall_length / boundary_length < _FLOOR_ENCLOSURE_MIN:
                continue
            candidates.append(node)

        return sorted(candidates, key=lambda n: (-n.area, n.face_id))

    @staticmethod
    def _is_wall_of(floor: AagNode, floor_normal: gp_Dir, wall: AagNode) -> bool:
        """Whether a neighbouring face is a wall rising from this floor.

        Three things have to hold, and the third is the one that matters:
        the wall must stand square to the floor, face inward toward the
        cavity, and lie on the floor's *outward* side. A pedestal fused
        underneath a face satisfies the first two with the roles swapped.
        """
        wall_normal = wall.outward_normal
        if wall_normal is None:
            return False
        if abs(floor_normal.Dot(wall_normal)) >= _WALL_PERPENDICULAR_MAX_DOT:
            return False
        to_floor = gp_Vec(wall.centroid, floor.centroid)
        to_wall = gp_Vec(floor.centroid, wall.centroid)
        return (
            to_floor.Dot(gp_Vec(wall_normal)) > 0.0
            and to_wall.Dot(gp_Vec(floor_normal)) > 0.0
        )

    # -- growth -------------------------------------------------------------

    def _grow(
        self,
        graph: AttributedAdjacencyGraph,
        floor: AagNode,
        candidate_ids: set[int],
        taken: set[int],
        total_area: float,
    ) -> list[int]:
        """Collect the cavity by walking concave edges out from the floor."""
        floor_normal = floor.outward_normal
        collected = {floor.face_id}
        area = floor.area
        queue: deque[int] = deque([floor.face_id])
        ceiling = total_area * _MAX_POCKET_AREA_SHARE

        while queue:
            current = queue.popleft()
            for edge in graph.concave_edges_of(current):
                neighbour_id = edge.other_face(current)
                if neighbour_id in collected or neighbour_id in taken:
                    continue
                neighbour = graph.node(neighbour_id)

                # Bores belong to the hole recognizer; absorbing them would
                # make one pocket out of a cavity and everything drilled
                # into it.
                if neighbour.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
                    continue
                if (
                    neighbour.surface_type is SurfaceType.TORUS
                    and neighbour.torus_minor_r > _MAX_BLEND_TORUS_MINOR_R
                ):
                    continue
                if neighbour.area > total_area * _MAX_WALL_AREA_SHARE:
                    continue
                if area + neighbour.area > ceiling:
                    continue

                if not self._may_absorb(graph, neighbour, floor_normal, collected, candidate_ids):
                    continue

                collected.add(neighbour_id)
                area += neighbour.area
                queue.append(neighbour_id)

        return sorted(collected)

    def _may_absorb(
        self,
        graph: AttributedAdjacencyGraph,
        neighbour: AagNode,
        floor_normal: Optional[gp_Dir],
        collected: set[int],
        candidate_ids: set[int],
    ) -> bool:
        """Whether the cavity should grow into this face."""
        if neighbour.surface_type is not SurfaceType.PLANE or floor_normal is None:
            return True  # blends and freeform walls are absorbed on adjacency

        normal = neighbour.outward_normal
        if normal is None:
            return True
        alignment = abs(normal.Dot(floor_normal))

        # Square to the floor is a wall; parallel to it is another floor
        # level. Anything in between is a sloped exterior surface, unless it
        # is itself a floor candidate -- both walls of a V-groove are, and
        # they sit at exactly such an angle to each other.
        if 0.3 < alignment < 0.9 and neighbour.face_id not in candidate_ids:
            return False

        # The surface a pocket was cut into meets the outside across convex
        # edges, and *mostly* so. An ordinary pocket wall also has one convex
        # edge -- its top rim, where it meets the host face -- so requiring
        # none would reject every wall there is. The test is therefore a
        # majority, not a presence.
        #
        # Only planar neighbours count as evidence. A bore piercing a pocket
        # wall leaves convex rim edges to its cylinder; those are openings
        # within the wall, not the wall reaching the outside, and counting
        # them made every bored chamber wall read as a parent face.
        edges = graph.edges_of(neighbour.face_id)
        external = [
            graph.node(edge.other_face(neighbour.face_id))
            for edge in graph.convex_edges_of(neighbour.face_id)
            if edge.other_face(neighbour.face_id) not in collected
            and graph.node(edge.other_face(neighbour.face_id)).surface_type
            is SurfaceType.PLANE
        ]
        if not external or len(external) * 2 < len(edges):
            return True

        # A through-cut wall genuinely reaches the outside twice, at opposite
        # ends, so its two convex neighbours face away from each other.
        for index, first in enumerate(external):
            normal_a = first.outward_normal
            if normal_a is None:
                continue
            for second in external[index + 1 :]:
                normal_b = second.outward_normal
                if normal_b is not None and normal_a.Dot(normal_b) < -0.7:
                    return True
        return False

    # -- measurement --------------------------------------------------------

    def _describe(
        self, graph: AttributedAdjacencyGraph, seed: AagNode, faces: list[int]
    ) -> Optional[FeatureInstance]:
        """Measure the cavity and build the feature."""
        floor = self._true_floor(graph, faces, seed)
        floor_normal = floor.outward_normal
        if floor_normal is None:
            return None

        depth = self._depth(graph, faces, floor_normal)
        widths = self._floor_widths(floor)
        if widths is None:
            return None
        min_width, max_width = widths

        walls = [
            graph.node(face_id)
            for face_id in faces
            if face_id != floor.face_id
            and graph.node(face_id).surface_type is SurfaceType.PLANE
        ]
        corner_radius = self._corner_radius(graph, faces, floor_normal)

        parameters = {
            "floor_normal": (
                round(floor_normal.X(), 6),
                round(floor_normal.Y(), 6),
                round(floor_normal.Z(), 6),
            ),
            "depth_mm": round(depth, 6),
            "min_width_mm": round(min_width, 6),
            "max_width_mm": round(max_width, 6),
            "corner_radius_mm": round(corner_radius, 6),
            # The reference leaves this hardcoded false and never writes
            # max_width_mm at all, which leaves a rule branch downstream
            # unreachable. Both are computed here.
            "is_open": self._is_open(walls, floor_normal),
            "face_count": len(faces),
        }

        # The floor leads the face list; later passes rely on that.
        ordered = [floor.face_id] + [f for f in faces if f != floor.face_id]
        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.POCKET,
            faces=ordered,
            parameters=parameters,
        )

    @staticmethod
    def _true_floor(
        graph: AttributedAdjacencyGraph, faces: list[int], seed: AagNode
    ) -> AagNode:
        """Pick the real floor from the collected faces.

        Walls come in opposed pairs, so their orientations repeat; a floor's
        orientation is unique to it. Grouping the planar faces by direction
        and taking the smallest group finds the floor even when the search
        started from a wall.
        """
        groups: dict[tuple[int, int, int], list[AagNode]] = {}
        for face_id in faces:
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            key = (
                int(round(abs(normal.X()) * 10)),
                int(round(abs(normal.Y()) * 10)),
                int(round(abs(normal.Z()) * 10)),
            )
            groups.setdefault(key, []).append(node)

        if not groups:
            return seed
        smallest = min(groups.values(), key=lambda members: (len(members), -max(m.area for m in members)))
        return max(smallest, key=lambda n: (n.area, -n.face_id))

    @staticmethod
    def _depth(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> float:
        """Extent of the cavity along the floor's normal."""
        box = Bnd_Box()
        found = False
        for face_id in faces:
            node = graph.node(face_id)
            if not node.bbox.IsVoid():
                box.Add(node.bbox)
                found = True
        if not found or box.IsVoid():
            return 0.0
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        return abs(
            (xmax - xmin) * floor_normal.X()
        ) + abs((ymax - ymin) * floor_normal.Y()) + abs((zmax - zmin) * floor_normal.Z())

    @staticmethod
    def _floor_widths(floor: AagNode) -> Optional[tuple[float, float]]:
        """The floor's narrowest and widest in-plane dimensions."""
        dims = sorted(d for d in floor.bbox_dims() if d > 0.1)
        if len(dims) < 2:
            return None
        return (dims[0], dims[-1])

    @staticmethod
    def _is_open(walls: list[AagNode], floor_normal: gp_Dir) -> bool:
        """Whether the cavity runs out of the part rather than being closed.

        A closed pocket is walled on every side, so its wall normals cover
        two independent in-plane directions. A slot open at one end covers
        only one.
        """
        axes: list[gp_Dir] = []
        for wall in walls:
            normal = wall.outward_normal
            if normal is None or abs(normal.Dot(floor_normal)) >= 0.3:
                continue
            if not any(abs(normal.Dot(existing)) > 0.9 for existing in axes):
                axes.append(normal)
        return len(axes) < 2

    @staticmethod
    def _corner_radius(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> float:
        """Smallest corner fillet radius in the cavity, or zero if sharp."""
        radii = [
            node.cyl_radius
            for node in (graph.node(face_id) for face_id in faces)
            if node.surface_type is SurfaceType.CYLINDER
            and node.cyl_cone_axis is not None
            and abs(node.cyl_cone_axis.Direction().Dot(floor_normal)) >= 0.95
        ]
        return min(radii) if radii else 0.0


class _PartEnvelope:
    """Answers whether a face lies on the outside of the part.

    Uses a support function rather than comparing against the six cardinal
    faces of a box, so an angled exterior -- a cast draft wall, a chamfered
    side -- is recognised as exterior too.
    """

    def __init__(self, graph: AttributedAdjacencyGraph) -> None:
        box = Bnd_Box()
        found = False
        for node in graph.nodes:
            if not node.bbox.IsVoid():
                box.Add(node.bbox)
                found = True
        if not found or box.IsVoid():
            self._corners: list[gp_Pnt] = []
            return
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        self._corners = [
            gp_Pnt(x, y, z)
            for x in (xmin, xmax)
            for y in (ymin, ymax)
            for z in (zmin, zmax)
        ]

    def is_outer(self, node: AagNode) -> bool:
        if not self._corners or node.surface_type is not SurfaceType.PLANE:
            return False
        normal = node.outward_normal
        if normal is None:
            return False
        direction = gp_Vec(normal)
        support = max(gp_Vec(gp_Pnt(0, 0, 0), corner).Dot(direction) for corner in self._corners)
        centroid = gp_Vec(gp_Pnt(0, 0, 0), node.centroid).Dot(direction)
        return (support - centroid) < _PART_OUTER_TOLERANCE_MM
