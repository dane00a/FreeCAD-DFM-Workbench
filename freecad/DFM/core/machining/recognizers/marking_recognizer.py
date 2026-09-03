# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes part marking: engraved or raised text, logos and dot-peen codes.

Marking is sub-threshold by design. A serial number is a few tenths deep, its
strokes are well under a millimetre wide, and its corners are dead sharp --
every one of which trips a face-level rule. None of it is a manufacturing
concern, because nobody end-mills a serial number: it is V-bit engraved, laser
marked, dot-peened, or formed in the die. Left unrecognized, one engraved line
of text fires dozens of thin-wall, sharp-edge and minimum-feature findings, and
every stroke reads as a slot, a boss and an undercut at the same time.

So the job is to claim the whole text block as one feature, and to claim
nothing that is not text. Recognition is by geometry class rather than by font
shape, because the same pass has to catch a seven-segment stick font, a spline
outline straight out of a customer's CAD, a dot-peen dimple grid and a cast
logo relief. Four passes, each looking at what the ones before it left alone:

1. *Glyph clusters* -- three or more small connected components sitting in a
   thin slab either side of a host plane, all on the same side, each with a
   flat stroke floor or top. Separate characters, which is the common case.
2. *Logotypes* -- one connected graphic whose letterforms merge into a single
   component. It fails the cluster pass twice over, so it is qualified
   statistically instead: one flat floor level, some curvature, and a floor
   that fills only a small share of its own footprint. Letterform bands are
   sparse where a functional recess floor fills its box.
3. *Background relief* -- the inverse: the background is milled away inside a
   plaque and the letters are left standing at the original surface. Dense by
   construction, so pass 2 can never see it. The tell is stroke-width islands
   that come back up exactly level with the surrounding host face.
4. *Floorless engraving* -- a V-tool or ball-nose cut has no flat floor at all,
   only walls running down to a bottom edge. The tell is opposing walls one
   stroke width apart; a functional cutout's walls are far apart.

The host normal is always taken from `outward_normal`, so a face sunk into the
material reads negative and a proud one positive whichever kernel built the
solid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from OCP.Bnd import Bnd_Box
from OCP.BRepGProp import BRepGProp
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps

from ...utils.geometry import FaceIndex
from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer


# Deepest cut or tallest proud letter the glyph pass will call marking. Deeper
# than this and it is a machined recess, whatever it is shaped like.
_SLAB_MM = 1.6

# Lateral size of one character. Bigger than this is signage, not part marking.
_GLYPH_MAX_MM = 20.0

# Widest stroke that is still a pen stroke. A wider flat floor is a filled
# blob -- a pocket -- not a letter.
_STROKE_MAX_MM = 3.5

# Below this a step is surface noise, not an intentional depth.
_MIN_DEPTH_MM = 0.05

# Three characters before it counts as a text block. Two adjacent slots are a
# coincidence; three are a legend.
_MIN_GLYPHS = 3

# cos(15 deg): a stroke is prismatic, so its faces are parallel or square to
# the host. Anything leaning in between is a V-groove or a tilted micro-optic
# and gets thrown out.
_ALIGN_DOT = 0.966

# A host face has to be big enough to carry text at all.
_HOST_MIN_AREA_MM2 = 25.0

# Slab depth used when *searching* for member faces in the logotype and relief
# passes. Kept separate from the depth that classifies marking-vs-pocket: the
# search has to reach the floor of the deepest mark accepted, or that floor is
# never even seen, while the classification below decides what the mark is.
_SEARCH_SLAB_MM = 3.0

# Deepest a single-level recess may be and still be a mark. Deeper is a pocket.
# Depth is legitimately absolute -- marking is shallow regardless of part size.
_MAX_DEPTH_MM = 2.8

# Letterform walls come in numbers; a functional recess has six faces or fewer.
_LOGO_MIN_FACES = 8

# At least some curvature, counted absolutely rather than as a ratio: a stick
# logotype can be fifteen planar strokes and two arcs. Purely rectilinear
# multi-face recesses -- steps, notched cavities -- stay out.
_LOGO_MIN_CURVED = 2

# Floor area over the footprint of its own bounding box. Letter bands are
# sparse; a functional recess floor fills its box.
_LOGO_FLOOR_FILL_MAX = 0.55

