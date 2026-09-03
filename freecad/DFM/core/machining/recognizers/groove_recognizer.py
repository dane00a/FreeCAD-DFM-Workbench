# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes turned grooves.

This is where lathe DFM lives. Every groove is the same primitive -- a
cylinder between two shoulders, each of which steps away to another coaxial
cylinder -- and they differ only in what the groove is *for*, which is read
from its proportions and its neighbours:

* next to a thread, it is a relief so the tool can run out
* about twice as wide as it is deep, it is an O-ring gland
* narrow and square, it is a retaining-ring groove
* anything else is just a groove

The classification matters because the rules differ. A relief groove is
judged against the thread pitch; a gland against its cord.

Three shapes of groove are found, because the same seal is cut three ways:

1. *turned* -- a cylinder between two shoulders, on a shaft or in a bore
2. *circular face gland* -- an annular channel sunk into a flat face, whose
   walls are two coaxial cylinders either side of a ring floor
3. *loop gland* -- a racetrack channel round the perimeter of a flat face,
   with straight planar walls and cylindrical corners

One guard carries most of the weight in the turned pass. A cylinder between
two shoulders is only a groove if it steps the *right way*: an outside groove
must be narrower than the material either side of it, and a bore groove wider.
Without that test a plain through-bore between two counterbores reads as a
groove, and the hole it really is disappears.
"""

from __future__ import annotations

from typing import Optional, Sequence

from OCP.gp import gp_Dir, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import (
    FeatureRecognizer,
    axes_are_coaxial,
    cylinder_length,
    neighbours,
)


# A shoulder stands square to the groove's axis.
_SHOULDER_AXIS_ALIGNMENT = 0.9

# O-ring glands follow AS568 cord sizes: the groove is wider than it is deep
# so the cord can roll and seal without being pinched.
_GLAND_MIN_WIDTH_MM = 2.0
_GLAND_MAX_WIDTH_MM = 10.0
_GLAND_MIN_RATIO = 1.4
_GLAND_MAX_RATIO = 2.2

# Beyond these a channel in a face is an annular pocket, not a seal gland.
_FACE_GLAND_MIN_WIDTH_MM = 1.0
_FACE_GLAND_MAX_WIDTH_MM = 12.0
_FACE_GLAND_MAX_DEPTH_MM = 10.0

# Retaining rings sit in a narrow square-shouldered groove.
_RETAINING_MAX_WIDTH_MM = 2.5
_RETAINING_MAX_DEPTH_MM = 1.5
_RETAINING_MIN_RATIO = 0.6
_RETAINING_MAX_RATIO = 1.8


class GrooveRecognizer(FeatureRecognizer):
    """Finds turned grooves and works out what each one is for."""

    prefix = "gv"

    @property
    def name(self) -> str:
        return "Groove Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored. A groove turned into the middle
        # of a bore is part of that bore's geometry as well as a groove in its
        # own right, and the hole recognizer will already have taken those
        # faces. Skipping them would lose every internal groove. The guards
        # below are what keep this honest, not the claim set.
        threads = self._thread_faces(prior)
        found: list[FeatureInstance] = []
        taken: set[int] = set()

        for node in graph.nodes_by_surface_type(SurfaceType.CYLINDER):
            if node.face_id in taken or node.cyl_cone_axis is None:
                continue
            feature = self._recognize_one(graph, node, threads)
            if feature is None:
                continue
            found.append(feature)
            taken.update(feature.faces)

        found.extend(self._circular_face_glands(graph))
        found.extend(self._loop_glands(graph))

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    @staticmethod
    def _thread_faces(
        prior: Optional[Sequence[FeatureInstance]],
    ) -> dict[int, FeatureInstance]:
        """Faces already known to carry a thread, by face id."""
        threads: dict[int, FeatureInstance] = {}
        for feature in prior or ():
            if feature.type in (FeatureType.THREADED_HOLE, FeatureType.EXTERNAL_THREAD):
                for face_id in feature.faces:
                    threads[face_id] = feature
        return threads

    # -- one candidate ------------------------------------------------------

    def _recognize_one(
        self,
        graph: AttributedAdjacencyGraph,
        groove: AagNode,
        threads: dict[int, FeatureInstance],
    ) -> Optional[FeatureInstance]:
        axis = groove.cyl_cone_axis
        axis_dir = axis.Direction()

        shoulders: list[AagNode] = []
        flanks: list[AagNode] = []
        for shoulder, _ in neighbours(graph, groove.face_id):
            if shoulder.surface_type is not SurfaceType.PLANE:
                continue
            normal = shoulder.outward_normal
            if normal is None or abs(normal.Dot(axis_dir)) <= _SHOULDER_AXIS_ALIGNMENT:
                continue
            flank = self._flank_beyond(graph, shoulder, groove, axis)
            if flank is None:
                continue
            shoulders.append(shoulder)
            flanks.append(flank)

        if len(shoulders) < 2 or len(flanks) < 2:
            return None

        # The step has to go the right way round, and which way depends on
        # which side the material is. A groove turned into a shaft is cut
        # inward, so the shaft either side of it is *larger*. A groove cut in
        # a bore goes outward, so the bore either side is *smaller*.
        #
        # Without this test a plain through-bore sitting between two
        # counterbores looks exactly like a groove -- a cylinder between two
        # shoulders with coaxial cylinders beyond -- and claiming it loses
        # the hole that is really there.
        larger = all(f.cyl_radius > groove.cyl_radius + 1e-6 for f in flanks)
        smaller = all(f.cyl_radius < groove.cyl_radius - 1e-6 for f in flanks)
        if groove.is_internal:
            if not smaller:
                return None
        elif not larger:
            return None

        width = cylinder_length(groove)
        depth = abs(flanks[0].cyl_radius - groove.cyl_radius)
        if width <= 1e-6 or depth <= 1e-6:
            return None

        adjacent_thread = next(
            (threads[f.face_id] for f in flanks if f.face_id in threads), None
        )
        groove_type = self._classify(width, depth, adjacent_thread is not None)

        parameters = {
            "axis": (
                round(axis_dir.X(), 6),
                round(axis_dir.Y(), 6),
                round(axis_dir.Z(), 6),
            ),
            "groove_diameter_mm": round(groove.cyl_radius * 2.0, 6),
            "flank_diameter_mm": round(flanks[0].cyl_radius * 2.0, 6),
            "width_mm": round(width, 6),
            "depth_mm": round(depth, 6),
            "is_internal": groove.is_internal,
            "thread_adjacent": adjacent_thread is not None,
            "groove_type": groove_type,
        }
        if adjacent_thread is not None:
            pitch = adjacent_thread.number("thread_pitch_mm")
            if pitch is not None:
                parameters["adjacent_thread_pitch_mm"] = pitch
            designation = adjacent_thread.param("thread_designation")
            if designation:
                parameters["adjacent_thread_designation"] = designation

        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=groove_type,
            faces=sorted(
                {groove.face_id} | {s.face_id for s in shoulders}
            ),
            parameters=parameters,
        )

    @staticmethod
    def _flank_beyond(
        graph: AttributedAdjacencyGraph,
        shoulder: AagNode,
        groove: AagNode,
        axis,
    ) -> Optional[AagNode]:
        """The coaxial cylinder on the far side of a shoulder."""
        for candidate, _ in neighbours(graph, shoulder.face_id):
            if candidate.face_id == groove.face_id:
                continue
            if candidate.surface_type is not SurfaceType.CYLINDER:
                continue
            if candidate.cyl_cone_axis is None:
                continue
            if candidate.is_internal != groove.is_internal:
                continue
            if axes_are_coaxial(candidate.cyl_cone_axis, axis):
                return candidate
        return None

    @staticmethod
    def _classify(width: float, depth: float, thread_adjacent: bool) -> str:
        """What the groove is for, from its proportions and its neighbours.

        Context beats dimension: a groove next to a thread is a relief
        whatever its size, because that is what it is doing there.
        """
        if thread_adjacent:
            return FeatureType.THREAD_RELIEF_GROOVE

        ratio = width / depth if depth > 1e-9 else 0.0

        if (
            _GLAND_MIN_WIDTH_MM <= width <= _GLAND_MAX_WIDTH_MM
            and _GLAND_MIN_RATIO <= ratio <= _GLAND_MAX_RATIO
        ):
            return FeatureType.O_RING_GLAND

        if (
            width <= _RETAINING_MAX_WIDTH_MM
            and depth <= _RETAINING_MAX_DEPTH_MM
            and _RETAINING_MIN_RATIO <= ratio <= _RETAINING_MAX_RATIO
        ):
            return FeatureType.RETAINING_RING_GROOVE

        return FeatureType.GROOVE

    # -- circular face glands -----------------------------------------------

    def _circular_face_glands(
        self, graph: AttributedAdjacencyGraph
    ) -> list[FeatureInstance]:
        """Annular channels sunk into a flat face.

        The signature is a sandwich: a concave outer wall, a ring floor square
        to its axis, and a convex inner wall of smaller radius on the far side.
        That is a seal gland on an end cap or a flange face, which is the
        commonest place an O-ring actually lives.

        Both walls must be near-complete revolutions. A rounded-rectangle loop
        gland has coaxial concave/convex quarter-cylinders at each corner and
        would otherwise emit one bogus circular gland per corner; those loops
        belong to the pass below.
        """
        found: list[FeatureInstance] = []
        claimed_outers: set[int] = set()

        for outer in graph.nodes_by_surface_type(SurfaceType.CYLINDER):
            if not outer.is_internal or outer.cyl_cone_axis is None:
                continue
            if outer.face_id in claimed_outers:
                continue
            axis = outer.cyl_cone_axis
            axis_dir = axis.Direction()

            for floor, _ in neighbours(graph, outer.face_id):
                if floor.surface_type is not SurfaceType.PLANE:
                    continue
                floor_normal = floor.outward_normal
                if floor_normal is None:
                    continue
                if abs(floor_normal.Dot(axis_dir)) <= _SHOULDER_AXIS_ALIGNMENT:
                    continue

                inner = self._inner_wall(graph, floor, outer, axis)
                if inner is None:
                    continue

                width = outer.cyl_radius - inner.cyl_radius
                depth = cylinder_length(outer)
                if not _FACE_GLAND_MIN_WIDTH_MM <= width <= _FACE_GLAND_MAX_WIDTH_MM:
                    continue
                if not 0.0 < depth <= _FACE_GLAND_MAX_DEPTH_MM:
                    continue
                if not (_full_ring(outer) and _full_ring(inner)):
                    continue

                groove_type = self._classify(width, depth, False)
                claimed_outers.add(outer.face_id)
                found.append(
                    FeatureInstance(
                        instance_id=self.instance_id(0),
                        type=groove_type,
                        faces=sorted({outer.face_id, floor.face_id, inner.face_id}),
                        parameters={
                            "axis": _axis_tuple(axis_dir),
                            "groove_diameter_mm": round(outer.cyl_radius * 2.0, 6),
                            "inner_diameter_mm": round(inner.cyl_radius * 2.0, 6),
                            "width_mm": round(width, 6),
                            "depth_mm": round(depth, 6),
                            "is_internal": False,
                            "thread_adjacent": False,
                            "groove_type": groove_type,
                            "gland_orientation": "face",
                            "gland_shape": "circular",
                        },
                    )
                )
                break

        return found

    @staticmethod
    def _inner_wall(
        graph: AttributedAdjacencyGraph,
        floor: AagNode,
        outer: AagNode,
        axis,
    ) -> Optional[AagNode]:
        """The convex island wall on the far side of a ring floor."""
        for candidate, _ in neighbours(graph, floor.face_id):
            if candidate.face_id == outer.face_id:
                continue
            if candidate.surface_type is not SurfaceType.CYLINDER:
                continue
            if candidate.is_internal or candidate.cyl_cone_axis is None:
                continue
            if not axes_are_coaxial(candidate.cyl_cone_axis, axis):
                continue
            if candidate.cyl_radius >= outer.cyl_radius - 1e-6:
                continue
            return candidate
        return None

    # -- loop glands --------------------------------------------------------

    def _loop_glands(self, graph: AttributedAdjacencyGraph) -> list[FeatureInstance]:
        """Racetrack channels round the perimeter of a flat face.

        A closed channel's floor is a single planar ring: one inner wire, which
        is the loop signature, where a straight slot floor has none. Every wall
        must face a partner across the channel at a consistent distance, and
        that is what separates a gland from a stepped recess that happens to
        close on itself.
        """
        found: list[FeatureInstance] = []

        for ring in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if ring.inner_loop_count != 1:
                continue
            floor_normal = ring.outward_normal
            if floor_normal is None:
                continue

            walls: list[AagNode] = []
            corners: list[AagNode] = []
            if not self._collect_loop_walls(graph, ring, floor_normal, walls, corners):
                continue
            if len(walls) < 2:
                continue

            paired = _paired_width(walls)
            if paired is None:
                continue
            width_min, width_max = paired
            if width_min < _FACE_GLAND_MIN_WIDTH_MM:
                continue
            if width_max > _FACE_GLAND_MAX_WIDTH_MM:
                continue
            # A channel has one width. Anything that wanders is a stepped
            # recess rather than a groove cut with a single tool.
            if width_max > width_min * 1.5:
                continue

            depth = max(
                (_extent_along(wall, floor_normal) for wall in walls), default=0.0
            )
            if not 0.0 < depth <= _FACE_GLAND_MAX_DEPTH_MM:
                continue

            mean_width = (width_min + width_max) * 0.5
            groove_type = self._classify(mean_width, depth, False)
            # The retaining-ring band overlaps the gland band for narrow square
            # sections, but a retaining ring is circular by definition. A
            # rectangular loop in that window is a gasket groove.
            if groove_type == FeatureType.RETAINING_RING_GROOVE:
                groove_type = FeatureType.O_RING_GLAND

            found.append(
                FeatureInstance(
                    instance_id=self.instance_id(0),
                    type=groove_type,
                    faces=sorted(
                        {ring.face_id}
                        | {w.face_id for w in walls}
                        | {c.face_id for c in corners}
                    ),
                    parameters={
                        "axis": _axis_tuple(floor_normal),
                        "width_mm": round(mean_width, 6),
                        "depth_mm": round(depth, 6),
                        "is_internal": False,
                        "thread_adjacent": False,
                        "groove_type": groove_type,
                        "gland_orientation": "face",
                        "gland_shape": "loop",
                    },
                )
            )

        return found

    @staticmethod
    def _collect_loop_walls(
        graph: AttributedAdjacencyGraph,
        ring: AagNode,
        floor_normal,
        walls: list[AagNode],
        corners: list[AagNode],
    ) -> bool:
        """Sort a ring floor's neighbours into walls and corner arcs.

        Returns False the moment something turns up that a channel cannot
        have: a face parallel to the floor means a step rather than a channel,
        and a cylinder lying across the axis is a bore passing through.
        """
        for neighbour, _ in neighbours(graph, ring.face_id):
            if neighbour.surface_type is SurfaceType.PLANE:
                normal = neighbour.outward_normal
                if normal is None or abs(normal.Dot(floor_normal)) >= 0.3:
                    return False
                walls.append(neighbour)
            elif neighbour.surface_type is SurfaceType.CYLINDER:
                if neighbour.cyl_cone_axis is None:
                    return False
                if abs(neighbour.cyl_cone_axis.Direction().Dot(floor_normal)) <= 0.95:
                    return False
                corners.append(neighbour)
            else:
                return False
        return True


# -- shared geometry ---------------------------------------------------------


def _axis_tuple(direction) -> tuple[float, float, float]:
    return (
        round(direction.X(), 6),
        round(direction.Y(), 6),
        round(direction.Z(), 6),
    )


def _full_ring(node: AagNode) -> bool:
    """Whether a cylinder is a near-complete revolution.

    Measured off the bounding box rather than the parametric range, because a
    ring produced by a boolean often arrives as several faces sharing a seam.
    """
    if node.bbox.IsVoid() or node.cyl_cone_axis is None:
        return False
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    spans = (xmax - xmin, ymax - ymin, zmax - zmin)
    axis = node.cyl_cone_axis.Direction()
    components = (abs(axis.X()), abs(axis.Y()), abs(axis.Z()))
    axial = components.index(max(components))
    return all(spans[i] >= 1.9 * node.cyl_radius for i in range(3) if i != axial)


def _paired_width(walls: list[AagNode]) -> Optional[tuple[float, float]]:
    """The narrowest and widest gap between facing wall pairs.

    Every wall has to face another across the channel. One that does not is
    looking out of the part, which means the loop is not closed.
    """
    smallest = float("inf")
    largest = 0.0
    for wall in walls:
        normal = wall.outward_normal
        if normal is None:
            return None
        nearest = float("inf")
        for other in walls:
            if other is wall:
                continue
            other_normal = other.outward_normal
            if other_normal is None or normal.Dot(other_normal) > -0.9:
                continue
            offset = gp_Vec(wall.centroid, other.centroid)
            nearest = min(nearest, abs(offset.Dot(gp_Vec(normal))))
        if nearest == float("inf"):
            return None
        smallest = min(smallest, nearest)
        largest = max(largest, nearest)
    return smallest, largest


def _extent_along(node: AagNode, direction: gp_Dir) -> float:
    """How far a face reaches along a direction, from its bounding box."""
    if node.bbox.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    return (
        abs((xmax - xmin) * direction.X())
        + abs((ymax - ymin) * direction.Y())
        + abs((zmax - zmin) * direction.Z())
    )
