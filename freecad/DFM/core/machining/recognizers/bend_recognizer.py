# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes press-brake bends.

A bend is where the brake folded the blank. In the B-rep it arrives as a
matched pair of coaxial cylinders -- the concave inside of the fold at radius
r, and the convex outside at r plus the gauge -- bridging two flat panels
through tangent edges.

The radial offset between the two cylinders *is* the material thickness, and
that is the whole discriminator. A machined fillet has an inside radius and
nothing behind it; only metal that was folded carries its own outside radius
one gauge out on the same axis. It is the same signature the sheet-metal
classifier keys on, which is why a part that has no bends is not sheet in the
first place.

Bends run on sheet parts only. On a milled part the same pair of coaxial
cylinders is a bore inside a boss, so the recognizer stands down entirely
unless the analyzer classified the part as sheet metal.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Ax1, gp_Pnt, gp_Vec

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder

from ...utils.geometry import FaceIndex
from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import FeatureInstance, FeatureType
from ..process_classifier import PartProcessType
from ..sheet_folds import (
    CoplanarPatchIndex,
    FoldSign,
    axes_colinear,
    band_axial_span,
    band_mean_radius,
    bend_band_is_sheared_free,
    cone_radius_at,
    cone_semi_angle_along,
    detect_sharp_folds,
    fold_sign,
)
from .base import FeatureRecognizer, axes_are_coaxial, cylinder_length


# The feature type this recognizer emits. Spelled out here until
# `features.FeatureType` carries it: the string is the contract, because it is
# what rules match on and what a saved analysis stores.
BEND = FeatureType.BEND


# The two cylinders of one bend are the same fold seen from both sides, so
# their axes are the same line to well inside modelling noise -- much tighter
# than the general coaxial test, which has to tolerate separately modelled
# bores.
_AXIS_DOT_TOL = 0.999
_LINE_DISTANCE_TOL_MM = 0.2

# How far the measured radial offset may sit from the classified gauge before
# the pair is something other than a fold.
_GAUGE_TOL_MM = 0.2

# Several bends can share one fold axis line: think two parallel flanges
# brought up off one base, whose bend lines happen to be colinear. Without an
# axial overlap test the first band's inside skin pairs with the second band's
# outside skin and the result is a chimera bend spanning two real ones. The
# margin keeps two bands that merely abut from counting as overlapping.
_AXIAL_OVERLAP_MARGIN_MM = 0.5

# A bend joins sheet FLATS. The little tangent walls at the rounded end of an
# emboss or a cutout are also a coaxial gauge-offset pair with tangent
# neighbours -- a perfect counterfeit hem -- and this is what refuses them:
# a real panel is many times the gauge across, so its area dwarfs the gauge
# squared. Expressed in gauges so it scales with the material.
_PANEL_AREA_FLOOR_GAUGES = 20.0

# Folded back on itself. At this angle the two panels are face to face and the
# brake made a hem, not a corner.
_HEM_MIN_ANGLE_DEG = 175.0

# The BEND_ANGLE_INCONSISTENT guard: how far the swept arc may sit from the
# fold angle before something is taken to have been merged into the band.
# Measured over every bent fixture the clean population tops out near 0.15
# degrees and there is no second population, so this is a guard rather than
# a fitted threshold; the smallest real merge, one unclaimed fragment of a
# split band, is tens of degrees.
_SWEPT_ANGLE_TOL_DEG = 2.0

# How close a skin fragment's radius must be to the bend's own. Set at the
# order of the padding OCCT adds to a curved face's box after a boolean.
_SAME_RADIUS_TOL_MM = 0.05


