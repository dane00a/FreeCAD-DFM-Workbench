# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about whether a tool can get to the geometry.

Both of these are about reach rather than dimension, and they are ordered:
an undercut is awkward, a feature with no approach at all is impossible. When
the second applies the first is redundant, so it stands down.
"""

from __future__ import annotations

from ...machining.features import CAVITY_TYPES, FeatureType
from ...machining.process_classifier import PartProcessType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# Types whose access concern is already covered by their own rules.
_SPEAKS_FOR_ITSELF = CAVITY_TYPES | {
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK,
    FeatureType.THREADED_HOLE,
    FeatureType.UNDERCUT,
    FeatureType.PATTERN,
}


@register_check(Rulebook.UNDERCUT_PRESENT)
class UndercutPresentCheck(MachiningCheck):
    """Geometry hiding behind other geometry.

    The advice depends on the shape of the surface, because what gets you in
    there differs: a flat shoulder wants a T-slot or dovetail cutter, a
    curved one wants a grooving tool or EDM.
    """

    @property
    def name(self) -> str:
        return "Undercut Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        # On a five-axis machine the tool can tilt, so most of these stop
        # being undercuts at all.
        if context.config.machine_mode == "5axis":
            return []

        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []

        for undercut in context.recognition.of_type(FeatureType.UNDERCUT):
            surface = str(undercut.param("surface_type", "PLANAR"))
            if surface == "CYLINDER":
                remedy = (
                    "An internal grooving tool, EDM, or flipping the part would "
                    "each reach it."
                )
            elif surface == "TORUS":
                remedy = "EDM or a form tool ground to the profile would reach it."
            else:
                remedy = (
                    "A T-slot or dovetail cutter, EDM, or a second setup with the "
                    "part flipped would each reach it."
                )

            count = int(undercut.param("face_count", len(undercut.faces)) or 1)
            plural = "surfaces" if count != 1 else "surface"
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{count} unreachable {plural}",
                    self.render(
                        feedback,
                        severity,
                        float(count),
                        0.0,
                        0.0,
                        "",
                        f"{count} {plural} here sit behind other geometry, so a "
                        "cutter coming straight down cannot reach them with the "
                        f"part fixtured as it is. {remedy}",
                    ),
                    faces=undercut.faces,
                    value=float(count),
                    comparison="=",
                )
            )
        return results


@register_check(Rulebook.TOOL_ACCESS_BLOCKED)
class ToolAccessBlockedCheck(MachiningCheck):
    """A feature with no approach at all.

    Not merely awkward: every face of it is unreachable, so there is no setup
    that presents it to a cutter. Either the part comes apart into pieces, or
    it is made another way.
    """

    @property
    def name(self) -> str:
        return "Unreachable Feature Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        if context.config.machine_mode == "5axis":
            return []

        unreachable = context.recognition.faces_of_type(FeatureType.UNDERCUT)
        if not unreachable:
            return []

        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []

        for feature in context.recognition.features:
            if feature.type in _SPEAKS_FOR_ITSELF or not feature.faces:
                continue
            if not all(face_id in unreachable for face_id in feature.faces):
                continue

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{feature.type.lower().replace('_', ' ')} unreachable",
                    self.render(
                        feedback,
                        severity,
                        float(len(feature.faces)),
                        0.0,
                        0.0,
                        "",
                        "Every surface of this feature is hidden behind other "
                        "geometry, so there is no direction a cutter can approach "
                        "it from. It cannot be machined from solid as drawn: the "
                        "part would have to be made in pieces and joined, or the "
                        "feature opened up.",
                    ),
                    faces=feature.faces,
                    value=float(len(feature.faces)),
                    comparison="=",
                )
            )
        return results
