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
from OCP.BRepTools import BRepTools
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_WIRE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopTools import (
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_IndexedMapOfShape,
)
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

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

# A profile that turns no more than four times is a rectangle, and the slot
# and pocket recognizers serve one of those better. A T-slot section turns
# six times.
_MAX_RECTANGULAR_CORNERS = 4

# Share of the part's own extent along the through axis a four-corner
# window's walls must reach before it counts as a cut that passes through
# rather than a recess. See the gate in _describe.
_THROUGH_SPAN_SHARE = 0.90


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

        # The openings' profiles, read off the B-rep once (see _RimWires).
        rim = _RimWires.build(graph, shape)

        found.extend(self._profile_loop_pass(graph, area_cap, part_box, visited))

        for seed in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if seed.face_id in visited:
                continue
            if not self._may_seed(graph, seed, area_cap, part_box):
                continue

            faces, boundary_normals = self._grow(graph, seed, area_cap, part_box, rim)
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

    # -- profile-loop pass ----------------------------------------------------

    def _profile_loop_pass(
        self,
        graph: AttributedAdjacencyGraph,
        area_cap: float,
        part_box: Bnd_Box,
        visited: set[int],
    ) -> list[FeatureInstance]:
        """Apertures grown by their opening rather than by concavity.

        A cut is a tool sweeping a closed profile: planes where the profile
        ran straight, cylinders or cones where it curved, meeting tangentially,
        convexly, concavely or sharply depending only on which way it turned.
        The concavity flood needs a traversal rule for every such meeting and
        stops where none was written, leaving the far side of the profile to
        another recognizer (a slot with a swell at its middle came back as two
        cavities and a partial bore). What is invariant about one cut is that
        every wall is swept along one axis and stands on one opening: the
        inner wire of the face it was cut into. Stepped apertures, whose lower
        walls never reach the entry, are left to the flood that follows.
        Faces claimed here join `visited`.
        """
        found: list[FeatureInstance] = []

        def swept_along(node: AagNode, axis: gp_Dir) -> bool:
            if node.surface_type is SurfaceType.PLANE:
                normal = node.outward_normal
                return normal is not None and abs(normal.Dot(axis)) < _WALL_PERPENDICULAR_MAX_DOT
            if node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
                cyl_axis = node.cyl_cone_axis
                return cyl_axis is not None and abs(cyl_axis.Direction().Dot(axis)) > 0.9
            return False

        # A host is a face an aperture was cut into; it never joins the cavity.
        def is_host(node: AagNode) -> bool:
            return node.inner_loop_count > 0 or node.area > area_cap

        for host in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if host.inner_loop_count <= 0:
                continue
            axis = host.outward_normal
            if axis is None:
                continue

            seeds: list[int] = []
            for edge in graph.edges_of(host.face_id):
                if not edge.is_inner_wire_edge:
                    continue
                nb_id = edge.other_face(host.face_id)
                if not graph.has_node(nb_id) or nb_id in visited:
                    continue
                nb = graph.node(nb_id)
                if is_host(nb) or not swept_along(nb, axis):
                    continue
                seeds.append(nb_id)
            if not seeds:
                continue

            on_wire = set(seeds)
            seen_here: set[int] = set()
            for seed in seeds:
                if seed in seen_here or seed in visited:
                    continue
                # The profile is the wire: walk only faces standing on this
                # opening, never outward by adjacency. Flooding across swept
                # neighbours leaked into a neighbouring pocket, a boss and a
                # set of flexure slits; being swept makes a face a wall of
                # some cut, standing on this opening makes it a wall of this
                # one.
                walls = {seed}
                stack = [seed]
                while stack:
                    cur = stack.pop()
                    for nb_id in graph.neighbors_of(cur):
                        if nb_id not in on_wire or nb_id in walls or nb_id in visited:
                            continue
                        if not graph.has_node(nb_id):
                            continue
                        if _is_part_outer(graph.node(nb_id), part_box):
                            continue
                        walls.add(nb_id)
                        stack.append(nb_id)
                seen_here |= walls
                if len(walls) < 2:
                    continue
                feature = self._describe_profile(graph, host, axis, sorted(walls), part_box)
                if feature is None:
                    continue
                visited.update(walls)
                found.append(feature)
        return found

    def _describe_profile(
        self,
        graph: AttributedAdjacencyGraph,
        host: AagNode,
        axis: gp_Dir,
        walls: list[int],
        part_box: Bnd_Box,
    ) -> Optional[FeatureInstance]:
        # Through: the walls reach an opening on the far side too, a plane
        # whose normal opposes the host's.
        exits = False
        for wall_id in walls:
            for edge in graph.edges_of(wall_id):
                nb_id = edge.other_face(wall_id)
                if nb_id == host.face_id or not graph.has_node(nb_id):
                    continue
                nb = graph.node(nb_id)
                if nb.surface_type is not SurfaceType.PLANE:
                    continue
                normal = nb.outward_normal
                if normal is not None and normal.Dot(axis) < -0.9:
                    exits = True
                    break
            if exits:
                break
        if not exits:
            return None

        planar = curved = 0
        min_r = 0.0
        for wall_id in walls:
            node = graph.node(wall_id)
            if node.surface_type is SurfaceType.PLANE:
                planar += 1
                continue
            curved += 1
            if node.surface_type is SurfaceType.CYLINDER and (
                min_r == 0.0 or node.cyl_radius < min_r
            ):
                min_r = node.cyl_radius
        if planar == 0:
            return None  # a bore: the hole family owns it
        if curved == 0:
            return None  # a square profile: slot or pocket

        # A simple profile belongs to whoever already reads it. Four corners
        # or fewer is a rounded rectangle, which pocket or slot serve well.
        # The reference counts concave wall-to-wall corners only here.
        corners = _concave_corners(graph, walls)
        if corners + curved <= _MAX_RECTANGULAR_CORNERS:
            return None

        gaps = self._facing_gaps(self._walls(graph, walls, axis))
        if not gaps:
            return None

        # One profile has one scale. 0.5 mm flexure slits opening into an
        # 85 mm bore share a boundary, not an operation: no tool cuts a
        # profile spanning 176 to 1.
        if not _one_scale(gaps):
            return None

        # The profile must actually swell. An arc about the width of the cut
        # is the cut turning; an arc much smaller is a corner break, which
        # belongs to the blend family.
        swells = any(
            graph.node(w).surface_type is SurfaceType.CYLINDER
            and 2.0 * graph.node(w).cyl_radius >= 0.5 * gaps[0]
            for w in walls
        )
        if not swells:
            return None

        cavity_box = _combined_box(graph.node(w) for w in walls)
        if cavity_box.IsVoid():
            return None
        extents = _box_extents(cavity_box)
        depth = abs(axis.X()) * extents[0] + abs(axis.Y()) * extents[1] + abs(axis.Z()) * extents[2]
        if not part_box.IsVoid() and self._spans_the_part(
            extents, _box_extents(part_box), axis
        ):
            return None

        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.THROUGH_CAVITY,
            faces=list(walls),
            parameters={
                "through_axis": (round(axis.X(), 6), round(axis.Y(), 6), round(axis.Z(), 6)),
                "depth_mm": round(depth, 6),
                "min_width_mm": round(gaps[0], 6),
                "max_width_mm": round(gaps[-1], 6),
                "width_mm": round(gaps[0], 6),
                "length_mm": round(gaps[-1], 6),
                "is_open": True,
                "corner_radius_mm": round(min_r, 6),
                "corner_count": corners + curved,
                "has_step_transitions": self._has_steps(graph, walls, axis),
                "face_count": len(walls),
            },
        )

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
            elif edge.concavity is Concavity.CONVEX:
                # A lobe is the same evidence. Where an arc bulges past the
                # wall it joins, the metal between them comes to a point and
                # the junction reads convex, but the wall still ends in an
                # arc. A keyhole's slot wall has one tangent corner (its
                # rounded end) and one convex lobe (the circle), so counting
                # tangents alone it never seeded.
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
        rim: "_RimWires",
    ) -> tuple[list[int], list[gp_Dir]]:
        """Collect the wall band and the normals of what it terminates against.

        Three steps, run in rounds until nothing is added:

        LOCAL -- a walk across junctions that are unambiguous: concave (a
        corner of the profile) or tangent (an arc flowing out of its wall).

        STEP -- a cut whose profile changes partway down meets the land
        across a convex step ring; from the small cardinal step faces already
        collected, the walk also crosses that one convex junction. It asks
        whether a wall belongs to this opening further along the axis, not
        whether it belongs to this profile.

        WIRE -- close the opening along its rim wire: every wall standing on
        a rim wire some collected wall stands on joins it. The wire is a
        cycle, so this carries the walk past a bulging lobe, a reflex vertex
        or any profile turn no local rule was written for. It replaces the
        terminal-arc branch and the closing-arc pass, both of which tried to
        decide from local convexity whether the arc beyond a junction was
        part of this profile.

        The steps feed each other (a lobe pulled in by the wire may have
        tangent neighbours, a wall reached locally may stand on a second
        wire), hence the rounds.
        """
        collected = {seed.face_id}
        boundary_normals: list[gp_Dir] = []
        queue: deque[int] = deque([seed.face_id])

        for _ in range(64):
            before = len(collected)
            self._walk(graph, queue, collected, boundary_normals, area_cap, part_box, False)
            queue.extend(
                face_id
                for face_id in sorted(collected)
                if self._is_step_face(graph.node(face_id), area_cap)
            )
            self._walk(graph, queue, collected, boundary_normals, area_cap, part_box, True)
            self._wire_step(graph, queue, collected, rim, area_cap, part_box)
            self._walk(graph, queue, collected, boundary_normals, area_cap, part_box, False)
            if len(collected) == before:
                break

        return (sorted(collected), boundary_normals)

    @staticmethod
    def _wire_step(
        graph: AttributedAdjacencyGraph,
        queue: deque[int],
        collected: set[int],
        rim: "_RimWires",
        area_cap: float,
        part_box: Bnd_Box,
    ) -> None:
        if not rim.wires:
            return
        for face_id in sorted(collected):
            for wire_index in rim.of_face.get(face_id, ()):
                for wall_id in rim.wires[wire_index]:
                    if wall_id in collected or not graph.has_node(wall_id):
                        continue
                    if not _admissible(graph.node(wall_id), area_cap, part_box):
                        continue
                    collected.add(wall_id)
                    queue.append(wall_id)

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

        # The census counts every face swept along the run, curved ends
        # included; the facing gaps are still measured between the flats.
        if sum(1 for f in faces if _is_wall(graph.node(f), axis)) < _MIN_WALLS:
            return None
        walls = self._walls(graph, faces, axis)

        gaps = self._facing_gaps(walls)
        if not gaps:
            return None
        # One profile has one scale, here as in the profile-loop pass. The
        # rim wire joins whatever stands on one closed profile, and on a
        # mount whose 0.5 mm flexure slits run out into an 85 mm bore that
        # is one 176-to-1 "cavity" in which every slit vanishes.
        if not _one_scale(gaps):
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

        # Which way do the walls face? A cavity is bounded by walls that look
        # into it; the outside of a part by walls that look away. The
        # silhouette guard above only catches a mis-grown band spanning the
        # whole part; a flood that traced one lug's outer profile and then
        # ran on into the slot cut in its top spans a quarter of it. Tested
        # against the cavity's own box centre, so no notion of where the
        # part is is needed: a wall whose outward normal points away from
        # the cavity has the cavity behind it.
        if _faces_outward(graph, faces, axis, cavity_box):
            return None

        corners = _concave_corners(graph, faces)
        radii = [
            node.cyl_radius
            for node in (graph.node(face_id) for face_id in faces)
            if node.surface_type is SurfaceType.CYLINDER
        ]

        # A window with four sharp corners and no radii is an ordinary
        # rectangular cut, and the slot and pocket recognizers describe it
        # better. Rounded windows are the exception: with no floor to seed
        # from, nothing else serves them at all.
        #
        # And so is a cut that goes all the way THROUGH. Pocket, slot and slit
        # are all floor-seeded, so for a through cut the handover is to
        # nobody and the opening is claimed by no recognizer at all. Whether
        # the wall band reaches through the part is a statement about the
        # cut, and the threshold sits in a measured gap: rejected recesses
        # top out at 0.74 of the part's extent, real through cuts sit at
        # 0.99999, and the margin is spent on the reject side because an
        # invented feature is priced. Measured against a box of the PLANE
        # faces only: a curved face's box is padded out to its control hull,
        # by about the margin this test needs.
        if corners <= _MAX_RECTANGULAR_CORNERS and not radii:
            if self._through_span_share(graph, axis, depth) < _THROUGH_SPAN_SHARE:
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
    def _through_span_share(graph: AttributedAdjacencyGraph, axis: gp_Dir, depth: float) -> float:
        """The cavity's depth as a share of the part's extent along its axis."""
        planes = _combined_box(graph.nodes_by_surface_type(SurfaceType.PLANE))
        if planes.IsVoid():
            return 0.0
        extents = _box_extents(planes)
        along = (
            abs(axis.X()) * extents[0]
            + abs(axis.Y()) * extents[1]
            + abs(axis.Z()) * extents[2]
        )
        return depth / along if along > 1.0e-9 else 0.0

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
                if _is_wall(graph.node(face_id), axis):
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


