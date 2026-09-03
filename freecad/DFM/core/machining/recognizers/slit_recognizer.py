# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes flexure slits.

A slit is a channel that runs clean out of the part at both ends. What
separates it from a slot is not its shape but where it stops: a slot ends
against material, a slit does not end at all. That matters because a narrow
deep slit is cut with a slitting saw or wire, not milled to a floor, and
because a slit deep enough relative to its width is a flexure -- the part is
meant to bend there.

Three shapes get here, and none of them is served by the slot recognizer.

The first has a floor: two facing walls rising from a coplanar band that runs
off both ends. The second has none. A pinch slit that penetrates its host wall
completely is two parallel faces and nothing else, so there is no floor to
seed from and the pair has to be found directly. The third is a V-groove: two
tilted walls meeting along a concave apex line, with neither a parallel pair
nor a floor between them.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Dir, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer, neighbours


# A wall stands steeply enough out of the floor. Above this the neighbour is a
# ledge lying alongside the floor rather than rising from it, and a ledge is
# not slot topology.
_WALL_PERPENDICULAR_MAX_DOT = 0.6

# The two walls must face each other. Loose enough that a dovetail's tilt
# still passes.
_FACING_WALLS_MAX_DOT = -0.7

# Below this the walls are a machining seam, above it a chamber rather than a
# channel.
_MIN_WIDTH_MM = 0.3
_MAX_WIDTH_MM = 60.0

# Coplanar to within this counts as the same floor continuing across a
# Boolean seam.
_COPLANAR_MIN_DOT = 0.99

# A concave planar neighbour facing along the run is an end wall: the channel
# stops there and belongs to the slot recognizer instead.
_END_WALL_MIN_DOT = 0.8

# A concave cylinder standing on the floor normal is a milled end radius when
# it is no wider than the channel. A larger one is a crossing bore the slit
# runs out into, which leaves the slit open.
_ROUND_END_AXIS_MIN_DOT = 0.9
_ROUND_END_MAX_RADIUS_SHARE = 0.75

# A face directly in front of the floor, anti-parallel to it and overlapping
# its footprint, caps the supposed opening.
_CAP_ANTI_PARALLEL_MIN_DOT = -0.9
_CAP_MIN_CLEARANCE_MM = 0.1
_CAP_MAX_DISTANCE_WIDTHS = 4.0
_CAP_MIN_OVERLAP_SHARE = 0.5

# Shallower than this is a surface break, not a channel.
_MIN_DEPTH_MM = 0.5

# Both walls leaning back over the floor is a dovetail: the channel is wider
# at the bottom than at its opening, so it has to be broached or wire-cut.
# Parallel walls read at zero.
_REENTRANT_MAX_DOT = -0.12

# A slit this narrow, and this deep for its width, is a flexure. Mirrors
# `flexure_slit_max_width_mm` and `flexure_slit_min_depth_ratio` in the
# machining thresholds -- recognizers are not handed the config.
# Defaults only: these duplicate the shared thresholds of the same name, so
# a shop that has set its own wire or saw kerf gets that instead.
_FLEXURE_MAX_WIDTH_MM = 4.0
_FLEXURE_MIN_DEPTH_RATIO = 3.0

# A full-penetration pair has to be strictly parallel: there is no floor to
# corroborate it, so the pairing itself carries all the evidence.
_PENETRATION_WALLS_MAX_DOT = -0.98

# The two walls of a slit are near-congruent, well-filled rectangles. A drill
# cap facing a large host face, a thread-relief shoulder and a partial-annulus
# wedge all pass the bounding-box arithmetic and die on these.
_WALL_AREA_RATIO_MAX = 3.0
_WALL_FILL_MIN = 0.5

# A planar face shared by both walls, no wider than this across the gap, is
# the floor or an end wall -- so the pair is not a full penetration. The face
# both walls exit through extends far beyond the slab and does not count.
_SHARED_FLOOR_MAX_WIDTHS = 1.5

