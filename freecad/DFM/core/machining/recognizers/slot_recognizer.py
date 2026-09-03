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


# A floor bigger than this share of the part is the part, not a slot bottom.
_MAX_FLOOR_AREA_SHARE = 0.15

# A slot floor needs at least its two walls. Deliberately not a share of
# its edges: a channel running out of the part has convex edges at the open
# ends, so a channel through a block is only half concave and a ratio test
# would reject exactly the shape this recognizer exists for. The facing-wall
# pair and the width-to-length test below are the real discriminators.
_MIN_CONCAVE_EDGES = 2

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

        taken: set[int] = set(claimed or ())
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
        # Only the walls this pass has already given to a slot. A wall an
        # earlier recognizer claimed -- a bore breaking through it, say --
        # says nothing about whether this floor is a channel of its own.
        walls_seen: set[int] = set()

        for floor in self._floor_candidates(graph, claimed, total_area):
            if floor.face_id in claimed:
                continue
            walls = self._facing_walls(graph, floor)
            if walls is None:
                continue
            # A channel of square section has two faces that could each be
            # read as its floor, and the same pair of walls between them.
            # Taken at face value that is two slots lying on top of each
            # other -- and because they share only two faces out of three,
            # the resolver keeps both. Whichever was found first is the
            # channel; this is its ceiling.
            if all(wall.face_id in walls_seen for wall in walls):
                continue
            if self._has_axial_bore(graph, floor):
                continue  # a drilled port through the floor, not a channel

            faces = self._collect(graph, floor, walls, claimed)
            feature = self._describe(graph, floor, walls, faces)
            if feature is None:
                continue
            found.append(feature)
            claimed.update(feature.faces)
            walls_seen.update(wall.face_id for wall in walls)

        return found

    def _floor_candidates(
        self, graph: AttributedAdjacencyGraph, taken: set[int], total_area: float
    ) -> list[AagNode]:
        candidates = []
        for node in graph.nodes:
            if node.surface_type is not SurfaceType.PLANE or node.face_id in taken:
                continue
            if node.area > total_area * _MAX_FLOOR_AREA_SHARE:
                continue
            edges = graph.edges_of(node.face_id)
            if not edges:
                continue
            concave = sum(1 for e in edges if e.concavity is Concavity.CONCAVE)
            if concave < _MIN_CONCAVE_EDGES:
                continue
            candidates.append(node)
        return sorted(candidates, key=lambda n: (-n.area, n.face_id))

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
                if normal_a.Dot(normal_b) < _FACING_WALLS_MAX_DOT:
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
        floor: AagNode,
        walls: tuple[AagNode, AagNode],
        taken: set[int],
    ) -> list[int]:
        """Gather the channel: floor, walls, end caps and corner fillets."""
        collected = {floor.face_id, walls[0].face_id, walls[1].face_id}
        floor_normal = floor.outward_normal

        # A single hop out from the floor and walls is enough: a channel is
        # shallow topology, and growing further would swallow the part.
        for face_id in (floor.face_id, walls[0].face_id, walls[1].face_id):
            for edge in graph.concave_edges_of(face_id):
                neighbour_id = edge.other_face(face_id)
                if neighbour_id in collected or neighbour_id in taken:
                    continue
                node = graph.node(neighbour_id)
                if node.surface_type is SurfaceType.PLANE:
                    normal = node.outward_normal
                    # End caps stand square to the floor like the walls do.
                    if (
                        normal is not None
                        and floor_normal is not None
                        and abs(normal.Dot(floor_normal)) < _WALL_PERPENDICULAR_MAX_DOT
                    ):
                        collected.add(neighbour_id)
                elif node.surface_type is SurfaceType.CYLINDER:
                    if self._is_corner_fillet(graph, node, floor_normal, collected):
                        collected.add(neighbour_id)

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
        walls: tuple[AagNode, AagNode],
        faces: list[int],
    ) -> Optional[FeatureInstance]:
        floor_normal = floor.outward_normal
        if floor_normal is None:
            return None

        # The width is the gap between the facing walls; the length is the
        # floor's other in-plane dimension.
        normal_a = walls[0].outward_normal
        if normal_a is None:
            return None
        width = abs(
            gp_Vec(walls[0].centroid, walls[1].centroid).Dot(gp_Vec(normal_a))
        )
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
