# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""More rules about milled cavities, and about the ones that are not milled.

Three of these are the tool library asking whether it has anything that will
do the job: a cutter short enough to reach a pocket floor without its shank
fouling the walls, and one whose diameter matches a slot width so the slot
comes out in a single pass.

The fourth pair is different. A flexure slit and a re-entrant slot are not
milling jobs at all, and the recognizer has already said so by giving them
their own feature types. What these two rules add is the process expectation,
so that a slit which is perfectly correct on the drawing does not go out for
quote as though an end mill would cut it.
"""

from __future__ import annotations

from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# A pocket floor is out of reach once the depth passes the flute length of the
# longest tool that fits: past that the shank rubs the wall and the cutter
# stops going down. Expressed as a ratio so a shop can warn before it bites.
_FLUTE_REACH_LIMIT = 1.0


@register_check(Rulebook.POCKET_ASPECT_RATIO)
class PocketAspectRatioCheck(MachiningCheck):
    """A pocket cannot be deeper than the cutter that fits it is long.

    Diameter is only half the question. A 6mm end mill will go into a 6mm-wide
    pocket, but it carries perhaps 18mm of flute, and below that is plain
    shank -- which rubs rather than cuts, and will not go down another
    millimetre. So the reach is set by the widest tool the pocket will admit,
    and a narrow deep pocket runs out of tool long before it runs out of
    machine.

    An extended-reach or reduced-neck cutter buys some of it back, at the cost
    of rigidity; that is a conversation to have deliberately rather than to
    discover at the machine.
    """

    @property
    def name(self) -> str:
        return "Pocket Tool Reach Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = _FLUTE_REACH_LIMIT

        results: list[CheckResult] = []
        for pocket in context.recognition.of_type(FeatureType.POCKET):
            depth = pocket.number("depth_mm") or 0.0
            width = pocket.number("min_width_mm") or 0.0
            if depth <= 0.0 or width <= 0.0:
                continue

            reach = self._longest_flute_that_fits(context, width)
            if reach is None:
                continue

            ratio = depth / reach
            graded = self.graded(ratio, target, limit, "max")
            if graded is None:
                continue

            severity, threshold = graded
            short_by = depth - reach
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{depth:.0f} mm deep, {reach:.0f} mm of flute",
                    self.render(
                        feedback,
                        severity,
                        ratio,
                        target if target is not None else limit,
                        limit,
                        "",
                        f"This pocket is {depth:.0f} mm deep and {width:.1f} mm "
                        "across at its narrowest. The longest end mill in the "
                        f"library that will fit that width carries {reach:.0f} mm "
                        f"of flute, so the tool runs out of cutting edge "
                        f"{short_by:.0f} mm above the floor and the shank starts "
                        "rubbing the walls. As drawn the floor cannot be reached. "
                        "Opening the pocket up so a bigger tool fits is the cheap "
                        "fix; failing that it needs an extended-reach or "
                        "reduced-neck cutter, which will want lighter cuts.",
                    ),
                    faces=pocket.faces,
                    value=ratio,
                    limit=threshold,
                    comparison=">",
                )
            )
        return results

    @staticmethod
    def _longest_flute_that_fits(context, width: float):
        """Flute length of the biggest end mill the pocket will admit.

        Unit system is deliberately ignored here. This is a physical question
        -- will anything on the shelf reach the floor -- not a question about
        which sizes the shop prefers to buy.
        """
        lengths = [
            tool.max_flute_length_mm
            for tool in context.config.tools_of_type("end_mill")
            if tool.min_diameter_mm <= width and tool.max_flute_length_mm > 0.0
        ]
        return max(lengths) if lengths else None


@register_check(Rulebook.SLOT_NONSTANDARD_WIDTH)
class SlotNonstandardWidthCheck(MachiningCheck):
    """A slot is cut in one pass by a cutter its own width.

    When no cutter matches, the slot is cut with an undersize one in two
    passes with a step across between them -- which puts a step in the wall
    where the passes meet, and doubles the cutting time. Neither is a defect,
    but both are cost, and both disappear if the width moves onto a size the
    shop stocks.

    Only narrow slots are judged. Past about a third of an inch the premise
    fails: a wide slot is roughed and finished with a smaller cutter as a
    matter of course, and matching its width to a tool would be an odd thing
    to ask for.
    """

    @property
    def name(self) -> str:
        return "Slot Nonstandard Width Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        tolerance = thresholds.standard_size_match_tol_mm
        severity = self.severity_from_rule_config(rule_config)

        # Size matching honours the configured unit system: a 15.875 mm (5/8")
        # end mill is not a standard size to a shop that buys metric, and the
        # other way about.
        tools = context.config.tools_of_type("end_mill", unit_filtered=True)
        if not tools:
            return []

        results: list[CheckResult] = []
        for slot in context.recognition.of_type(
            FeatureType.SLOT, FeatureType.CHANNEL, FeatureType.THROUGH_CAVITY
        ):
            width = slot.number("width_mm") or 0.0
            if width <= 0.0:
                continue
            if width > thresholds.slot_nonstandard_width_max_mm:
                continue
            if any(tool.fits_diameter(width, tolerance) for tool in tools):
                continue

            # Both ends of every tool's size range are candidates, since a
            # library entry may cover a range rather than one ground size.
            diameters = sorted(
                {tool.min_diameter_mm for tool in tools}
                | {tool.max_diameter_mm for tool in tools}
            )
            nearest = min(diameters, key=lambda d: (abs(width - d), d))

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{width:.2f} mm wide, nearest cutter {nearest:.2f} mm",
                    self.render(
                        feedback,
                        severity,
                        width,
                        nearest,
                        nearest,
                        "mm",
                        f"This slot is {width:.2f} mm wide and no end mill in the "
                        f"library is that size -- the nearest is {nearest:.2f} mm. "
                        "As drawn it has to be cut with an undersize cutter in two "
                        "passes, stepping across between them, which leaves a witness "
                        "line down the wall where the passes meet and takes twice as "
                        "long. Moving the width onto a stocked size lets it go in one "
                        "pass. If the width is a fit for something, leave it and "
                        "expect the two-pass price.",
                    ),
                    faces=slot.faces,
                    value=width,
                    limit=nearest,
                    comparison="=",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.FLEXURE_SLIT_PROCESS)
class FlexureSlitProcessCheck(MachiningCheck):
    """A narrow deep slit is sawn or wired, not milled.

    Clamp slits and flexure cuts are drawn as slots but nothing about them
    suits an end mill: at that width the only cutter that fits has no rigidity
    at all, and the depth is many times what it could reach. A slitting saw
    takes the whole depth in one pass, and wire EDM does it with no cutting
    force at all.

    Advisory, because the feature is right -- the note is there so it gets
    planned and priced as the operation it actually is.
    """

    @property
    def name(self) -> str:
        return "Flexure Slit Process Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        results: list[CheckResult] = []
        for slit in context.recognition.of_type(FeatureType.FLEXURE_SLIT):
            width = slit.number("width_mm") or 0.0
            depth = slit.number("depth_mm") or 0.0

            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    f"{width:.2f} mm slit, {depth:.1f} mm deep",
                    self.render(
                        feedback,
                        Severity.INFO,
                        width,
                        0.0,
                        0.0,
                        "mm",
                        f"This is a clamp or flexure slit: {width:.2f} mm wide and "
                        f"{depth:.1f} mm deep. That is slitting-saw or wire-EDM "
                        "work, not end-milling -- no cutter that narrow will reach "
                        "anywhere near that depth. Plan it as its own operation, "
                        "and deburr the slit walls properly: the faces either side "
                        "of a flexure move in service, and a burr left in the root "
                        "is where it will start a crack.",
                    ),
                    faces=slit.faces,
                    value=width,
                    comparison="=",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.BROACHED_SLOT_PROCESS)
class BroachedSlotProcessCheck(MachiningCheck):
    """A slot whose walls overhang its opening cannot be milled at all.

    A dovetail or fir-tree profile is wider inside than at the mouth, so a
    rotating cutter coming in from the opening side cannot reach the flanks --
    whatever fits through the gap is already past the widest part of the
    slot. It has to be broached, wire-cut, or milled with a dedicated dovetail
    cutter fed along the slot, and each of those is a different machine and a
    different cost from the milling it looks like.
    """

    @property
    def name(self) -> str:
        return "Broached Slot Process Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        severity = self.severity_from_rule_config(rule_config)

        results: list[CheckResult] = []
        for slot in context.recognition.of_type(FeatureType.BROACHED_SLOT):
            width = slot.number("width_mm") or 0.0
            depth = slot.number("depth_mm") or 0.0

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"re-entrant slot, {width:.2f} mm at the mouth",
                    self.render(
                        feedback,
                        severity,
                        width,
                        0.0,
                        0.0,
                        "mm",
                        f"This slot is {width:.2f} mm at the mouth and "
                        f"{depth:.1f} mm deep, and its walls lean out over the "
                        "opening. Anything that fits through the gap is already "
                        "past the widest part of the slot, so no end mill coming in "
                        "from the top can cut the flanks. As drawn it needs "
                        "broaching, wire EDM, or a dovetail cutter fed along the "
                        "length -- and the last of those only works if the slot "
                        "runs out at both ends. If the undercut is not functional, "
                        "straight walls would make this an ordinary milling job.",
                    ),
                    faces=slot.faces,
                    value=width,
                    comparison="=",
                    unit="mm",
                )
            )
        return results
