# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about holes.

Holes drive more DFM findings than anything else, and several of these rules
change their answer depending on how the part is made. A bore on the axis of a
turned part is made with a boring bar, which deflects far sooner than a drill;
a flat-bottomed bore is routine on a lathe and suspicious on a mill. Getting
that branching right is most of the work.
"""

from __future__ import annotations

import math
from typing import Optional

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from ...machining.face_extent import band_along
from ...machining.context import MachiningContext
from ...machining.aag import SurfaceType
from ...machining.features import FeatureInstance, FeatureType
from ...machining.process_classifier import PartProcessType, axes_colinear
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


_BORE_TYPES = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.BREAKTHROUGH_HOLE,
    FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK,
    FeatureType.THREADED_HOLE,
)

# A breakthrough hole is deliberately absent: it has no floor at all, so it
# cannot have a flat one.
_FLOORED_BORE_TYPES = tuple(
    t for t in _BORE_TYPES if t != FeatureType.BREAKTHROUGH_HOLE
)


def _axis_of(feature: FeatureInstance) -> Optional[gp_Dir]:
    axis = feature.param("axis")
    if not axis or len(axis) != 3:
        return None
    try:
        return gp_Dir(float(axis[0]), float(axis[1]), float(axis[2]))
    except Exception:
        return None


def _is_on_part_axis(context: MachiningContext, feature: FeatureInstance) -> bool:
    """Whether a bore runs down the turning axis of a turned part.

    Such a bore is made by a boring bar reaching in from the end, not by a
    drill, and a boring bar chatters at a much lower depth-to-diameter ratio.

    Only on a part that is actually turned. A profile-extrusion blank keeps
    its extrusion axis but is never rotated, so its on-axis bores are drilled
    or reamed like any milled feature.
    """
    process = context.part_process
    if process.type not in (PartProcessType.TURNED, PartProcessType.MILL_TURN):
        return False
    part_axis = process.axis_of_revolution
    axis = _axis_of(feature)
    if part_axis is None or axis is None:
        return False
    if abs(axis.Dot(part_axis.Direction())) < 0.99:
        return False

    # The bore must also sit *on* the axis, not merely parallel to it: a bolt
    # circle on a turned face is live-tooled, not bored. Asked of the first
    # cylinder or cone only, as the reference does.
    for face_id in feature.faces:
        if not context.graph.has_node(face_id):
            continue
        node = context.graph.node(face_id)
        if node.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
            continue
        if node.cyl_cone_axis is None:
            return False
        offset = gp_Vec(part_axis.Location(), node.cyl_cone_axis.Location())
        return offset.Crossed(gp_Vec(part_axis.Direction())).Magnitude() < 1.0
    return False


# =============================================================================


@register_check(Rulebook.HOLE_DEPTH_RATIO)
class HoleDepthRatioCheck(MachiningCheck):
    """Deep holes are hard to drill straight and hard to clear chips from.

    Depth is judged against diameter because that is what governs: a 3mm hole
    20mm deep is far harder than a 20mm hole 100mm deep. Two refinements
    matter. A bore down the axis of a turned part is bored, not drilled, and
    is held to tighter ratios. And a hole interrupted by a crossing cavity is
    judged on its longest continuous run when the gap is wide enough that the
    drill has to be started again from the far side.
    """

    @property
    def name(self) -> str:
        return "Hole Depth Ratio Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)

        results: list[CheckResult] = []
        for hole in context.recognition.of_type(*_BORE_TYPES):
            diameter = hole.number("diameter_mm") or 0.0
            if diameter <= 1e-6:
                continue

            depth = self._effective_depth(hole, diameter, thresholds)
            if depth <= 0.0:
                continue

            bored = _is_on_part_axis(context, hole)
            if bored:
                # A boring bar overhangs its holder and deflects; the limits
                # are the shop's, not the material's.
                warn = thresholds.hole_deep_bore_warn_ratio
                error = thresholds.hole_deep_bore_error_ratio
            else:
                warn = target if target is not None else thresholds.hole_deep_warn_ratio
                error = limit if limit is not None else thresholds.hole_deep_error_ratio

            ratio = depth / diameter
            graded = self.graded(ratio, warn, error, "max")
            if graded is None:
                continue

            severity, threshold = graded
            if bored:
                advice = (
                    f"This {diameter:.1f} mm bore runs {depth:.1f} mm down the "
                    f"turning axis, {ratio:.1f} times its diameter. A boring bar "
                    "reaching that far deflects and chatters, leaving taper and a "
                    "poor finish at the bottom. Consider a larger bore, a shorter "
                    "one, or carbide bar."
                )
            else:
                advice = (
                    f"This hole is {depth:.1f} mm deep on a {diameter:.1f} mm "
                    f"diameter, {ratio:.1f} times its diameter. Deep holes wander "
                    "off line and trap chips; expect peck cycling, and beyond "
                    "about ten diameters a gun drill rather than a twist drill."
                )

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{ratio:.1f}x diameter",
                    self.render(feedback, severity, ratio, warn, error, "", advice),
                    faces=hole.faces,
                    value=ratio,
                    limit=threshold,
                    comparison=">",
                )
            )
        return results

    @staticmethod
    def _effective_depth(hole: FeatureInstance, diameter: float, thresholds) -> float:
        """The depth the drill actually has to reach in one go.

        A hole broken by a crossing cavity is still drilled in one pass when
        the gap is narrow -- the drill crosses it and picks up the far side.
        Once the gap is wide there is nothing to guide the drill, so the hole
        is made from both ends and the longest single run is what counts.
        """
        depth = hole.number("depth_mm") or 0.0
        void = hole.number("max_void_mm")
        contiguous = hole.number("max_contiguous_depth_mm")
        if void is None or contiguous is None:
            return depth
        if void > diameter * thresholds.hole_deep_single_pass_void_ratio:
            return contiguous
        return depth


@register_check(Rulebook.HOLE_FLAT_BOTTOM)
class HoleFlatBottomCheck(MachiningCheck):
    """A twist drill always leaves a cone, so a flat floor means another tool.

    Not always a problem: a shallow flat-bottomed recess is a spot face, cut
    in one plunge with a short rigid end mill. It matters when the hole is
    deep enough that the end mill has to reach, or small enough that the
    only end mill that fits is fragile.

    Exempt on turned parts, where a flat floor is what a boring bar naturally
    produces.
    """

    @property
    def name(self) -> str:
        return "Flat-Bottomed Hole Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []

        for hole in context.recognition.of_type(*_FLOORED_BORE_TYPES):
            if hole.param("is_through") or not hole.param("flat_bottom"):
                continue
            if hole.param("terminates_in_cavity"):
                continue
            if _is_on_part_axis(context, hole):
                continue  # single-point bored and faced flat: routine

            diameter = hole.number("diameter_mm") or 0.0
            depth = hole.number("depth_mm") or 0.0
            if diameter <= 0.0:
                continue

            # Above this diameter the hole is bored and faced rather than
            # drilled, so a flat floor is expected at any depth.
            if diameter > thresholds.hole_flat_bottom_max_diameter_mm:
                continue
            # A shallow recess wider than the fragile-tool floor is a spot
            # face: one plunge with a short rigid cutter.
            if diameter > thresholds.hole_flat_bottom_min_diameter_mm and depth <= diameter:
                continue

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"flat floor, {diameter:.1f} mm x {depth:.1f} mm deep",
                    self.render(
                        feedback,
                        severity,
                        depth,
                        diameter,
                        diameter,
                        "mm",
                        f"This {diameter:.1f} mm blind hole has a flat floor "
                        f"{depth:.1f} mm down. A twist drill leaves a 118 degree "
                        "cone, so the flat has to be made with an end mill reaching "
                        "to depth or a special flat-bottom drill. If the flat is not "
                        "functional, a drill-pointed floor is cheaper and faster.",
                    ),
                    faces=hole.faces,
                    value=depth,
                    limit=diameter,
                    comparison="=",
                    unit="mm",
                )
            )
        return results


#: What counts as the outside of a round part, for a hole measured against
#: it. Small radii are stubs and blends rather than a barrel, and a barrel
#: has to be meaningfully bigger than the bore inside it -- a sliver of the
#: bore's own wall with an inverted orientation flag would otherwise read as
#: a containment of the same size and give a wall of nothing.
_MIN_CONTAINMENT_RADIUS_MM = 5.0
_MIN_CONTAINMENT_GAP_MM = 0.5

#: How closely a barrel has to run with the bore to be the wall it breaks out
#: through.
_AXIS_PARALLEL_MIN_DOT = 0.95

@register_check(Rulebook.HOLE_EDGE_DISTANCE)
class HoleEdgeDistanceCheck(MachiningCheck):
    """Material left between a hole and the outside of the part.

    Too little and the drill pushes the wall out, or breaks through it. The
    distance is measured from the bore *surface* rather than its axis, so the
    number reported is the wall a machinist would measure.
    """

    @property
    def name(self) -> str:
        return "Hole Edge Distance Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.hole_edge_distance_mm

        dx, dy, dz = context.plane_bbox_dims()
        if min(dx, dy, dz) <= 0.0:
            return []
        # Exact boxes on the planar faces, not the graph's padded ones: the
        # padding of a plane bounded by arcs moves with the seam, and on
        # euro_lock_cylinder it decided a wall built at exactly the 2.0 mm
        # design value (the reference read 1.997 and 1.971 on one solid).
        bounds = context.plane_bbox_bounds(exact=True)
        if bounds is None:
            return []

        results: list[CheckResult] = []
        for hole in context.recognition.of_type(*_BORE_TYPES):
            # Every cylinder level of the hole, not just the bore. A
            # counterbore's seat is wider than its bore and so always the
            # nearer thing to the edge; measuring the bore alone called a seat
            # sitting on the edge comfortable. The worst level is reported.
            wall: Optional[float] = None
            worst: Optional[HoleLevel] = None
            boundary: list[int] = []
            for level in hole_levels(context.graph, hole, context.shape):
                measured = self._measure(context, hole, bounds, level)
                if measured is None:
                    continue
                distance, faces = measured
                if wall is None or distance < wall:
                    wall, worst, boundary = distance, level, faces
            if wall is None or wall >= limit:
                continue
            boundary = self._attribute(context, worst, wall, boundary)

            # On a counterbore it is usually a seat that runs out of wall,
            # and naming the bore would send the operator to the wrong
            # diameter.
            what = "hole" if worst.is_bore else (
                f"hole's {worst.radius * 2.0:.1f} mm seat"
            )
            # A warning either way, a bore-out included: the reference grades
            # the condition by its message, not by its severity.
            severity = Severity.WARNING
            if wall <= 0.0:
                advice = (
                    f"This {what} breaks out through the side of the part by "
                    f"{abs(wall):.2f} mm. Either it is meant to be a slot open to "
                    "the edge, or it needs moving inboard."
                )
            else:
                advice = (
                    f"Only {wall:.2f} mm of material separates this {what} from "
                    f"the outside of the part, below the {limit:.2f} mm minimum. "
                    "The drill will push the thin wall outward and may break "
                    "through; move the hole inboard or leave more stock."
                )

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{wall:.2f} mm to edge",
                    self.render(feedback, severity, wall, limit, limit, "mm", advice),
                    # The boundary the hole is near, as the reference
                    # attributes it, and nothing when none could be named --
                    # which is also what decides whether a thin-wall finding
                    # is the same gap (the hole's own wall would match every
                    # thin-wall finding on that hole).
                    faces=boundary,
                    value=wall,
                    limit=limit,
                    comparison="<",
                    unit="mm",
                )
            )
        return results

    @staticmethod
    def _attribute(
        context: MachiningContext, level: "HoleLevel", wall: float, faces: list[int]
    ) -> list[int]:
        """The faces of the part the reported wall runs out to.

        A barrel already named itself. Otherwise the box distance is pinned to
        the planes running alongside the level (the normal within 0.3 of
        square to its axis) whose axis-to-plane wall matches the reported
        one. Then every face coplanar with a winner joins it: a hole on an
        edge splits the face bounding it into two coplanar halves, and naming
        one highlights half the wall it is about to break out of.
        """
        axis = level.axis_dir
        start = level.axis_pnt.Translated(gp_Vec(axis).Multiplied(level.lo))
        length = level.hi - level.lo
        winners = list(faces)
        if not winners:
            for node in context.graph.nodes:
                if node.surface_type is not SurfaceType.PLANE or node.plane_normal is None:
                    continue
                if abs(node.plane_normal.Dot(axis)) > 0.3:
                    continue
                gap = _segment_to_plane(start, axis, length, node.centroid, node.plane_normal)
                if abs(gap - level.radius - wall) > 0.05:
                    continue
                winners.append(node.face_id)
        if not winners:
            return []
        extra: list[int] = []
        for node in context.graph.nodes:
            if node.surface_type is not SurfaceType.PLANE or node.plane_normal is None:
                continue
            if node.face_id in winners:
                continue
            for face_id in winners:
                if not context.graph.has_node(face_id):
                    continue
                w = context.graph.node(face_id)
                if w.surface_type is not SurfaceType.PLANE or w.plane_normal is None:
                    continue
                if abs(w.plane_normal.Dot(node.plane_normal)) < 0.999:
                    continue
                n = w.plane_normal
                off_w = w.centroid.X() * n.X() + w.centroid.Y() * n.Y() + w.centroid.Z() * n.Z()
                off_n = node.centroid.X() * n.X() + node.centroid.Y() * n.Y() + node.centroid.Z() * n.Z()
                if abs(off_w - off_n) > 0.05:
                    continue
                extra.append(node.face_id)
                break
        return winners + extra

    @staticmethod
    def _distance_to_outside(
        context: MachiningContext,
        hole: FeatureInstance,
        bounds,
        level: "Optional[HoleLevel]" = None,
    ) -> Optional[float]:
        """Smallest gap from one level's surface to the part's outer envelope.

        Without a level, the bore's own level is measured.
        """
        if level is None:
            levels = hole_levels(context.graph, hole, context.shape)
            level = next((lv for lv in levels if lv.is_bore), None)
            if level is None:
                return None
        measured = HoleEdgeDistanceCheck._measure(context, hole, bounds, level)
        return None if measured is None else measured[0]

    @staticmethod
    def _measure(
        context: MachiningContext, hole: FeatureInstance, bounds, level: "HoleLevel"
    ) -> "Optional[tuple[float, list[int]]]":
        """One level's gap to the outside, and the barrel faces that set it.

        Sides of the envelope the hole enters or leaves by are skipped: a
        vertical hole in a plate does not have an edge-distance problem with
        the top and bottom faces it passes through. The faces list is empty
        when the box gave the answer.
        """
        radius = level.radius
        if radius <= 0.0:
            return None
        # The level's own axis segment, bounded to its faces. Measured at both
        # ends, since the distance to each side of the box is linear along
        # the axis and its least is at an end.
        axis = level.axis_dir
        start = level.axis_pnt.Translated(gp_Vec(axis).Multiplied(level.lo))
        length = level.hi - level.lo
        centre = start.Translated(gp_Vec(axis).Multiplied(length * 0.5))

        lows, highs = bounds
        best = _cylinder_to_box(start, axis, length, radius, lows, highs)
        if best is None:
            # Every side of the box is one the hole enters or leaves by; the
            # reference stops there, barrel included.
            return None

        # That box is built from the part's flat faces, and a round part has
        # none facing outward -- so a bolt circle set close to the rim of a
        # flange or an end cap is invisible to it. The wall that matters
        # there is to the outside diameter, and that is a cylinder.
        return HoleEdgeDistanceCheck._wall_to_containing_od(
            context, hole, axis, centre, radius, level, best
        )

    @staticmethod
    def _wall_to_containing_od(
        context: MachiningContext,
        hole: FeatureInstance,
        axis: gp_Dir,
        centre: gp_Pnt,
        radius: float,
        level: "HoleLevel",
        best: float,
    ) -> "tuple[float, list[int]]":
        """The thinnest wall from this bore out to a barrel it sits inside.

        Competes with ``best``, the box answer, and returns the winner with
        the barrels that set it. A barrel tied with the box (within a
        micron) wins the attribution, being the boundary that is physically
        there; a strictly nearer one clears any earlier ties.

        A cylinder with its material on the inside is the outside of the
        part. For one running the same way as the bore, the wall between them
        is the barrel's radius, less how far the bore's axis sits off the
        barrel's, less the bore's own radius.

        Four things have to hold before that arithmetic means anything, and
        each of them has a part behind it. The barrel has to be bigger than
        the bore, or a sliver of the bore's own wall with its orientation
        flag inverted reads as a barrel of the same size and the wall comes
        out nought. The bore has to be inside it, or the subtraction is
        measuring across open air. The two have to overlap along the axis, or
        a tapped hole at one end of an end cap gets paired with a relief
        groove sixteen millimetres away at the other. And the barrel's
        surface has to actually exist where the bore approaches it: an
        external corner radius is a quarter of a cylinder facing away from
        the part, and read as a barrel it puts every corner screw a
        millimetre from the edge.
        """
        # The level's own extent along the axis. A bore that stops well short
        # of the barrel it sits beside never breaks out into it, however
        # close the two are in section: a tapped hole at one end of an end
        # cap and a relief groove at the other are sixteen millimetres apart
        # and have nothing to say about each other.

        winners: list[int] = []
        own = set(hole.faces)

        for node in context.graph.nodes:
            if node.surface_type is not SurfaceType.CYLINDER:
                continue
            if node.face_id in own or node.cyl_cone_axis is None:
                continue
            # Material inside the surface: the outside of the part, not a bore.
            if node.is_reversed:
                continue
            if node.cyl_radius < _MIN_CONTAINMENT_RADIUS_MM:
                continue
            if node.cyl_radius < radius + _MIN_CONTAINMENT_GAP_MM:
                continue

            direction = node.cyl_cone_axis.Direction()
            if abs(direction.Dot(axis)) < _AXIS_PARALLEL_MIN_DOT:
                continue

            origin = node.cyl_cone_axis.Location()
            offset = gp_Vec(origin, centre)
            axis_offset = offset.Crossed(gp_Vec(direction)).Magnitude()
            if axis_offset > node.cyl_radius + radius:
                continue  # the bore is outside this barrel altogether

            span = _axial_extent(node, origin, direction, context.shape)
            bore_span = level.span_along(origin, direction)
            if span is None or bore_span is None:
                continue
            if bore_span[1] < span[0] or bore_span[0] > span[1]:
                continue  # they do not meet anywhere along the axis

            if axis_offset > 1e-6:
                radial = gp_Vec(offset.XYZ())
                axial = gp_Vec(direction.XYZ())
                axial.Multiply(radial.Dot(gp_Vec(direction)))
                radial.Subtract(axial)
                length = radial.Magnitude()
                if length > 1e-6:
                    radial.Divide(length)
                    middle = 0.5 * (
                        max(bore_span[0], span[0]) + min(bore_span[1], span[1])
                    )
                    on_surface = gp_Pnt(
                        origin.X() + middle * direction.X() + node.cyl_radius * radial.X(),
                        origin.Y() + middle * direction.Y() + node.cyl_radius * radial.Y(),
                        origin.Z() + middle * direction.Z() + node.cyl_radius * radial.Z(),
                    )
                    box = Bnd_Box()
                    box.Add(node.bbox)
                    box.Enlarge(1.0)
                    if box.IsOut(on_surface):
                        continue  # a corner radius, not a barrel

            wall = node.cyl_radius - axis_offset - radius
            if wall < best + 1.0e-6:
                if wall < best - 1.0e-6:
                    best = wall
                    winners = []
                elif wall < best:
                    best = wall
                winners.append(node.face_id)
        return best, winners


@register_check(Rulebook.HOLE_WEB_THICKNESS)
class HoleWebThicknessCheck(MachiningCheck):
    """Material left between two parallel holes.

    A thin web between bores breaks out when the second one is drilled. The
    distance is between the two axes, measured perpendicular to them, less
    both radii -- which is the web a machinist would measure.
    """

    @property
    def name(self) -> str:
        return "Hole Web Thickness Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.hole_web_thickness_mm

        holes = context.recognition.of_type(*_BORE_TYPES)
        results: list[CheckResult] = []
        seen: set[tuple[str, str]] = set()

        for index, first in enumerate(holes):
            axis_a = self._axis_line(context, first)
            for second in holes[index + 1 :]:
                axis_b = self._axis_line(context, second)
                if axis_a is None or axis_b is None:
                    continue
                # Only parallel bores leave a web of constant thickness;
                # crossing bores are a different concern with its own rule.
                if abs(axis_a.Direction().Dot(axis_b.Direction())) < 0.99:
                    continue

                separation = gp_Vec(
                    axis_a.Location(), axis_b.Location()
                ).CrossMagnitude(gp_Vec(axis_a.Direction()))
                web = (
                    separation
                    - (first.number("diameter_mm") or 0.0) / 2.0
                    - (second.number("diameter_mm") or 0.0) / 2.0
                )
                # Seats count too. The web between two counterbored holes is
                # set by whatever is widest where they face each other. Bore
                # against bore is measured as before; seat levels are added
                # only where the two levels overlap along the axis, because a
                # seat near the top face and a bore that exists only deeper
                # share no material.
                origin = axis_a.Location()
                direction = axis_a.Direction()
                for level_a in hole_levels(context.graph, first, context.shape):
                    band_a = level_a.span_along(origin, direction)
                    for level_b in hole_levels(context.graph, second, context.shape):
                        if level_a.is_bore and level_b.is_bore:
                            continue  # already measured
                        band_b = level_b.span_along(origin, direction)
                        if band_b[1] < band_a[0] - 0.05 or band_b[0] > band_a[1] + 0.05:
                            continue
                        web = min(web, separation - level_a.radius - level_b.radius)
                if web < 0.0 or web >= limit:
                    continue

                key = (first.instance_id, second.instance_id)
                if key in seen:
                    continue
                seen.add(key)

                severity = Severity.WARNING
                results.append(
                    self.finding(
                        rule,
                        severity,
                        f"{web:.2f} mm web",
                        self.render(
                            feedback,
                            severity,
                            web,
                            limit,
                            limit,
                            "mm",
                            f"Only {web:.2f} mm of material separates these two "
                            f"holes, below the {limit:.2f} mm minimum. The web will "
                            "deflect or break out as the second hole is drilled; "
                            "move them apart or accept that they may join.",
                        ),
                        faces=sorted(set(first.faces + second.faces)),
                        value=web,
                        limit=limit,
                        comparison="<",
                        unit="mm",
                    )
                )
        return results

    @staticmethod
    def _axis_line(context: MachiningContext, hole: FeatureInstance):
        for face_id in hole.faces:
            node = context.graph.node(face_id)
            if node.cyl_cone_axis is not None:
                return node.cyl_cone_axis
        return None


# Two parallel axes nearer than this are the same axis.
_COAXIAL_TOL_MM = 0.01


def _cylinder_to_box(start: gp_Pnt, axis: gp_Dir, length: float, radius: float, lows, highs):
    """Least gap from a bounded cylinder's side to the sides of a box.

    The reference's measurement. Per side, the cylinder reaches toward it by
    its radius times the sine of the axis's angle to that side's normal. A
    side the axis runs nearly along is where the hole enters or leaves and is
    skipped; so is one the axis segment straddles or touches, which is an
    entry or exit too. An axis that crosses a side outright gives minus the
    reach. ``None`` when every side was skipped.
    """
    end = start.Translated(gp_Vec(axis).Multiplied(length))
    eps = 1e-2
    components = (axis.X(), axis.Y(), axis.Z())
    p0 = (start.X(), start.Y(), start.Z())
    p1 = (end.X(), end.Y(), end.Z())
    best: Optional[float] = None
    for k in range(3):
        reach = radius * math.sqrt(max(0.0, 1.0 - components[k] ** 2))
        parallel = abs(components[k]) > 0.85
        for d0, d1 in ((p0[k] - lows[k], p1[k] - lows[k]), (highs[k] - p0[k], highs[k] - p1[k])):
            straddle = (d0 < -eps and d1 > eps) or (d0 > eps and d1 < -eps)
            touches = abs(d0) <= eps or abs(d1) <= eps
            if parallel or straddle or touches:
                continue
            if (d0 < 0.0 < d1) or (d1 < 0.0 < d0):
                gap = -reach
            else:
                gap = min(abs(d0), abs(d1)) - reach
            best = gap if best is None else min(best, gap)
    return best


def _segment_to_plane(start: gp_Pnt, axis: gp_Dir, length: float, point: gp_Pnt, normal: gp_Dir) -> float:
    """Least distance from an axis segment to a plane; 0 if it crosses."""
    end = start.Translated(gp_Vec(axis).Multiplied(length))
    d0 = gp_Vec(point, start).Dot(gp_Vec(normal))
    d1 = gp_Vec(point, end).Dot(gp_Vec(normal))
    if (d0 < 0.0 < d1) or (d1 < 0.0 < d0):
        return 0.0
    return min(abs(d0), abs(d1))


def _first_cylinder(context: MachiningContext, hole: FeatureInstance):
    """The cylinder the reference reads a hole's axis and radius from.

    It takes the first cylinder in the feature's face list, which there is
    the seed the recognizer grew the hole from: the bore, smallest radius
    first. Ours keeps face lists sorted by id, which on a counterbore can put
    the seat first, so the seed is recovered as the smallest-radius cylinder
    (lowest id on a tie).
    """
    best = None
    for face_id in hole.faces:
        if not context.graph.has_node(face_id):
            continue
        node = context.graph.node(face_id)
        if node.surface_type is not SurfaceType.CYLINDER or node.cyl_cone_axis is None:
            continue
        if best is None or node.cyl_radius < best.cyl_radius - 1e-9:
            best = node
    return best


def _bore_box(context: MachiningContext, hole: FeatureInstance) -> Optional[Bnd_Box]:
    """The box round a hole's cylinders and cones, its caps left out."""
    box = Bnd_Box()
    found = False
    for face_id in hole.faces:
        if not context.graph.has_node(face_id):
            continue
        node = context.graph.node(face_id)
        if node.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
            continue
        if node.bbox.IsVoid():
            continue
        box.Add(node.bbox)
        found = True
    return box if found and not box.IsVoid() else None


