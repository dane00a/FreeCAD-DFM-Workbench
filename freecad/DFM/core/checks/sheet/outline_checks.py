# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about the cut outline: tabs and notches.

Both are features of the flat blank, put there before anything went near the
brake, and both fail the same way at the extremes. A tab is a peninsula of
metal with nothing bracing it, so it bends in the nest or in the tote. A notch
is the bite that leaves it, and a narrow deep one is a slender punch working
in shear on both sides at once.

A tab has an absolute floor as well as a gauge multiple, because handling
damage does not scale with thickness: a 2 mm finger snaps off a heavy plate as
readily as off a thin one.
"""

from __future__ import annotations

from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import (
    SHEET_DIMENSION_EPS_MM,
    SHEET_MIN_NOTCH_FACTOR,
    SHEET_MIN_TAB_WIDTH_MM,
    SHEET_NOTCH_MAX_DEPTH_RATIO,
    SHEET_TAB_MAX_ASPECT,
    SHEET_TAB_WIDTH_FACTOR,
    SheetCheck,
    gauge_phrase,
    sorted_features,
    threshold,
)


@register_check(Rulebook.SHEET_TAB_NARROW)
class SheetTabNarrowCheck(SheetCheck):
    """An outline tab too narrow or too slender to survive handling.

    Two ways to fail, and either is enough. Below the width floor there is not
    enough metal at the root; past the aspect ratio there is enough metal but
    too much lever on it, and the tab arrives at the brake already bent.
    """

    @property
    def name(self) -> str:
        return "Sheet Tab Width Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(context, "sheet_tab_width_factor", SHEET_TAB_WIDTH_FACTOR)
        floor = threshold(context, "sheet_min_tab_width_mm", SHEET_MIN_TAB_WIDTH_MM)
        max_aspect = threshold(context, "sheet_tab_max_aspect", SHEET_TAB_MAX_ASPECT)
        minimum = max(floor, gauge * factor)

        results: list[CheckResult] = []
        for tab in sorted_features(context, FeatureType.TAB):
            width = tab.number("width_mm", 0.0) or 0.0
            aspect = tab.number("aspect", 0.0) or 0.0
            if width <= 0.0:
                continue

            too_narrow = width < minimum
            too_slender = aspect > max_aspect
            if not too_narrow and not too_slender:
                continue

            if too_narrow:
                problem = (
                    f"is only {width:.1f} mm wide, under the {minimum:.1f} mm "
                    f"minimum (the greater of {floor:.1f} mm and "
                    f"{gauge_phrase(factor)})"
                )
                overview = f"{width:.1f} mm tab"
                value, limit = width, minimum
            else:
                problem = (
                    f"runs {aspect:.1f} times its own width, past the "
                    f"{max_aspect:.0f}:1 the shop will handle"
                )
                overview = f"{aspect:.1f}:1 tab"
                value, limit = aspect, max_aspect

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    overview,
                    self.render(
                        feedback,
                        Severity.WARNING,
                        value,
                        limit,
                        limit,
                        "",
                        f"This outline tab {problem}. A tab that fine bends over in "
                        "the nest, catches on the slat, or breaks off in the tote, "
                        "and it arrives at the brake already out of shape. Widen "
                        "it, shorten it, or carry it on a wider root and trim it "
                        "back later.",
                    ),
                    faces=tab.faces,
                    value=value,
                    limit=limit,
                    comparison="<" if too_narrow else ">",
                )
            )
        return results


@register_check(Rulebook.SHEET_NOTCH_NARROW)
class SheetNotchNarrowCheck(SheetCheck):
    """An outline notch too narrow to punch or too deep for its width.

    The epsilon matters more here than anywhere else in the family: the width
    threshold sits at exactly one gauge, so a 2.0 mm notch in 2.0 mm material
    lands precisely on it and float noise off a STEP round trip would otherwise
    decide the verdict.
    """

    @property
    def name(self) -> str:
        return "Sheet Notch Width Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        factor = self.safe_float(rule_config.limit)
        if factor is None:
            factor = threshold(context, "sheet_min_notch_factor", SHEET_MIN_NOTCH_FACTOR)
        depth_ratio = threshold(
            context, "sheet_notch_max_depth_ratio", SHEET_NOTCH_MAX_DEPTH_RATIO
        )
        epsilon = threshold(context, "sheet_dimension_eps_mm", SHEET_DIMENSION_EPS_MM)

        results: list[CheckResult] = []
        for notch in sorted_features(context, FeatureType.NOTCH):
            width = notch.number("width_mm", 0.0) or 0.0
            depth = notch.number("length_mm", 0.0) or 0.0
            if width <= 0.0:
                continue

            minimum = gauge * factor
            max_depth = width * depth_ratio
            too_narrow = width < minimum - epsilon
            too_deep = depth > max_depth + epsilon
            if not too_narrow and not too_deep:
                continue

            if too_narrow:
                problem = (
                    f"is only {width:.2f} mm wide, under the {minimum:.2f} mm "
                    f"minimum of {gauge_phrase(factor)}"
                )
                overview = f"{width:.2f} mm notch"
                value, limit, comparison = width, minimum, "<"
            else:
                problem = (
                    f"is {depth:.1f} mm deep at {width:.1f} mm wide, past the "
                    f"{depth_ratio:.0f} times its width the tooling will stand "
                    f"({max_depth:.1f} mm)"
                )
                overview = f"{depth:.0f} mm deep, {width:.1f} mm wide"
                value, limit, comparison = depth, max_depth, ">"

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    overview,
                    self.render(
                        feedback,
                        Severity.WARNING,
                        value,
                        limit,
                        limit,
                        "mm",
                        f"This outline notch {problem}. A punch that narrow or that "
                        "long in section deflects and then breaks, and the edges "
                        "tear rather than shear. Open the notch out, shorten it, or "
                        "cut this feature on the laser instead.",
                    ),
                    faces=notch.faces,
                    value=value,
                    limit=limit,
                    comparison=comparison,
                )
            )
        return results
