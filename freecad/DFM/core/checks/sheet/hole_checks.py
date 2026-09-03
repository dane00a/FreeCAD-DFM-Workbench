# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about holes in a sheet part.

A hole in sheet is not a drilled hole. It is punched or lasered through the
full gauge in one hit, so the questions are different ones: whether the punch
survives the stock, whether the web between two hits survives the punch, and
whether the hole is far enough from a fold that forming does not pull it out of
round.

The countersink rule is the one exception, and it is not about the cutting at
all -- it is about what is left underneath the cone once the sheet is this
thin.
"""

from __future__ import annotations

import math

from OCP.gp import gp_Vec

from ...machining.aag import SurfaceType
from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import (
    SHEET_HOLE_BEND_CLEARANCE_FACTOR,
    SHEET_HOLE_PITCH_FACTOR,
    SHEET_MAX_COUNTERSINK_DEPTH_FACTOR,
    SHEET_MIN_COUNTERSINK_LAND_MM,
    SHEET_MIN_HOLE_FACTOR,
    SheetCheck,
    bends_of,
    hole_cyl_node,
    hole_in_panel,
    is_hole_type,
    panel_away_dir,
    sorted_features,
    threshold,
)


@register_check(Rulebook.SHEET_HOLE_NEAR_BEND)
class SheetHoleNearBendCheck(SheetCheck):
    """A hole sitting inside the metal the fold stretches.

    The deformation zone reaches out from the tangent line by the bend radius
    plus a couple of gauges, and a hole inside it does not stay round: the
    material either side of it moves by different amounts and pulls it into an
    oval. Distance is measured from the tangent line to the near edge of the
    hole, not to its centre, because it is the edge that gets there first.
    """

    @property
    def name(self) -> str:
        return "Sheet Hole Near Bend Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(
                context,
                "sheet_hole_bend_clearance_factor",
                SHEET_HOLE_BEND_CLEARANCE_FACTOR,
            )

        bends = bends_of(context)
        if not bends:
            return []

        results: list[CheckResult] = []
        for hole in sorted_features(context):
            if not is_hole_type(hole.type):
                continue
            bore = hole_cyl_node(context, hole)
            if bore is None or bore.cyl_cone_axis is None:
                continue
            radius = bore.cyl_radius
            centre = bore.cyl_cone_axis.Location()

            for bend in bends:
                minimum = gauge * factor + bend.inner_radius
                for panel_id in bend.panels:
                    if not hole_in_panel(context, hole, panel_id):
                        continue
                    direction = panel_away_dir(context, bend, panel_id)
                    if direction is None:
                        continue
                    along = gp_Vec(bend.origin, centre).Dot(gp_Vec(direction))
                    clearance = along - radius
                    # Negative means the hole is on the other side of the fold
                    # or inside the bend itself, which this rule has nothing to
                    # say about.
                    if clearance < 0.0 or clearance >= minimum:
                        continue

                    results.append(
                        self.finding(
                            rule,
                            Severity.WARNING,
                            f"{clearance:.1f} mm from the bend",
                            self.render(
                                feedback,
                                Severity.WARNING,
                                clearance,
                                minimum,
                                minimum,
                                "mm",
                                f"The edge of this hole sits {clearance:.1f} mm "
                                "from the bend tangent line, inside the metal the "
                                f"fold stretches. It needs {minimum:.1f} mm -- "
                                f"{factor:.1f} gauges plus the bend radius. As "
                                "drawn the hole pulls into an oval when the bend "
                                "is formed. Move it clear of the bend zone, or "
                                "punch it after forming as a secondary operation.",
                            ),
                            faces=hole.faces,
                            value=clearance,
                            limit=minimum,
                            comparison="<",
                        )
                    )
        return results


@register_check(Rulebook.SHEET_HOLE_SMALL)
class SheetHoleSmallCheck(SheetCheck):
    """A hole smaller than the stock it goes through breaks the punch.

    The punch is a slender column in compression, and the load on it is set by
    the thickness it has to shear. Below about one gauge in diameter it snaps
    rather than cuts. A laser does not care, which is why the finding names the
    process rather than simply refusing the hole.
    """

    @property
    def name(self) -> str:
        return "Sheet Hole Size Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(context, "sheet_min_hole_factor", SHEET_MIN_HOLE_FACTOR)
        minimum = gauge * factor

        results: list[CheckResult] = []
        for hole in sorted_features(context):
            if not is_hole_type(hole.type):
                continue
            diameter = hole.number("diameter_mm", 0.0) or 0.0
            if diameter <= 0.0 or diameter >= minimum:
                continue
            ratio = self.ratio(diameter, gauge)

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{diameter:.2f} mm hole in {gauge:.2f} mm",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        ratio,
                        factor,
                        factor,
                        "",
                        f"This hole is {diameter:.2f} mm across in {gauge:.2f} mm "
                        "material, under the punching minimum of one gauge. A "
                        "punch that slender snaps instead of shearing, and "
                        "replacing it stops the press. Cutting it on the laser "
                        "gets round that -- confirm which process the blank runs "
                        "on, or open the hole up to at least "
                        f"{minimum:.2f} mm.",
                    ),
                    faces=hole.faces,
                    value=ratio,
                    limit=factor,
                    comparison="<",
                )
            )
        return results


@register_check(Rulebook.SHEET_HOLE_PITCH)
class SheetHolePitchCheck(SheetCheck):
    """Two holes close enough that punching one distorts the other.

    The web between them is what carries the shear load of the second hit, and
    below about two gauges it stretches and dishes rather than holding. The
    measurement is edge to edge, since that is the metal that has to survive.
    """

    @property
    def name(self) -> str:
        return "Sheet Hole Pitch Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(context, "sheet_hole_pitch_factor", SHEET_HOLE_PITCH_FACTOR)
        minimum = gauge * factor

        holes = []
        for hole in sorted_features(context):
            if not is_hole_type(hole.type):
                continue
            bore = hole_cyl_node(context, hole)
            if bore is None or bore.cyl_cone_axis is None:
                continue
            holes.append((hole, bore.cyl_cone_axis.Location(), bore.cyl_radius))

        results: list[CheckResult] = []
        for index, (first, first_centre, first_radius) in enumerate(holes):
            for second, second_centre, second_radius in holes[index + 1 :]:
                web = first_centre.Distance(second_centre) - first_radius - second_radius
                if web <= 0.0 or web >= minimum:
                    continue

                results.append(
                    self.finding(
                        rule,
                        Severity.WARNING,
                        f"{web:.1f} mm web",
                        self.render(
                            feedback,
                            Severity.WARNING,
                            web,
                            minimum,
                            minimum,
                            "mm",
                            f"These two holes leave only {web:.1f} mm of metal "
                            f"between their edges. Punching needs {minimum:.1f} mm "
                            f"-- {factor:.0f} gauges -- or the web dishes and tears "
                            "as the second hit goes through, and the first hole "
                            "distorts with it. Space them further apart, or run "
                            "the blank on the laser.",
                        ),
                        faces=sorted(set(first.faces) | set(second.faces)),
                        value=web,
                        limit=minimum,
                        comparison="<",
                    )
                )
        return results


@register_check(Rulebook.SHEET_COUNTERSINK_DEEP)
class SheetCountersinkDeepCheck(SheetCheck):
    """A countersink that eats most of the gauge under it.

    Two separate problems share this rule, and they are guarded differently on
    purpose. The depth ratio protects manufacturability, since tolerances scale
    with the gauge. The absolute land floor protects the part in service:
    fatigue does not care about ratios, and in thin stock a countersink that
    passes the ratio can still leave a feathered rim that yields under the
    fastener.
    """

    @property
    def name(self) -> str:
        return "Sheet Countersink Depth Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        depth_factor = threshold(
            context,
            "sheet_max_countersink_depth_factor",
            SHEET_MAX_COUNTERSINK_DEPTH_FACTOR,
        )
        min_land = threshold(
            context, "sheet_min_countersink_land_mm", SHEET_MIN_COUNTERSINK_LAND_MM
        )
        max_depth = gauge * depth_factor

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.COUNTERSINK):
            depth = self._cone_depth(context, feature)
            if depth is None:
                continue
            land = gauge - depth
            too_deep = depth > max_depth
            knife_edge = land < min_land
            if not too_deep and not knife_edge:
                continue

            if knife_edge:
                advice = (
                    f"This countersink is {depth:.2f} mm deep in {gauge:.2f} mm "
                    f"sheet, leaving only {land:.2f} mm of land under the cone. "
                    f"Under {min_land:.2f} mm the rim is a knife edge: it folds "
                    "over under fastener preload and starts a fatigue crack at the "
                    "hole. In gauge this thin, dimple the sheet rather than "
                    "countersinking it."
                )
                overview = f"{land:.2f} mm of land left"
            else:
                advice = (
                    f"This countersink is {depth:.2f} mm deep in {gauge:.2f} mm "
                    f"sheet, past {depth_factor:.0%} of the gauge "
                    f"({max_depth:.2f} mm). The far side thins toward a knife edge "
                    "that will not hold the fastener flush or square. Reduce the "
                    "depth, use a shallower included angle, or dimple the sheet "
                    "instead."
                )
                overview = f"{depth:.2f} mm deep in {gauge:.2f} mm"

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    overview,
                    self.render(
                        feedback,
                        Severity.WARNING,
                        depth,
                        max_depth,
                        max_depth,
                        "mm",
                        advice,
                    ),
                    faces=feature.faces,
                    value=depth,
                    limit=max_depth,
                    comparison=">",
                )
            )
        return results

    @staticmethod
    def _cone_depth(context, feature):
        """Depth read off the cone face itself rather than off a parameter.

        The two end radii and the half angle give it directly, and they are
        measured geometry rather than something a recognizer had to infer.
        """
        for face_id in sorted(feature.faces):
            if not context.graph.has_node(face_id):
                continue
            node = context.graph.node(face_id)
            if node.surface_type is not SurfaceType.CONE:
                continue
            semi_angle = abs(node.cone_semi_angle)
            if semi_angle < 1e-6:
                return None
            return abs(node.cone_r0 - node.cone_r1) / math.tan(semi_angle)
        return None


@register_check(Rulebook.SHEET_MACHINED_FEATURE)
class SheetMachinedFeatureCheck(SheetCheck):
    """A tapped hole on a formed part is a second trip through the shop.

    Worth saying not because it is wrong but because it is a cost: the part
    comes off the brake, goes to a machine, and comes back. And the thread it
    gets is only as long as the gauge, which in sheet is rarely enough
    engagement for the fastener the drawing calls for.
    """

    @property
    def name(self) -> str:
        return "Sheet Machined Feature Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.THREADED_HOLE):
            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    f"tapped through {gauge:.2f} mm",
                    self.render(
                        feedback,
                        Severity.INFO,
                        gauge,
                        gauge,
                        gauge,
                        "mm",
                        "This is a tapped hole on a formed part. Tapping is a "
                        "secondary operation after the brake, so the part is "
                        "handled twice, and the thread only engages over the "
                        f"{gauge:.2f} mm of gauge -- often a turn or two. A formed "
                        "extruded boss, a clinch nut, or a rivet nut gives full "
                        "engagement without the second setup.",
                    ),
                    faces=feature.faces,
                    value=gauge,
                    limit=gauge,
                    comparison="",
                )
            )
        return results