def _axis_segment(context: MachiningContext, node) -> tuple[gp_Pnt, float]:
    """Start point and length of a cylinder's axis over the face's own span."""
    anchor = node.cyl_cone_axis.Location()
    direction = node.cyl_cone_axis.Direction()
    _, low, high = band_along(context.shape, node.face_id, node.bbox, anchor, direction, 1e18, -1e18)
    return anchor.Translated(gp_Vec(direction).Multiplied(low)), high - low


def _segment_segment_distance(p0, u, length_p, q0, v, length_q) -> float:
    """Closest approach of two bounded segments with unit directions.

    The reference's closed form, clamping included, kept as is.
    """
    w = gp_Vec(q0, p0)
    b = u.Dot(v)
    d = u.XYZ().Dot(w.XYZ())
    e = v.XYZ().Dot(w.XYZ())
    denom = 1.0 - b * b
    if denom < 1e-9:
        t = 0.0
        s = d if b > 0.0 else -d
    else:
        t = (b * e - d) / denom
        s = (e - b * d) / denom
    if t < 0.0:
        t = 0.0
        s = e
    elif t > length_p:
        t = length_p
        s = e + b * length_p
    if s < 0.0:
        s = 0.0
        t = min(max(-d, 0.0), length_p)
    elif s > length_q:
        s = length_q
        t = min(max(-d + b * length_q, 0.0), length_p)
    p = gp_Pnt(p0.X() + t * u.X(), p0.Y() + t * u.Y(), p0.Z() + t * u.Z())
    q = gp_Pnt(q0.X() + s * v.X(), q0.Y() + s * v.Y(), q0.Z() + s * v.Z())
    return p.Distance(q)