# Lateral bound on the whole graphic, as max(absolute floor, share of the part).
# A logo scales with the part, so a pure absolute ceiling cannot be right for a
# large one -- but shallow functional patterns that pass the sparseness test
# still have to be excluded. Keyed to the PART bbox, not the host face: a logo
# engraved on a boss fills its host face yet is tiny next to the whole part.
_LOGO_MAX_DIAG_MM = 50.0
_LOGO_MAX_PART_FRACTION = 0.35

# Spread allowed across the floor faces of one graphic before it reads as a
# multi-level cavity rather than a single engraving pass.
_LOGO_LEVEL_TOL_MM = 0.3

# One or two islands in a recess are a boss risk; three are letters.
_RELIEF_MIN_ISLANDS = 3

# How exactly an island top has to land back at the host surface. Machining a
# boss to finish flush with the surface around it is rare outside marking.
_RELIEF_LEVEL_TOL_MM = 0.15

# Counter-form engraving fragments one graphic into a sparse band component
# plus dense counter pools. Pools at the same floor level whose box, dilated by
# this gap, touches an emitted logotype are absorbed into it.
_ABSORB_GAP_MM = 5.0

# The absorbed whole may exceed the per-component compactness cap -- it is one
# graphic, just a bigger one -- but not without limit.
_ABSORB_MAX_DIAG_MM = 80.0

# Two walls are opposing when their outward normals point at each other.
_WALL_OPPOSING_DOT = -0.85

# A member face may be half again the glyph cap: booleans split strokes, and
# the fragments have to survive collection even when the whole does not.
_MEMBER_MAX_DIAG_FACTOR = 1.5

# Slack on the assembled component, for the same reason.
_GLYPH_MAX_DIAG_FACTOR = 1.2

# One host face can carry several legends -- a rack panel marked at both ends.
# Glyphs whose centres sit within this multiple of the glyph cap belong to one
# marking operation. Deliberately generous, not typographic: it only has to
# separate blocks so far apart that one strip test would reject them all.
_CLUSTER_LINK_FACTOR = 2.5

# A text block is a compact strip, not geometry scattered over the whole part.
_STRIP_MAX_DIAG_FACTOR = 5.0


@dataclass
class _Glyph:
    """One qualified character: its faces and how it sits on the host."""

    faces: list[AagNode]
    raised: bool
    depth: float
    stroke_width: float
    center: gp_Pnt


@dataclass
class _Component:
    """A connected group of member faces, measured against the host plane.

    Doubles as a counter pool: a component that clears the depth and level
    gates but fails the sparseness test may still be the dense half of a
    counter-form engraving, so it is kept rather than dropped.
    """

    faces: list[AagNode]
    box: Bnd_Box
    level: float
    depth: float
    floor_area: float
    raised: bool
    is_logotype: bool


@dataclass
class _Wall:
    """A stroke wall: which way it faces, and where it is."""

    direction: gp_Dir
    point: gp_Pnt


