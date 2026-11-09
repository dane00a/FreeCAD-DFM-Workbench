# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Reduce one analysis to the records a baseline is counted from.

The comparison in :mod:`.spec` works on plain dicts in the reference
engine's vocabulary -- severity strings, a category, a rule id -- because
that is what makes it portable and what makes it testable without a shape.
This module is the one place where that vocabulary meets the workbench's
own: a :class:`~..models.CheckResult` carries a ``Severity`` enum and a
``Rulebook`` member, and something has to spell them.

What comes out is deliberately narrower than what goes in. A
:class:`~..models.CheckResult` also carries a message, a measured value and
the faces it names; none of those are recorded. The message is prose that
gets reworded, the value is a dimension off a part that is not ours to
distribute, and the face ids renumber on edits that change nothing. The
count is the durable part, and it is the part that catches a rule that
started or stopped firing.

One thing the count no longer catches, since :mod:`..aggregation` began
collapsing identical findings before they reach here: a rule that keeps
firing but on a different number of the same feature. Forty-eight identical
broached slots and forty of them are both one finding. What still catches
that is the feature block below, which counts the recognized features
themselves and is not aggregated -- a recognizer that loses eight slots
shows up as eight missing SLOTs even when the findings look untouched.
Worth knowing before reading a clean findings block as proof that nothing
moved.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import CheckResult, Severity
from .spec import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARN, Observation


#: How a finding's severity is spelled in a baseline. Anything unmapped --
#: ``SUCCESS``, which no check emits today -- falls through to ``info``,
#: which is the same fallback the reference engine takes for a severity it
#: does not recognize.
_SEVERITY_NAMES = {
    Severity.ERROR: SEVERITY_ERROR,
    Severity.WARNING: SEVERITY_WARN,
    Severity.INFO: SEVERITY_INFO,
}


def finding_record(result: CheckResult) -> dict[str, Any]:
    """One finding, as the three fields the baseline counts.

    The category is the rule's family -- hole, thread, sheet, tool access --
    which is the workbench's equivalent of the reference engine's category
    string. It is read through the rule rather than stored on the finding,
    so a rule that moves family is caught here without every check having to
    learn about it.
    """
    family = getattr(result.rule_id.value, "family", None)
    category = getattr(result, "category", "") or (
        getattr(family, "value", "") if family is not None else ""
    )
    return {
        "severity": _SEVERITY_NAMES.get(result.severity, SEVERITY_INFO),
        "category": category,
        "rule_id": result.rule_id.name,
    }


def feature_record(feature: Any) -> dict[str, Any]:
    """One recognized feature, as its type and the parameters that classify it.

    The whole parameter map is handed over rather than pre-filtered. Which
    parameters count as classification lives in one place -- the discriminant
    list in :mod:`.spec` -- and filtering here as well would give the same
    decision two homes and eventually two answers.
    """
    return {
        "type": getattr(feature, "type", ""),
        "parameters": dict(getattr(feature, "parameters", {}) or {}),
    }


def pmi_record(annotations: Any) -> dict[str, Any]:
    """An annotation set in the shape :func:`..baselines.spec.tally_pmi` reads.

    The same two arrays the reference engine's ``gdt.json`` carries and its
    tally walks: ``annotations`` (every tolerance, dimension and unclassified
    presentation label) and ``datums``. Free-text notes and surface-finish
    requirements live in their own arrays there and are not counted, so they
    are not handed over here either -- a note the baseline calls ``note`` is
    an *unclassified label*, not a note body. A part with no PMI, which is a
    FreeCAD document part, comes through with both arrays empty.
    """
    if annotations is None:
        return {}

    entries: list[dict[str, Any]] = []
    for frame in getattr(annotations, "frames", None) or []:
        entries.append(
            {
                "type": getattr(frame, "type", ""),
                "tolerance_value_mm": getattr(frame, "tolerance_value_mm", 0.0),
            }
        )

    datums: list[dict[str, Any]] = []
    for datum in getattr(annotations, "datums", None) or []:
        datums.append(
            {
                "is_target": bool(getattr(datum, "is_target", False)),
                "face_ids": list(getattr(datum, "face_ids", None) or []),
            }
        )

    return {"annotations": entries, "datums": datums}


def observe(
    fixture: str,
    findings: list[CheckResult],
    context: Any = None,
    annotations: Any = None,
    process: str = "",
    material: str = "",
) -> Observation:
    """Everything one fixture run contributes to a baseline.

    ``context`` is the machining analysis, and is allowed to be absent: a
    process with no machining rules never builds one, and a fixture analysed
    under such a process still has findings worth baselining. Without it the
    feature and classification blocks record nothing rather than guessing,
    which the comparison reads as "no features, classification UNKNOWN" --
    true, and stable.
    """
    recognition = getattr(context, "recognition", None) if context else None
    features = list(getattr(recognition, "features", None) or [])

    part_process = getattr(context, "part_process", None) if context else None
    process_type = getattr(getattr(part_process, "type", None), "value", "UNKNOWN")

    return Observation(
        fixture=fixture,
        findings=[finding_record(f) for f in findings],
        features=[feature_record(f) for f in features],
        pmi=pmi_record(annotations),
        part_process_type=process_type,
        process=process,
        material=material,
    )
