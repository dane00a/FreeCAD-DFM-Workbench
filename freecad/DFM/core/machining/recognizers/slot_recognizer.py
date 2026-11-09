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

import math
from typing import Optional, Sequence

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..face_extent import band_along, extent_of_face, face_vertices_extent
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer, axes_are_coaxial, normal_bucket_up_to_sign
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

# The share of a slot's wall area its two width-defining walls must carry.
# The middle of a measured gap across every slot in the reference corpus:
# the two rejections (engraved glyphs) sit at 0.00099 and 0.00108, the
# lowest real slot at 0.0172, and the distribution has no other gap.
_MIN_DEFINING_WALL_SHARE = 0.005

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
        self._shape = shape

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
            if self._defining_wall_share(graph, floor, faces, walls) < _MIN_DEFINING_WALL_SHARE:
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
            # Up to sign, not componentwise |x|,|y|,|z|: the latter also
            # merges mirrored normals, and on a slot sunk at 45 degrees it
            # bucketed the slanted floor with the two big walls and took an
            # end wall for the floor (see normal_bucket_up_to_sign).
            key = normal_bucket_up_to_sign(node.outward_normal)
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
            if not self._is_recessed(graph, node, envelope, by_neighbour=True):
                continue
            candidates.append(node)
        return sorted(candidates, key=lambda n: (-n.area, n.face_id))

    @staticmethod
    def _is_recessed(
        graph: AttributedAdjacencyGraph,
        node: AagNode,
        envelope,
        by_neighbour: bool = False,
    ) -> bool:
        """Whether this face is cut into the part rather than a side of it.

        ``by_neighbour`` is the seed vote: it counts distinct neighbours, not
        edges. The question is what fraction of the floor's boundary is
        concave, and a boundary shared with one neighbour is one boundary
        however many edges the B-rep cut it into. The reference measured it
        on text_neg_decor_groove: three congruent bars 120 degrees apart, and
        the one at 0 degrees met one neighbour along two edges -- the foot of
        a ring cylinder's parametric seam, which falls at theta = 0, split
        the shared boundary -- so it voted 3 of 5 against a 2/3 bar and read
        as a STEP while its twins read as SLOTs. A neighbour counts as
        concave when any edge it shares is. Parametric seams do not vote
        either: they are not a boundary of anything.
        """
        edges = graph.edges_of(node.face_id)
        if not edges:
            return False
        concave = 0
        counted = 0
        votes: dict[int, Optional[bool]] = {}
        for edge in edges:
            # Inner wires to a small drilled bore do not vote. A hole through
            # a slot floor meets it at a convex rim, and counting that as
            # evidence the floor is an exterior face is backwards: a bore
            # through a floor says nothing about whether the floor is
            # recessed. Narrow on purpose -- a window a through-channel opens
            # in an outer face is not a drilled hole and still votes.
            if _is_drilled_through(graph, edge, node):
                continue
            if by_neighbour and edge.is_seam:
                continue
            other = edge.other_face(node.face_id)
            if edge.concavity is Concavity.CONCAVE:
                concave += 1
                if by_neighbour:
                    votes[other] = True
                else:
                    counted += 1
                continue
            neighbour = graph.node(other)
            if envelope.is_outer(neighbour):
                if by_neighbour:
                    votes.setdefault(other, None)
                continue  # the recess running out of the part: not a vote
            if by_neighbour:
                if votes.get(other) is None:
                    votes[other] = False
            else:
                counted += 1
        if concave < _MIN_CONCAVE_EDGES:
            return False
        if by_neighbour:
            voting = [v for v in votes.values() if v is not None]
            concave_nb = sum(1 for v in voting if v)
            return concave_nb >= len(voting) * _MIN_CONCAVE_EDGE_SHARE
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
    def _defining_wall_share(
        graph: AttributedAdjacencyGraph,
        floor: AagNode,
        faces: list[int],
        walls: tuple[AagNode, AagNode],
    ) -> float:
        """The share of the slot's wall area its width-defining pair carries.

        Everything downstream trusts width_mm, so the pair whose separation
        it is had better be most of the wall. On an engraved glyph the "8"
        of a bold legend was a 32-face slot on the strength of two 0.1 mm2
        slivers at its waist (exactly opposed because the letterform is
        symmetric), its other walls freeform and invisible to planar
        pairing; the width was the glyph's waist. A wall here is anything
        not lying parallel to the floor, freeform included, so the
        denominator sees the stroke walls.
        """
        floor_normal = floor.outward_normal
        defining_ids = {walls[0].face_id, walls[1].face_id}
        defining = total = 0.0
        for face_id in faces:
            if face_id == floor.face_id or not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            if node.surface_type is SurfaceType.PLANE and floor_normal is not None:
                normal = node.outward_normal
                if normal is not None and abs(normal.Dot(floor_normal)) > 0.5:
                    continue  # a floor or roof, not a wall
            total += node.area
            if face_id in defining_ids:
                defining += node.area
        return defining / total if total > 1e-9 else 0.0

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
                # ...but the rounded end of a slot cut with an end mill has
                # its axis along the floor normal too: the cutter stands
                # normal to the floor. A slot's end radius is the same cut as
                # its walls and arrives tangent to them; a port is a separate
                # cut and crosses them.
                if any(
                    graph.has_node(tangent.other_face(node.face_id))
                    and graph.node(tangent.other_face(node.face_id)).surface_type
                    is SurfaceType.PLANE
                    for tangent in graph.edges_of(node.face_id)
                    if tangent.is_tangent
                ):
                    continue
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
        width, width_normal = self._width(graph, faces, floor_normal)
        if width is None:
            return None

        # The walls must bound the floor. A slot's floor spans the width
        # between its walls and no further: measured across the wall normal
        # it should come back as the width. A face carrying several pads is
        # mostly bounded by concave edges (their feet), passes the recessed
        # floor vote, and any two facing pad walls offer a plausible width;
        # the result was a "slot" as wide as one pad and as long as the
        # whole face. 2.5x leaves room for a floor wider than its stem (a
        # T-slot), the one shape where a slot spreads past its walls.
        #
        # Measured from the floor's own vertices, not from its axis-aligned
        # bbox diagonal projected onto the wall normal: that is not a
        # rotation-invariant quantity, and the question is. On
        # text_neg_decor_groove three congruent bars 120 degrees apart, floors
        # with identical boxes, read 0.600, 11.064 and 1.200 against a true
        # 1.200, so one bar was turned away and stopped being checked as a
        # slot at all. Vertices are exact for straight edges and understate a
        # bulging arc by its sagitta -- the permissive direction for a sanity
        # bound with 2.5x of headroom. A face too poor to measure (< 2
        # vertices) is not acted on.
        if width_normal is not None and width > 0.0:
            across = face_vertices_extent(self._shape, floor.face_id, width_normal)
            if across > 0.0 and across > width * 2.5 + 0.1:
                return None

        # Length: the floor's extent along the slot's own run direction
        # (floor normal x width normal). The largest dimension of the
        # floor's axis-aligned box, kept as the fallback, is a diagonal on
        # any slot not parallel to X, Y or Z.
        length = -1.0
        if width_normal is not None:
            run = gp_Vec(floor_normal).Crossed(gp_Vec(width_normal))
            if run.Magnitude() > 0.5:
                length = extent_of_face(self._shape, floor.face_id, gp_Dir(run))
        if not length > 0.0:
            dims = sorted(d for d in floor.bbox_dims() if d > 1e-6)
            length = dims[-1] if dims else 0.0
        if width <= 1e-6 or length <= 1e-6:
            return None
        if width >= length * _MAX_WIDTH_TO_LENGTH:
            return None  # too square to be a channel

        depth = self._depth(graph, faces, floor_normal, self._shape)
        corner_radius, corner_cylinders = self._corner_radius(
            graph, faces, floor_normal, width
        )
        if corner_cylinders:
            faces = sorted(set(faces) | corner_cylinders)

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
        # Written only when the slot has an internal vertical corner: a
        # fillet, or a genuinely sharp concave wall-to-wall corner (then
        # zero). With neither -- a relief whose only vertical corners are
        # external, round its feet -- it stays unset, and the corner-radius
        # rule has nothing to say about it, as in the reference.
        if corner_radius is not None:
            parameters["corner_radius_mm"] = round(corner_radius, 6)
        elif self._sharp_corner_count(graph, faces, floor_normal) > 0:
            parameters["corner_radius_mm"] = 0.0

        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.SLOT,
            faces=[floor.face_id] + [f for f in faces if f != floor.face_id],
            parameters=parameters,
        )

    @staticmethod
    def _width(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> tuple[Optional[float], Optional[gp_Dir]]:
        """The narrowest distance across the channel, and the normal it is measured along."""
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
        narrowest_normal = None
        for index, (first, normal_a) in enumerate(walls):
            for second, normal_b in walls[index + 1 :]:
                if normal_a.Dot(normal_b) >= _FACING_WALLS_MAX_DOT:
                    continue
                across = abs(
                    gp_Vec(first.centroid, second.centroid).Dot(gp_Vec(normal_a))
                )
                if across > 0.01 and (narrowest is None or across < narrowest):
                    narrowest = across
                    narrowest_normal = normal_a
        return narrowest, narrowest_normal

    @staticmethod
    def _depth(
        graph: AttributedAdjacencyGraph,
        faces: list[int],
        floor_normal: gp_Dir,
        shape=None,
    ) -> float:
        # The faces' combined span along the floor normal, measured on the
        # faces (band_along); the box projection is only a per-face fallback.
        if shape is not None:
            lo, hi = 1.0e18, -1.0e18
            origin = gp_Pnt(0.0, 0.0, 0.0)
            for face_id in faces:
                node = graph.node(face_id)
                _, lo, hi = band_along(
                    shape, face_id, node.bbox, origin, floor_normal, lo, hi
                )
            return hi - lo if hi > lo else 0.0
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
    def _sharp_corner_count(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> int:
        """Concave edges where two square-meeting walls of the slot join."""
        count = 0
        for wall_id in sorted(set(faces)):
            if not graph.has_node(wall_id):
                continue
            wall = graph.node(wall_id)
            if wall.surface_type is not SurfaceType.PLANE:
                continue
            wall_normal = wall.outward_normal
            if wall_normal is None or abs(wall_normal.Dot(floor_normal)) > 0.5:
                continue
            for edge in graph.concave_edges_of(wall_id):
                other_id = edge.other_face(wall_id)
                if not graph.has_node(other_id):
                    continue
                other = graph.node(other_id)
                if other.surface_type is not SurfaceType.PLANE:
                    continue
                other_normal = other.outward_normal
                if other_normal is None or abs(other_normal.Dot(floor_normal)) > 0.5:
                    continue
                if abs(other_normal.Dot(wall_normal)) < 0.3:
                    count += 1
        return count

    @staticmethod
    def _corner_radius(
        graph: AttributedAdjacencyGraph,
        faces: list[int],
        floor_normal: gp_Dir,
        width: float,
    ) -> tuple[Optional[float], set[int]]:
        """The smallest internal corner fillet, and the fillets it found.

        A vertical cylinder beside one of the slot's walls is a corner when
        its two planar wall neighbours are not facing each other (facing
        walls flowing into a cylinder make the rounded end, which is the
        slot's length, not a corner). Two further gates, from the reference
        (690e961b), because a ring a bar runs into looks exactly like a
        corner fillet -- a cylinder standing on the floor normal met by two
        walls 120 degrees apart -- and every rosette bar reported the ring's
        R17.4 path curvature as its corner radius:

        * both bounding walls must be this slot's own walls; a cylinder
          shared with a neighbouring feature is not this slot's corner;
        * the radius must not exceed the slot width: a cutter that fits the
          slot leaves at most width/2, so anything wider is the slot's path
          bending (measured gap: 0.375 x width highest real fillet, 14.5 x
          the rejection).
        """
        face_set = set(faces)
        found: set[int] = set()
        seen: set[int] = set()
        smallest: Optional[float] = None
        for wall_id in sorted(face_set):
            if not graph.has_node(wall_id):
                continue
            wall = graph.node(wall_id)
            if wall.surface_type is not SurfaceType.PLANE:
                continue
            wall_normal = wall.outward_normal
            if wall_normal is None or abs(wall_normal.Dot(floor_normal)) > 0.5:
                continue  # the floor
            for edge in graph.edges_of(wall_id):
                cyl_id = edge.other_face(wall_id)
                if cyl_id in seen or not graph.has_node(cyl_id):
                    continue
                cyl = graph.node(cyl_id)
                if cyl.surface_type is not SurfaceType.CYLINDER or cyl.cyl_cone_axis is None:
                    continue
                if abs(cyl.cyl_cone_axis.Direction().Dot(floor_normal)) < 0.95:
                    continue
                seen.add(cyl_id)
                neighbour_walls: list[tuple[int, gp_Dir]] = []
                for cyl_edge in graph.edges_of(cyl_id):
                    nb_id = cyl_edge.other_face(cyl_id)
                    if not graph.has_node(nb_id):
                        continue
                    nb = graph.node(nb_id)
                    if nb.surface_type is not SurfaceType.PLANE:
                        continue
                    normal = nb.outward_normal
                    if normal is None or abs(normal.Dot(floor_normal)) > 0.5:
                        continue
                    neighbour_walls.append((nb_id, normal))
                if len(neighbour_walls) != 2:
                    continue
                if neighbour_walls[0][1].Dot(neighbour_walls[1][1]) < -0.5:
                    continue  # the rounded end of the slot
                if (
                    neighbour_walls[0][0] not in face_set
                    or neighbour_walls[1][0] not in face_set
                ):
                    continue
                if width > 1e-6 and cyl.cyl_radius > width:
                    continue
                found.add(cyl_id)
                if smallest is None or cyl.cyl_radius < smallest:
                    smallest = cyl.cyl_radius
        return smallest, found

    @staticmethod
    def _is_open(
        graph: AttributedAdjacencyGraph, faces: list[int], floor_normal: gp_Dir
    ) -> bool:
        """Whether the channel runs out of the part at an end.

        Closed as soon as any two of its walls are not parallel -- tested on
        the normals themselves, so a slot rotated or tilted 45 degrees is
        judged the same as a cardinal one.
        """
        normals: list[gp_Dir] = []
        for face_id in faces:
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None or abs(normal.Dot(floor_normal)) > 0.5:
                continue
            normals.append(normal)
        for index, first in enumerate(normals):
            for second in normals[index + 1 :]:
                if abs(first.Dot(second)) < 0.7:
                    return False
        return True

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
                depth = (
                    first.cyl_p0.Distance(first.cyl_p1)
                    if first.cyl_p0 and first.cyl_p1
                    else 0.0
                )
                # Measured on the faces along the cutter axis (band_along),
                # not as a parameter span or a projected box diagonal.
                shape = getattr(self, "_shape", None)
                if shape is not None:
                    lo, hi = 1.0e18, -1.0e18
                    origin = gp_Pnt(0.0, 0.0, 0.0)
                    for face_id in (first.face_id, second.face_id, *walls):
                        node = graph.node(face_id)
                        _, lo, hi = band_along(
                            shape, face_id, node.bbox, origin, axis_a.Direction(), lo, hi
                        )
                    if hi > lo:
                        depth = hi - lo
                found.append(
                    FeatureInstance(
                        instance_id=self.instance_id(0),
                        type=FeatureType.SLOT,
                        faces=faces,
                        parameters={
                            "width_mm": round(first.cyl_radius * 2.0, 6),
                            "length_mm": round(separation + first.cyl_radius * 2.0, 6),
                            "depth_mm": round(depth, 6),
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


def _is_drilled_through(graph: AttributedAdjacencyGraph, edge, face: AagNode) -> bool:
    """A small bore drilled through ``face``, met at one of its inner wires."""
    if not edge.is_inner_wire_edge:
        return False
    other_id = edge.other_face(face.face_id)
    if not graph.has_node(other_id):
        return False
    other = graph.node(other_id)
    if other.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
        return False
    radius = other.cyl_radius
    if radius <= 0.0:
        return False
    return math.pi * radius * radius < 0.25 * face.area