class BendRecognizer(FeatureRecognizer):
    """Recognizes bends on sheet-metal parts, in four fold families.

    Every fold carries ``bend_family`` naming which of the four it is, and a
    consumer reads that rather than the surface types, because the families
    deliberately do not carry the same parameters:

    * ``cylindrical`` -- the press-brake bend: a concave cylinder, its convex
      partner one gauge out, two tangent planar panels. A hem is this family
      with ``is_hem`` set; that is a statement about the angle, not a fifth
      kind of surface.
    * ``conical`` -- a tapered flange, whose fold radius varies along the
      fold line and arrives as a cone pair. It reports the radius range and
      half-angle, not one ``inner_radius_mm``, because a taper has no single
      radius and naming one end would be worse than naming neither.
    * ``rolled`` -- a fold that blends cylinder into cylinder, with no planar
      panel. No ``angle_deg`` and no sign, since both are read off panel
      normals and a curved panel has none; it carries
      ``refusal = FLANGE_FACE_NOT_PLANAR`` because nothing downstream can
      walk it yet.
    * ``sharp`` -- two panels meeting square with no modelled radius. Emitted
      by :func:`append_sharp_fold_bends`, a post-pass, and it owns no faces:
      the fold surface does not exist, and its two "faces" would be the two
      largest panels on the part.

    Every family but ``cylindrical`` names its refusal. Recognising a fold is
    not the same as being able to unfold it, and a refusal must never
    degrade into a default.
    """

    prefix = "bd"

    @property
    def name(self) -> str:
        return "Bend Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        gauge = self._sheet_gauge()
        if gauge <= 0.0:
            return []  # no gauge, no sheet, no bends

        patch_index = CoplanarPatchIndex(graph)
        spans = _face_spans(shape)
        taken: set[int] = set()
        found: list[FeatureInstance] = []

        # Pass 0 seeds on cylinders, pass 1 on cones. A cone is developable
        # too -- generator to a ray through the apex, baseline to an arc -- so
        # a tapered flange is a fold like any other and only the recognition
        # was missing.
        for pass_index, seed_type in enumerate((SurfaceType.CYLINDER, SurfaceType.CONE)):
            seeds = graph.nodes_by_surface_type(seed_type)
            # Seed on the INSIDE face of the pair: the one that looks into the
            # fold the way a bore looks into its hole.
            for inner in seeds:
                if not inner.is_internal or inner.cyl_cone_axis is None:
                    continue
                if inner.face_id in taken:
                    continue

                outer = _outside_face(seeds, inner, gauge, taken, conical=pass_index == 1)
                if outer is None:
                    continue

                panels = _tangent_panel_groups(graph, patch_index, inner)

                # THE LAST ARC OF A SCROLL HAS ONE PANEL, NOT TWO. A press
                # brake joins two flats, but a rolled scroll's tip is tangent
                # to the previous arc on one side and runs out to the blank's
                # own radial cut edge on the other, which is never tangent. It
                # is a fold by every other measure, so refusing it reported a
                # three-arc scroll as two bends. Admitted only with the cut
                # edge to show for it (a planar face bridging both skins,
                # sharp to each, one gauge thick); the other one-panel bands
                # (a bead on a curved panel, lance crowns) have no such cap or
                # a cap several gauges wide. Cylindrical only: nothing has a
                # conical run-out to measure it on.
                scroll_runout = False
                if len(panels) == 1 and pass_index == 0:
                    cap_thick = _runout_cap_thickness(graph, inner, outer)
                    scroll_runout = cap_thick > 0.0 and abs(cap_thick - gauge) <= _GAUGE_TOL_MM
                if len(panels) != 2 and not scroll_runout:
                    continue

                # A scroll run-out has one panel, so only that one is floored.
                floor = _PANEL_AREA_FLOOR_GAUGES * gauge * gauge
                if panels[0].area < floor or (not scroll_runout and panels[1].area < floor):
                    continue

                # A strip sheared free on two sides was never on a press
                # brake: it is the crown of a lanced arch. Declined here, so
                # no claim is made that the formed pass would then trip on and
                # no flange rule is ever pointed at a ramp.
                if pass_index == 0 and bend_band_is_sheared_free(graph, inner, outer, gauge):
                    continue

                # One panel and that one a cylinder: no pair of flats to take
                # an angle between, exactly as for any other rolled fold.
                both_planar = not scroll_runout and panels[0].planar and panels[1].planar
                angle_deg = 0.0
                if both_planar:
                    first = panels[0].rep.outward_normal
                    second = panels[1].rep.outward_normal
                    if first is None or second is None:
                        continue
                    # The bend angle read off the panels themselves: 90
                    # degrees for a right-angle corner, approaching 180 for a
                    # hem.
                    angle_deg = math.degrees(first.Angle(second))

                taken.add(inner.face_id)
                taken.add(outer.face_id)
                faces = [inner.face_id, outer.face_id]
                faces.extend(_split_skin_fragments(seeds, inner, outer, taken))

                # The swept angle: the arc the fold surface covers about its
                # own axis, summed over the inside skin's pieces. A second,
                # independent measurement of the fold angle; a disagreement
                # means something got merged into the band.
                inner_r = band_mean_radius(inner)
                outer_r = band_mean_radius(outer)
                swept_rad = 0.0
                for face_id in faces:
                    if not graph.has_node(face_id):
                        continue
                    r = band_mean_radius(graph.node(face_id))
                    if abs(r - inner_r) <= abs(r - outer_r):
                        swept_rad += spans.get(face_id, (0.0, 0.0))[0]
                swept_deg = math.degrees(swept_rad)

                # The sign's frame: from the larger panel to the smaller.
                fi, ti = 0, 1
                if not scroll_runout and (
                    panels[1].area > panels[0].area
                    or (
                        panels[1].area == panels[0].area
                        and panels[1].rep.face_id < panels[0].rep.face_id
                    )
                ):
                    fi, ti = 1, 0
                axis = inner.cyl_cone_axis
                direction = axis.Direction()
                origin = axis.Location()
                sign = FoldSign()
                if both_planar:
                    sign = fold_sign(
                        panels[fi].rep.outward_normal,
                        panels[ti].rep.outward_normal,
                        direction,
                    )
                is_hem = both_planar and angle_deg >= _HEM_MIN_ANGLE_DEG
                if pass_index == 1:
                    family = "conical"
                else:
                    family = "cylindrical" if both_planar else "rolled"

                parameters: dict = {"bend_family": family}
                if pass_index == 0:
                    parameters["inner_radius_mm"] = round(inner.cyl_radius, 6)
                    parameters["outer_radius_mm"] = round(outer.cyl_radius, 6)
                    parameters["thickness_mm"] = round(outer.cyl_radius - inner.cyl_radius, 6)
                else:
                    parameters["inner_radius_min_mm"] = round(min(inner.cone_r0, inner.cone_r1), 6)
                    parameters["inner_radius_max_mm"] = round(max(inner.cone_r0, inner.cone_r1), 6)
                    parameters["cone_semi_angle_deg"] = round(
                        math.degrees(inner.cone_semi_angle), 6
                    )
                    parameters["thickness_mm"] = round(gauge, 6)
                if both_planar:
                    parameters["angle_deg"] = round(angle_deg, 6)
                    parameters["is_hem"] = is_hem
                parameters["swept_angle_deg"] = round(swept_deg, 6)
                parameters["length_mm"] = round(
                    spans.get(inner.face_id, (0.0, cylinder_length(inner)))[1], 6
                )
                parameters["axis"] = [direction.X(), direction.Y(), direction.Z()]
                parameters["axis_origin"] = [origin.X(), origin.Y(), origin.Z()]
                parameters["panel_a"] = panels[0].rep.face_id
                # A scroll run-out has no second panel, so the key is omitted
                # rather than filled with the cut edge: naming that would make
                # the band read "cylindrical" and fire the flat-fold rules on a
                # scroll, and the panel graph would take a cut edge for a
                # flange. The graph records a one-sided fold as unlinked.
                if not scroll_runout:
                    parameters["panel_b"] = panels[1].rep.face_id
                if sign.sign_known:
                    parameters["signed_angle_deg"] = round(sign.signed_deg, 6)
                    parameters["fold_sign_from"] = panels[fi].rep.face_id
                    parameters["fold_sign_to"] = panels[ti].rep.face_id
                if sign.toward_known and sign.toward is not None:
                    parameters["fold_toward"] = [
                        sign.toward.X(),
                        sign.toward.Y(),
                        sign.toward.Z(),
                    ]
                # Compared against the about-axis turn, which equals the swept
                # arc for both banded families; the angle between normals
                # differs from it on a cone for purely geometric reasons. A
                # hem keeps the normal angle: antiparallel normals cannot be
                # signed.
                if not both_planar:
                    parameters["refusal"] = "FLANGE_FACE_NOT_PLANAR"
                elif not sign.sign_known and not is_hem:
                    parameters["refusal"] = "FOLD_SIGN_AMBIGUOUS"
                else:
                    reference = abs(sign.signed_deg) if sign.sign_known else angle_deg
                    if abs(swept_deg - reference) > _SWEPT_ANGLE_TOL_DEG:
                        parameters["refusal"] = "BEND_ANGLE_INCONSISTENT"

                found.append(
                    FeatureInstance(
                        instance_id=self.instance_id(len(found)),
                        type=BEND,
                        faces=faces,
                        parameters=parameters,
                    )
                )

        return found

    # -- gating ---------------------------------------------------------------

    def _sheet_gauge(self) -> float:
        """The classified sheet thickness, or zero when the part is not sheet.

        The recognizer is handed the classification rather than working it out
        again, and it is the gate as much as the measurement: on a milled part
        a coaxial pair of cylinders is a bore inside a boss, and calling that
        a bend would put brake rules on a part that never sees a brake.
        """
        process = getattr(self, "part_process", None)
        if process is None or process.type is not PartProcessType.SHEET_METAL:
            return 0.0
        return float(getattr(process, "sheet_thickness_mm", 0.0) or 0.0)