class MarkingRecognizer(FeatureRecognizer):
    """Recognizes engraved and embossed text."""

    prefix = "mk"

    @property
    def name(self) -> str:
        return "Marking Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored. Every stroke of an engraved
        # character has already been claimed by the cavity and protrusion
        # passes -- as a slot, a boss, an undercut -- and honouring those
        # claims would leave nothing to recognize. Overruling them is the
        # whole point of running this late.
        _CORNER_CACHE.clear()
        hosts = [
            node
            for node in graph.nodes_by_surface_type(SurfaceType.PLANE)
            if node.area >= _HOST_MIN_AREA_MM2 and node.outward_normal is not None
        ]
        if not hosts:
            return []

        # The part bbox is the reference for the part-relative half of the
        # lateral bound. Computed once from the whole graph.
        part_box = Bnd_Box()
        for node in graph.nodes:
            if not node.bbox.IsVoid():
                part_box.Add(node.bbox)
        max_component = max(
            _LOGO_MAX_DIAG_MM, _bbox_diag(part_box) * _LOGO_MAX_PART_FRACTION
        )

        # Faces an emitted marking already owns. A text block sitting between
        # two candidate host planes must not be emitted twice, and each pass
        # only ever looks at what the ones before it left alone.
        taken: set[int] = set()
        found: list[FeatureInstance] = []
        found.extend(self._glyph_clusters(graph, hosts, taken))
        found.extend(self._logotypes(graph, hosts, taken, max_component))
        found.extend(
            self._background_reliefs(graph, hosts, taken, max_component, shape)
        )
        found.extend(self._floorless_marks(graph, hosts, taken, max_component))

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- pass 1: glyph clusters ---------------------------------------------

    def _glyph_clusters(
        self,
        graph: AttributedAdjacencyGraph,
        hosts: list[AagNode],
        taken: set[int],
    ) -> list[FeatureInstance]:
        """Separate characters, clustered into text blocks."""
        found: list[FeatureInstance] = []

        for host in hosts:
            normal = host.outward_normal
            origin = host.centroid
            members = _members(
                graph,
                host,
                normal,
                origin,
                taken,
                _SLAB_MM,
                _GLYPH_MAX_MM * _MEMBER_MAX_DIAG_FACTOR,
            )
            if len(members) < _MIN_GLYPHS:
                continue

            glyphs = [
                glyph
                for glyph in (
                    self._qualify_glyph(faces, normal, origin)
                    for faces in _components(graph, members)
                )
                if glyph is not None
            ]

            # Recessed and proud marking are separate blocks even on one host:
            # cutting text and standing it proud are different operations.
            for raised in (False, True):
                group = [glyph for glyph in glyphs if glyph.raised is raised]
                if len(group) < _MIN_GLYPHS:
                    continue

                for cluster in _link_by_proximity(
                    group, _GLYPH_MAX_MM * _CLUSTER_LINK_FACTOR
                ):
                    if len(cluster) < _MIN_GLYPHS:
                        continue
                    strip = Bnd_Box()
                    for glyph in cluster:
                        for face in glyph.faces:
                            strip.Add(face.bbox)
                    if _bbox_diag(strip) > _GLYPH_MAX_MM * _STRIP_MAX_DIAG_FACTOR:
                        continue

                    face_ids = sorted(
                        {face.face_id for glyph in cluster for face in glyph.faces}
                    )
                    taken.update(face_ids)
                    depths = sorted(glyph.depth for glyph in cluster)
                    widths = sorted(glyph.stroke_width for glyph in cluster)
                    found.append(
                        FeatureInstance(
                            instance_id="",
                            type=FeatureType.MARKING_TEXT,
                            faces=face_ids,
                            parameters={
                                "marking_type": "raised" if raised else "engraved",
                                "glyph_count": len(cluster),
                                # Median, not mean: one glyph split oddly by a
                                # boolean must not move the reported size.
                                "depth_mm": round(depths[len(depths) // 2], 6),
                                "stroke_width_mm": round(widths[len(widths) // 2], 6),
                                "host_face": host.face_id,
                            },
                        )
                    )

        return found

    @staticmethod
    def _qualify_glyph(
        faces: list[AagNode], normal: gp_Dir, origin: gp_Pnt
    ) -> Optional[_Glyph]:
        """Whether one connected component reads as a single character."""
        box = Bnd_Box()
        deepest = 0.0
        below = 0
        above = 0
        stroke_width = 0.0
        has_offset_parallel = False

        for face in faces:
            box.Add(face.bbox)
            smin, smax = _bbox_signed_range(face.bbox, origin, normal)
            deepest = max(deepest, abs(smin), abs(smax))
            if face.surface_type is not SurfaceType.PLANE:
                continue
            face_normal = face.outward_normal
            if face_normal is None or abs(face_normal.Dot(normal)) <= _ALIGN_DOT:
                continue

            # A host-parallel face is the stroke floor, the raised top, or a
            # coplanar sliver left where the character encloses a counter.
            mid = 0.5 * (smin + smax)
            if mid < -_MIN_DEPTH_MM:
                below += 1
            elif mid > _MIN_DEPTH_MM:
                above += 1
            if abs(mid) > _MIN_DEPTH_MM:
                has_offset_parallel = True
                # The MAX across the offset-parallel faces, not the min.
                # Booleans split a stroke where segments overlap, and the
                # narrow dimension of a split sliver under-reads the stroke.
                # A whole segment face is stroke by length, so the largest of
                # the second-smallest dimensions is the true stroke width --
                # and anything wider than a stroke is a blob the cap rejects.
                stroke_width = max(stroke_width, _second_smallest_dim(face.bbox))

        if not has_offset_parallel:
            return None
        if below > 0 and above > 0:
            return None  # cut and proud at once -- not one character
        if deepest < _MIN_DEPTH_MM or deepest > _SLAB_MM:
            return None
        if _bbox_diag(box) > _GLYPH_MAX_MM * _GLYPH_MAX_DIAG_FACTOR:
            return None
        if stroke_width > _STROKE_MAX_MM:
            return None

        return _Glyph(
            faces=faces,
            raised=above > 0,
            depth=deepest,
            stroke_width=stroke_width,
            center=_bbox_center(box),
        )

    # -- pass 2: connected logotypes ----------------------------------------

    def _logotypes(
        self,
        graph: AttributedAdjacencyGraph,
        hosts: list[AagNode],
        taken: set[int],
        max_component: float,
    ) -> list[FeatureInstance]:
        """One graphic whose letterforms merge into a single component."""
        found: list[FeatureInstance] = []

        for host in hosts:
            normal = host.outward_normal
            origin = host.centroid
            members = _members(
                graph, host, normal, origin, taken, _SEARCH_SLAB_MM, _LOGO_MAX_DIAG_MM
            )
            if len(members) < _LOGO_MIN_FACES:
                continue

            emitted: list[tuple[FeatureInstance, _Component]] = []
            pools: list[_Component] = []

            for faces in _components(graph, members):
                component = self._qualify_logotype(
                    faces, normal, origin, max_component
                )
                if component is None:
                    continue
                if not component.is_logotype:
                    pools.append(component)
                    continue

                face_ids = sorted(face.face_id for face in component.faces)
                taken.update(face_ids)
                feature = FeatureInstance(
                    instance_id="",
                    type=FeatureType.MARKING_TEXT,
                    faces=face_ids,
                    parameters={
                        "marking_type": "raised" if component.raised else "engraved",
                        "logotype": True,
                        "glyph_count": 1,
                        "depth_mm": round(component.depth, 6),
                        # Band width for a connected outline: floor area over
                        # the graphic's diagonal. Crude, but the right order of
                        # magnitude, and the rules only surface it as advice.
                        "stroke_width_mm": round(
                            component.floor_area
                            / max(_bbox_diag(component.box), 1.0),
                            6,
                        ),
                        "host_face": host.face_id,
                    },
                )
                found.append(feature)
                emitted.append((feature, component))

            self._absorb_counter_pools(emitted, pools, taken)

        return found

    @staticmethod
    def _qualify_logotype(
        faces: list[AagNode],
        normal: gp_Dir,
        origin: gp_Pnt,
        max_component: float,
    ) -> Optional[_Component]:
        """Measure a component and decide whether it is an engraved graphic.

        Returns None when the component is not marking at all. Returns a
        component with `is_logotype` false when it is a single-level recess in
        the slab that failed the letterform tests -- it may yet be the dense
        counter half of a graphic emitted alongside it.
        """
        box = Bnd_Box()
        curved = 0
        below = 0
        above = 0
        floor_levels: list[float] = []
        floor_area = 0.0

        for face in faces:
            box.Add(face.bbox)
            face_normal = face.outward_normal
            if face.surface_type is not SurfaceType.PLANE or face_normal is None:
                curved += 1
                continue
            if abs(face_normal.Dot(normal)) <= _ALIGN_DOT:
                continue
            smin, smax = _bbox_signed_range(face.bbox, origin, normal)
            mid = 0.5 * (smin + smax)
            if abs(mid) <= _MIN_DEPTH_MM:
                continue  # coplanar sliver, not a floor
            if mid < 0:
                below += 1
            else:
                above += 1
            floor_levels.append(mid)
            floor_area += face.area

        if not floor_levels:
            return None
        if below > 0 and above > 0:
            return None
        floor_level_min = min(floor_levels)
        floor_level_max = max(floor_levels)
        if floor_level_max - floor_level_min > _LOGO_LEVEL_TOL_MM:
            return None  # more than one floor level is a machined cavity
        depth = max(abs(floor_level_min), abs(floor_level_max))
        if depth < _MIN_DEPTH_MM or depth > _MAX_DEPTH_MM:
            return None

        # A floored engraving bottoms out AT its flat floor: the strokes run
        # from the host down to it and stop. If the component reaches
        # meaningfully deeper than that flat, the flat is not the true bottom
        # -- the strokes are drafted or V-cut and taper to a bottom edge, which
        # is the floorless pass's job. This is what makes the wide search slab
        # safe: without it, this pass would latch onto a mid-level flat and
        # starve pass 4.
        comp_min, comp_max = _bbox_signed_range(box, origin, normal)
        if max(abs(comp_min), abs(comp_max)) > depth + _LOGO_LEVEL_TOL_MM:
            return None

        # Kept even though a real logo scales with the part, because it does
        # work the sparseness test does not: shallow functional patterns --
        # rows of proud islands, edge strips -- are sparse too, and this is
        # what excludes them.
        if _bbox_diag(box) > max_component:
            return None

        level = 0.5 * (floor_level_min + floor_level_max)
        pool = _Component(
            faces=faces,
            box=box,
            level=level,
            depth=depth,
            floor_area=floor_area,
            raised=above > 0,
            is_logotype=False,
        )

        if len(faces) < _LOGO_MIN_FACES:
            return pool
        if curved < _LOGO_MIN_CURVED:
            return pool

        # Sparse floor. Measured across the whole component, not per face:
        # booleans fragment a band floor into rectangles that each fill their
        # own box, so a per-face metric reads text as solid.
        floor_bbox_area = _in_plane_bbox_area(box, normal)
        if floor_bbox_area <= 0.0 or floor_area / floor_bbox_area > _LOGO_FLOOR_FILL_MAX:
            return pool

        pool.is_logotype = True
        return pool

    @staticmethod
    def _absorb_counter_pools(
        emitted: list[tuple[FeatureInstance, _Component]],
        pools: list[_Component],
        taken: set[int],
    ) -> None:
        """Fold a graphic's dense counter pools into the logotype beside it.

        Counter-form engraving leaves the letters standing and cuts everything
        round them, so one graphic arrives as a sparse band component plus
        several dense pools. Left alone the pools fall through to the pocket
        recognizer, which then reports cutter-radius trouble on letterform
        corners. A pool joins a logotype when it sits at the same floor level,
        touches it, and the union still reads as one graphic. A functional
        recess that merely shares the host face fails one of the three.
        """
        for pool in pools:
            for feature, logo in emitted:
                if abs(pool.level - logo.level) > _LOGO_LEVEL_TOL_MM:
                    continue
                if not _boxes_within(logo.box, pool.box, _ABSORB_GAP_MM):
                    continue
                combined = Bnd_Box()
                combined.Add(logo.box)
                combined.Add(pool.box)
                if _bbox_diag(combined) > _ABSORB_MAX_DIAG_MM:
                    continue
                for face in pool.faces:
                    taken.add(face.face_id)
                feature.faces = sorted(
                    set(feature.faces) | {face.face_id for face in pool.faces}
                )
                logo.box = combined
                break

    # -- pass 3: background relief ------------------------------------------

    def _background_reliefs(
        self,
        graph: AttributedAdjacencyGraph,
        hosts: list[AagNode],
        taken: set[int],
        max_component: float,
        shape,
    ) -> list[FeatureInstance]:
        """Letters left standing where the background round them was milled."""
        index = FaceIndex(shape) if shape is not None else None
        found: list[FeatureInstance] = []

        for host in hosts:
            normal = host.outward_normal
            origin = host.centroid
            members = _members(
                graph, host, normal, origin, taken, _SEARCH_SLAB_MM, _LOGO_MAX_DIAG_MM
            )
            if len(members) < _LOGO_MIN_FACES:
                continue

            for faces in _components(graph, members):
                if len(faces) < _LOGO_MIN_FACES:
                    continue
                measured = self._qualify_relief(faces, normal, origin, index)
                if measured is None:
                    continue
                islands, stroke_width, depth, box = measured
                if _bbox_diag(box) > max_component:
                    continue

                face_ids = sorted(face.face_id for face in faces)
                taken.update(face_ids)
                found.append(
                    FeatureInstance(
                        instance_id="",
                        type=FeatureType.MARKING_TEXT,
                        faces=face_ids,
                        parameters={
                            "marking_type": "raised",
                            "background_relief": True,
                            "glyph_count": islands,
                            "depth_mm": round(depth, 6),
                            "stroke_width_mm": round(stroke_width, 6),
                            "host_face": host.face_id,
                        },
                    )
                )

        return found

    @staticmethod
    def _qualify_relief(
        faces: list[AagNode],
        normal: gp_Dir,
        origin: gp_Pnt,
        index: Optional[FaceIndex],
    ) -> Optional[tuple[int, float, float, Bnd_Box]]:
        """Islands, stroke width, depth and extent of a relieved plaque."""
        box = Bnd_Box()
        islands = 0
        above = 0
        stroke_width = 0.0
        floor_levels: list[float] = []

        for face in faces:
            box.Add(face.bbox)
            if face.surface_type is not SurfaceType.PLANE:
                continue
            face_normal = face.outward_normal
            if face_normal is None or abs(face_normal.Dot(normal)) <= _ALIGN_DOT:
                continue
            smin, smax = _bbox_signed_range(face.bbox, origin, normal)
            mid = 0.5 * (smin + smax)

            if abs(mid) <= _RELIEF_LEVEL_TOL_MM:
                # A host-level face inside the component is an island top --
                # the relieved background flows round it. Text strokes are
                # narrow; anything wider is a structural boss and kills the
                # whole component. Width is area over half perimeter, which is
                # exact for a band; a bbox cannot do this, since a C's bbox is
                # the whole letter rather than its stroke.
                perimeter = _face_perimeter(index, face.face_id)
                width = (
                    2.0 * face.area / perimeter
                    if perimeter > 1e-9
                    else _second_smallest_dim(face.bbox)
                )
                if (
                    width > _STROKE_MAX_MM
                    or _bbox_diag(face.bbox) > _GLYPH_MAX_MM * _GLYPH_MAX_DIAG_FACTOR
                ):
                    return None
                islands += 1
                stroke_width = max(stroke_width, width)
            elif mid < -_MIN_DEPTH_MM:
                floor_levels.append(mid)
            elif mid > _MIN_DEPTH_MM:
                above += 1  # proud of the host, so nothing was relieved

        if above > 0:
            return None
        if islands < _RELIEF_MIN_ISLANDS:
            return None
        if not floor_levels:
            return None
        floor_level_min = min(floor_levels)
        floor_level_max = max(floor_levels)
        if floor_level_max - floor_level_min > _LOGO_LEVEL_TOL_MM:
            return None  # a stepped background is a functional cavity
        depth = max(abs(floor_level_min), abs(floor_level_max))
        if depth < _MIN_DEPTH_MM or depth > _MAX_DEPTH_MM:
            return None
        return (islands, stroke_width, depth, box)

    # -- pass 4: floorless engraving ----------------------------------------

    def _floorless_marks(
        self,
        graph: AttributedAdjacencyGraph,
        hosts: list[AagNode],
        taken: set[int],
        max_component: float,
    ) -> list[FeatureInstance]:
        """V-cut and drafted letterforms, which have no flat floor at all.

        The first three passes all key on a flat face at depth. A stroke cut
        with a V-bit or a ball-nose has none: its walls run from the surface
        down to a bottom edge where they meet. Such a component reaches the
        earlier passes and is dropped for having no floor, and its walls then
        misfire as undercut and sharp-edge noise.

        Runs last over unclaimed faces only, so it cannot disturb anything a
        floored pass already recognized.
        """
        found: list[FeatureInstance] = []

        for host in hosts:
            normal = host.outward_normal
            origin = host.centroid
            members = _members(
                graph, host, normal, origin, taken, _SEARCH_SLAB_MM, _LOGO_MAX_DIAG_MM
            )
            if len(members) < _LOGO_MIN_FACES:
                continue

            for faces in _components(graph, members):
                if len(faces) < _LOGO_MIN_FACES:
                    continue
                measured = self._qualify_floorless(
                    faces, normal, origin, max_component
                )
                if measured is None:
                    continue
                depth, stroke_width = measured

                face_ids = sorted(face.face_id for face in faces)
                taken.update(face_ids)
                found.append(
                    FeatureInstance(
                        instance_id="",
                        type=FeatureType.MARKING_TEXT,
                        faces=face_ids,
                        parameters={
                            "marking_type": "engraved",
                            "logotype": True,
                            "floorless": True,
                            "glyph_count": 1,
                            "depth_mm": round(depth, 6),
                            "stroke_width_mm": round(stroke_width, 6),
                            "host_face": host.face_id,
                        },
                    )
                )

        return found

    @staticmethod
    def _qualify_floorless(
        faces: list[AagNode],
        normal: gp_Dir,
        origin: gp_Pnt,
        max_component: float,
    ) -> Optional[tuple[float, float]]:
        """Depth and stroke width of a floorless mark, or None."""
        box = Bnd_Box()
        curved = 0
        flats = 0
        deepest = 0.0
        walls: list[_Wall] = []

        for face in faces:
            box.Add(face.bbox)
            smin, smax = _bbox_signed_range(face.bbox, origin, normal)
            deepest = max(deepest, abs(smin), abs(smax))
            if face.surface_type is not SurfaceType.PLANE:
                curved += 1
                continue
            face_normal = face.outward_normal
            if face_normal is None:
                continue

            if abs(face_normal.Dot(normal)) > _ALIGN_DOT:
                # An offset-parallel face: a flat floor at depth, or a proud
                # top. Either one disqualifies -- floored marks belong to the
                # earlier passes, and proud geometry is not engraving.
                if abs(0.5 * (smin + smax)) > _MIN_DEPTH_MM:
                    flats += 1
                continue

            # A wall. Its outward in-plane direction points away from the
            # material, which is to say across the stroke at the opposing
            # wall: the outward normal with the host-normal component removed.
            direction = gp_Vec(face_normal)
            direction = direction.Subtracted(
                gp_Vec(normal).Multiplied(face_normal.Dot(normal))
            )
            if direction.Magnitude() < 1e-6:
                continue
            walls.append(_Wall(direction=gp_Dir(direction), point=face.centroid))

        if flats > 0:
            return None
        if len(walls) < _LOGO_MIN_FACES:
            return None  # too little wall mass to be a letterform
        if curved < _LOGO_MIN_CURVED:
            return None  # a purely rectilinear recess is functional
        if deepest < _MIN_DEPTH_MM or deepest > _MAX_DEPTH_MM:
            return None
        if _bbox_diag(box) > max_component:
            return None

        # A stroke is a pair of opposing walls, and the gap between them is
        # the stroke width. A letterform's strokes are narrow; the opposing
        # walls of a window or a cutout are far apart. Take, per wall, the
        # smallest gap to a wall facing back at it, and use the median so
        # unpaired corner and terminal walls do not skew the answer.
        gaps: list[float] = []
        for wall in walls:
            best = None
            for other in walls:
                if other is wall:
                    continue
                if wall.direction.Dot(other.direction) > _WALL_OPPOSING_DOT:
                    continue
                gap = gp_Vec(wall.point, other.point).Dot(gp_Vec(wall.direction))
                if gap <= _MIN_DEPTH_MM:
                    continue  # behind this wall, not across the stroke
                best = gap if best is None else min(best, gap)
            if best is not None:
                gaps.append(best)

        if not gaps:
            return None  # no opposing pairs, so nothing stroke-like here
        gaps.sort()
        stroke_width = gaps[len(gaps) // 2]
        if stroke_width > _STROKE_MAX_MM:
            return None
        return (deepest, stroke_width)


# =============================================================================
# Geometry helpers
# =============================================================================


def _members(
    graph: AttributedAdjacencyGraph,
    host: AagNode,
    normal: gp_Dir,
    origin: gp_Pnt,
    taken: set[int],
    slab: float,
    max_diag: float,
) -> list[AagNode]:
    """Faces living entirely in a thin slab either side of the host plane.

    Planar members must be parallel or square to the host, because a stroke is
    prismatic -- that is what keeps V-grooves and tilted micro-optic facets
    out. Small non-planar members are let through: dot-peen dimples and the
    rounds on stroke ends are curved by nature.
    """
    members: list[AagNode] = []
    for node in graph.nodes:
        if node.face_id == host.face_id or node.face_id in taken:
            continue
        if node.bbox.IsVoid():
            continue
        smin, smax = _bbox_signed_range(node.bbox, origin, normal)
        if smin < -slab or smax > slab:
            continue
        if _bbox_diag(node.bbox) > max_diag:
            continue
        if node.surface_type is SurfaceType.PLANE:
            node_normal = node.outward_normal
            if node_normal is None:
                continue
            alignment = abs(node_normal.Dot(normal))
            if alignment < _ALIGN_DOT and alignment > 1.0 - _ALIGN_DOT:
                continue
        members.append(node)
    return members


def _components(
    graph: AttributedAdjacencyGraph, members: list[AagNode]
) -> list[list[AagNode]]:
    """Connected groups of member faces, walking adjacency within the set.

    Seeded and returned in ascending face id order, so the same part always
    yields the same components in the same order.
    """
    by_id = {node.face_id: node for node in members}
    seen: set[int] = set()
    groups: list[list[AagNode]] = []

    for face_id in sorted(by_id):
        if face_id in seen:
            continue
        seen.add(face_id)
        stack = [face_id]
        group: list[AagNode] = []
        while stack:
            current = stack.pop()
            group.append(by_id[current])
            for edge in graph.edges_of(current):
                other = edge.other_face(current)
                if other in by_id and other not in seen:
                    seen.add(other)
                    stack.append(other)
        groups.append(sorted(group, key=lambda node: node.face_id))

    return groups


def _link_by_proximity(glyphs: list[_Glyph], radius: float) -> list[list[_Glyph]]:
    """Group glyphs whose centres sit within `radius` of each other."""
    parent = list(range(len(glyphs)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(glyphs)):
        for j in range(i + 1, len(glyphs)):
            if glyphs[i].center.Distance(glyphs[j].center) < radius:
                parent[root(i)] = root(j)

    clusters: dict[int, list[_Glyph]] = {}
    for i, glyph in enumerate(glyphs):
        clusters.setdefault(root(i), []).append(glyph)
    return [clusters[key] for key in sorted(clusters)]


#: Corner coordinates by box identity. Every pass here measures every
#: candidate face against every host plane, so the same eight corners get
#: unpacked from the same box hundreds of times over -- on a bladed disk that
#: alone was five seconds of OpenCascade accessor calls.
#:
#: Keyed by identity and cleared at the start of each run. The graph holds
#: its boxes for the whole of one analysis so they cannot be collected
#: underneath it, but across analyses a freed box could hand its address to a
#: new one, and a stale entry would then be silently wrong.
_CORNER_CACHE: dict[int, tuple] = {}


def _corners(box: Bnd_Box) -> tuple:
    key = id(box)
    cached = _CORNER_CACHE.get(key)
    if cached is None:
        if box.IsVoid():
            cached = ()
        else:
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            cached = tuple(
                (x, y, z)
                for x in (xmin, xmax)
                for y in (ymin, ymax)
                for z in (zmin, zmax)
            )
        _CORNER_CACHE[key] = cached
    return cached


def _bbox_signed_range(
    box: Bnd_Box, origin: gp_Pnt, normal: gp_Dir
) -> tuple[float, float]:
    """Least and greatest signed distance from a plane to a box's corners.

    Negative is into the material when the normal is the host's outward one.
    """
    corners = _corners(box)
    if not corners:
        return (0.0, 0.0)
    ox, oy, oz = origin.X(), origin.Y(), origin.Z()
    nx, ny, nz = normal.X(), normal.Y(), normal.Z()
    low = high = None
    for x, y, z in corners:
        offset = (x - ox) * nx + (y - oy) * ny + (z - oz) * nz
        if low is None or offset < low:
            low = offset
        if high is None or offset > high:
            high = offset
    return (low, high)


def _bbox_diag(box: Bnd_Box) -> float:
    if box.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin
    return (dx * dx + dy * dy + dz * dz) ** 0.5


def _bbox_center(box: Bnd_Box) -> gp_Pnt:
    if box.IsVoid():
        return gp_Pnt()
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return gp_Pnt(0.5 * (xmin + xmax), 0.5 * (ymin + ymax), 0.5 * (zmin + zmax))


def _second_smallest_dim(box: Bnd_Box) -> float:
    """The middle of a box's three dimensions.

    For a stroke floor face -- width by length by nothing -- this is the width.
    """
    if box.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return sorted((xmax - xmin, ymax - ymin, zmax - zmin))[1]


def _in_plane_bbox_area(box: Bnd_Box, normal: gp_Dir) -> float:
    """Footprint of a box projected onto the host plane.

    The axis most nearly along the normal is the depth direction and drops out;
    the other two give the area the floor would fill if it were solid.
    """
    if box.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    dims = (xmax - xmin, ymax - ymin, zmax - zmin)
    components = (abs(normal.X()), abs(normal.Y()), abs(normal.Z()))
    depth_axis = max(range(3), key=lambda i: components[i])
    area = 1.0
    for i in range(3):
        if i != depth_axis:
            area *= max(dims[i], 1e-6)
    return area


def _boxes_within(first: Bnd_Box, second: Bnd_Box, gap: float) -> bool:
    """Whether the boxes touch once the first is dilated by `gap`."""
    if first.IsVoid() or second.IsVoid():
        return False
    axmin, aymin, azmin, axmax, aymax, azmax = first.Get()
    bxmin, bymin, bzmin, bxmax, bymax, bzmax = second.Get()
    return (
        axmin - gap <= bxmax
        and bxmin <= axmax + gap
        and aymin - gap <= bymax
        and bymin <= aymax + gap
        and azmin - gap <= bzmax
        and bzmin <= azmax + gap
    )


def _face_perimeter(index: Optional[FaceIndex], face_id: int) -> float:
    """Total boundary length of one face, or zero when the shape is not to hand.

    The relief pass needs it because area over half perimeter gives the width
    of a band, and the width of a band is the width of a letter stroke.
    """
    if index is None or face_id < 1 or face_id > len(index):
        return 0.0
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(index.face_at(face_id), props)
    return props.Mass()
