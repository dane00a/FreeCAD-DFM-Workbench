# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about milled cavities: pockets and slots.

Most of these come down to the cutter. A pocket cannot have a corner sharper
than the tool that cleared it, cannot be narrower than the smallest tool that
fits, and cannot be deeper than that tool can reach without chattering. So
several of the limits here are not material properties at all -- they are
derived from the shop's tool library, and change when the shop does.
"""

from __future__ import annotations



from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


@register_check(Rulebook.POCKET_DEPTH_RATIO)
class PocketDepthRatioCheck(MachiningCheck):
    """Deep pockets are cut with a long tool, and long tools chatter.

    Judged against the pocket's narrowest width, because that is what limits
    the tool diameter, and tool stiffness falls with the cube of length over
    diameter.
    """

    @property
    def name(self) -> str:
        return "Pocket Depth Ratio Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None:
            target = thresholds.pocket_deep_warn_ratio
        if limit is None:
            limit = thresholds.pocket_deep_error_ratio

        results: list[CheckResult] = []
        for pocket in context.recognition.of_type(FeatureType.POCKET):
            width = pocket.number("min_width_mm") or 0.0
            depth = pocket.number("depth_mm") or 0.0
            if width <= 1e-6 or depth <= 1e-6:
                continue

            ratio = depth / width
            graded = self.graded(ratio, target, limit, "max")
            if graded is None:
                continue

            severity, threshold = graded
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{ratio:.1f}x width",
                    self.render(
                        feedback,
                        severity,
                        ratio,
                        target,
                        limit,
                        "",
                        f"This pocket is {depth:.1f} mm deep and only {width:.1f} mm "
                        f"across at its narrowest, {ratio:.1f} times its width. The "
                        "cutter has to reach that far on a diameter the pocket will "
                        "admit, so expect chatter and taper down the walls. A wider "
                        "pocket, a shallower one, or a stepped opening would all "
                        "help.",
                    ),
                    faces=pocket.faces,
                    value=ratio,
                    limit=threshold,
                    comparison=">",
                )
            )
        return results


@register_check(Rulebook.POCKET_CORNER_RADIUS)
class PocketCornerRadiusCheck(MachiningCheck):
    """A rotating cutter cannot leave a sharp inside corner.

    The corner it leaves is its own radius, so a drawing asking for a sharp
    one is asking for a second process: EDM, broaching, or a relief cut.
    Which of those depends on whether the cavity goes through.
    """

    @property
    def name(self) -> str:
        return "Pocket Corner Radius Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.pocket_corner_radius_min_mm

        achievable = context.config.smallest_internal_corner_radius()
        results: list[CheckResult] = []

        # Every cavity the recognizers measure a corner radius on, not just
        # the ones called pockets. A cavity cut clean through the part has
        # exactly the same square corners as a blind one and is milled the
        # same way -- and while it was left out, nothing spoke for those
        # corners at all: the sharp-edge rule stood down because the cavity
        # was a recognized feature, and this rule never looked at it.
        for cavity in context.recognition.of_type(
            FeatureType.POCKET, FeatureType.SLOT, FeatureType.THROUGH_CAVITY
        ):
            # A slot open at its end has no inside corner there to speak of.
            if cavity.param("is_open") and cavity.type == FeatureType.SLOT:
                continue
            # Absent means the recognizer found no fillet at all, which is a
            # square corner. Present and zero means the same thing measured.
            radius = cavity.number("corner_radius_mm", 0.0) or 0.0
            if radius > limit:
                continue

            # Two different problems share this rule. A square corner is a
            # request for a second process, which is a cost and a lead time --
            # a warning. A corner specified with a radius that is real but
            # tighter than any cutter in the shop cannot be made as drawn at
            # all, and is an error.
            infeasible = achievable is not None and radius < achievable - 0.01
            severity = Severity.ERROR if (infeasible and radius > 0.0) else Severity.WARNING

            through = bool(cavity.param("is_through"))
            process = "wire EDM" if through or cavity.param("is_open") else "sinker EDM"
            if radius <= 0.0:
                remedy = (
                    f"A radius of {achievable:.1f} mm or more would let it be milled."
                    if achievable
                    else "Allowing a small radius would let it be milled."
                )
                advice = (
                    "This cavity has square inside corners. A rotating cutter "
                    f"leaves its own radius behind, so as drawn it needs {process} "
                    f"or a corner relief. {remedy}"
                )
            else:
                advice = (
                    f"The inside corners are {radius:.2f} mm, tighter than the "
                    f"{achievable:.2f} mm the smallest cutter in the library can "
                    f"leave. As drawn this needs {process}."
                )

            results.append(
                self.finding(
                    rule,
                    severity,
                    "square corners" if radius <= 0.0 else f"{radius:.2f} mm corners",
                    self.render(feedback, severity, radius, limit, limit, "mm", advice),
                    faces=cavity.faces,
                    value=radius,
                    limit=limit,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.POCKET_NARROW_OPENING)
class PocketNarrowOpeningCheck(MachiningCheck):
    """A pocket has to be wider than the tool that clears it.

    Comfortably wider: a cutter the exact width of the pocket is engaged on
    both flanks at once with nowhere for chips to go. Twice the diameter is
    the usual minimum for a pocket that can actually be cleared.
    """

    @property
    def name(self) -> str:
        return "Pocket Too Narrow Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        smallest = context.config.smallest_end_mill_diameter()
        if smallest is None:
            return []

        required = smallest * context.config.thresholds.pocket_narrow_opening_tool_multiple
        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []

        for pocket in context.recognition.of_type(FeatureType.POCKET):
            width = pocket.number("min_width_mm") or 0.0
            if width <= 1e-6 or width >= required:
                continue

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{width:.2f} mm opening",
                    self.render(
                        feedback,
                        severity,
                        width,
                        required,
                        required,
                        "mm",
                        f"This pocket is {width:.2f} mm across at its narrowest. The "
                        f"smallest end mill available is {smallest:.2f} mm, which "
                        f"needs about {required:.2f} mm of width to clear a pocket "
                        "without cutting on both flanks at once. As drawn it would "
                        "need a smaller special cutter, or EDM.",
                    ),
                    faces=pocket.faces,
                    value=width,
                    limit=required,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.SLOT_DEPTH_RATIO)
class SlotDepthRatioCheck(MachiningCheck):
    """A deep slot is cut by a tool no wider than the slot.

    Tighter than the pocket limit, because a slot cutter is engaged on both
    flanks for the whole pass with no room to step over.
    """

    @property
    def name(self) -> str:
        return "Slot Depth Ratio Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.slot_deep_warn_ratio

        results: list[CheckResult] = []
        for slot in context.recognition.of_type(FeatureType.SLOT, FeatureType.CHANNEL):
            width = slot.number("width_mm") or 0.0
            depth = slot.number("depth_mm") or 0.0
            if width <= 1e-6 or depth <= 1e-6:
                continue

            ratio = depth / width
            if ratio <= limit:
                continue

            severity = Severity.WARNING
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{ratio:.1f}x width",
                    self.render(
                        feedback,
                        severity,
                        ratio,
                        limit,
                        limit,
                        "",
                        f"This slot is {depth:.1f} mm deep and {width:.1f} mm wide, "
                        f"{ratio:.1f} times its width. The cutter is engaged on both "
                        "flanks for the whole pass with nowhere to clear chips, so "
                        "expect chatter and a tapered slot. Cutting it in steps, or "
                        "widening it enough to step over, would both help.",
                    ),
                    faces=slot.faces,
                    value=ratio,
                    limit=limit,
                    comparison=">",
                )
            )
        return results


@register_check(Rulebook.SLOT_OVERHANG)
class SlotOverhangCheck(MachiningCheck):
    """A long slot cut at full stickout will chatter.

    Two things have to be true together, which is why this is its own rule
    rather than a second threshold on depth. A long shallow groove is fine:
    the tool is short. A short deep one is fine: the pass is brief. It is the
    combination -- a long pass at full extension -- that rings.
    """

    @property
    def name(self) -> str:
        return "Slot Cutter Overhang Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []

        for slot in context.recognition.of_type(FeatureType.SLOT, FeatureType.CHANNEL):
            width = slot.number("width_mm") or 0.0
            length = slot.number("length_mm") or 0.0
            depth = slot.number("depth_mm") or 0.0
            if width <= 1e-6:
                continue

            length_ratio = length / width
            depth_ratio = depth / width
            if length_ratio <= thresholds.slot_overhang_warn_ratio:
                continue
            if depth_ratio < thresholds.slot_overhang_depth_gate_ratio:
                continue

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{length_ratio:.0f}x long, {depth_ratio:.1f}x deep",
                    self.render(
                        feedback,
                        severity,
                        length_ratio,
                        thresholds.slot_overhang_warn_ratio,
                        thresholds.slot_overhang_warn_ratio,
                        "",
                        f"This slot runs {length:.0f} mm on a {width:.1f} mm width "
                        f"and {depth:.1f} mm deep. That is a long pass with the "
                        "cutter at nearly full stickout, which is where chatter "
                        "marks and a bell-mouthed entry come from. Roughing "
                        "undersize and finishing with a light spring pass is the "
                        "usual answer.",
                    ),
                    faces=slot.faces,
                    value=length_ratio,
                    limit=thresholds.slot_overhang_warn_ratio,
                    comparison=">",
                )
            )
        return results