def append_sharp_fold_bends(
    features: list,
    graph: AttributedAdjacencyGraph,
    gauge: float,
    prefix: str = "shf",
) -> None:
    """Emit one ``sharp`` BEND per zero-radius fold. Claims no faces.

    A post-pass rather than part of :meth:`BendRecognizer.recognize`: the
    detector must exclude faces a formed hood owns (its wall-to-crest corner
    is coined sharp by design), and the formed pass runs after the bend pass.
    Idempotent: a fold whose ``fold_key`` a BEND already carries is skipped,
    so calling it twice adds each fold once.
    """
    if gauge <= 0.0:
        return
    already = {
        str(f.parameters.get("fold_key"))
        for f in features
        if f.type == BEND and f.parameters.get("fold_key")
    }
    count = 0
    for fold in detect_sharp_folds(graph, gauge, features):
        if fold.fold_key in already:
            continue
        parameters: dict = {
            "bend_family": "sharp",
            "fold_key": fold.fold_key,
            "inner_radius_mm": 0.0,
            "outer_radius_mm": 0.0,
            "thickness_mm": round(gauge, 6),
            "angle_deg": round(fold.fold_angle_deg, 6),
            "swept_angle_deg": 0.0,  # no surface, no arc
            "length_mm": round(fold.crease_length_mm, 6),
            "panel_a": fold.panel_a,
            "panel_b": fold.panel_b,
        }
        if fold.crease_dir is not None:
            parameters["axis"] = [fold.crease_dir.X(), fold.crease_dir.Y(), fold.crease_dir.Z()]
        if fold.crease_midpoint is not None:
            point = fold.crease_midpoint
            parameters["axis_origin"] = [point.X(), point.Y(), point.Z()]
        if fold.crease_dir is not None:
            sign = fold_sign(fold.n_a, fold.n_b, fold.crease_dir)
            if sign.sign_known:
                parameters["signed_angle_deg"] = round(sign.signed_deg, 6)
                parameters["fold_sign_from"] = fold.panel_a
                parameters["fold_sign_to"] = fold.panel_b
            if sign.toward_known and sign.toward is not None:
                parameters["fold_toward"] = [sign.toward.X(), sign.toward.Y(), sign.toward.Z()]
        parameters["refusal"] = "SHARP_FOLD_UNMODELLED"
        features.append(
            FeatureInstance(
                instance_id=f"{prefix}_{count}",
                type=BEND,
                faces=[],
                parameters=parameters,
            )
        )
        count += 1


