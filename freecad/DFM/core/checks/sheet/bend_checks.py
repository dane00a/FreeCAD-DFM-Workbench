# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about the fold itself: radius, flange, angle, reliefs and hems.

Everything here is a question about the press brake. The die has a smallest
radius it will form, a shortest flange it will hold, and an angle past which it
needs over-bending tooling; the sheet has a place where it tears if the fold
runs out with nothing cut to let it, and a corner where two folds collide if
nothing is cut away between them.

Two of these rules read the material rather than the feature list. A relief cut
leaves nothing behind for a recognizer to claim -- a scallop, a square notch, a
V and an open corner cutback are four shapes for one intent -- so both relief
rules probe the solid for metal where metal would be a problem. Testing for a
witness feature instead reports every real part as unrelieved.
"""

from __future__ import annotations

from OCP.gp import gp_Pnt, gp_Vec

from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import (
    GA14_STEEL_MM,
    SHEET_BEND_OVER_LENGTH_EPS_MM,
    SHEET_BEND_RADIUS_ERROR_FACTOR,
    SHEET_BEND_RADIUS_WARN_FACTOR,
    SHEET_DIAGONAL_BEND_MIN_THICKNESS_MM,
    SHEET_DIMENSION_EPS_MM,
    SHEET_MIN_FLANGE_FACTOR,
    SheetCheck,
    SolidProbe,
    bend_axial_span,
    bend_geom,
    bends_of,
    gauge_phrase,
    hem_min_return_mm,
    max_std_bend_deg,
    panel_away_dir,
    panels_coplanar,
    patch_extents_along,
    sorted_features,
    threshold,
)


# How far a panel has to run past the end of a fold before the fold counts as
# terminating mid-edge rather than at the edge of the blank. Two gauges is
# enough to clear the wrap of the bend itself.
_BEND_END_MARGIN_GAUGES = 2.0

# Axial rows probed just past an open bend end, in gauges. The near row is what
# catches a relief merely tangent to the end line: it clears the strip further
# out and leaves a cusp of metal right at the fold, which is where the tear
# starts.
_RELIEF_PROBE_ROWS = (0.25, 0.75)

# Two bends form a corner when their axes are near enough to square.
_CORNER_AXIS_MAX_DOT = 0.3

# How far past the end of either fold the corner may sit and still be a corner
# both bends reach.
_CORNER_REACH_GAUGES = 3.0

# A hem counts as closed once the fold radius has collapsed below half the
# gauge, and closed hems only crack on material heavier than this.
_CLOSED_HEM_RADIUS_FACTOR = 0.5
_CLOSED_HEM_MIN_GAUGE_MM = 2.0


@register_check(Rulebook.SHEET_BEND_RADIUS_SMALL)
class SheetBendRadiusCheck(SheetCheck):
    """A fold tighter than the material takes cracks on the outside.

    The outer fibre of a bend stretches, and how far it stretches is set by the
    ratio of the inside radius to the thickness. Below one gauge the surface
    starts to orange-peel and then split; below half a gauge nothing on the
    punch rack will form it.
    """

    @property
    def name(self) -> str:
        return "Sheet Bend Radius Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        warn_factor = self.safe_float(rule_config.target)
        error_factor = self.safe_float(rule_config.limit)
        if warn_factor is None:
            warn_factor = threshold(
                context, "sheet_bend_radius_warn_factor", SHEET_BEND_RADIUS_WARN_FACTOR
            )
        if error_factor is None:
            error_factor = threshold(
                context, "sheet_bend_radius_error_factor", SHEET_BEND_RADIUS_ERROR_FACTOR
            )
        epsilon = threshold(context, "sheet_dimension_eps_mm", SHEET_DIMENSION_EPS_MM)

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.BEND):
            # A hem folds to nearly nothing by design, and a hemming die rather
            # than a vee owns that geometry. The return length is what matters
            # on a hem, and SHEET_HEM_DIMENSIONS asks about it.
            if feature.param("is_hem"):
                continue
            radius = feature.number("inner_radius_mm", 0.0) or 0.0
            if radius <= 0.0:
                continue

            warn_radius = gauge * warn_factor
            error_radius = gauge * error_factor
            if radius >= warn_radius - epsilon:
                continue

            is_error = radius < error_radius
            severity = Severity.ERROR if is_error else Severity.WARNING
            factor = error_factor if is_error else warn_factor
            ratio = self.ratio(radius, gauge)

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"r{radius:.2f} mm, {ratio:.2f}x gauge",
                    self.render(
                        feedback,
                        severity,
                        ratio,
                        warn_factor,
                        error_factor,
                        "",
                        f"This bend has an inside radius of {radius:.2f} mm in "
                        f"{gauge:.2f} mm material, under {gauge_phrase(factor)}"
                        ". The outside of the fold stretches further than "
                        "the material will take and cracks along the bend line, "
                        "and forming it needs a punch ground for the job rather "
                        "than anything on the rack. An inside radius of at least "
                        f"one gauge, {gauge:.2f} mm here, forms with standard "
                        "tooling.",
                    ),
                    faces=feature.faces,
                    value=ratio,
                    limit=factor,
                    comparison="<",
                )
            )
        return results


@register_check(Rulebook.SHEET_FLANGE_SHORT)
class SheetFlangeShortCheck(SheetCheck):
    """A flange the die cannot hold comes off the brake distorted.

    The flange is measured from the bend tangent line out to the end of the
    panel, and the panel is flooded across its coplanar fragments first: a
    Boolean seam splits one physical flat into several faces, and measuring
    whichever fragment touches the bend reads a full-length flange as a stub.
    """

    @property
    def name(self) -> str:
        return "Sheet Flange Short Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = threshold(context, "sheet_min_flange_factor", SHEET_MIN_FLANGE_FACTOR)
        # The rule is declared in millimetres, so a configured figure is read
        # as one: an absolute floor standing in for the gauge-relative answer,
        # which is what a shop quoting a fixed die grip would enter.
        configured = self.safe_float(rule_config.limit)

        results: list[CheckResult] = []
        for bend in bends_of(context):
            # A hem is folded flat against its parent, so there is no die grip
            # to lose. SHEET_HEM_DIMENSIONS judges its return instead.
            if bend.feature.param("is_hem"):
                continue
            minimum = (
                configured
                if configured is not None
                else gauge * factor + bend.inner_radius
            )
            basis = (
                "the configured minimum"
                if configured is not None
                else f"{gauge_phrase(factor)} plus the bend radius"
            )
            for panel_id in bend.panels:
                direction = panel_away_dir(context, bend, panel_id)
                if direction is None:
                    continue
                extents = patch_extents_along(context, panel_id, bend.origin, direction)
                if extents is None:
                    continue
                flange = extents[1]
                if flange <= 1e-9 or flange >= minimum:
                    continue

                results.append(
                    self.finding(
                        rule,
                        Severity.WARNING,
                        f"{flange:.1f} mm flange",
                        self.render(
                            feedback,
                            Severity.WARNING,
                            flange,
                            minimum,
                            minimum,
                            "mm",
                            f"This flange stands only {flange:.1f} mm out from the "
                            f"bend line, and the die needs {minimum:.1f} mm -- "
                            f"{basis} -- to hold "
                            "it. A flange this short slips out of the vee as the "
                            "ram comes down, so the angle wanders and the part "
                            "comes off twisted. Lengthen the flange, or leave it "
                            "long, fold it, and trim it back afterwards.",
                        ),
                        faces=[panel_id],
                        value=flange,
                        limit=minimum,
                        comparison="<",
                    )
                )
        return results


@register_check(Rulebook.SHEET_BEND_ANGLE_EXTREME)
class SheetBendAngleExtremeCheck(SheetCheck):
    """A fold past what the brake reaches for its gauge.

    The ceiling is banded rather than fixed, because it is the material that
    sets it: thin stock folds right back on itself, heavy stock does not. Hems
    are deliberately not excluded -- a hem in heavy gauge is precisely the fold
    a brake cannot make, and excluding them silenced the rule on the one case
    it exists for.
    """

    @property
    def name(self) -> str:
        return "Sheet Bend Angle Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        ceiling = self.safe_float(rule_config.limit)
        if ceiling is None:
            ceiling = max_std_bend_deg(gauge, context)

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.BEND):
            angle = feature.number("angle_deg", 0.0) or 0.0
            if angle <= ceiling:
                continue

            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    f"{angle:.0f} deg fold",
                    self.render(
                        feedback,
                        Severity.INFO,
                        angle,
                        ceiling,
                        ceiling,
                        "deg",
                        f"This bend is folded to {angle:.0f} degrees, past the "
                        f"{ceiling:.0f} degrees the brake reaches in {gauge:.2f} mm "
                        "material. Getting there takes over-bending tooling and a "
                        "springback allowance, so it is a setup to plan for rather "
                        "than a fault. The ceiling moves with the gauge: 14 gauge "
                        "and thinner folds right back to 180 degrees, 11 gauge and "
                        "heavier tops out near 125.",
                    ),
                    faces=feature.faces,
                    value=angle,
                    limit=ceiling,
                    comparison=">",
                )
            )
        return results


@register_check(Rulebook.SHEET_BEND_LONGER_THAN_BODY)
class SheetBendLongerThanBodyCheck(SheetCheck):
    """A bend line that crosses the blank diagonally.

    Only a bend whose axis is not parallel to an outline edge can run longer
    than the part's largest extent, and that is exactly the diagonal bend this
    is about. Read against the smaller of the two extents instead, every
    ordinary full-length flange trips: a 1000 by 100 blank with a 1000 mm
    flange is the most routine part in the shop.

    The gate is heavy stock, and the direction is easy to get backwards. Gauge
    numbering runs the other way from thickness, so the problem range is thick
    material: thin sheet flexes into position under the ram, heavy sheet fights
    back and keeps the distortion.

    Two limitations worth knowing before trusting the verdict. The extent is
    measured on the folded part in its modelled orientation rather than on the
    flat blank, so a bend on an already-folded flange is compared against an
    envelope the folding has shrunk. And it is measured on world axes, so a
    part modelled at an angle to them reads as diagonal when it is not.
    """

    @property
    def name(self) -> str:
        return "Sheet Diagonal Bend Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        min_gauge = threshold(
            context,
            "sheet_diagonal_bend_min_thickness_mm",
            SHEET_DIAGONAL_BEND_MIN_THICKNESS_MM,
        )
        if gauge < min_gauge:
            return []  # thin stock: the brake absorbs it

        # Planar faces only. OpenCascade pads a curved face's bounding box
        # after a Boolean, and on parts where the bend length and the extent
        # are equal by construction that padding would quietly become the
        # tolerance instead of the epsilon below.
        extent = max(context.plane_bbox_dims())
        if extent <= 0.0:
            return []
        epsilon = threshold(
            context, "sheet_bend_over_length_eps_mm", SHEET_BEND_OVER_LENGTH_EPS_MM
        )

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.BEND):
            length = feature.number("length_mm", 0.0) or 0.0
            if length <= 0.0 or length <= extent + epsilon:
                continue

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{length:.0f} mm bend on a {extent:.0f} mm part",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        length,
                        extent,
                        extent,
                        "mm",
                        f"This bend is {length:.0f} mm long on a part whose largest "
                        f"dimension is {extent:.0f} mm, so the bend line runs "
                        "diagonally across the blank rather than parallel to an "
                        f"edge. In {gauge:.2f} mm material that is hard on the "
                        "brake: the sheet does not enter the die evenly so it "
                        "wants to walk sideways, the wider die opening opens out "
                        "the inside radius, unequal flanges twist the part, and "
                        "the bend line can only be gauged off a point or two "
                        "instead of a full edge. This is a process limit rather "
                        f"than a property of the material -- about 14 gauge "
                        f"({min_gauge:.2f} mm) and heavier is where it starts to "
                        "bite. The part may well be fine; rotating the bend onto "
                        "an edge or splitting it into two pieces is cheap early "
                        "and expensive late, so it is worth raising before the "
                        "design is frozen.",
                    ),
                    faces=feature.faces,
                    value=length,
                    limit=extent,
                    comparison=">",
                )
            )
        return results


@register_check(Rulebook.SHEET_CLOSED_FLANGE_LOOP)
class SheetClosedFlangeLoopCheck(SheetCheck):
    """Panels and bends that close into a ring cannot be folded from one blank.

    Found by union-find over the panel graph: adding a bend whose two panels
    are already connected closes a cycle, and a cycle means the last fold has
    nowhere to go.
    """

    @property
    def name(self) -> str:
        return "Sheet Closed Flange Loop Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        parent: dict[int, int] = {}

        def find(node: int) -> int:
            root = parent.setdefault(node, node)
            while root != parent[root]:
                root = parent[root]
            while parent[node] != root:  # path compression, iterative
                parent[node], node = root, parent[node]
            return root

        bend_count = 0
        closed = False
        for bend in bends_of(context):
            if bend.panel_a is None or bend.panel_b is None:
                continue
            bend_count += 1
            first, second = find(bend.panel_a), find(bend.panel_b)
            if first == second:
                closed = True
                break
            parent[first] = second

        if not closed:
            return []

        # A seam is a decision to make, not a defect: the part is producible
        # once somebody says how it is joined. WARNING, per the reference.
        return [
            self.finding(
                rule,
                Severity.WARNING,
                f"{bend_count} bends close a loop",
                self.render(
                    feedback,
                    Severity.WARNING,
                    float(bend_count),
                    0.0,
                    0.0,
                    "",
                    "The panels and bends on this part close into a loop, so it "
                    "cannot be folded from one flat blank -- the last fold has "
                    "nowhere to go. It needs a seam: welded, riveted, or tab and "
                    "slot. Which one it is wants settling before the part is "
                    "quoted, because the seam is most of the labour.",
                ),
                value=float(bend_count),
            )
        ]


@register_check(Rulebook.SHEET_HEM_DIMENSIONS)
class SheetHemDimensionsCheck(SheetCheck):
    """A hem the die cannot catch, or one closed flat in heavy stock.

    The return is the shorter of the two panels off the fold: the folded-back
    lip, as against the parent sheet it lies on.
    """

    @property
    def name(self) -> str:
        return "Sheet Hem Dimensions Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        configured = self.safe_float(rule_config.limit)
        epsilon = threshold(context, "sheet_dimension_eps_mm", SHEET_DIMENSION_EPS_MM)

        results: list[CheckResult] = []
        for feature in sorted_features(context, FeatureType.BEND):
            if not feature.param("is_hem"):
                continue
            bend = bend_geom(feature)
            if bend is None:
                continue

            hem_return = None
            for panel_id in bend.panels:
                direction = panel_away_dir(context, bend, panel_id)
                if direction is None:
                    continue
                extents = patch_extents_along(context, panel_id, bend.origin, direction)
                if extents is None or extents[1] <= 1e-9:
                    continue
                hem_return = extents[1] if hem_return is None else min(hem_return, extents[1])
            if hem_return is None:
                continue

            minimum = configured if configured is not None else hem_min_return_mm(gauge, context)
            short_return = hem_return < minimum - epsilon
            closed_heavy = (
                bend.inner_radius < _CLOSED_HEM_RADIUS_FACTOR * gauge
                and gauge > _CLOSED_HEM_MIN_GAUGE_MM
            )
            if not short_return and not closed_heavy:
                continue

            if short_return:
                basis = (
                    "a flat quarter inch at 14 gauge and thinner"
                    if gauge <= GA14_STEEL_MM
                    else f"{minimum / gauge:.0f} times the gauge"
                )
                advice = (
                    f"This hem returns only {hem_return:.1f} mm past the fold, and "
                    f"the hemming die needs {minimum:.1f} mm -- {basis} -- to "
                    "flatten it. A lip shorter than that is not caught properly by "
                    "the die and comes back wavy or only half closed. Lengthen the "
                    "return."
                )
                overview = f"{hem_return:.1f} mm return"
            else:
                advice = (
                    f"This hem is closed -- the fold radius is under half the "
                    f"gauge -- in {gauge:.2f} mm material. Metal this heavy cracks "
                    "along the outside of a fold flattened that far. Open the hem, "
                    "or use a teardrop, and leave a radius of about one gauge in it."
                )
                overview = f"closed hem in {gauge:.2f} mm"

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    overview,
                    self.render(
                        feedback,
                        Severity.WARNING,
                        hem_return,
                        minimum,
                        minimum,
                        "mm",
                        advice,
                    ),
                    faces=feature.faces,
                    value=hem_return,
                    limit=minimum,
                    comparison="<",
                )
            )
        return results


@register_check(Rulebook.SHEET_BEND_RELIEF_MISSING)
class SheetBendReliefCheck(SheetCheck):
    """A fold that runs out mid-panel with the sheet carrying on past it.

    The end of the fold is a stress raiser with nowhere to go: the metal on one
    side is being pulled round and the metal beyond the fold is not, and it
    tears at the join. A relief cut puts a free edge there instead.

    Whether a relief exists is asked of the material and not of the feature
    list. Real reliefs come in every shape a laser can cut, and none of them
    reliably reads back as a recognized notch.
    """

    @property
    def name(self) -> str:
        return "Sheet Bend Relief Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        probe = SolidProbe(context.shape)
        margin = _BEND_END_MARGIN_GAUGES * gauge

        results: list[CheckResult] = []
        for bend in bends_of(context):
            span = bend_axial_span(context, bend)
            if span is None:
                continue
            bend_low, bend_high = span

            # Panels are measured as coplanar patches for the same reason the
            # flange rule does it: a seam splits the base plate, and the
            # fragment touching the bend spans only the bend.
            patches = []
            for panel_id in bend.panels:
                extents = patch_extents_along(context, panel_id, bend.origin, bend.axis)
                if extents is not None:
                    patches.append((panel_id, extents[0], extents[1]))
            if not patches:
                continue
            panel_low = min(entry[1] for entry in patches)
            panel_high = max(entry[2] for entry in patches)

            ends = (
                (bend_low, panel_low < bend_low - margin, True),
                (bend_high, panel_high > bend_high + margin, False),
            )
            for position, is_open, at_low in ends:
                if not is_open:
                    continue
                if not self._material_beyond(
                    context, probe, bend, patches, position, at_low, gauge, margin
                ):
                    continue

                end_point = bend.point_at(position)
                results.append(
                    self.finding(
                        rule,
                        Severity.WARNING,
                        "no relief at the bend end",
                        self.render(
                            feedback,
                            Severity.WARNING,
                            gauge,
                            gauge,
                            gauge,
                            "mm",
                            "This bend stops partway along the panel edge and the "
                            "sheet carries straight on past the end of the fold, "
                            "with nothing cut to release it. The metal tears at the "
                            "point the bend runs out. Cut a relief at the free end, "
                            f"at least one gauge wide -- {gauge:.2f} mm -- and "
                            "deeper than the bend radius plus the gauge, so the "
                            f"tear has nowhere to start. The end sits at "
                            f"({end_point.X():.1f}, {end_point.Y():.1f}, "
                            f"{end_point.Z():.1f}).",
                        ),
                        faces=bend.feature.faces,
                        value=gauge,
                        limit=gauge,
                        comparison="<",
                    )
                )
        return results

    @staticmethod
    def _material_beyond(
        context, probe, bend, patches, position, at_low, gauge, margin
    ) -> bool:
        """Whether sheet continues past the end of the fold at a continuing panel.

        Two axial rows, not one. A relief cut merely tangent to the end line
        clears the outer row and leaves a cusp of metal right at the fold, and
        that cusp is where the tear starts.
        """
        for row in _RELIEF_PROBE_ROWS:
            offset = -row * gauge if at_low else row * gauge
            beyond = bend.point_at(position + offset)
            for panel_id, low, high in patches:
                past = (low < position - margin) if at_low else (high > position + margin)
                if not past:
                    continue
                if probe.material_in_fold_strip(context, bend, panel_id, beyond, gauge):
                    return True
        return False


@register_check(Rulebook.SHEET_CORNER_RELIEF_MISSING)
class SheetCornerReliefCheck(SheetCheck):
    """Two folds meeting at a corner with metal still in the way.

    Both flanges swing up through the same corner, and if the blank was not cut
    back there they arrive in the same place. The corner buckles, splits, or
    simply stops the second fold from closing.

    As with bend relief, the corner is probed for metal rather than searched
    for a cutout feature: a corner relief is cut square on one part and round
    on the next, and neither shape reads back reliably.
    """

    @property
    def name(self) -> str:
        return "Sheet Corner Relief Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        probe = SolidProbe(context.shape)
        bends = bends_of(context)
        reach = _CORNER_REACH_GAUGES * gauge

        results: list[CheckResult] = []
        for index, first in enumerate(bends):
            for second in bends[index + 1 :]:
                base_id = self._shared_base(context, first, second)
                if base_id is None:
                    continue
                if abs(first.axis.Dot(second.axis)) > _CORNER_AXIS_MAX_DOT:
                    continue

                corner = self._closest_point(first, second)
                if corner is None:
                    continue
                if not self._both_reach(context, (first, second), corner, reach):
                    continue

                base = context.graph.node(base_id) if context.graph.has_node(base_id) else None
                if base is None:
                    continue
                # No metal at the corner means it is already relieved, whatever
                # shape the cutback happens to be.
                if not probe.material_at_mid_gauge(base, corner, gauge):
                    continue

                results.append(
                    self.finding(
                        rule,
                        Severity.WARNING,
                        "no corner relief",
                        self.render(
                            feedback,
                            Severity.WARNING,
                            0.0,
                            0.0,
                            0.0,
                            "",
                            "Two bends meet at this corner with nothing cut away "
                            "between them, so both flanges swing up into the same "
                            "metal and the corner buckles or splits as the second "
                            "fold closes. Cut the corner back, square or round, far "
                            "enough to clear both bend zones. The corner is at "
                            f"({corner.X():.1f}, {corner.Y():.1f}, "
                            f"{corner.Z():.1f}).",
                        ),
                        faces=sorted(set(first.feature.faces) | set(second.feature.faces)),
                    )
                )
        return results

    @staticmethod
    def _shared_base(context, first, second):
        """The panel both bends fold off, comparing planes rather than face ids.

        A fuse seam gives one base plate several face ids, so two bends off the
        same plate share no id at all and an identity test finds no corner.
        """
        for candidate in (first.panel_a, first.panel_b):
            for other in (second.panel_a, second.panel_b):
                if panels_coplanar(context, candidate, other):
                    return candidate
        return None

    @staticmethod
    def _closest_point(first, second):
        """Where the two fold lines come nearest: the corner between them."""
        u = gp_Vec(first.axis)
        v = gp_Vec(second.axis)
        w = gp_Vec(second.origin, first.origin)
        uv = u.Dot(v)
        denominator = 1.0 - uv * uv
        if abs(denominator) < 1e-9:
            return None
        distance = (uv * w.Dot(v) - w.Dot(u)) / denominator
        return first.point_at(distance)

    @staticmethod
    def _both_reach(context, bends, corner: gp_Pnt, reach: float) -> bool:
        """Whether each fold actually runs as far as the corner it points at."""
        for bend in bends:
            span = bend_axial_span(context, bend)
            if span is None:
                return False
            along = gp_Vec(bend.origin, corner).Dot(gp_Vec(bend.axis))
            if along < span[0] - reach or along > span[1] + reach:
                return False
        return True