def _faces_outward(
    graph: AttributedAdjacencyGraph, faces: list[int], axis: gp_Dir, box: Bnd_Box
) -> bool:
    """More planar walls look away from the cavity's box centre than into it.

    Walls only: a face square to the through axis is an end the cavity
    passes through, not a side of it.
    """
    if box.IsVoid():
        return False
    x0, y0, z0, x1, y1, z1 = box.Get()
    mid = gp_Pnt((x0 + x1) * 0.5, (y0 + y1) * 0.5, (z0 + z1) * 0.5)
    facing_in = facing_out = 0
    for face_id in faces:
        if not graph.has_node(face_id):
            continue
        node = graph.node(face_id)
        if node.surface_type is not SurfaceType.PLANE:
            continue
        normal = node.outward_normal
        if normal is None:
            continue
        if abs(normal.Dot(axis)) > 0.7:
            continue
        to_mid = gp_Vec(node.centroid, mid)
        if to_mid.Magnitude() < 1.0e-9:
            continue
        if to_mid.Dot(gp_Vec(normal)) > 0.0:
            facing_in += 1
        else:
            facing_out += 1
    return facing_out > facing_in


def _is_wall(node: AagNode, axis: gp_Dir) -> bool:
    """Whether a face is swept along the through axis: a wall of the cavity.

    A plane whose normal is square to the axis, or a cylinder whose axis runs
    PARALLEL to it -- a corner radius or a whole rounded end. Counting planes
    only made the census depend on how the profile is cornered: an obround
    cutout, the default laser or punch shape, has three flats and one
    semicylindrical end, and was rejected for having three walls.
    """
    if node.surface_type is SurfaceType.PLANE:
        normal = node.outward_normal
        return normal is not None and abs(normal.Dot(axis)) < _WALL_PERPENDICULAR_MAX_DOT
    if node.surface_type is SurfaceType.CYLINDER:
        cylinder_axis = node.cyl_cone_axis
        return cylinder_axis is not None and abs(cylinder_axis.Direction().Dot(axis)) > 0.9
    return False


