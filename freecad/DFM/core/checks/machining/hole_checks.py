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

from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from ...machining.context import MachiningContext
from ...machining.features import FeatureInstance, FeatureType
from ...machining.process_classifier import axes_colinear
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


_BORE_TYPES = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK,
    FeatureType.THREADED_HOLE,
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
    """Whether a bore runs down the turning axis of the part.

    Such a bore is made by a boring bar reaching in from the end, not by a
    drill, and a boring bar chatters at a much lower depth-to-diameter ratio.
    """
    part_axis = context.part_process.axis_of_revolution
    axis = _axis_of(feature)
    if part_axis is None or axis is None:
        return False
    if abs(axis.Dot(part_axis.Direction())) < 0.996:  # within about 5 degrees
        return False

    # The bore must also sit *on* the axis, not merely parallel to it.
    for face_id in feature.faces:
        node = context.graph.node(face_id)
        if node.cyl_cone_axis is None:
            continue
        offset = gp_Vec(part_axis.Location(), node.cyl_cone_axis.Location())
        if offset.CrossMagnitude(gp_Vec(part_axis.Direction())) <= 1.0:
            return True
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

        for hole in context.recognition.of_type(*_BORE_TYPES):
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
        bounds = context.plane_bbox_bounds()
        if bounds is None:
            return []

        results: list[CheckResult] = []
        for hole in context.recognition.of_type(*_BORE_TYPES):
            wall = self._distance_to_outside(context, hole, bounds)
            if wall is None or wall >= limit:
                continue

            severity = Severity.ERROR if wall <= 0.0 else Severity.WARNING
            if wall <= 0.0:
                advice = (
                    f"This hole breaks out through the side of the part by "
                    f"{abs(wall):.2f} mm. Either it is meant to be a slot open to "
                    "the edge, or it needs moving inboard."
                )
            else:
                advice = (
                    f"Only {wall:.2f} mm of material separates this hole from the "
                    f"outside of the part, below the {limit:.2f} mm minimum. The "
                    "drill will push the thin wall outward and may break through; "
                    "move the hole inboard or leave more stock."
                )

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{wall:.2f} mm to edge",
                    self.render(feedback, severity, wall, limit, limit, "mm", advice),
                    faces=hole.faces,
                    value=wall,
                    limit=limit,
                    comparison="<",
                    unit="mm",
                )
            )
        return results

    @staticmethod
    def _distance_to_outside(
        context: MachiningContext, hole: FeatureInstance, bounds
    ) -> Optional[float]:
        """Smallest gap from the bore surface to the part's outer envelope.

        Faces of the envelope the bore runs roughly parallel to are skipped:
        a vertical hole in a plate does not have an edge-distance problem
        with the top and bottom faces it passes through.
        """
        axis = _axis_of(hole)
        radius = (hole.number("diameter_mm") or 0.0) / 2.0
        if axis is None or radius <= 0.0:
            return None

        centre = None
        for face_id in hole.faces:
            node = context.graph.node(face_id)
            if node.cyl_cone_axis is not None:
                centre = node.cyl_cone_axis.Location()
                break
        if centre is None:
            return None

        lows, highs = bounds
        components = (axis.X(), axis.Y(), axis.Z())
        position = (centre.X(), centre.Y(), centre.Z())

        best: Optional[float] = None
        for index in range(3):
            # The bore enters and leaves through faces it is normal to.
            if abs(components[index]) > 0.85:
                continue
            # How far the bore reaches toward this face, allowing for tilt.
            reach = radius * math.sqrt(max(0.0, 1.0 - components[index] ** 2))
            for face_coordinate in (lows[index], highs[index]):
                gap = abs(position[index] - face_coordinate) - reach
                best = gap if best is None else min(best, gap)
        return best


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
            axis_a = HoleWebThicknessCheck._axis_line(context, first)
            radius_a = (first.number("diameter_mm") or 0.0) / 2.0
            for second in holes[index + 1 :]:
                axis_b = HoleWebThicknessCheck._axis_line(context, second)
                radius_b = (second.number("diameter_mm") or 0.0) / 2.0
                if axis_a is None or axis_b is None:
                    continue
                if abs(axis_a.Direction().Dot(axis_b.Direction())) > 0.99:
                    continue  # parallel bores are the web rule's business

                distance = self._axis_distance(axis_a, axis_b)
                if distance is None or distance > radius_a + radius_b + 0.1:
                    continue
                if not self._spans_overlap(context, first, second, axis_a, axis_b):
                    continue
                pairs.append((first, second, distance))

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
                    "These two holes break into one another. The second drill "
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
            box = Bnd_Box()
            found = False
            for face_id in feature.faces:
                node = context.graph.node(face_id)
                if node.bbox.IsVoid():
                    continue
                box.Add(node.bbox)
                found = True
            return box if found else None

        box_a, box_b = envelope(first), envelope(second)
        if box_a is None or box_b is None:
            return True
        return not box_a.IsOut(box_b)
