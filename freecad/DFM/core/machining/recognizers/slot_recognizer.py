# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes slots.

A slot is a channel: a floor with two facing walls, longer than it is wide.
What separates it from a pocket is that the pocket recognizer has already had
its turn -- a cavity walled all the way round is a pocket, and what reaches
here is what was left over, typically because it runs out of the part at one
or both ends.

Two shapes qualify. The common one is a floor with parallel walls. The other
has no floor at all: an obround slot milled right through, bounded by two
half-cylinders of equal radius and the flats between them.
"""

from __future__ import annotations


from typing import Optional, Sequence

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer, axes_are_coaxial
from .pocket_recognizer import _PartEnvelope


# A floor bigger than this share of the part is the part, not a slot bottom.
_MAX_FLOOR_AREA_SHARE = 0.15

# A slot floor needs at least its two walls.
_MIN_CONCAVE_EDGES = 2

# And most of its boundary has to be walls. A recessed floor is surrounded by
# the material it was cut out of; a face with mostly convex edges is the
# outside of the part looking at a couple of things standing on it. Two
# thirds is the reference's line: not all, because a blend meeting the floor
# leaves a tangent edge, but not half either.
#
# Where the channel leaves the part is not counted at all. Those edges are
# convex -- there is no wall there, the material has run out -- and counting
# them against the floor rejects the open-ended channel this recognizer
# mainly exists for: two long walls and two open ends is exactly half. What
# the test is really asking is whether the rest of this face's boundary is
# wall, and an open end is not a vote either way.
_MIN_CONCAVE_EDGE_SHARE = 2.0 / 3.0

# How far off parallel two faces can be and still count as facing the same
# way. Used to spot a lid: a second plane parallel to the floor means what
# was taken for a floor is one side of an opening that has no floor at all.
_PARALLEL_MIN_DOT = 0.7

# A wall stands square to the floor.
_WALL_PERPENDICULAR_MAX_DOT = 0.3

# Two walls face each other when their outward normals oppose.
_FACING_WALLS_MAX_DOT = -0.9

# Wider than this fraction of its length and it is a pocket shape, not a
# channel -- whatever the pocket recognizer decided about enclosure.
_MAX_WIDTH_TO_LENGTH = 0.8


class SlotRecognizer(FeatureRecognizer):
    """Finds milled channels, floored or cut right through."""

    prefix = "s"

    @property
    def name(self) -> str:
        return "Slot Recognizer"

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

        # What earlier passes claimed is deliberately ignored. Nearly every
        # slot on a part is also a cavity the pocket pass can seed, and if
        # this one stands down wherever that happened there is no slot
        # reading left for the resolver to weigh against the pocket one --
        # a channel would be a pocket purely because the pocket pass runs
        # first. Both readings are emitted; the resolver settles it on the
        # aspect ratio, which is the question actually at issue.
        taken: set[int] = set()
        found: list[FeatureInstance] = []

        found.extend(self._floored_slots(graph, taken, total_area))
        for feature in found:
            taken.update(feature.faces)
        found.extend(self._obround_slots(graph, taken))

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- floored slots ------------------------------------------------------

    def _floored_slots(
        self, graph: AttributedAdjacencyGraph, taken: set[int], total_area: float
    ) -> list[FeatureInstance]:
        found: list[FeatureInstance] = []
        claimed = set(taken)
        envelope = _PartEnvelope(graph)

        for seed in self._floor_candidates(graph, claimed, total_area):
            if seed.face_id in claimed:
                continue
            walls = self._facing_walls(graph, seed)
            if walls is None:
                continue

            faces = self._collect(graph, seed, walls, total_area, envelope)

            # The face that seeded this is not necessarily the floor. A
            # closed end wall of a channel has plenty of concave edges and
            # passes every test above, and taken as the floor it swaps the
            # slot's length with its depth. Which face is the floor is a
            # question about the set as a whole: a channel has walls in
            # facing pairs and one floor, so of the directions its faces
            # look in, the floor's is the one with fewest faces in it.
            floor = self._true_floor(graph, faces, seed, envelope)
            floor_normal = floor.outward_normal
            if floor_normal is None:
                continue

            if self._has_axial_bore(graph, floor):
                # A bore straight down through the floor is a port drilled
                # into a channel, and a channel broken by ports comes in
                # segments: only one of them would seed here and the rest
                # would be read as pockets, so the same channel would be
                # reported twice over in two vocabularies. Leave all of it
                # to the pocket pass.
                continue

            # A second plane facing the same way as the floor means there is
            # no floor: this is an opening with four walls, two facing pairs,
            # running through to both ends. The minority-cluster test cannot
            # see it -- a square opening ties two clusters at two apiece --
            # so it is asked directly.
            if self._has_parallel_partner(graph, faces, floor, floor_normal):
                continue

            # Walking out again from the real floor, because seeding from an
            # end wall never reaches the opposite end wall: the seed sees the
            # floor and the long walls, and the far end is round the other
            # side of them. Left out, the far wall is unclaimed and seeds the
            # same slot a second time.
            if floor.face_id != seed.face_id:
                faces = sorted(
                    set(faces)
                    | self._small_concave_neighbours(graph, floor.face_id, total_area, envelope)
                )

            feature = self._describe(graph, floor, faces)
            if feature is None:
                continue
            found.append(feature)
            claimed.update(feature.faces)

        return found

    @staticmethod
    def _true_floor(
        graph: AttributedAdjacencyGraph, faces: list[int], seed: AagNode, envelope
    ) -> AagNode:
        """Which of these faces is the floor.

        A slot's walls come in facing pairs and its floor stands alone, so
        grouping the recessed faces by the direction they look in -- without
        sign, since a facing pair looks two ways along one axis -- leaves the
        floor in the smallest group. Ties go to the group with the most area
        between its members, which is what tells a stepped slot's floors from
        its walls, and within that group to the largest face.
        """
        candidates = []
        for face_id in faces:
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            if node.outward_normal is None:
                continue
            if not SlotRecognizer._is_recessed(graph, node, envelope):
                continue
            candidates.append(node)
        if len(candidates) < 2:
            return seed

        clusters: dict[tuple[int, int, int], list[AagNode]] = {}
        for node in candidates:
            normal = node.outward_normal
            key = (
                int(round(abs(normal.X()) * 10.0)),
                int(round(abs(normal.Y()) * 10.0)),
                int(round(abs(normal.Z()) * 10.0)),
            )
            clusters.setdefault(key, []).append(node)

        smallest = min(len(group) for group in clusters.values())
        best: list[AagNode] = []
        best_area = -1.0
        for group in clusters.values():
            if len(group) != smallest:
                continue
            area = sum(node.area for node in group)
            if area > best_area:
                best_area, best = area, group
        if not best:
            return seed
        return max(best, key=lambda node: (node.area, node.face_id))

    @staticmethod
    def _has_parallel_partner(
        graph: AttributedAdjacencyGraph,
        faces: list[int],
        floor: AagNode,
        floor_normal: gp_Dir,
    ) -> bool:
        """Whether something in here faces the same way the floor does."""
        for face_id in faces:
            if face_id == floor.face_id:
                continue
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            if abs(normal.Dot(floor_normal)) > _PARALLEL_MIN_DOT:
                return True
        return False

    @staticmethod
    def _small_concave_neighbours(
        graph: AttributedAdjacencyGraph,
        face_id: int,
        total_area: float,
        envelope,
    ) -> set[int]:
        """Faces cut into the same recess as this one.

        Small, concave to it, and not the face the recess opens through. That
        face is a side of the part with a hole in it, and the hole's edges
        read as concave, so without excluding it every cavity wall on the
        part has the whole side of the part for a neighbour -- which then
        goes into the recess's bounding box and makes it as deep as the part.

        It is recognized by being on the outside rather than by having an
        inner wire. A channel wall with a cross-passage opening into it has
        an inner wire too, and it is a wall of this recess, not the way in.
        """
        found: set[int] = set()
        for edge in graph.concave_edges_of(face_id):
            neighbour = graph.node(edge.other_face(face_id))
            if neighbour.inner_loop_count > 0 and envelope.is_outer(neighbour):
                continue
            if neighbour.area < total_area * _MAX_FLOOR_AREA_SHARE:
                found.add(neighbour.face_id)
        return found

    def _floor_candidates(
        self, graph: AttributedAdjacencyGraph, taken: set[int], total_area: float
    ) -> list[AagNode]:
        envelope = _PartEnvelope(graph)
        candidates = []
        for node in graph.nodes:
            if node.surface_type is not SurfaceType.PLANE or node.face_id in taken:
                continue
            if node.area > total_area * _MAX_FLOOR_AREA_SHARE:
                continue
            if not self._is_recessed(graph, node, envelope):
                continue
            candidates.append(node)
        return sorted(candidates, key=lambda n: (-n.area, n.face_id))

    @staticmethod
    def _is_recessed(
        graph: AttributedAdjacencyGraph, node: AagNode, envelope
    ) -> bool:
        """Whether this face is cut into the part rather than a side of it."""
        edges = graph.edges_of(node.face_id)
        if not edges:
            return False
        concave = 0
        counted = 0
        for edge in edges:
            if edge.concavity is Concavity.CONCAVE:
                concave += 1
                counted += 1
                continue
            neighbour = graph.node(edge.other_face(node.face_id))
            if envelope.is_outer(neighbour):
                continue  # the recess running out of the part: not a vote
            counted += 1
        if concave < _MIN_CONCAVE_EDGES:
            return False
        return concave >= counted * _MIN_CONCAVE_EDGE_SHARE

    def _facing_walls(
        self, graph: AttributedAdjacencyGraph, floor: AagNode
    ) -> Optional[tuple[AagNode, AagNode]]:
        """The two walls of the channel, or None if there is no facing pair."""
        floor_normal = floor.outward_normal
        if floor_normal is None:
            return None

        walls = []
        for edge in graph.concave_edges_of(floor.face_id):
            node = graph.node(edge.other_face(floor.face_id))
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None or abs(normal.Dot(floor_normal)) >= _WALL_PERPENDICULAR_MAX_DOT:
                continue
            walls.append(node)

        for index, first in enumerate(walls):
            normal_a = first.outward_normal
            for second in walls[index + 1 :]:
                normal_b = second.outward_normal
                if normal_a is None or normal_b is None:
                    continue
                if normal_a.Dot(normal_b) >= _FACING_WALLS_MAX_DOT:
                    continue
                # Facing, not back to back. A rib's two sides are
                # anti-parallel exactly as a channel's are, and the
                # difference is which of the material and the air lies
                # between them: the walls of a channel look into it, so the
                # step from one to the other runs the way the first one
                # faces.
                across = gp_Vec(first.centroid, second.centroid).Dot(
                    gp_Vec(normal_a)
                )
                if across <= 0.0:
                    continue
                return (first, second)
        return None

    @staticmethod
    def _has_axial_bore(graph: AttributedAdjacencyGraph, floor: AagNode) -> bool:
        """Whether a bore runs through the floor along its normal.

        Such a floor is the bottom of a pocket with a port drilled in it, and
        the pocket recognizer will already have claimed it or declined it for
        its own reasons. Growing a slot from it would double-report.
        """
        floor_normal = floor.outward_normal
        if floor_normal is None:
            return False
        for edge in graph.edges_of(floor.face_id):
            node = graph.node(edge.other_face(floor.face_id))
            if (
                node.surface_type is SurfaceType.CYLINDER
                and node.cyl_cone_axis is not None
                and abs(node.cyl_cone_axis.Direction().Dot(floor_normal)) > 0.95
            ):
                return True
        return False

    def _collect(
        self,
        graph: AttributedAdjacencyGraph,
        seed: AagNode,
        walls: tuple[AagNode, AagNode],
        total_area: float,
        envelope,
    ) -> list[int]:
        """Gather the recess this face is part of.

        One hop out from the seed and from each of its facing walls. The
        walls have to be walked as well as the seed: when the seed is itself
        a wall, its own facing partner is on the far side of the channel and
        shares no edge with it, so without this the far wall never joins the
        set and the floor cannot be picked out of it.

        No filtering by orientation here. What is and is not a wall is
        decided once the floor is known, and deciding it against the seed's
        normal gets a channel seeded from its end wall exactly wrong.
        """
        collected = {seed.face_id, walls[0].face_id, walls[1].face_id}
        for face_id in (seed.face_id, walls[0].face_id, walls[1].face_id):
            collected |= self._small_concave_neighbours(graph, face_id, total_area, envelope)
        return sorted(collected)

    @staticmethod
    def _is_corner_fillet(
        graph: AttributedAdjacencyGraph,
        node: AagNode,
        floor_normal: Optional[gp_Dir],
        collected: set[int],
    ) -> bool:
        """A vertical radius in the corner of the channel.

        Its axis runs along the floor normal, and it sits between two walls
        that are not facing each other -- a cylinder between *facing* walls is
        the rounded end of the slot, which belongs to the slot's length rather
        than to its corners.
        """
        if floor_normal is None or node.cyl_cone_axis is None:
            return False
        if abs(node.cyl_cone_axis.Direction().Dot(floor_normal)) < 0.95:
            return False

        planar = [
            graph.node(edge.other_face(node.face_id))
            for edge in graph.edges_of(node.face_id)
            if graph.node(edge.other_face(node.face_id)).surface_type is SurfaceType.PLANE
        ]
        if len(planar) != 2:
            return False
        normal_a = planar[0].outward_normal
        normal_b = planar[1].outward_normal
        if normal_a is None or normal_b is None:
            return False
        return normal_a.Dot(normal_b) >= -0.5

    def _describe(
        self,
        graph: AttributedAdjacencyGraph,
        floor: AagNode,
        faces: list[int],
    ) -> Optional[FeatureInstance]:
        floor_normal = floor.outward_normal
        if floor_normal is None:
            return None

        # The width is the narrowest gap between any facing pair of walls in
        # the set, not the pair the seed happened to reach. Seeded from an
        # end wall, the only facing pair within reach is the two ends, and
        # the distance between those is the slot's length.
        width = self._width(graph, faces, floor_normal)
        if width is None:
            return None
        dims = sorted(d for d in floor.bbox_dims() if d > 1e-6)
        length = dims[-1] if dims else 0.0
        if width <= 1e-6 or length <= 1e-6:
            return None
        if width >= length * _MAX_WIDTH_TO_LENGTH:
            return None  # too square to be a channel

        depth = self._depth(graph, faces, floor_normal)
        corner_radius = self._corner_radius(graph, faces, floor_normal)

        parameters = {
            "width_mm": round(width, 6),
            "length_mm": round(length, 6),
            "depth_mm": round(depth, 6),
            "floor_normal": (
                round(floor_normal.X(), 6),
                round(floor_normal.Y(), 6),
                round(floor_normal.Z(), 6),
            ),
            "is_open": self._is_open(graph, faces, floor_normal),
        }
        # Written only when a real fillet was found. A slot with square
        # corners must not carry a zero here: a rule reading it would see a
        # measured radius of nought rather than an unmeasured one.
        if corner_radius is not None:
            parameters["corner_radius_mm"] = round(corner_radius, 6)

        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.SLOT,
            faces=[floor.face_id] + [f for f in faces if f != floor.face_id],
            parameters=parameters,
        )

    @staticmethod
    def _width(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> Optional[float]:
        """The narrowest distance across the channel."""
        walls = []
        for face_id in faces:
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            if abs(normal.Dot(floor_normal)) >= _WALL_PERPENDICULAR_MAX_DOT:
                continue
            walls.append((node, normal))

        narrowest = None
        for index, (first, normal_a) in enumerate(walls):
            for second, normal_b in walls[index + 1 :]:
                if normal_a.Dot(normal_b) >= _FACING_WALLS_MAX_DOT:
                    continue
                across = abs(
                    gp_Vec(first.centroid, second.centroid).Dot(gp_Vec(normal_a))
                )
                if across > 0.01 and (narrowest is None or across < narrowest):
                    narrowest = across
        return narrowest

    @staticmethod
    def _depth(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> float:
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
        return (
            abs((xmax - xmin) * floor_normal.X())
            + abs((ymax - ymin) * floor_normal.Y())
            + abs((zmax - zmin) * floor_normal.Z())
        )

    @staticmethod
    def _corner_radius(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> Optional[float]:
        radii = [
            node.cyl_radius
            for node in (graph.node(face_id) for face_id in faces)
            if node.surface_type is SurfaceType.CYLINDER
            and node.cyl_cone_axis is not None
            and abs(node.cyl_cone_axis.Direction().Dot(floor_normal)) >= 0.95
        ]
        return min(radii) if radii else None

    @staticmethod
    def _is_open(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> bool:
        """Whether the channel runs out of the part at an end."""
        axes: list[gp_Dir] = []
        for face_id in faces:
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None or abs(normal.Dot(floor_normal)) >= _WALL_PERPENDICULAR_MAX_DOT:
                continue
            if not any(abs(normal.Dot(existing)) > 0.9 for existing in axes):
                axes.append(normal)
        return len(axes) < 2

    # -- obround slots ------------------------------------------------------

    def _obround_slots(
        self, graph: AttributedAdjacencyGraph, taken: set[int]
    ) -> list[FeatureInstance]:
        """Slots milled through, bounded by two equal half-cylinder ends.

        Concavity is deliberately not consulted here. A through cut has no
        floor to be concave to, and its ends read differently depending on
        which face won the edge's orientation.
        """
        cylinders = [
            node
            for node in graph.nodes_by_surface_type(SurfaceType.CYLINDER)
            if node.is_internal and node.face_id not in taken and node.cyl_cone_axis is not None
        ]
        found: list[FeatureInstance] = []
        claimed = set(taken)

        for index, first in enumerate(cylinders):
            if first.face_id in claimed:
                continue
            for second in cylinders[index + 1 :]:
                if second.face_id in claimed:
                    continue
                if abs(first.cyl_radius - second.cyl_radius) > first.cyl_radius * 0.05:
                    continue
                axis_a, axis_b = first.cyl_cone_axis, second.cyl_cone_axis
                if abs(axis_a.Direction().Dot(axis_b.Direction())) < 0.98:
                    continue
                separation = gp_Vec(
                    axis_a.Location(), axis_b.Location()
                ).CrossMagnitude(gp_Vec(axis_a.Direction()))
                if separation < 0.05:
                    continue  # coaxial: one bore, not two ends of a slot
                if axes_are_coaxial(axis_a, axis_b):
                    continue

                walls = self._shared_flats(graph, first, second, axis_a.Direction())
                if walls is None:
                    continue

                faces = sorted({first.face_id, second.face_id, *walls})
                found.append(
                    FeatureInstance(
                        instance_id=self.instance_id(0),
                        type=FeatureType.SLOT,
                        faces=faces,
                        parameters={
                            "width_mm": round(first.cyl_radius * 2.0, 6),
                            "length_mm": round(separation + first.cyl_radius * 2.0, 6),
                            "depth_mm": round(
                                first.cyl_p0.Distance(first.cyl_p1)
                                if first.cyl_p0 and first.cyl_p1
                                else 0.0,
                                6,
                            ),
                            "is_open": False,
                            "is_through": True,
                            "axis": (
                                round(axis_a.Direction().X(), 6),
                                round(axis_a.Direction().Y(), 6),
                                round(axis_a.Direction().Z(), 6),
                            ),
                            # An obround end radius is half the width by
                            # construction, not a discretionary fillet, so no
                            # corner radius is recorded.
                        },
                    )
                )
                claimed.update(faces)
                break

        return found

    @staticmethod
    def _shared_flats(
        graph: AttributedAdjacencyGraph,
        first: AagNode,
        second: AagNode,
        axis: gp_Dir,
    ) -> Optional[list[int]]:
        """The two facing flats joining the ends of an obround slot."""
        neighbours_a = {
            edge.other_face(first.face_id) for edge in graph.edges_of(first.face_id)
        }
        neighbours_b = {
            edge.other_face(second.face_id) for edge in graph.edges_of(second.face_id)
        }
        shared = [
            graph.node(face_id)
            for face_id in sorted(neighbours_a & neighbours_b)
            if graph.node(face_id).surface_type is SurfaceType.PLANE
        ]
        flats = [
            node
            for node in shared
            if node.outward_normal is not None
            and abs(node.outward_normal.Dot(axis)) <= 0.3
        ]
        for index, one in enumerate(flats):
            for other in flats[index + 1 :]:
                normal_a, normal_b = one.outward_normal, other.outward_normal
                if normal_a is not None and normal_b is not None:
                    if normal_a.Dot(normal_b) < _FACING_WALLS_MAX_DOT:
                        return [one.face_id, other.face_id]
        return None