# V-groove apex angles. Ordinary cavity corners are right angles and drafted
# walls sit within a few degrees of one, so that band is excluded outright.
_V_APEX_MIN_DEG = 15.0
_V_APEX_MAX_DEG = 120.0
_V_RIGHT_CORNER_MIN_DEG = 82.0
_V_RIGHT_CORNER_MAX_DEG = 98.0

# Groove-scale slant. A 45 degree seat wider than this is an optical mirror
# face, not a groove.
_V_MAX_SLANT_MM = 8.0
_V_MIN_SLANT_MM = 0.02

# A groove runs; a chamfered corner does not.
_V_MIN_RUN_TO_WIDTH = 3.0


class SlitRecognizer(FeatureRecognizer):
    """Recognizes flexure slits."""

    prefix = "sl"

    @property
    def name(self) -> str:
        return "Slit Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored. A flexure slit with a floor is
        # read as a slot by the pocket and slot passes, which run first, so
        # standing down on claimed faces means the slit reading never happens
        # -- and a slit is the more specific answer: it is cut with a saw or
        # a wire, not an end mill, and a rule says so.
        #
        # The resolver settles the overlap, and its priority table already
        # ranks the slit family above both slots and pockets for exactly this
        # reason. Honouring the claim set here defeated that ordering before
        # it ever ran.
        taken: set[int] = set()

        found = self._floored_slits(graph, taken)
        found.extend(self._penetrating_slits(graph, found, taken))
        found.extend(self._v_grooves(graph, found, taken))

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- floored slits ------------------------------------------------------

    def _floored_slits(
        self, graph: AttributedAdjacencyGraph, taken: set[int]
    ) -> list[FeatureInstance]:
        found: list[FeatureInstance] = []

        for floor in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if floor.face_id in taken:
                continue
            up = floor.outward_normal  # floor toward the opening
            if up is None:
                continue

            walls = self._wall_pair(graph, floor, up)
            if walls is None:
                continue
            first, second = walls

            normal_a = first.outward_normal
            normal_b = second.outward_normal
            if normal_a is None or normal_b is None:
                continue
            if normal_a.Dot(normal_b) > _FACING_WALLS_MAX_DOT:
                continue

            across = gp_Vec(first.centroid, second.centroid).Dot(gp_Vec(normal_a))
            if across <= 0.0:
                continue  # the first wall must face the second across the gap
            width = abs(across)
            if width < _MIN_WIDTH_MM or width > _MAX_WIDTH_MM:
                continue

            run_vector = gp_Vec(normal_a).Crossed(gp_Vec(up))
            if run_vector.Magnitude() < 1e-6:
                continue
            run = gp_Dir(run_vector.XYZ())

            # A Boolean seam splits one long floor into coplanar segments, and
            # whether the channel runs out is a property of the whole floor,
            # not of the segment that seeded it -- the real end wall can sit
            # on a different segment.
            group = self._floor_group(graph, floor, up)
            if min(group) != floor.face_id:
                continue  # each group seeds once, from its lowest face

            if self._has_closed_end(graph, group, up, run, width):
                continue
            if self._is_capped_above(graph, floor, up, normal_a, run, width):
                continue

            # Depth from centroids rather than bounding boxes. A wall's
            # world-aligned box inflates as soon as the channel is rotated off
            # a cardinal, while a swept wall's centroid sits at exactly
            # mid-depth however it is turned.
            depth = 2.0 * max(
                gp_Vec(floor.centroid, first.centroid).Dot(gp_Vec(up)),
                gp_Vec(floor.centroid, second.centroid).Dot(gp_Vec(up)),
            )
            low = min(_interval_along(graph.node(f), run)[0] for f in group)
            high = max(_interval_along(graph.node(f), run)[1] for f in group)
            length = high - low
            if depth < _MIN_DEPTH_MM or length < width:
                continue

            reentrant = (
                normal_a.Dot(up) < _REENTRANT_MAX_DOT
                and normal_b.Dot(up) < _REENTRANT_MAX_DOT
            )
            found.append(
                FeatureInstance(
                    instance_id=self.instance_id(0),
                    type=self._floored_type(reentrant, width, depth),
                    faces=[floor.face_id, first.face_id, second.face_id],
                    parameters={
                        "width_mm": round(width, 6),
                        "depth_mm": round(depth, 6),
                        "length_mm": round(length, 6),
                        "through_both_ends": True,
                        "slit_profile": "reentrant" if reentrant else "parallel",
                    },
                )
            )

        return self._merge_by_wall_pair(found)

    def _floored_type(self, reentrant: bool, width: float, depth: float) -> str:
        if reentrant:
            return FeatureType.BROACHED_SLOT
        if width <= self._max_width() and depth / width >= self._min_depth_ratio():
            return FeatureType.FLEXURE_SLIT
        return FeatureType.SLOT

    def _max_width(self) -> float:
        return self.threshold("flexure_slit_max_width_mm", _FLEXURE_MAX_WIDTH_MM)

    def _min_depth_ratio(self) -> float:
        return self.threshold(
            "flexure_slit_min_depth_ratio", _FLEXURE_MIN_DEPTH_RATIO
        )

    @staticmethod
    def _wall_pair(
        graph: AttributedAdjacencyGraph, floor: AagNode, up: gp_Dir
    ) -> Optional[tuple[AagNode, AagNode]]:
        """The floor's two steep concave planar neighbours, or None.

        A near-parallel concave plane alongside the floor is a ledge, and a
        ledge disqualifies the seed outright. That is also what stops a slot
        *wall* seeding: seen as a floor, the opposing wall turns up here as a
        ledge.
        """
        walls: dict[int, AagNode] = {}
        for other, edge in neighbours(graph, floor.face_id):
            if edge.concavity is not Concavity.CONCAVE:
                continue
            if other.surface_type is not SurfaceType.PLANE:
                continue
            normal = other.outward_normal
            if normal is None:
                continue
            if abs(normal.Dot(up)) >= _WALL_PERPENDICULAR_MAX_DOT:
                return None  # a ledge, not a channel
            walls.setdefault(other.face_id, other)

        if len(walls) != 2:
            return None
        first, second = (walls[key] for key in sorted(walls))
        return (first, second)

    @staticmethod
    def _floor_group(
        graph: AttributedAdjacencyGraph, floor: AagNode, up: gp_Dir
    ) -> list[int]:
        """The floor and every coplanar segment tangent to it."""
        group = [floor.face_id]
        inside = {floor.face_id}
        index = 0
        while index < len(group):
            current = group[index]
            index += 1
            for other, edge in neighbours(graph, current):
                if not edge.is_tangent:
                    continue
                if other.surface_type is not SurfaceType.PLANE:
                    continue
                if other.face_id in inside:
                    continue
                normal = other.outward_normal
                if normal is None or abs(normal.Dot(up)) < _COPLANAR_MIN_DOT:
                    continue
                inside.add(other.face_id)
                group.append(other.face_id)
        return sorted(group)

    @staticmethod
    def _has_closed_end(
        graph: AttributedAdjacencyGraph,
        group: list[int],
        up: gp_Dir,
        run: gp_Dir,
        width: float,
    ) -> bool:
        """Whether anything stops the channel at either end."""
        for face_id in group:
            for other, edge in neighbours(graph, face_id):
                if edge.concavity is not Concavity.CONCAVE:
                    continue
                if other.surface_type is SurfaceType.PLANE:
                    normal = other.outward_normal
                    if normal is not None and abs(normal.Dot(run)) > _END_WALL_MIN_DOT:
                        return True
                elif other.surface_type is SurfaceType.CYLINDER:
                    axis = other.cyl_cone_axis
                    if axis is None:
                        continue
                    if (
                        abs(axis.Direction().Dot(up)) > _ROUND_END_AXIS_MIN_DOT
                        and other.cyl_radius <= _ROUND_END_MAX_RADIUS_SHARE * width
                    ):
                        return True
        return False

    @staticmethod
    def _is_capped_above(
        graph: AttributedAdjacencyGraph,
        floor: AagNode,
        up: gp_Dir,
        across: gp_Dir,
        run: gp_Dir,
        width: float,
    ) -> bool:
        """Whether something closes the opening the channel is supposed to have.

        A blind rectangular shaft presents each of its four walls as a floor
        whose adjacent wall pair opposes correctly and whose run is open both
        ways. The tell is the face directly opposite: it mirrors the
        footprint and roofs the supposed opening over.
        """
        floor_across = _interval_along(floor, across)
        floor_run = _interval_along(floor, run)
        across_extent = floor_across[1] - floor_across[0]
        run_extent = floor_run[1] - floor_run[0]

        for node in graph.nodes:
            if node.surface_type is not SurfaceType.PLANE:
                continue
            if node.face_id == floor.face_id:
                continue
            normal = node.outward_normal
            if normal is None or normal.Dot(up) > _CAP_ANTI_PARALLEL_MIN_DOT:
                continue
            ahead = gp_Vec(floor.centroid, node.centroid).Dot(gp_Vec(up))
            if ahead < _CAP_MIN_CLEARANCE_MM:
                continue
            if ahead > _CAP_MAX_DISTANCE_WIDTHS * width + 1.0:
                continue
            if (
                _overlap(floor_across, _interval_along(node, across))
                >= _CAP_MIN_OVERLAP_SHARE * across_extent
                and _overlap(floor_run, _interval_along(node, run))
                >= _CAP_MIN_OVERLAP_SHARE * run_extent
            ):
                return True
        return False

    @staticmethod
    def _merge_by_wall_pair(found: list[FeatureInstance]) -> list[FeatureInstance]:
        """Fold together the two readings of one embedded slit.

        A through-cut slit sunk in material has an end face at each end, and
        both qualify as the seed floor for the same wall pair. That is one
        slit described twice: the merged feature owns both end faces and the
        walls, and the dimensions are identical by construction.
        """
        merged: list[FeatureInstance] = []
        homes: dict[tuple[int, int], FeatureInstance] = {}
        for feature in found:
            key = (min(feature.faces[1:3]), max(feature.faces[1:3]))
            home = homes.get(key)
            if home is None:
                homes[key] = feature
                merged.append(feature)
                continue
            for face_id in feature.faces:
                if face_id not in home.faces:
                    home.faces.append(face_id)
        return merged

    # -- full-penetration slits ---------------------------------------------

    def _penetrating_slits(
        self,
        graph: AttributedAdjacencyGraph,
        found: list[FeatureInstance],
        taken: set[int],
    ) -> list[FeatureInstance]:
        """Slits that go right through their host wall, so have no floor.

        Nothing but the pair itself is left to recognize: two parallel faces
        looking at each other across a gap, opening out of the part on every
        side. The gates are all that keep the pairing honest.
        """
        used = set(taken)
        for feature in found:
            used.update(feature.faces)

        planes = [
            node
            for node in graph.nodes_by_surface_type(SurfaceType.PLANE)
            if node.outward_normal is not None
        ]
        results: list[FeatureInstance] = []

        for index, first in enumerate(planes):
            if first.face_id in used:
                continue
            normal_a = first.outward_normal
            for second in planes[index + 1 :]:
                if second.face_id in used:
                    continue
                normal_b = second.outward_normal
                if normal_a.Dot(normal_b) > _PENETRATION_WALLS_MAX_DOT:
                    continue
                gap = gp_Vec(first.centroid, second.centroid).Dot(gp_Vec(normal_a))
                if gap <= _MIN_WIDTH_MM or gap > self._max_width():
                    continue

                across, along = _in_plane_frame(normal_a)
                first_across = _interval_along(first, across)
                first_along = _interval_along(first, along)
                second_across = _interval_along(second, across)
                second_along = _interval_along(second, along)
                overlap_across = _overlap(first_across, second_across)
                overlap_along = _overlap(first_along, second_along)
                # Deep in both in-plane directions: a shallow reveal between
                # two plates is not a slit.
                if overlap_across < self._min_depth_ratio() * gap:
                    continue
                if overlap_along < self._min_depth_ratio() * gap:
                    continue

                if not self._walls_are_congruent(
                    first, second, first_across, first_along, second_across, second_along
                ):
                    continue
                if self._shares_a_floor(graph, first, second, normal_a, gap):
                    continue

                results.append(
                    FeatureInstance(
                        instance_id=self.instance_id(0),
                        type=FeatureType.FLEXURE_SLIT,
                        faces=[first.face_id, second.face_id],
                        parameters={
                            "width_mm": round(gap, 6),
                            "depth_mm": round(min(overlap_across, overlap_along), 6),
                            "length_mm": round(max(overlap_across, overlap_along), 6),
                            "through_both_ends": True,
                            "full_penetration": True,
                            "slit_profile": "parallel",
                        },
                    )
                )
                used.add(first.face_id)
                used.add(second.face_id)
                break  # the first wall is spoken for

        return results

    @staticmethod
    def _walls_are_congruent(
        first: AagNode,
        second: AagNode,
        first_across: tuple[float, float],
        first_along: tuple[float, float],
        second_across: tuple[float, float],
        second_along: tuple[float, float],
    ) -> bool:
        """Whether the pair really is two sides of one cut."""
        if first.area > _WALL_AREA_RATIO_MAX * second.area:
            return False
        if second.area > _WALL_AREA_RATIO_MAX * first.area:
            return False
        first_fill = first.area / max(
            1e-9,
            (first_across[1] - first_across[0]) * (first_along[1] - first_along[0]),
        )
        second_fill = second.area / max(
            1e-9,
            (second_across[1] - second_across[0]) * (second_along[1] - second_along[0]),
        )
        return first_fill >= _WALL_FILL_MIN and second_fill >= _WALL_FILL_MIN

    @staticmethod
    def _shares_a_floor(
        graph: AttributedAdjacencyGraph,
        first: AagNode,
        second: AagNode,
        across: gp_Dir,
        gap: float,
    ) -> bool:
        """Whether a face shared by both walls closes the cut.

        A planar neighbour of both, narrow enough across the gap to be the
        floor or an end wall, means the cut stops -- either it is closed, or
        it has a floor and the floored pass already owns it. The face the two
        walls exit through runs far past the slab and is no evidence.
        """
        shared = {
            edge.other_face(first.face_id) for edge in graph.edges_of(first.face_id)
        } & {edge.other_face(second.face_id) for edge in graph.edges_of(second.face_id)}
        for face_id in sorted(shared):
            if not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            if _extent_along(node, across) <= _SHARED_FLOOR_MAX_WIDTHS * gap:
                return True
        return False

    # -- V-grooves ----------------------------------------------------------

    def _v_grooves(
        self,
        graph: AttributedAdjacencyGraph,
        found: list[FeatureInstance],
        taken: set[int],
    ) -> list[FeatureInstance]:
        """Two tilted walls meeting along a concave apex line.

        A fibre-alignment groove or a machine way: no parallel pair and no
        floor, so neither pass above can see it. It is the apex that gives it
        away, and the apex angle that separates it from the ordinary
        right-angled corner where a wall meets a floor.
        """
        used = set(taken)
        for feature in found:
            used.update(feature.faces)

        results: list[FeatureInstance] = []
        for edge in sorted(graph.edges, key=lambda e: (e.face_id_a, e.face_id_b)):
            if edge.concavity is not Concavity.CONCAVE:
                continue
            if edge.edge_curve_type != "line":
                continue
            if not graph.has_node(edge.face_id_a) or not graph.has_node(edge.face_id_b):
                continue
            first = graph.node(edge.face_id_a)
            second = graph.node(edge.face_id_b)
            if (
                first.surface_type is not SurfaceType.PLANE
                or second.surface_type is not SurfaceType.PLANE
            ):
                continue
            if first.face_id in used or second.face_id in used:
                continue

            normal_a = first.outward_normal
            normal_b = second.outward_normal
            if normal_a is None or normal_b is None:
                continue
            apex_deg = 180.0 - math.degrees(
                math.acos(max(-1.0, min(1.0, normal_a.Dot(normal_b))))
            )
            if apex_deg < _V_APEX_MIN_DEG or apex_deg > _V_APEX_MAX_DEG:
                continue
            if _V_RIGHT_CORNER_MIN_DEG < apex_deg < _V_RIGHT_CORNER_MAX_DEG:
                continue
            if first.area > _WALL_AREA_RATIO_MAX * second.area:
                continue
            if second.area > _WALL_AREA_RATIO_MAX * first.area:
                continue

            run_vector = gp_Vec(normal_a).Crossed(gp_Vec(normal_b))
            if run_vector.Magnitude() < 1e-6:
                continue
            run = gp_Dir(run_vector.XYZ())
            slant_a = _extent_along(
                first, gp_Dir(gp_Vec(run).Crossed(gp_Vec(normal_a)).XYZ())
            )
            slant_b = _extent_along(
                second, gp_Dir(gp_Vec(run).Crossed(gp_Vec(normal_b)).XYZ())
            )
            if slant_a > _V_MAX_SLANT_MM or slant_b > _V_MAX_SLANT_MM:
                continue
            if slant_a < _V_MIN_SLANT_MM or slant_b < _V_MIN_SLANT_MM:
                continue

            slant = 0.5 * (slant_a + slant_b)
            half = math.radians(apex_deg) * 0.5
            width = 2.0 * slant * math.sin(half)
            depth = slant * math.cos(half)
            length = min(_extent_along(first, run), _extent_along(second, run))
            if length < _V_MIN_RUN_TO_WIDTH * width:
                continue

            results.append(
                FeatureInstance(
                    instance_id=self.instance_id(0),
                    type=FeatureType.V_GROOVE,
                    faces=[first.face_id, second.face_id],
                    parameters={
                        "apex_angle_deg": round(apex_deg, 6),
                        "width_mm": round(width, 6),
                        "depth_mm": round(depth, 6),
                        "length_mm": round(length, 6),
                        "groove_profile": "v",
                    },
                )
            )
            used.add(first.face_id)
            used.add(second.face_id)

        return results