# What keeps a drawn hole flange a hole. A punched-and-drawn collar is the
# same skin signature as a fold band -- concave bore, convex collar one gauge
# out, coaxial, overlapping -- and it is a genuine hole. The discriminant is
# closure: a collar wraps its axis (fraction -> 1), a fold band covers an arc
# (a 90-degree scroll arc 0.25, the widest fold in the corpus 0.625). Reasoned,
# not measured: the corpus has no drawn-collar fixture.
_SKIN_WRAP_CEILING = 0.9

#: The reads a lone cylinder or cone can produce. A POCKET, SLOT or STEP always
#: carries a planar member, so it could never pass the every-face test.
_SKIN_MACHINING_READS = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.BREAKTHROUGH_HOLE,
    FeatureType.PARTIAL_BORE,
    FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK,
    FeatureType.THREADED_HOLE,
)


def _band_wrap_fraction(node: AagNode) -> float:
    """Fraction of a full revolution the band covers; 1 when unmeasurable.

    The mean band radius rather than ``cyl_radius``, so a cone reads right.
    """
    if node.cyl_cone_axis is None:
        return 1.0
    direction = node.cyl_cone_axis.Direction()
    origin = node.cyl_cone_axis.Location()
    low, high = band_axial_span(node, origin, direction)
    full = 2.0 * math.pi * band_mean_radius(node) * (high - low)
    return node.area / full if full > 1.0e-9 else 1.0


