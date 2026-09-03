# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes cavities open at both ends.

A through cavity is a window cut clean through the part: a T-slot, an L-slot,
a cross, a wire-EDM profile. It has no floor, so nothing anchors it the way a
pocket floor anchors a pocket, and the only thing that says it goes all the
way is that its walls terminate against the outside of the part at both ends
of one axis.

That is the whole test. Grow the connected band of wall faces, collect the
outward normals of everything the band runs out onto, and look for an axis
with terminations on both signs. A blind pocket has them on one sign only.

Plain rectangular windows are deliberately left alone: four interior corners
and no corner radii is a shape the slot and pocket recognizers already read
well. This recognizer exists for the profiles they fragment -- the ones with
six or eight corners, where a per-face reading loses the sense that it is one
window.
"""

from __future__ import annotations

from collections import deque
from typing import Optional, Sequence

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer, neighbours


# A face bigger than this share of the part is the surface the cavity was cut
# into, not one of its walls.
_MAX_WALL_AREA_SHARE = 0.15

# A step face -- the ledge where a taper relief meets its land -- is small
# next to the walls it separates.
_STEP_FACE_MAX_AREA_SHARE = 0.5

# A step normal is always on a machine cardinal: the transitions real
# machining produces are axis-aligned.
_STEP_FACE_CARDINAL_MIN = 0.95

# How close to the part envelope a face has to sit to count as on the outside
# of it. Face bounding boxes on curved surfaces pad roughly a tenth of a
# millimetre past the true extent, so the aggregated part box runs slightly
# proud of the block outline; a tight tolerance misses genuine outer faces.
_PART_OUTER_TOLERANCE_MM = 0.5

# A normal counts as lying on an axis above this.
_CARDINAL_ALIGNMENT_MIN = 0.9

# A wall stands square to the through axis.
_WALL_PERPENDICULAR_MAX_DOT = 0.3

# The corner cylinder of a rounded window has its axis lying in the wall it
# blends into. A bore crossing that wall would not.
_CORNER_FILLET_AXIS_MAX_DOT = 0.3

# A rectangle is four walls. Fewer is not a closed profile.
_MIN_WALLS = 4

# A cavity that grew to one or two faces is a stray seed, not a profile.
_MIN_CAVITY_FACES = 3

# Two walls face each other when their outward normals oppose this closely.
_ANTI_PARALLEL_MAX_DOT = -0.9

# Below this a wall pair is coplanar rather than facing.
_MIN_GAP_MM = 0.001

# A cavity whose footprint covers this much of the part in both directions
# across the through axis is the part's own silhouette, mis-grown.
_OUTER_SILHOUETTE_SPAN_SHARE = 0.85

# Four sharp corners is a plain rectangular window; the slot and pocket
# recognizers serve it better.
_MAX_RECTANGULAR_CORNERS = 4


class ThroughCavityRecognizer(FeatureRecognizer):
    """Recognizes cavities open at both ends."""

    prefix = "tc"

    @property
    def name(self) -> str:
        return "Through Cavity Recognizer"

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

        # What earlier recognizers claimed is deliberately not consulted. A
        # T-slot arrives here already broken into a slot and a step or two,
        # and the point of the feature is to name the window those pieces
        # belong to; refusing to seed on a claimed wall would leave nothing
        # to seed on. The four-corner test below is what keeps the overlap
        # narrow -- anything the slot recognizer reads cleanly is skipped.
        area_cap = total_area * _MAX_WALL_AREA_SHARE
        part_box = _combined_box(graph.node(n.face_id) for n in graph.nodes)

        visited: set[int] = set()
        found: list[FeatureInstance] = []

        for seed in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if seed.face_id in visited:
                continue
            if not self._may_seed(graph, seed, area_cap, part_box):
                continue

            faces, boundary_normals = self._grow(graph, seed, area_cap, part_box)
            if len(faces) < _MIN_CAVITY_FACES:
                visited.add(seed.face_id)
                continue

            visited.update(faces)
            feature = self._describe(graph, faces, boundary_normals, part_box)
            if feature is not None:
                found.append(feature)

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- seeding ------------------------------------------------------------

    def _may_seed(
        self,
        graph: AttributedAdjacencyGraph,
        seed: AagNode,
        area_cap: float,
        part_box: Bnd_Box,
    ) -> bool:
        """Whether this face can be the start of a cavity wall band."""
        # Large faces and faces carrying inner loops are the surfaces things
        # were cut into. Growing from one walks the block's outer sides.
        if seed.area > area_cap or seed.inner_loop_count > 0:
            return False
        if _is_part_outer(seed, part_box):
            return False

        # A cavity wall meets its neighbours at real interior corners. A boss
        # wall or a chamfer is convex nearly all round -- the material sticks
        # outward -- and would otherwise grow a phantom cavity around a
        # protrusion.
        concave = 0
        corner_radii = 0
        for other, edge in neighbours(graph, seed.face_id):
            if edge.concavity is Concavity.CONCAVE:
                concave += 1
            elif edge.concavity is Concavity.TANGENT or edge.is_tangent:
                # A rounded window's wall meets its corner radii tangentially
                # rather than concavely, and its edges to the faces it opens
                # through are convex rims. Such a wall can have no concave
                # edge at all, so two tangent corner radii are evidence in
                # their own right.
                if other.surface_type is SurfaceType.CYLINDER and other.is_internal:
                    corner_radii += 1
        return concave >= 2 or corner_radii >= 2

    # -- growth -------------------------------------------------------------

    def _grow(
        self,
        graph: AttributedAdjacencyGraph,
        seed: AagNode,
        area_cap: float,
        part_box: Bnd_Box,
    ) -> tuple[list[int], list[gp_Dir]]:
        """Collect the wall band and the normals of what it terminates against.

        Two passes. The first walks genuine interior corners only, which
        gathers the sub-cavity around the seed. The second re-runs from the
        step faces it found, this time crossing their convex edges as well:
        a step at a land-to-relief transition meets the walls above it at an
        external corner, so a concave-only walk stops dead there even though
        both sections belong to the same window.
        """
        collected = {seed.face_id}
        boundary_normals: list[gp_Dir] = []

        queue: deque[int] = deque([seed.face_id])
        self._walk(graph, queue, collected, boundary_normals, area_cap, part_box, False)

        steps = [
            face_id
            for face_id in sorted(collected)
            if self._is_step_face(graph.node(face_id), area_cap)
        ]
        queue = deque(steps)
        self._walk(graph, queue, collected, boundary_normals, area_cap, part_box, True)

        return (sorted(collected), boundary_normals)

    def _walk(
        self,
        graph: AttributedAdjacencyGraph,
        queue: deque[int],
        collected: set[int],
        boundary_normals: list[gp_Dir],
        area_cap: float,
        part_box: Bnd_Box,
        cross_steps: bool,
    ) -> None:
        while queue:
            current = queue.popleft()
            here = graph.node(current)
            crossing = cross_steps and here.surface_type is SurfaceType.PLANE

            for other, edge in neighbours(graph, current):
                if other.face_id in collected:
                    continue

                if edge.concavity is Concavity.TANGENT or edge.is_tangent:
                    if self._may_chain_tangentially(here, other, area_cap, part_box):
                        collected.add(other.face_id)
                        queue.append(other.face_id)
                    continue

                if edge.concavity is Concavity.CONVEX:
                    # The far side of a convex edge is where the cavity runs
                    # out of the part. Record which way it faces even when the
                    # walk does not go there -- that is the evidence the
                    # through-axis test lives on.
                    normal = other.outward_normal
                    if normal is not None:
                        boundary_normals.append(normal)
                    if crossing and self._may_absorb(other, area_cap, part_box):
                        collected.add(other.face_id)
                        queue.append(other.face_id)
                    continue

                if edge.concavity is not Concavity.CONCAVE:
                    continue
                if other.surface_type is not SurfaceType.PLANE:
                    continue
                if not self._may_absorb(other, area_cap, part_box):
                    # A neighbour filtered out is still a boundary: it is
                    # where the cavity stops. Record its facing.
                    normal = other.outward_normal
                    if normal is not None:
                        boundary_normals.append(normal)
                    continue
                collected.add(other.face_id)
                queue.append(other.face_id)

    @staticmethod
    def _may_absorb(node: AagNode, area_cap: float, part_box: Bnd_Box) -> bool:
        """Whether a planar face is a wall rather than something the cavity
        was cut into."""
        if node.surface_type is not SurfaceType.PLANE:
            return False
        if node.area > area_cap or node.inner_loop_count > 0:
            return False
        return not _is_part_outer(node, part_box)

    @classmethod
    def _may_chain_tangentially(
        cls, here: AagNode, other: AagNode, area_cap: float, part_box: Bnd_Box
    ) -> bool:
        """Whether a tangent edge continues the same wall band.

        Two configurations qualify. A wall meets its corner radius
        tangentially, so the cylinder is admitted when it is concave and its
        axis lies *in* the wall -- a bore crossing the wall would meet it at a
        sharp circular edge instead, never tangentially. And a wall split by a
        Boolean seam continues into its other half.
        """
        if (
            here.surface_type is SurfaceType.PLANE
            and other.surface_type is SurfaceType.CYLINDER
            and other.is_internal
            and other.cyl_cone_axis is not None
        ):
            wall_normal = here.outward_normal
            if wall_normal is None:
                return False
            axis = other.cyl_cone_axis.Direction()
            return abs(axis.Dot(wall_normal)) < _CORNER_FILLET_AXIS_MAX_DOT

        if here.surface_type in (SurfaceType.CYLINDER, SurfaceType.PLANE):
            return cls._may_absorb(other, area_cap, part_box)
        return False

    @staticmethod
    def _is_step_face(node: AagNode, area_cap: float) -> bool:
        """A small cardinal ledge, the sort a taper relief steps down at."""
        if node.surface_type is not SurfaceType.PLANE:
            return False
        normal = node.outward_normal
        if normal is None:
            return False
        if node.area > area_cap * _STEP_FACE_MAX_AREA_SHARE:
            return False
        cardinal = max(abs(normal.X()), abs(normal.Y()), abs(normal.Z()))
        return cardinal >= _STEP_FACE_CARDINAL_MIN

    # -- measurement --------------------------------------------------------

    def _describe(
        self,
        graph: AttributedAdjacencyGraph,
        faces: list[int],
        boundary_normals: list[gp_Dir],
        part_box: Bnd_Box,
    ) -> Optional[FeatureInstance]:
        axis = self._through_axis(graph, faces, boundary_normals)
        if axis is None:
            return None

        walls = self._walls(graph, faces, axis)
        if len(walls) < _MIN_WALLS:
            return None

        gaps = self._facing_gaps(walls)
        if not gaps:
            return None

        # Extents come from the cavity's own box, not the part's. Measuring
        # the depth across the part reports the length of the block whenever
        # its long dimension happens to lie on the chosen axis, which turns a
        # 13 mm window in a plate into a 151 mm shaft.
        cavity_box = _combined_box(graph.node(face_id) for face_id in faces)
        extents = _box_extents(cavity_box)
        depth = (
            abs(axis.X()) * extents[0]
            + abs(axis.Y()) * extents[1]
            + abs(axis.Z()) * extents[2]
        )

        if self._spans_the_part(extents, _box_extents(part_box), axis):
            return None

        corners = self._count_corners(graph, faces)
        radii = [
            node.cyl_radius
            for node in (graph.node(face_id) for face_id in faces)
            if node.surface_type is SurfaceType.CYLINDER
        ]

        # A window with four sharp corners and no radii is an ordinary
        # rectangular cut, and the slot and pocket recognizers describe it
        # better. Rounded windows are the exception: with no floor to seed
        # from, nothing else serves them at all.
        if corners <= _MAX_RECTANGULAR_CORNERS and not radii:
            return None

        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.THROUGH_CAVITY,
            faces=list(faces),
            parameters={
                "through_axis": (
                    round(axis.X(), 6),
                    round(axis.Y(), 6),
                    round(axis.Z(), 6),
                ),
                "depth_mm": round(depth, 6),
                "min_width_mm": round(gaps[0], 6),
                "max_width_mm": round(gaps[-1], 6),
                # The same two numbers under the names the slot rules read, so
                # a through cavity is measured by them without a special case.
                "width_mm": round(gaps[0], 6),
                "length_mm": round(gaps[-1], 6),
                "is_open": True,
                "corner_radius_mm": round(min(radii), 6) if radii else 0.0,
                "corner_count": corners + len(radii),
                "has_step_transitions": self._has_steps(graph, faces, axis),
                "face_count": len(faces),
            },
        )

    @staticmethod
    def _through_axis(
        graph: AttributedAdjacencyGraph,
        faces: list[int],
        boundary_normals: list[gp_Dir],
    ) -> Optional[gp_Dir]:
        """The axis the cavity runs along, or None if it is blind.

        Every wall of a window stands parallel to the direction the window
        runs, so the run is the axis with the most walls square to it. It only
        counts as a run if the band terminates against the part at both ends:
        a blind pocket has an opening on one side only, and its walls are
        perpendicular to the same axis.
        """
        best_axis: Optional[gp_Dir] = None
        best_count = 0
        for axis in (gp_Dir(1, 0, 0), gp_Dir(0, 1, 0), gp_Dir(0, 0, 1)):
            count = 0
            for face_id in faces:
                node = graph.node(face_id)
                if node.surface_type is not SurfaceType.PLANE:
                    continue
                normal = node.outward_normal
                if normal is not None and abs(normal.Dot(axis)) < _WALL_PERPENDICULAR_MAX_DOT:
                    count += 1
            if count < _MIN_WALLS:
                continue

            positive = any(n.Dot(axis) > _CARDINAL_ALIGNMENT_MIN for n in boundary_normals)
            negative = any(n.Dot(axis) < -_CARDINAL_ALIGNMENT_MIN for n in boundary_normals)
            if not (positive and negative):
                continue
            if count > best_count:
                best_count = count
                best_axis = axis
        return best_axis

    @staticmethod
    def _walls(
        graph: AttributedAdjacencyGraph, faces: list[int], axis: gp_Dir
    ) -> list[AagNode]:
        """Planar faces standing along the run, in ascending face order."""
        found = []
        for face_id in faces:
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None or abs(normal.Dot(axis)) >= _WALL_PERPENDICULAR_MAX_DOT:
                continue  # a step ring, not a wall
            found.append(node)
        return found

    @staticmethod
    def _facing_gaps(walls: list[AagNode]) -> list[float]:
        """Perpendicular distances between every facing pair, ascending."""
        gaps: list[float] = []
        for index, first in enumerate(walls):
            normal_a = first.outward_normal
            if normal_a is None:
                continue
            for second in walls[index + 1 :]:
                normal_b = second.outward_normal
                if normal_b is None or normal_a.Dot(normal_b) > _ANTI_PARALLEL_MAX_DOT:
                    continue
                gap = abs(
                    gp_Vec(first.centroid, second.centroid).Dot(gp_Vec(normal_a))
                )
                if gap > _MIN_GAP_MM:
                    gaps.append(gap)
        return sorted(gaps)

    @staticmethod
    def _count_corners(graph: AttributedAdjacencyGraph, faces: list[int]) -> int:
        """Corners in the profile: each pair of collected walls that meets.

        Every turn counts, not only the ones that turn inward. Half a T-slot's
        corners are external: where the step meets the neck wall above it the
        material forms a 90 degree outside corner, so a concave-only count
        reads a T as a plain rectangle and rejects exactly the profile this
        recognizer exists for. Tangent seams are excluded -- a wall split in
        two by a Boolean is one wall, not a corner.
        """
        inside = set(faces)
        seen: set[tuple[int, int]] = set()
        for face_id in faces:
            for edge in graph.edges_of(face_id):
                if edge.other_face(face_id) not in inside:
                    continue
                if edge.concavity not in (Concavity.CONCAVE, Concavity.CONVEX):
                    continue
                if edge.is_tangent:
                    continue
                seen.add((edge.face_id_a, edge.face_id_b))
        return len(seen)

    @staticmethod
    def _has_steps(
        graph: AttributedAdjacencyGraph, faces: list[int], axis: gp_Dir
    ) -> bool:
        """Whether the profile changes section part way along the run."""
        for face_id in faces:
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is not None and abs(normal.Dot(axis)) > _CARDINAL_ALIGNMENT_MIN:
                return True
        return False

    @staticmethod
    def _spans_the_part(
        cavity: tuple[float, float, float],
        part: tuple[float, float, float],
        axis: gp_Dir,
    ) -> bool:
        """Whether the footprint is really the part's own outline.

        A window is bounded by material on all sides. Even an open T-slot
        spans the part in at most one direction across its run -- the
        direction it runs out at. Two means the band grew round the outside.
        """
        components = (abs(axis.X()), abs(axis.Y()), abs(axis.Z()))
        spanning = 0
        for index in range(3):
            if components[index] > _CARDINAL_ALIGNMENT_MIN:
                continue  # the run itself
            if (
                part[index] > 1e-9
                and cavity[index] >= _OUTER_SILHOUETTE_SPAN_SHARE * part[index]
            ):
                spanning += 1
        return spanning >= 2


def _is_part_outer(node: AagNode, part_box: Bnd_Box) -> bool:
    """Whether a planar face lies on the outside of the block.

    True when its centroid sits at the part's extreme along its own outward
    normal. Block sides satisfy this; the walls of a cavity are always well
    inside. Only cardinal faces are tested, which is what the reference
    intends: an angled exterior is not a through-cavity wall candidate either
    way, and the test is here to stop the walk absorbing the block.
    """
    if part_box.IsVoid() or node.surface_type is not SurfaceType.PLANE:
        return False
    normal = node.outward_normal
    if normal is None:
        return False
    xmin, ymin, zmin, xmax, ymax, zmax = part_box.Get()
    centroid = node.centroid
    checks = (
        (normal.X(), centroid.X(), xmin, xmax),
        (normal.Y(), centroid.Y(), ymin, ymax),
        (normal.Z(), centroid.Z(), zmin, zmax),
    )
    for component, coordinate, low, high in checks:
        if component > _CARDINAL_ALIGNMENT_MIN:
            return abs(coordinate - high) < _PART_OUTER_TOLERANCE_MM
        if component < -_CARDINAL_ALIGNMENT_MIN:
            return abs(coordinate - low) < _PART_OUTER_TOLERANCE_MM
    return False


def _combined_box(nodes) -> Bnd_Box:
    box = Bnd_Box()
    for node in nodes:
        if not node.bbox.IsVoid():
            box.Add(node.bbox)
    return box


def _box_extents(box: Bnd_Box) -> tuple[float, float, float]:
    if box.IsVoid():
        return (0.0, 0.0, 0.0)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return (xmax - xmin, ymax - ymin, zmax - zmin)
