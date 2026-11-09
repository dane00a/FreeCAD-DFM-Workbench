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
    FeatureType.BREAKTHROUGH_HOLE,
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
        # Asked whatever the machine: the recognizer only produces these
        # faces when no cardinal approach reaches them, and the reference
        # reports every one it is handed.
        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []
        sealed = _sealed_cavity_faces(context)

        for undercut in context.recognition.of_type(FeatureType.UNDERCUT):
            # A cavity every face of which is unreachable, and which nothing
            # reachable adjoins, is a sealed void -- and `sealed_void` says
            # so once, as an error, about the cavity. Saying it again per
            # wall puts six warnings on top of that one error and none of
            # them adds anything: there is no wall here to reach separately,
            # there is a void with no way in.
            if any(face_id in sealed for face_id in undercut.faces):
                continue

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
            # Which way it faces belongs in the sentence. Identical findings
            # are merged into one row, and the reference merges only those
            # whose recorded details -- face normal, blocked approach, axis --
            # agree to four decimals: two shoulders facing opposite ways are
            # two undercuts, not one said twice.
            orientation = _orientation(undercut)
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
                        f"{count} {plural} here{orientation} sit behind other "
                        "geometry, so a cutter coming straight down cannot "
                        "reach them with the part fixtured as it is. "
                        f"{remedy}",
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
        # Only a three-axis machine is limited to the cardinal approaches the
        # undercut faces were tested against; anything that can index reaches
        # them.
        if context.config.machine_mode != "3axis":
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


def _vector_text(raw) -> str:
    """A stored direction to four decimals, the precision findings merge at."""
    parts = []
    for value in raw:
        rounded = round(float(value), 4) + 0.0  # no "-0"
        parts.append(f"{rounded:g}")
    return "(" + ", ".join(parts) + ")"


def _orientation(undercut) -> str:
    """The direction details of an undercut, as words in its message."""
    words = []
    normal = undercut.param("face_normal")
    if isinstance(normal, (list, tuple)) and len(normal) >= 3:
        words.append(f"facing {_vector_text(normal[:3])}")
    blocked = undercut.param("blocked_tad")
    if isinstance(blocked, (list, tuple)) and len(blocked) >= 3:
        words.append(f"approached along {_vector_text(blocked[:3])}")
    axis = undercut.param("axis")
    if isinstance(axis, (list, tuple)) and len(axis) >= 3:
        words.append(f"on axis {_vector_text(axis[:3])}")
    return (" (" + ", ".join(words) + ")") if words else ""


def _sealed_cavity_faces(context) -> set[int]:
    """Faces belonging to a cavity that is sealed shut.

    Sealed means two things together, and the second is what keeps this
    narrow. Every face of the cavity has to be unreachable -- but that alone
    also describes a small open recess whose walls each happen to be blocked
    from every cardinal direction, a mirror seat cut at forty-five degrees
    being the case in point, and those are genuinely worth a warning apiece.
    So the cavity also has to have nothing reachable next to it: every face
    it touches is either part of it or unreachable too. That is a void with
    no way in, which is one thing to report rather than six.
    """
    unreachable: set[int] = set()
    for feature in context.recognition.of_type(FeatureType.UNDERCUT):
        unreachable.update(feature.faces)
    if not unreachable:
        return set()

    sealed: set[int] = set()
    for cavity in context.recognition.features:
        if cavity.type in (FeatureType.UNDERCUT, FeatureType.PATTERN):
            continue
        if not cavity.faces:
            continue
        if any(face_id not in unreachable for face_id in cavity.faces):
            continue
        members = set(cavity.faces)
        for face_id in cavity.faces:
            for neighbour in context.graph.neighbors_of(face_id):
                if neighbour in members or neighbour in unreachable:
                    continue
                break
            else:
                continue
            break
        else:
            sealed.update(cavity.faces)
    return sealed