def _one_scale(gaps: list[float]) -> bool:
    """Whether a profile's facing gaps belong to one cut.

    Faces standing on one opening are one aperture topologically, not one
    cut: 0.5 mm slits opening into an 85 mm bore span 176 to 1, and no tool
    cuts that. A slot widening into a swell is 70 against 180.
    """
    return bool(gaps) and gaps[-1] <= 10.0 * gaps[0]


def _admissible(node: AagNode, area_cap: float, part_box: Bnd_Box) -> bool:
    """A face that may be a wall: not a host, not oversized, not the outside.

    The rim-wire step's test. Unlike `_may_absorb` it admits a curved face,
    since a lobe on the wire is exactly what that step exists to collect.
    """
    if node.area > area_cap or node.inner_loop_count > 0:
        return False
    return not _is_part_outer(node, part_box)


class _RimWires:
    """Which faces stand on which opening's rim wire.

    An opening is a tool sweeping a closed profile, and on the face it was
    cut into that profile is a wire. Two walls belong to the same opening
    when their rim edges lie on the same rim wire. That replaces adjacency
    across a concave edge, a relation that is discontinuous where the truth
    is constant: as a lobe grows from inscribed to bulging, its junction
    walks concave -> tangent -> convex while the number of openings never
    changes. Convexity stays in the classification instead (is this inner
    wire a depression or a protrusion), where it compares ~90 against ~270
    degrees.

    Known limits, all failing safe (the wire is not found and the local walk
    decides alone): a blended or chamfered rim, a split or non-planar host,
    an island, and an open notch (no inner wire at all).
    """

    def __init__(self) -> None:
        self.wires: list[list[int]] = []
        self.of_face: dict[int, list[int]] = {}

    @classmethod
    def build(cls, graph: AttributedAdjacencyGraph, shape) -> "_RimWires":
        rim = cls()
        if shape is None or shape.IsNull():
            return rim
        face_map = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)
        # The graph's face ids are this map's indices. If the two ever
        # disagree, return nothing rather than attach walls to the wrong
        # opening; the local walk still runs.
        if face_map.Extent() == 0 or face_map.Extent() != graph.face_count:
            return rim
        edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces)

        for host_id in range(1, face_map.Extent() + 1):
            if not graph.has_node(host_id):
                continue
            host = graph.node(host_id)
            if host.inner_loop_count <= 0 or host.surface_type is not SurfaceType.PLANE:
                continue
            hn = host.outward_normal
            if hn is None:
                continue
            face = TopoDS.Face_s(face_map.FindKey(host_id))
            outer = BRepTools.OuterWire_s(face)
            wires = TopExp_Explorer(face, TopAbs_WIRE)
            while wires.More():
                wire = TopoDS.Wire_s(wires.Current())
                wires.Next()
                if not outer.IsNull() and wire.IsSame(outer):
                    continue
                walls, spans, depression = rim._walls_on(
                    graph, host, hn, host_id, wire, face_map, edge_faces
                )
                if not depression:
                    continue
                # One rim, one section. Walls of one profile reach equally far
                # along the axis; a face on the rim reaching much further is
                # a deeper cut sharing the rim (a through bore biting the edge
                # of a shallow pocket), and joining it would hand this profile
                # an exit it does not have. Against the median, loosely (1.5x),
                # because curved-face boxes pad and Boolean-split walls run
                # a little short.
                ordered = sorted(span for _, span in spans)
                median = ordered[len(ordered) // 2] if ordered else 0.0
                if median > 1.0e-9:
                    walls = {face_id for face_id, span in spans if span <= 1.5 * median}
                # One face across the whole wire is a bore, not a profile.
                if len(walls) < 2:
                    continue
                index = len(rim.wires)
                rim.wires.append(sorted(walls))
                for wall_id in sorted(walls):
                    rim.of_face.setdefault(wall_id, []).append(index)
        return rim

    @staticmethod
    def _walls_on(graph, host, hn, host_id, wire, face_map, edge_faces):
        walls: set[int] = set()
        spans: list[tuple[int, float]] = []
        edges = TopExp_Explorer(wire, TopAbs_EDGE)
        while edges.More():
            edge = edges.Current()
            edges.Next()
            if not edge_faces.Contains(edge):
                continue
            incident = edge_faces.FindFromKey(edge)
            if incident.Extent() != 2:
                continue  # not a manifold rim edge
            for other in incident:
                face_id = face_map.FindIndex(other)
                if face_id < 1 or face_id == host_id or not graph.has_node(face_id):
                    continue
                cand = graph.node(face_id)
                # Depression or protrusion, per rim edge: the face across a
                # cut's rim lies below the host plane, a boss's above it.
                # Measured on the far face's centroid, since a point on the
                # rim lies in the host plane by construction.
                side = gp_Vec(host.centroid, cand.centroid).Dot(gp_Vec(hn))
                if side > 1.0e-6:
                    return walls, spans, False
                # A wall of this opening is swept along the rim's axis. A
                # blended rim puts tori across the wire, none of them a wall,
                # so the wire is dropped and the local walk decides as
                # before. Cones stay out: a rim chamfer and a drafted wall
                # are both cones about the axis and nothing local tells them
                # apart.
                if cand.surface_type is SurfaceType.PLANE:
                    cn = cand.outward_normal
                    is_wall = cn is not None and abs(cn.Dot(hn)) < 0.3
                elif cand.surface_type is SurfaceType.CYLINDER:
                    axis = cand.cyl_cone_axis
                    is_wall = axis is not None and abs(axis.Direction().Dot(hn)) > 0.9
                else:
                    is_wall = False
                if not is_wall or face_id in walls:
                    continue
                walls.add(face_id)
                span = 0.0
                if not cand.bbox.IsVoid():
                    sxn, syn, szn, sxx, syx, szx = cand.bbox.Get()
                    span = (
                        abs(hn.X()) * (sxx - sxn)
                        + abs(hn.Y()) * (syx - syn)
                        + abs(hn.Z()) * (szx - szn)
                    )
                spans.append((face_id, span))
        return walls, spans, True


def _concave_corners(graph: AttributedAdjacencyGraph, faces: list[int]) -> int:
    """Concave wall-to-wall meetings, each face pair counted once.

    The reference's `count_corners`, used by both passes. Only interior
    (concave) corners count: a pair of faces meeting convexly inside the set
    is not a corner of the profile. Counting every turn made two parallel
    slits joined across a convex ledge a six-corner profile, which this
    recognizer then claimed from the slit recognizer (nist_ftc_10); the
    reference counts four there and leaves the slits alone.
    """
    inside = set(faces)
    seen: set[tuple[int, int]] = set()
    for face_id in faces:
        for edge in graph.edges_of(face_id):
            if edge.concavity is not Concavity.CONCAVE:
                continue
            nb = edge.other_face(face_id)
            if nb not in inside:
                continue
            seen.add((min(face_id, nb), max(face_id, nb)))
    return len(seen)


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