# =============================================================================
# Geometry helpers
# =============================================================================


def _interval_along(node: AagNode, direction: gp_Dir) -> tuple[float, float]:
    """The span of a face's bounding box projected onto a direction."""
    if node.bbox.IsVoid():
        return (0.0, 0.0)
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    values = [
        x * direction.X() + y * direction.Y() + z * direction.Z()
        for x in (xmin, xmax)
        for y in (ymin, ymax)
        for z in (zmin, zmax)
    ]
    return (min(values), max(values))


def _extent_along(node: AagNode, direction: gp_Dir) -> float:
    low, high = _interval_along(node, direction)
    return high - low


def _overlap(first: tuple[float, float], second: tuple[float, float]) -> float:
    """How much two intervals share; negative when they are apart."""
    return min(first[1], second[1]) - max(first[0], second[0])


def _in_plane_frame(normal: gp_Dir) -> tuple[gp_Dir, gp_Dir]:
    """Two directions spanning the plane a normal stands on."""
    seed = gp_Dir(1, 0, 0) if abs(normal.X()) < 0.9 else gp_Dir(0, 1, 0)
    across = gp_Dir(gp_Vec(normal).Crossed(gp_Vec(seed)).XYZ())
    along = gp_Dir(gp_Vec(normal).Crossed(gp_Vec(across)).XYZ())
    return (across, along)
