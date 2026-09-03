# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Workholding rules: can this part be located, gripped and reached?

These are the cheapest useful machining rules -- they read the adjacency
graph and the bounding box, and need no feature recognition at all -- but
they catch the problems that cost the most on the shop floor. A part nobody
can hold is not a part.

All of them stand down on turned parts, which are held by their outside
diameter in a chuck rather than located on a flat and clamped in a vise.
"""

from __future__ import annotations

from typing import Optional

from ...machining.context import MachiningContext
from ...machining.process_classifier import PartProcessType
from ...models import CheckResult, Severity
from ...processes.process import RuleFeedback, RuleLimit
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# A turned part is gripped by its OD, so the vise-workholding rules have
# nothing to say about it.
_MILLING_ONLY = frozenset({PartProcessType.MILLED, PartProcessType.UNKNOWN})


def _is_small_part(context: MachiningContext) -> bool:
    """True when the part is below vise scale in every direction.

    Below this it is not clamped in a vise at all -- it goes into soft jaws,
    a fixture plate, or wax -- so the rules that assume a vise would be
    answering a question nobody asked.
    """
    limit = context.config.thresholds.small_part_max_dim_mm
    return all(dimension < limit for dimension in context.bbox_dims())


def _has_parallel_datum_pair(context: MachiningContext) -> bool:
    """Whether two opposed outward faces could be gripped by vise jaws."""
    thresholds = context.config.thresholds
    candidates = context.external_planar_faces(min_area=thresholds.datum_face_min_area_mm2)

    for index, first in enumerate(candidates):
        normal_a = first.outward_normal
        if normal_a is None:
            continue
        for second in candidates[index + 1 :]:
            normal_b = second.outward_normal
            if normal_b is None or normal_a.Dot(normal_b) >= -0.95:
                continue  # not facing away from each other
            separation = abs(
                (second.centroid.X() - first.centroid.X()) * normal_a.X()
                + (second.centroid.Y() - first.centroid.Y()) * normal_a.Y()
                + (second.centroid.Z() - first.centroid.Z()) * normal_a.Z()
            )
            if separation >= thresholds.min_jaw_separation_mm:
                return True
    return False


# =============================================================================


@register_check(Rulebook.NO_DATUM_FACE)
class NoDatumFaceCheck(MachiningCheck):
    """The part needs one flat face big enough to sit on."""

    applicable_processes = _MILLING_ONLY

    @property
    def name(self) -> str:
        return "Datum Face Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        minimum_area = context.config.thresholds.datum_face_min_area_mm2
        faces = context.external_planar_faces(min_area=minimum_area)
        if faces:
            return []

        largest = context.external_planar_faces()
        best_area = largest[0].area if largest else 0.0
        severity = self.severity_from_rule_config(rule_config)
        message = self.render(
            feedback,
            severity,
            best_area,
            minimum_area,
            minimum_area,
            "mm2",
            "No flat face is large enough to locate the part against. The "
            f"largest external flat measures {best_area:.0f} mm2 against a "
            f"{minimum_area:.0f} mm2 minimum, so the first operation will need "
            "a soft-jaw or sacrificial fixture cut to shape.",
        )
        return [
            self.finding(
                rule,
                severity,
                f"largest flat {best_area:.0f} mm2",
                message,
                faces=[largest[0].face_id] if largest else [],
                value=best_area,
                limit=minimum_area,
                comparison="<",
                unit="mm2",
            )
        ]


@register_check(Rulebook.NO_PARALLEL_DATUM_PAIR)
class NoParallelDatumPairCheck(MachiningCheck):
    """A vise needs two opposed flats to grip."""

    applicable_processes = _MILLING_ONLY

    @property
    def name(self) -> str:
        return "Parallel Clamping Faces Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        # A part too small for a vise is held another way; the small-part rule
        # carries that concern instead of this one firing spuriously.
        if _is_small_part(context) or _has_parallel_datum_pair(context):
            return []

        thresholds = context.config.thresholds
        severity = self.severity_from_rule_config(rule_config)
        message = self.render(
            feedback,
            severity,
            0.0,
            thresholds.min_jaw_separation_mm,
            thresholds.min_jaw_separation_mm,
            "mm",
            "No pair of opposed flat faces is available for vise jaws to grip. "
            "Holding this will need a custom fixture, soft jaws machined to "
            "the profile, or a sacrificial tab added for the first operation.",
        )
        return [
            self.finding(
                rule,
                severity,
                "no opposed clamping faces",
                message,
                comparison="=",
            )
        ]


@register_check(Rulebook.THIN_CLAMPING_DIMENSION)
class ThinClampingDimensionCheck(MachiningCheck):
    """There has to be enough stock for the jaws to bite on."""

    applicable_processes = _MILLING_ONLY

    @property
    def name(self) -> str:
        return "Clamping Thickness Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        if _is_small_part(context):
            return []

        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.min_clamping_thickness_mm

        smallest, _, _ = context.sorted_bbox_dims()
        if smallest <= 0.0 or smallest >= limit:
            return []

        severity = Severity.WARNING
        message = self.render(
            feedback,
            severity,
            smallest,
            limit,
            limit,
            "mm",
            f"The part is only {smallest:.2f} mm thick in its smallest "
            f"direction, below the {limit:.2f} mm a vise needs to grip "
            "securely. Expect it to lift or chatter under cutter load; "
            "consider machining it from thicker stock and parting it off.",
        )
        return [
            self.finding(
                rule,
                severity,
                f"{smallest:.2f} mm < {limit:.2f} mm",
                message,
                value=smallest,
                limit=limit,
                comparison="<",
                unit="mm",
            )
        ]


@register_check(Rulebook.SMALL_PART_HOLDING)
class SmallPartHoldingCheck(MachiningCheck):
    """Below vise scale, workholding becomes the interesting problem."""

    applicable_processes = _MILLING_ONLY

    @property
    def name(self) -> str:
        return "Small Part Holding Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        if not _is_small_part(context):
            return []

        # Only worth saying when a vise would actually have struggled --
        # otherwise every small part collects a note that adds nothing.
        thresholds = context.config.thresholds
        smallest, _, _ = context.sorted_bbox_dims()
        if _has_parallel_datum_pair(context) and smallest >= thresholds.min_clamping_thickness_mm:
            return []

        dx, dy, dz = context.bbox_dims()
        severity = self.severity_from_rule_config(rule_config)
        if severity is Severity.ERROR:
            severity = Severity.INFO  # a holding note, not a defect

        message = self.render(
            feedback,
            severity,
            max(dx, dy, dz),
            thresholds.small_part_max_dim_mm,
            thresholds.small_part_max_dim_mm,
            "mm",
            f"At {dx:.1f} x {dy:.1f} x {dz:.1f} mm this is below vise scale, so "
            "the workholding is part of the job: soft jaws, a tab left on for "
            "the first operations, or a fixture plate. Worth agreeing with the "
            "shop before quoting.",
        )
        return [
            self.finding(
                rule,
                severity,
                f"{dx:.0f} x {dy:.0f} x {dz:.0f} mm",
                message,
                value=max(dx, dy, dz),
                limit=thresholds.small_part_max_dim_mm,
                comparison="<",
                unit="mm",
            )
        ]