def _is_gauge_offset_skin(
    graph: AttributedAdjacencyGraph, band: AagNode, gauge: float
) -> bool:
    """True when ``band`` is one skin of a gauge-offset pair: metal, not a void.

    Asked from either side: a concave band looks outward for a convex partner
    one gauge out, a convex band inward for a concave one. The orientation
    pair keeps a counterbore out -- a stepped bore's two cylinders are both
    concave, so a step that happens to measure one gauge never lands here.
    """
    if gauge <= 0.0:
        return False
    if band.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
        return False
    if band.cyl_cone_axis is None:
        return False
    if _band_wrap_fraction(band) >= _SKIN_WRAP_CEILING:
        return False
    direction = band.cyl_cone_axis.Direction()
    origin = band.cyl_cone_axis.Location()
    b_lo, b_hi = band_axial_span(band, origin, direction)
    want = gauge if band.is_internal else -gauge
    for cand in graph.nodes_by_surface_type(band.surface_type):
        if cand is band or cand.face_id == band.face_id:
            continue
        if cand.is_internal == band.is_internal:
            continue
        if not axes_colinear(cand.cyl_cone_axis, band.cyl_cone_axis):
            continue
        c_lo, c_hi = band_axial_span(cand, origin, direction)
        if c_hi < b_lo + 0.5 or c_lo > b_hi - 0.5:
            continue  # no axial overlap
        if band.surface_type is SurfaceType.CYLINDER:
            if abs((cand.cyl_radius - band.cyl_radius) - want) > _GAUGE_TOL_MM:
                continue
        else:
            # One gauge measured normal to the surface, as the cone pairing
            # does it: the radius difference alone loosens as the taper
            # steepens.
            band_angle = cone_semi_angle_along(band, direction)
            if abs(cone_semi_angle_along(cand, direction) - band_angle) > 0.02:
                continue
            station = 0.5 * (max(b_lo, c_lo) + min(b_hi, c_hi))
            gap = (
                cone_radius_at(cand, origin, direction, station)
                - cone_radius_at(band, origin, direction, station)
            ) * math.cos(band_angle)
            if abs(gap - want) > 0.25:
                continue
        if _band_wrap_fraction(cand) >= _SKIN_WRAP_CEILING:
            continue
        return True
    return False


def drop_sheet_skin_machining_reads(
    features: list, graph: AttributedAdjacencyGraph, gauge: float
) -> list:
    """Retract the machining reads that are nothing but a sheet skin.

    Sheet-only, beside the bend-duplicate fillet drop. That one reaches only
    faces a BEND claimed; a band the bend pass declined stays unclaimed and
    the hole pass's read of it survives -- the tip arc of a rolled scroll came
    back as a BREAKTHROUGH_HOLE as deep as the part is wide, billed as a hole
    and hiding the fold from the invisible-fold census. Every face must be
    skin, so a countersink (whose cone cap has no partner) is kept whole.
    """
    if gauge <= 0.0:
        return features

    def skin_read(feature) -> bool:
        if feature.type not in _SKIN_MACHINING_READS or not feature.faces:
            return False
        for face_id in feature.faces:
            if not graph.has_node(face_id):
                return False
            if not _is_gauge_offset_skin(graph, graph.node(face_id), gauge):
                return False
        return True

    return [f for f in features if not skin_read(f)]


class _PanelGroup:
    """One physical panel beside a fold band: coplanar flats, or a curved band."""

    __slots__ = ("rep", "area", "plane", "planar")

    def __init__(self, rep: AagNode, area: float, plane, planar: bool) -> None:
        self.rep = rep
        self.area = area
        self.plane = plane
        self.planar = planar


