# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Shared plumbing for machining checks.

Every machining check reads the same shared analysis, so the base class
handles unwrapping it, applying the process gate, and assembling findings.
A subclass supplies only :meth:`evaluate`.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence

from ...machining.context import MachiningContext
from ...machining.process_classifier import PartProcessType
from ...models import CheckResult, Severity
from ...processes.process import RuleFeedback, RuleLimit
from ...rules import Rulebook
from ...base.base_check import BaseCheck
from ....core.analyzers.machining_analyzer import CONTEXT_KEY


class MachiningCheck(BaseCheck):
    """Base for a rule that reads the shared machining analysis.

    `applicable_processes` gates the rule by manufacturing family. An empty
    set means it applies to every process.
    """

    applicable_processes: frozenset = frozenset()

    @property
    def required_analyzer_id(self) -> str:
        return "MACHINING_ANALYZER"

    # -- the BaseCheck contract ---------------------------------------------

    def run_check(
        self,
        analysis_data_map,
        rule_config: RuleLimit,
        rule: Rulebook,
        feedback: Optional[RuleFeedback] = None,
        **kwargs,
    ) -> list[CheckResult]:
        context = self.context_of(analysis_data_map)
        if context is None or not self.applies_to(context):
            return []
        return self.evaluate(context, rule_config, rule, feedback or RuleFeedback())

    def evaluate(
        self,
        context: MachiningContext,
        rule_config: RuleLimit,
        rule: Rulebook,
        feedback: RuleFeedback,
    ) -> list[CheckResult]:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def context_of(analysis_data_map) -> Optional[MachiningContext]:
        if not analysis_data_map:
            return None
        context = analysis_data_map.get(CONTEXT_KEY)
        if context is None:
            # Be forgiving about the key: the analyzer owns it, but a check
            # should not break if that ever changes.
            context = next(iter(analysis_data_map.values()), None)
        return context if isinstance(context, MachiningContext) else None

    def applies_to(self, context: MachiningContext) -> bool:
        """Whether this rule has anything to say about this part.

        Sheet-metal parts are not machined from solid, so every machining rule
        stands down; beyond that a rule may restrict itself to a family.
        """
        if context.process_type is PartProcessType.SHEET_METAL:
            return False
        if not self.applicable_processes:
            return True
        return context.process_type in self.applicable_processes

    def finding(
        self,
        rule: Rulebook,
        severity: Severity,
        overview: str,
        message: str,
        faces: Sequence[int] = (),
        edges: Sequence[int] = (),
        value: float = 0.0,
        limit: float = 0.0,
        comparison: str = "",
        unit: str = "",
    ) -> CheckResult:
        """Assemble a finding, referencing geometry by adjacency-graph id.

        Face and edge ids are the same one-based indices the rest of the
        workbench uses, so they flow straight through to the viewport
        highlighting without translation.
        """
        geometry: list[tuple[str, int]] = [("Face", int(i)) for i in faces]
        geometry += [("Edge", int(i)) for i in edges]
        return CheckResult(
            rule_id=rule,
            severity=severity,
            overview=overview,
            message=message,
            ignore=False,
            value=round(float(value), 4),
            limit=round(float(limit), 4),
            comparison=comparison,
            unit=unit,
            failing_geometry=geometry,
        )

    def graded(
        self,
        measured: float,
        target: Optional[float],
        limit: Optional[float],
        comparison: str,
    ) -> Optional[tuple[Severity, float]]:
        """Grade a measurement against a target/limit pair.

        Returns the severity and the threshold that produced it, or None when
        the measurement is acceptable. `comparison` is "min" when smaller is
        worse and "max" when larger is worse.
        """
        tolerance = 1e-4
        if comparison == "max":
            if limit is not None and measured > limit + tolerance:
                return (Severity.ERROR, limit)
            if target is not None and measured > target + tolerance:
                return (Severity.WARNING, target)
            return None

        if limit is not None and measured < limit - tolerance:
            return (Severity.ERROR, limit)
        if target is not None and measured < target - tolerance:
            return (Severity.WARNING, target)
        return None

    def render(
        self,
        feedback: RuleFeedback,
        severity: Severity,
        measured: float,
        target: float,
        limit: float,
        unit: str,
        fallback: str,
    ) -> str:
        """Fill a feedback template, falling back to the rule's own wording.

        Processes can override every message, but a rule must still say
        something useful when a process has not been given custom text.
        """
        template = feedback.error_msg if severity is Severity.ERROR else feedback.warning_msg
        if not template or not template.strip():
            return fallback
        return self.format_feedback(template, measured, target, limit, unit)

    @staticmethod
    def ratio(numerator: float, denominator: float) -> float:
        return numerator / denominator if abs(denominator) > 1e-9 else 0.0

    @staticmethod
    def as_float(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(number) else number