@register_check(Rulebook.HOLE_INTERSECTING)
class HoleIntersectingCheck(MachiningCheck):
    """Holes that break into one another.

    A drill entering the side of an existing bore has nothing to centre on:
    it deflects, and can grab or snap. Worth reporting once per pair.

    A part with many such pairs is a manifold or a cross-drilled coupling
    where this is the whole design intent. Reporting each pair there is
    noise, so past a threshold the finding collapses into a single note about
    the network.
    """

    @property
    def name(self) -> str:
        return "Intersecting Holes Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        holes = context.recognition.of_type(*_BORE_TYPES)
        pairs: list[tuple[FeatureInstance, FeatureInstance, float]] = []

        for index, first in enumerate(holes):
            for second in holes[index + 1 :]:
                gap = self._pair_gap(context, first, second)
                if gap is not None:
                    pairs.append((first, second, gap))

        if not pairs:
            return []

        severity = self.severity_from_rule_config(rule_config)
        threshold = context.config.thresholds.hole_intersecting_network_threshold

        if len(pairs) >= threshold:
            # A designed manifold. One note, not twenty.
            faces = sorted({f for a, b, _ in pairs for f in a.faces + b.faces})
            return [
                self.finding(
                    rule,
                    Severity.INFO,
                    f"{len(pairs)} intersecting pairs",
                    self.render(
                        feedback,
                        Severity.INFO,
                        float(len(pairs)),
                        float(threshold),
                        float(threshold),
                        "",
                        f"This part has {len(pairs)} pairs of holes breaking into "
                        "one another, which reads as a deliberate manifold or "
                        "cross-drilled network rather than an accident. Each "
                        "breakthrough is still an interrupted cut: drill the "
                        "through passages before the cross bores, and expect to "
                        "deburr the intersections.",
                    ),
                    faces=faces,
                    value=float(len(pairs)),
                    limit=float(threshold),
                    comparison=">",
                )
            ]

        return [
            self.finding(
                rule,
                severity,
                f"{(a.number('diameter_mm') or 0):.1f} mm into "
                f"{(b.number('diameter_mm') or 0):.1f} mm",
                self.render(
                    feedback,
                    severity,
                    distance,
                    0.0,
                    0.0,
                    "mm",
                    # Named, as the reference names the pair in its details:
                    # two pairs at the same gap are two findings, not one
                    # finding twice.
                    f"Holes {a.instance_id} and {b.instance_id} break into one "
                    "another. The second drill "
                    "enters the side of the first bore with nothing to centre on, "
                    "so it will deflect and may grab. Drill the larger passage "
                    "first, feed lightly through the breakout, and expect to "
                    "deburr where they meet.",
                ),
                faces=sorted(set(a.faces + b.faces)),
                value=distance,
                comparison="<",
                unit="mm",
            )
            for a, b, distance in pairs
        ]

    @staticmethod
    def _one_milled_opening(first, second, context) -> bool:
        """Whether the two bores are really one milled opening.

        A hole shallower than about three quarters of its own diameter was
        not drilled -- it was contoured, in one pass round the outline. Where
        both of a pair are that shallow they are a keyhole or an obround cut
        as a single shape, and there is no drill plunging into a void to
        worry about. A genuine crossing passage keeps at least one bore
        deeper than it is wide.
        """
        limit = context.config.thresholds.hole_intersecting_thin_plate_dd_max
        for hole in (first, second):
            diameter = hole.number("diameter_mm") or 0.0
            depth = hole.number("depth_mm") or 0.0
            if diameter <= 0.0 or depth <= 0.0:
                return False  # nothing measured; not our call to make
            if depth >= diameter * limit:
                return False
        return True

    @classmethod
    def _pair_gap(cls, context, first, second) -> Optional[float]:
        """Whether two holes break into one another, and how near their axes run.

        ``None`` when they do not. The reference's test, in its order:

        1. Parallel bores that share an axis are a stepped cavity, and one
           wholly inside the other is a nest; neither is a breakthrough.
        2. The boxes round the two bores' curved faces must overlap. Planar
           caps are left out: a counterbore's shoulder can run right across
           the part and drag a box with it.
        3. Their axis segments, each bounded to its own face, must come within
           the sum of the radii (and a tenth). Bounded rather than infinite:
           two holes from opposite faces can have axes passing within a radius
           and bores that stop half the part apart.
        4. Two bores both shallower than about three quarters of their
           diameter are one milled opening, a keyhole or an obround cut in a
           single contour, with no drill plunging into a void.

        The measured gap is reported as 0 when step 3 could not be asked.
        """
        ca = _first_cylinder(context, first)
        cb = _first_cylinder(context, second)
        if ca is not None and cb is not None:
            da = ca.cyl_cone_axis.Direction()
            db = cb.cyl_cone_axis.Direction()
            if abs(da.Dot(db)) > 0.99:
                offset = gp_Vec(ca.cyl_cone_axis.Location(), cb.cyl_cone_axis.Location())
                perpendicular = gp_Vec(da).Crossed(offset).Magnitude()
                if perpendicular < _COAXIAL_TOL_MM:
                    return None
                larger = max(ca.cyl_radius, cb.cyl_radius)
                smaller = min(ca.cyl_radius, cb.cyl_radius)
                if perpendicular + smaller < larger:
                    return None

        box_a, box_b = _bore_box(context, first), _bore_box(context, second)
        if box_a is None or box_b is None:
            return None
        ax1, ay1, az1, ax2, ay2, az2 = box_a.Get()
        bx1, by1, bz1, bx2, by2, bz2 = box_b.Get()
        if not (
            ax1 < bx2 and ax2 > bx1 and ay1 < by2 and ay2 > by1 and az1 < bz2 and az2 > bz1
        ):
            return None

        gap = 0.0
        if ca is not None and cb is not None:
            a_start, a_len = _axis_segment(context, ca)
            b_start, b_len = _axis_segment(context, cb)
            gap = _segment_segment_distance(
                a_start, ca.cyl_cone_axis.Direction(), a_len,
                b_start, cb.cyl_cone_axis.Direction(), b_len,
            )
            if gap > ca.cyl_radius + cb.cyl_radius + 0.1:
                return None

        if cls._one_milled_opening(first, second, context):
            return None
        return gap

    @staticmethod
    def _axis_distance(axis_a, axis_b) -> Optional[float]:
        """Shortest distance between two infinite axis lines."""
        da = gp_Vec(axis_a.Direction())
        db = gp_Vec(axis_b.Direction())
        between = gp_Vec(axis_a.Location(), axis_b.Location())
        normal = da.Crossed(db)
        magnitude = normal.Magnitude()
        if magnitude < 1e-9:
            return between.CrossMagnitude(da)
        return abs(between.Dot(normal)) / magnitude

    @staticmethod
    def _spans_overlap(context, first, second, axis_a, axis_b) -> bool:
        """Whether the two bores actually reach each other.

        Two axes can pass close without the holes meeting, if one stops short.
        Compared through bounding boxes, which is coarse but errs toward
        reporting rather than missing.
        """
        from OCP.Bnd import Bnd_Box

        def envelope(feature) -> Optional[Bnd_Box]:
            """The space the bore itself occupies.

            Built from the round faces only. A counterbore's shoulder and a
            hole's entry lip are planar and can run right across the part,
            and a box drawn round those says two holes at opposite corners
            overlap.
            """
            box = Bnd_Box()
            found = False
            for face_id in feature.faces:
                node = context.graph.node(face_id)
                if node.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
                    continue
                if node.bbox.IsVoid():
                    continue
                box.Add(node.bbox)
                found = True
            return box if found else None

        box_a, box_b = envelope(first), envelope(second)
        if box_a is None or box_b is None:
            return True
        return not box_a.IsOut(box_b)