def _runout_cap_thickness(
    graph: AttributedAdjacencyGraph, inner: AagNode, outer: AagNode
) -> float:
    """Thickness of the cut end closing a fold band, or -1 when there is none.

    The cap is the blank's sheared end a scroll's last arc dies into: planar,
    adjacent to both skins, and joined to each by a sharp edge (it is radial,
    so never tangent). Its smallest in-plane extent is the gauge, which the
    caller checks, so a large face that happens to touch both skins cannot
    pose as a cut edge.
    """
    best = -1.0
    for edge in graph.edges_of(inner.face_id):
        if edge.concavity is Concavity.TANGENT:
            continue
        cap_id = edge.other_face(inner.face_id)
        if not graph.has_node(cap_id):
            continue
        cap = graph.node(cap_id)
        if cap.surface_type is not SurfaceType.PLANE or cap.bbox.IsVoid():
            continue
        # It must close the outer skin too, non-tangentially. A face touching
        # only one skin is a flange or a floor, not a cut end.
        touches_outer = any(
            other.concavity is not Concavity.TANGENT
            and other.other_face(outer.face_id) == cap_id
            for other in graph.edges_of(outer.face_id)
        )
        if not touches_outer:
            continue
        # One bbox dimension of a plane is ~0; the next smallest is the cap's
        # thickness.
        thick = sorted(cap.bbox_dims())[1]
        if best < 0.0 or thick < best:
            best = thick
    return best


def _tangent_panel_groups(
    graph: AttributedAdjacencyGraph, patch_index: CoplanarPatchIndex, band: AagNode
) -> list[_PanelGroup]:
    """The developable patches a fold band blends into, one per physical panel.

    A bend has exactly two. Boolean seams split one flat into coplanar
    fragments, all tangent to the band, so planes are grouped by the plane
    they lie in. A rolled section blends cylinder into cylinder and has no
    planar neighbour at all, so cylinders and cones count too, grouped by
    surface type, radius and axis line. That is the general statement --
    exactly two tangent developable neighbour patches -- rather than a
    special case for rolled parts.
    """
    groups: list[_PanelGroup] = []
    for edge in graph.edges_of(band.face_id):
        if edge.concavity is not Concavity.TANGENT:
            continue
        other = edge.other_face(band.face_id)
        if not graph.has_node(other):
            continue
        neighbour = graph.node(other)

        if neighbour.surface_type is SurfaceType.PLANE:
            plane = patch_index.plane_of(neighbour.face_id)
            if plane is None:
                continue
            for group in groups:
                if not group.planar or group.plane != plane:
                    continue
                group.area += neighbour.area
                if neighbour.area > group.rep.area:
                    group.rep = neighbour
                break
            else:
                groups.append(_PanelGroup(neighbour, neighbour.area, plane, True))
            continue

        if neighbour.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
            continue
        for group in groups:
            if group.planar:
                continue
            if group.rep.surface_type is not neighbour.surface_type:
                continue
            # `cyl_radius`, as the reference reads it: on two cones it is 0
            # for both, so coaxial cones always group.
            if abs(group.rep.cyl_radius - neighbour.cyl_radius) > 0.05:
                continue
            if not axes_colinear(group.rep.cyl_cone_axis, neighbour.cyl_cone_axis):
                continue
            group.area += neighbour.area
            if neighbour.area > group.rep.area:
                group.rep = neighbour
            break
        else:
            groups.append(_PanelGroup(neighbour, neighbour.area, None, False))
    return groups


def _outside_face(
    seeds: Sequence[AagNode],
    inner: AagNode,
    gauge: float,
    taken: set[int],
    conical: bool = False,
) -> Optional[AagNode]:
    """The convex band one gauge out on the inside face's own axis.

    For a cone the gap is measured NORMAL to the surface: two cones one gauge
    apart differ in radius by gauge / cos(half-angle), out by 41% at a
    45-degree taper.
    """
    axis = inner.cyl_cone_axis
    direction = axis.Direction()
    origin = axis.Location()
    low, high = band_axial_span(inner, origin, direction)

    for candidate in seeds:
        if candidate.is_internal or candidate.cyl_cone_axis is None:
            continue
        if candidate.face_id in taken:
            continue
        if not axes_are_coaxial(
            candidate.cyl_cone_axis,
            axis,
            direction_dot=_AXIS_DOT_TOL,
            line_distance=_LINE_DISTANCE_TOL_MM,
        ):
            continue
        candidate_low, candidate_high = band_axial_span(candidate, origin, direction)
        if candidate_high < low + _AXIAL_OVERLAP_MARGIN_MM:
            continue
        if candidate_low > high - _AXIAL_OVERLAP_MARGIN_MM:
            continue
        if not conical:
            if abs((candidate.cyl_radius - inner.cyl_radius) - gauge) > _GAUGE_TOL_MM:
                continue
        else:
            if abs(
                cone_semi_angle_along(candidate, direction)
                - cone_semi_angle_along(inner, direction)
            ) > 0.02:
                continue
            s = 0.5 * (max(low, candidate_low) + min(high, candidate_high))
            normal_gap = abs(
                cone_radius_at(candidate, origin, direction, s)
                - cone_radius_at(inner, origin, direction, s)
            ) * math.cos(inner.cone_semi_angle)
            if abs(normal_gap - gauge) > 0.25:
                continue
        return candidate
    return None


