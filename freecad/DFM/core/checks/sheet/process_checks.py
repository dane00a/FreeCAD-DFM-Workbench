# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about the part as a whole rather than any one feature.

Whether the gauge is sheet at all, what the part is going to cost in
operations, and two rules about folds that were never modelled as folds.

Those last two are the same observation seen from either side of the
classifier. On a part that reads as sheet, a square junction between two skins
is a bend somebody drew without a radius. On a part that reads as machined but
is shaped like sheet, the same square junctions say the drawing may have been
meant as folded metal. The geometry is honestly ambiguous -- a sharp-cornered
constant-gauge shell is indistinguishable from a machined angle bracket -- so
the second one stays an advisory and the machined verdict stands.
"""

from __future__ import annotations

from ...machining.features import FeatureType
from ...machining.process_classifier import PartProcessType, detect_sharp_fold_shell
from ...machining.sheet_folds import detect_sharp_folds
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from ..machining.base import MachiningCheck
from .base import (
    SHEET_MAX_THICKNESS_ALU_MM,
    SHEET_MIN_THICKNESS_MM,
    SheetCheck,
    features_with_sharp_folds,
    material_declared,
    material_is_aluminium,
    max_thickness_mm,
    threshold,
)


# =============================================================================
# Gauge range
# =============================================================================


#: Slack on the measured gauge before it is called out of range.
_GAUGE_MEASUREMENT_SLACK_MM = 0.05


@register_check(Rulebook.SHEET_THICKNESS_OUT_OF_RANGE)
class SheetThicknessRangeCheck(SheetCheck):
    """The measured gauge against the range the shop works as sheet.

    The two ends answer different questions. The floor asks whether this is
    sheet metal at all, which is a question about geometry and so is the same
    for every alloy. The ceiling asks what the shop forms rather than machines,
    and that is nearly twice as thick in aluminium as in steel -- so a single
    number could only ever be right for one of them.

    Material is declared, never inferred: no face carries a signal for alloy.
    An undeclared part is judged against steel, which is the tighter of the two
    and therefore the strict answer, and the finding says so in ordinary prose.
    A customer who left a field blank that was never required has done nothing
    wrong.
    """

    @property
    def name(self) -> str:
        return "Sheet Gauge Range Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        family = str(getattr(context.config, "material_family", "") or "")

        minimum = self.safe_float(rule_config.min_value)
        if minimum is None:
            minimum = threshold(context, "sheet_min_thickness_mm", SHEET_MIN_THICKNESS_MM)
        maximum = self.safe_float(rule_config.max_value)
        if maximum is None:
            maximum = max_thickness_mm(family, context)

        # The measured gauge carries tessellation noise: a 0.125 in plate
        # reads 3.1767 against a 3.175 ceiling, which is nobody's
        # manufacturing problem. Five hundredths of slack at each end sits
        # well below any real gauge step (30 ga to 29 ga is 0.08 mm).
        slack = _GAUGE_MEASUREMENT_SLACK_MM
        if minimum - slack <= gauge <= maximum + slack:
            return []

        declared = material_declared(family)
        aluminium = material_is_aluminium(family)
        family_name = "aluminium" if aluminium else "steel"
        alu_ceiling = threshold(
            context, "sheet_max_thickness_alu_mm", SHEET_MAX_THICKNESS_ALU_MM
        )

        if gauge < minimum:
            advice = (
                f"The measured gauge is {gauge:.3f} mm, below the {minimum:.3f} mm "
                "floor this analysis treats as sheet metal -- 30 gauge. At this "
                "thickness the material is foil or shim stock, formed and handled "
                "by different processes entirely. Worth confirming the intended "
                "material and thickness."
            )
            limit = minimum
            comparison = "<"
        elif declared:
            advice = (
                f"The measured gauge is {gauge:.2f} mm, above the {maximum:.2f} mm "
                f"ceiling for {family_name}, the declared material. Above this the "
                "shop works it as plate rather than sheet: different handling, "
                "different tooling, and the press-brake figures in the rest of this "
                "report stop applying."
            )
            limit = maximum
            comparison = ">"
        else:
            advice = (
                f"The measured gauge is {gauge:.2f} mm, above the {maximum:.2f} mm "
                "sheet ceiling for steel. No material was declared with this part, "
                "so the steel range was used -- it is the tighter of the two, and "
                f"aluminium runs to {alu_ceiling:.1f} mm. Above the ceiling the shop "
                "works it as plate rather than sheet. Name the alloy and it will be "
                "re-checked against the right figure."
            )
            limit = maximum
            comparison = ">"

        return [
            self.finding(
                rule,
                Severity.INFO,
                f"{gauge:.2f} mm gauge",
                self.render(feedback, Severity.INFO, gauge, minimum, maximum, "mm", advice),
                value=gauge,
                limit=limit,
                comparison=comparison,
                unit="mm",
            )
        ]


# =============================================================================
# Cost census
# =============================================================================


@register_check(Rulebook.SHEET_FEATURE_COMPLEXITY)
class SheetFeatureComplexityCheck(SheetCheck):
    """What the part is going to take to make, counted.

    The machining complexity rule is muted on sheet parts and its drill-and-
    mill operation model would be meaningless here anyway. This is the sheet
    reading: one cut program covers the whole profile however many holes are in
    it, each bend is a stroke on the brake and each hem is two, each formed
    feature is a die station, and each tapped hole is a trip to a second
    machine.

    Distinct bend radii are counted separately from bends, because two radii
    means two punches and a tool change between them.
    """

    @property
    def name(self) -> str:
        return "Sheet Feature Complexity Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        bends = hems = formed = holes = tapped = tabs = notches = 0
        conical = rolled = sharp = 0
        radii: set[int] = set()

        features = sorted(features_with_sharp_folds(context), key=lambda f: f.instance_id)
        for feature in features:
            if feature.type == FeatureType.BEND:
                # Only a press-brake fold is a brake stroke. A conical flange
                # needs a tapered die, a rolled section a roll pass, and a
                # sharp fold was never formed at all; each is named and none
                # is costed as a stroke.
                family = str(feature.param("bend_family") or "cylindrical")
                if family == "conical":
                    conical += 1
                    continue
                if family == "rolled":
                    rolled += 1
                    continue
                if family == "sharp":
                    sharp += 1
                    continue
                if feature.param("is_hem"):
                    hems += 1
                else:
                    bends += 1
                radii.add(round((feature.number("inner_radius_mm", 0.0) or 0.0) * 10.0))
            elif feature.type == FeatureType.SHEET_FORMED:
                formed += 1
            elif feature.type == FeatureType.TAB:
                tabs += 1
            elif feature.type == FeatureType.NOTCH:
                notches += 1
            elif feature.is_hole:
                holes += 1
                if feature.type == FeatureType.THREADED_HOLE:
                    tapped += 1

        strokes = bends + 2 * hems
        operations = 1 + strokes + formed + tapped

        # The census stands whatever the numbers are; a configured pair only
        # decides whether it is worth raising the tone.
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        # Informational whatever the count. An operation estimate is what a
        # quote is built from, not a fault to be graded: a busy panel is not
        # a worse panel, and colouring it as one buries the findings that
        # are. The threshold still selects the wording.
        severity = Severity.INFO
        threshold_value = float(operations)
        graded = self.graded(float(operations), target, limit, "max")
        if graded is not None:
            _, threshold_value = graded

        tapping = (
            f" plus {tapped} secondary tapping operation"
            + ("s" if tapped != 1 else "")
            if tapped
            else ""
        )
        unpriced = ""
        if conical + rolled + sharp > 0:
            unpriced = (
                f" Besides those, {conical} conical bend(s), {rolled} rolled "
                f"section(s) and {sharp} sharp zero-radius fold(s) are not "
                "press-brake strokes and are left out of that estimate."
            )
            if rolled + sharp > 0:
                unpriced += " The bend-program finding says what each one blocks."
            else:
                unpriced += (
                    " The conical folds are fully recognized, but a tapered "
                    "flange has no single bend radius, so no developed length "
                    "comes from one either -- price the tapered die from the "
                    "drawing."
                )
        return [
            self.finding(
                rule,
                severity,
                f"~{operations} operations",
                self.render(
                    feedback,
                    severity,
                    float(operations),
                    target if target is not None else float(operations),
                    limit if limit is not None else float(operations),
                    "",
                    # "press-brake bend(s)", not "bend(s)": the count is the
                    # cylindrical folds that cost a stroke only. On a part
                    # whose one fold is sharp, "0 bend(s) ... plus 1 sharp
                    # fold" contradicted the feature list it sat beside.
                    f"This part carries {bends} press-brake bend(s) and {hems} hem(s) across "
                    f"{len(radii)} distinct bend radii, {formed} formed feature(s), "
                    f"{holes} hole(s) of which {tapped} are tapped, {tabs} tab(s) "
                    f"and {notches} notch(es). That prices as one cut profile plus "
                    f"{strokes} brake stroke(s) plus {formed} forming station(s)"
                    f"{tapping} -- about {operations} operations. Each distinct bend "
                    "radius is its own punch and a tool change, so consolidating "
                    "radii is usually the cheapest saving on a part like this."
                    + unpriced,
                ),
                value=float(operations),
                limit=threshold_value,
                comparison=">",
            )
        ]


# =============================================================================
# Folds that were never modelled as folds
# =============================================================================


@register_check(Rulebook.SHEET_SHARP_FOLD)
class SheetSharpFoldCheck(SheetCheck):
    """A square junction between two panel skins, where a bend belongs.

    A press brake always leaves a radius -- roughly one gauge on the inside --
    so a corner drawn square is a corner nobody can form. It is nearly always a
    modelling shortcut rather than a requirement, and it matters because the
    unmodelled bend carries no geometry for any of the other rules to check.

    Faces owned by a recognized sheet feature are not candidates. A formed
    hood's crest corners are coined sharp by the die on purpose, and bend and
    hem geometry is a fold already done right.
    """

    @property
    def name(self) -> str:
        return "Sheet Sharp Fold Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = self.gauge(context)
        # The one walk the bend census and this rule share, so the two cannot
        # disagree about what a sharp fold is. Handed the recognized features,
        # not the list with the sharp BENDs added: those own no faces, so the
        # exclusions come out the same either way.
        # The closing comparison is only made when there is something to
        # compare against: on a part whose only fold is this sharp one, "unlike
        # the bends elsewhere" sent a reader looking for geometry that does not
        # exist. Count the folds that carry a radius.
        modelled_bends = sum(
            1
            for feature in context.recognition.features
            if feature.type == FeatureType.BEND
            and str(feature.param("bend_family") or "cylindrical") != "sharp"
        )
        if modelled_bends > 0:
            sharp_tail = (
                f" Unlike the {modelled_bends} modelled bend(s) elsewhere on this "
                "part, it carries no bend geometry to check."
            )
        else:
            sharp_tail = (
                " It carries no bend geometry to check, and this part has no "
                "modelled bend to compare it against."
            )
        results: list[CheckResult] = []
        for fold in detect_sharp_folds(context.graph, gauge, context.recognition.features):
            where = ""
            point = fold.crease_midpoint
            if point is not None:
                where = f" The fold is at ({point.X():.1f}, {point.Y():.1f}, {point.Z():.1f})."
            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    "zero-radius fold",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        0.0,
                        gauge,
                        gauge,
                        "mm",
                        "Two panels meet here at a sharp, zero-radius fold. A "
                        "press brake always forms a radius -- about one gauge "
                        f"on the inside, {gauge:.2f} mm here -- so as drawn "
                        "this corner cannot be folded. Model the bend radius."
                        + sharp_tail
                        + where,
                    ),
                    faces=[fold.panel_a, fold.panel_b],
                    value=0.0,
                    limit=gauge,
                    comparison="<",
                )
            )
        return results


@register_check(Rulebook.SHEET_INTENT_SHARP_CORNERS)
class SheetIntentSharpCornersCheck(MachiningCheck):
    """A milled part shaped like folded sheet, with the folds drawn square.

    This one deliberately does not carry the sheet gate: it fires on parts the
    classifier called milled. A constant-thickness shell whose corners are
    square is genuinely ambiguous -- it is what a machined angle bracket looks
    like, and it is also what folded sheet looks like when the modeller left
    the radii out -- so the finding stays an advisory and the milled verdict
    stands. One process voice per part.
    """

    applicable_processes = frozenset({PartProcessType.MILLED})

    @property
    def name(self) -> str:
        return "Sheet Intent Sharp Corners Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        gauge = detect_sharp_fold_shell(context.graph)
        if gauge is None:
            return []

        return [
            self.finding(
                rule,
                Severity.INFO,
                f"~{gauge:.2f} mm shell, square corners",
                self.render(
                    feedback,
                    Severity.INFO,
                    gauge,
                    gauge,
                    gauge,
                    "mm",
                    f"This part is a constant-thickness shell, about {gauge:.2f} mm, "
                    "with square corners where sheet-metal bends would be. It also "
                    "carries machined features, so it has been quoted as machined "
                    "from solid. If it was meant as folded sheet, model the bend "
                    "radii -- roughly one material thickness on the inside -- and "
                    "move the machined features to secondary operations after "
                    "forming. Done that way it is usually a fraction of the price.",
                ),
                value=gauge,
                limit=gauge,
                comparison="",
                unit="mm",
                # A machined part that should have been sheet is a process
                # advisory in the reference ("process"), not a sheet finding.
                category="process",
            )
        ]