class HoleLevel:
    """One distinct cylinder radius of a hole, and the axial band it covers.

    A hole is not always one cylinder. A counterbore is a bore plus one or
    more wider coaxial seats, and every seat is nearer to whatever the hole
    is near than the bore is -- which is the point of a seat. The kernel
    splits a wall at seams and at intersecting geometry, so one level can
    own several faces. `is_bore` marks the level carrying the feature's own
    diameter.
    """

    def __init__(self, radius: float, axis_pnt: gp_Pnt, axis_dir: gp_Dir, lo: float, hi: float):
        self.radius = radius
        self.axis_pnt = axis_pnt
        self.axis_dir = axis_dir
        self.lo = lo
        self.hi = hi
        self.is_bore = False
        self.faces: list = []

    def span_along(self, origin: gp_Pnt, direction: gp_Dir) -> "tuple[float, float]":
        """The level's band measured along `direction` from `origin`."""
        shift = gp_Vec(origin, self.axis_pnt).Dot(gp_Vec(direction))
        sense = 1.0 if self.axis_dir.Dot(direction) >= 0.0 else -1.0
        ends = (shift + sense * self.lo, shift + sense * self.hi)
        return (min(ends), max(ends))


def hole_levels(graph, hole: FeatureInstance, shape=None) -> "list[HoleLevel]":
    """The distinct cylinder levels of a hole feature, from its own faces.

    Works off the faces rather than parameter names, so stacked seats,
    double-ended seats and anything later shaped like them are covered.
    Each level's axial band is the face's real span along its axis when the
    shape is given; the box projection overstates it on any tilted bore, by
    an amount that moves with the seam.
    """
    levels: list[HoleLevel] = []
    bore_radius = 0.5 * (hole.number("diameter_mm") or 0.0)
    for face_id in hole.faces:
        if not graph.has_node(face_id):
            continue
        node = graph.node(face_id)
        if node.surface_type is not SurfaceType.CYLINDER or node.cyl_radius <= 0.0:
            continue
        if node.cyl_cone_axis is None or node.bbox.IsVoid():
            continue
        anchor = node.cyl_cone_axis.Location()
        direction = node.cyl_cone_axis.Direction()
        low, high = _axial_extent(node, anchor, direction, shape)

        into = None
        for level in levels:
            if abs(level.radius - node.cyl_radius) > 1.0e-3:
                continue
            if abs(level.axis_dir.Dot(direction)) < 0.99:
                continue
            offset = gp_Vec(level.axis_pnt, anchor)
            if gp_Vec(level.axis_dir).Crossed(offset).Magnitude() > 0.5:
                continue
            into = level
            break
        if into is None:
            level = HoleLevel(node.cyl_radius, anchor, direction, low, high)
            level.is_bore = bore_radius > 0.0 and abs(node.cyl_radius - bore_radius) < 1.0e-3
            level.faces.append(face_id)
            levels.append(level)
            continue
        # Re-measured against the level's own anchor so bands from
        # differently anchored faces compare.
        shift = gp_Vec(into.axis_pnt, anchor).Dot(gp_Vec(into.axis_dir))
        into.lo = min(into.lo, low + shift)
        into.hi = max(into.hi, high + shift)
        into.faces.append(face_id)
    return levels


def _axial_extent(node, origin, direction, shape=None) -> "Optional[tuple[float, float]]":
    """How far a face reaches along a direction, from a point on it.

    Measured on the face when the shape is given (band_along), with the
    bounding-box projection as the fallback.
    """
    if shape is not None:
        ok, low, high = band_along(
            shape, node.face_id, node.bbox, origin, direction, math.inf, -math.inf
        )
        if ok and high > low:
            return (low, high)
    if node.bbox.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    low, high = math.inf, -math.inf
    for x in (xmin, xmax):
        for y in (ymin, ymax):
            for z in (zmin, zmax):
                along = gp_Vec(origin, gp_Pnt(x, y, z)).Dot(gp_Vec(direction))
                low = min(low, along)
                high = max(high, along)
    return (low, high)