def _face_spans(shape) -> dict[int, tuple[float, float]]:
    """(U, V) parameter spans of every cylindrical and conical face.

    U is the angle a band covers about its own axis -- the swept angle of a
    fold. V is its extent along the generator, which is what ``length_mm``
    reports. One pass over the shape rather than one per face.
    """
    out: dict[int, tuple[float, float]] = {}
    if shape is None:
        return out
    index = FaceIndex(shape)
    for face_id in range(1, len(index) + 1):
        face = index.face_at(face_id)
        if face.IsNull():
            continue
        adaptor = BRepAdaptor_Surface(face, True)
        if adaptor.GetType() not in (GeomAbs_Cylinder, GeomAbs_Cone):
            continue
        out[face_id] = (
            abs(adaptor.LastUParameter() - adaptor.FirstUParameter()),
            abs(adaptor.LastVParameter() - adaptor.FirstVParameter()),
        )
    return out


# =============================================================================
# Geometry helpers
# =============================================================================


def _split_skin_fragments(
    cylinders: Sequence[AagNode],
    inner: AagNode,
    outer: AagNode,
    taken: set[int],
) -> list[int]:
    """Claim the other pieces of a skin that arrived in fragments.

    A skin cut where it turns under a panel comes back as two faces that do
    not even share an edge, so the bend claimed one and the blend pass
    reported the other as a standalone fillet: the same metal described
    twice. A fragment has the same axis line and radius AND overlaps the
    inside skin along the axis. The overlap is what separates it from a bend
    line interrupted by a relief cut, which is two bends on one axis and one
    radius occupying disjoint stretches of it.
    """
    axis = inner.cyl_cone_axis
    direction = axis.Direction()
    axis_vec = gp_Vec(direction)
    origin = axis.Location()
    skin_low, skin_high = _axial_span(inner, axis)
    claimed: list[int] = []
    for node in cylinders:
        if node.face_id in taken or node.cyl_cone_axis is None:
            continue
        # Same radius to a boolean's accuracy: a cut running along a fold
        # re-fits the surface it divides, and its pieces come back a few
        # microns apart (6.0 against 6.00358). Still far below the gauge
        # separating the inner and outer skins, which must never merge.
        # The comparable radius, not ``cyl_radius``: on a cone that reads 0
        # and every unclaimed cone on the part would match.
        radius = band_mean_radius(node)
        same_radius = (
            abs(radius - band_mean_radius(inner)) < _SAME_RADIUS_TOL_MM
            or abs(radius - band_mean_radius(outer)) < _SAME_RADIUS_TOL_MM
        )
        if not same_radius:
            continue
        if abs(node.cyl_cone_axis.Direction().Dot(direction)) < 0.999:
            continue
        offset = gp_Vec(origin, node.cyl_cone_axis.Location())
        if axis_vec.Crossed(offset).Magnitude() > 0.05:
            continue
        low, high = _axial_span(node, axis)
        overlap = min(skin_high, high) - max(skin_low, low)
        if overlap < 0.5 * (high - low):
            continue
        claimed.append(node.face_id)
        taken.add(node.face_id)
    return claimed


def _axial_span(node: AagNode, axis: gp_Ax1) -> tuple[float, float]:
    """How far a face reaches along an axis, from the axis origin.

    Taken from the bounding box rather than the trimmed parameter range so the
    two faces of a pair are measured on one common axis -- the inside face's
    -- and their overlap means the same thing for both.
    """
    if node.bbox.IsVoid():
        return (0.0, 0.0)
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    origin = axis.Location()
    direction = gp_Vec(axis.Direction())
    values = [
        gp_Vec(origin, gp_Pnt(x, y, z)).Dot(direction)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
        for z in (zmin, zmax)
    ]
    return (min(values), max(values))
