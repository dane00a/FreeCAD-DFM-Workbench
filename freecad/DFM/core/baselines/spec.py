# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""What a fixture's analysis is supposed to say, and what changed.

A rule is easy to get right on the part it was written for and easy to get
wrong on the other two hundred. The geometry oracle already answers "are
these the same parts"; this answers the question that follows it, "do they
still get the same answers". An approved baseline is a count of what the
analysis said about one fixture -- how many findings, at what severities,
from which rules, over which recognized features -- and a re-run that
disagrees with it has to be explained before it is committed.

Counts, and nothing but counts. No face ids, no dimensions, no positions.
Partly because those churn on edits that change nothing anyone cares about
-- renumber one face and every stored id moves -- and partly because the
corpus is commercial work that is deliberately not distributed, and a
baseline full of measured diameters would put the parts back in the
repository through the side door. What is stored here could not rebuild a
shape if you tried.

The schema is the reference engine's, key for key, so a baseline written by
either implementation loads in the other. Two fields are additions and are
marked as such below: this workbench chooses a process per fixture where the
reference engine has only one, so the choice has to travel with the
recording or the re-run is not the same experiment.

Absent means "not asserted". That is what makes the format survivable: a
block added later -- ``features`` was, and ``pmi`` was -- leaves every
baseline recorded before it silently unchecked on that axis rather than
uniformly drifted, and re-approving a fixture is what opts it in. A present
but empty block is the opposite statement: tracked, and found to contain
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..machining._version import ENGINE_VERSION


# The severity buckets, in the reference engine's spelling. Anything the
# analysis reports that is not an error or a warning lands in `info`, which
# is the same fallback the reference takes for an unrecognized severity.
SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"


#: Parameters that say what a feature *is*, as opposed to how big it is or
#: where it sits. Only these are counted, and the list is deliberately short.
#:
#: ``subtype`` is the emboss / louver / lance verdict on a formed sheet
#: feature. ``open_edges`` is the topological count that verdict is derived
#: from, so a change to it names the cause rather than the symptom.
#: ``is_hem`` separates a hem from a plain bend, which are different forming
#: operations that fire the same rules. ``bend_family`` is which of the four
#: fold geometries -- cylindrical, conical, rolled, sharp -- a bend was read
#: as. The four deliberately carry different parameters (a conical fold has
#: no single inner radius, a sharp one owns no faces), so a fold changing
#: family changes which sheet rules can measure it at all, and the finding
#: that goes missing leaves no trace in ``by_rule``.
#:
#: Everything else was considered and left out. Face-id parameters
#: (``host_face``, ``floor_face``, ``panel_a``) renumber on edits that mean
#: nothing. ``hole_type`` and ``groove_type`` duplicate ``type`` exactly.
#: Unbounded integers (``count``, ``glyph_count``, ``rows``) scale with the
#: fixture and would fill the key space with noise. Predicates like
#: ``is_through`` and ``flat_bottom`` each already have a rule whose findings
#: the baseline counts.
#:
#: Adding an entry makes every already-approved baseline drift once, because
#: the bidirectional comparison sees a key it was never told about. Grow it
#: on purpose, and re-record the corpus in the same commit.
FEATURE_DISCRIMINANTS = ("subtype", "open_edges", "is_hem", "bend_family")


#: Which engine behaviour a recording was made under, in the reference's
#: ``engine_version`` field. This engine is a port, so the number is the
#: reference engine version whose behaviour it reproduces, and it moves when
#: the port is resynchronised to a newer reference -- at which point every
#: baseline recorded against the old target reports itself as needing a
#: second look, exactly as the reference's own baselines do when its engine
#: is versioned up. It lives with the engine, in machining/_version.py,
#: where the machining cache keys on it too.

#: What a run with no version stamp reports, in the reference's spelling.
UNSTAMPED = "(unstamped)"


# =============================================================================
# The recorded form
# =============================================================================


@dataclass
class BaselineSpec:
    """One approved fixture: what its analysis said when someone read it.

    Every count field is optional in the JSON and optional here, because
    absent is a meaningful state and not merely a missing zero -- see the
    module docstring. The dataclass defaults are the *unasserted* values, so
    a spec built from an empty dict asserts nothing at all.
    """

    fixture: str = ""

    #: Free text. Never asserted; this is where the reason a fixture looks
    #: the way it does gets written down, which is the only place anyone
    #: will find it two years later.
    notes: str = ""

    #: Which process and material the recording was made under. Additions to
    #: the reference schema, and inputs rather than outputs: the reference
    #: engine runs one configuration, where this workbench has to be told
    #: whether a part is going on a mill or through a press brake. Recorded
    #: so the re-run is the same experiment, never compared -- a baseline
    #: cannot drift from its own inputs.
    process: str = ""
    material: str = ""

    feature_count: Optional[int] = None
    severity_error: Optional[int] = None
    severity_warn: Optional[int] = None
    severity_info: Optional[int] = None

    #: Findings per rule family and per rule. Both are asserted in both
    #: directions: an approved baseline is a complete statement of acceptable
    #: output, so a rule that fires without being listed is drift even though
    #: nobody wrote it down as forbidden.
    by_category: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)

    #: Per-PMI-element counts. ``None`` means a baseline recorded before PMI
    #: was tracked; an empty dict means a part that genuinely carries none.
    pmi: Optional[dict[str, int]] = None

    #: Per-feature-signature counts. ``None`` and empty carry the same
    #: distinction as ``pmi``.
    #:
    #: This block exists because a recognizer can change what a feature *is*
    #: while leaving every finding count, category and severity identical --
    #: a louver read as a lance, a bend read as a hem. That whole class of
    #: change is invisible to every other block here.
    features: Optional[dict[str, int]] = None

    #: Rules that must not fire at all. Largely redundant now that ``by_rule``
    #: is bidirectional, and kept because it states an intention the counts
    #: cannot: "this one in particular is the mistake we are guarding
    #: against". Hand-authored, and preserved across re-recording.
    must_not_fire: list[str] = field(default_factory=list)

    #: How the part was classified: MILLED, TURNED, MILL_TURN, SHEET_METAL,
    #: UNKNOWN. Categorical, so any change fails. The turned surface fraction
    #: that sits beside it in the classifier is deliberately not recorded:
    #: it drifts in the third decimal on geometry edits that change nothing
    #: actionable, and a baseline nobody can keep green gets deleted.
    part_process_type: Optional[str] = None

    #: The engine version the counts were approved under. ``None`` means a
    #: baseline from before versioning, which the comparison reports as
    #: needing re-approval rather than skipping -- an unversioned approval
    #: could date from any point in the engine's history.
    engine_version: Optional[str] = None

    #: The reference's ``metrics`` block, carried and never compared. The
    #: reference took it out of its drift gate on 2026-10-02: nobody can look
    #: at a part and know whether a measured number is right, so approving
    #: one was a rubber stamp. A handful of its baselines still carry the
    #: block, and the rule there is that it is inert, not deleted -- so it
    #: is read, ignored, and written back exactly as it came in.
    metrics: Optional[dict[str, Any]] = None

    # -- serialisation ----------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """The dict written to disk.

        Optional fields are omitted rather than written as null, so that
        "never asserted" and "asserted to be nothing" stay distinguishable
        after a round trip. ``pmi`` and ``features`` are the exception in
        reverse: once tracked they are always written, empty dict included,
        because the key's presence is the whole signal.
        """
        out: dict[str, Any] = {"fixture": self.fixture}
        if self.notes:
            out["notes"] = self.notes
        if self.process:
            out["process"] = self.process
        if self.material:
            out["material"] = self.material
        if self.feature_count is not None:
            out["feature_count"] = self.feature_count
        severity: dict[str, int] = {}
        if self.severity_error is not None:
            severity[SEVERITY_ERROR] = self.severity_error
        if self.severity_warn is not None:
            severity[SEVERITY_WARN] = self.severity_warn
        if self.severity_info is not None:
            severity[SEVERITY_INFO] = self.severity_info
        if severity:
            out["severity"] = severity
        if self.by_category:
            out["by_category"] = dict(sorted(self.by_category.items()))
        if self.by_rule:
            out["by_rule"] = dict(sorted(self.by_rule.items()))
        if self.pmi is not None:
            out["pmi"] = dict(sorted(self.pmi.items()))
        if self.features is not None:
            out["features"] = dict(sorted(self.features.items()))
        if self.metrics is not None:
            out["metrics"] = self.metrics
        if self.must_not_fire:
            out["must_not_fire"] = sorted(self.must_not_fire)
        if self.part_process_type is not None:
            out["part_process_type"] = self.part_process_type
        if self.engine_version is not None:
            out["engine_version"] = self.engine_version
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BaselineSpec":
        """Parse a recorded spec. Anything missing stays unasserted.

        Nothing here validates: a baseline is written by this workbench and
        read back by it, and a file someone has hand-edited into nonsense
        should fail loudly at the comparison rather than be quietly
        normalised into something that passes.
        """
        severity = data.get("severity") or {}
        return cls(
            fixture=str(data.get("fixture", "")),
            notes=str(data.get("notes", "")),
            process=str(data.get("process", "")),
            material=str(data.get("material", "")),
            feature_count=_optional_int(data.get("feature_count")),
            severity_error=_optional_int(severity.get(SEVERITY_ERROR)),
            severity_warn=_optional_int(severity.get(SEVERITY_WARN)),
            severity_info=_optional_int(severity.get(SEVERITY_INFO)),
            by_category=_int_map(data.get("by_category")),
            by_rule=_int_map(data.get("by_rule")),
            pmi=None if data.get("pmi") is None else _int_map(data.get("pmi")),
            features=(
                None if data.get("features") is None else _int_map(data.get("features"))
            ),
            must_not_fire=[str(r) for r in data.get("must_not_fire") or []],
            part_process_type=(
                None
                if data.get("part_process_type") is None
                else str(data["part_process_type"])
            ),
            engine_version=(
                None
                if data.get("engine_version") is None
                else str(data["engine_version"])
            ),
            metrics=(
                dict(data["metrics"]) if isinstance(data.get("metrics"), dict) else None
            ),
        )


def _optional_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(k): int(v) for k, v in value.items()}


# =============================================================================
# The observed form
# =============================================================================


@dataclass
class Observation:
    """One analysis run, reduced to the plain records the tallies read.

    The shape is the reference engine's output files rather than this
    workbench's objects -- a finding is a dict with a severity, a category
    and a rule id; a feature is a dict with a type and a parameter map -- so
    that the tallies below are a line-for-line port and can be unit-tested
    without OpenCascade, a document, or a shape. Turning the workbench's own
    dataclasses into these records is :mod:`..baselines.observe`'s job, and
    the only place the two vocabularies meet.
    """

    fixture: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    features: list[dict[str, Any]] = field(default_factory=list)
    #: Annotations and datums, in the reference's ``gdt.json`` shape. Empty
    #: for every part today -- nothing in FreeCAD supplies PMI yet -- and
    #: tallied anyway so the block is already in the baselines on the day
    #: something does.
    pmi: dict[str, Any] = field(default_factory=dict)
    part_process_type: str = "UNKNOWN"
    process: str = ""
    material: str = ""
    engine_version: str = ENGINE_VERSION


@dataclass
class FindingTally:
    """Findings counted every way the baseline asserts them."""

    total: int = 0
    error: int = 0
    warn: int = 0
    info: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)


def tally_findings(findings: list[dict[str, Any]]) -> FindingTally:
    """Total up findings by severity, rule family and rule."""
    t = FindingTally()
    for finding in findings:
        t.total += 1
        severity = finding.get("severity") or SEVERITY_INFO
        if severity == SEVERITY_ERROR:
            t.error += 1
        elif severity == SEVERITY_WARN:
            t.warn += 1
        else:
            t.info += 1

        category = finding.get("category") or ""
        if category:
            t.by_category[category] = t.by_category.get(category, 0) + 1

        rule = finding.get("rule_id") or ""
        if rule:
            t.by_rule[rule] = t.by_rule.get(rule, 0) + 1
    return t


def tally_pmi(pmi: Optional[dict[str, Any]]) -> dict[str, int]:
    """Per-element PMI counts, keyed by a kind that survives geometry edits.

    Counts only: a datum is recorded as resolved or unresolved, never by the
    faces it resolved to, because those renumber whenever anything upstream
    of them changes. What this catches is a dimension vanishing, a datum
    losing its geometry link, a tolerance disappearing from the extraction.

    The key set is exactly the reference's (``tally_pmi`` in its
    ``expected_checker.cpp``): the goldens are the specification, so a key
    the reference never writes could only ever read as drift.
    """
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    if not isinstance(pmi, dict):
        return counts

    annotations = pmi.get("annotations")
    if isinstance(annotations, list):
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            kind = annotation.get("type") or ""
            if kind.startswith("dimension_"):
                # A dimension with a tolerance on it is a requirement; one
                # without is a drawing convenience, and the two drift apart
                # for entirely different reasons.
                value = annotation.get("tolerance_value_mm") or 0.0
                bump("dimension_valued" if value > 0.0 else "dimension_drawing_only")
            elif kind in ("note", "unknown", ""):
                bump("note")
            else:
                bump("tol_" + kind)

    datums = pmi.get("datums")
    if isinstance(datums, list):
        for datum in datums:
            if not isinstance(datum, dict):
                continue
            if datum.get("is_target"):
                # A datum target is anchored by its own placement point, so
                # having no faces is correct for one rather than a failure
                # to resolve.
                bump("datum_target")
                continue
            faces = datum.get("face_ids") or []
            bump("datum_resolved" if faces else "datum_unresolved")
    return counts


def _discriminant_suffix(value: Any) -> Optional[str]:
    """A discriminant value as a stable key suffix, or nothing.

    Only discrete kinds are representable. A float would give every geometry
    tweak its own key and turn the block into noise; ``True`` is deliberately
    checked before ``int`` because in Python a bool *is* an int and would
    otherwise be recorded as ``is_hem_1``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return None


def tally_features(features: list[dict[str, Any]]) -> dict[str, int]:
    """Per-feature classification counts: what was recognized, not how big.

    One ``type_<TYPE>`` per feature, plus one key per kind-discriminant
    parameter the feature carries. See :data:`FEATURE_DISCRIMINANTS` for what
    counts as one and why the list is as short as it is.
    """
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    for feature in features:
        if not isinstance(feature, dict):
            continue
        kind = feature.get("type") or ""
        if kind:
            bump("type_" + kind)

        parameters = feature.get("parameters")
        if not isinstance(parameters, dict):
            continue
        for key in FEATURE_DISCRIMINANTS:
            if key not in parameters:
                continue
            suffix = _discriminant_suffix(parameters[key])
            if suffix is not None:
                bump(f"{key}_{suffix}")
    return counts


def spec_from_observation(
    observation: Observation,
    notes: str = "",
    must_not_fire: Optional[list[str]] = None,
) -> BaselineSpec:
    """Turn a run into the baseline that approves it.

    Every count block is filled, ``pmi`` and ``features`` included and
    written even when empty, because approving is the only way a fixture
    ever gains them: a legacy file skips those checks until it is
    re-approved through here, and nobody should be counting findings by
    hand to add the block themselves.

    ``notes`` and ``must_not_fire`` are hand-authored and cannot be derived
    from a run, so they are carried in from the spec being replaced rather
    than reset. The reference engine drops them on every save; losing a
    deliberately written negative assertion because someone approved an
    unrelated change is a cost with nothing on the other side of it.
    """
    tally = tally_findings(observation.findings)
    return BaselineSpec(
        fixture=observation.fixture,
        notes=notes,
        process=observation.process,
        material=observation.material,
        feature_count=len(observation.features),
        severity_error=tally.error,
        severity_warn=tally.warn,
        severity_info=tally.info,
        by_category=dict(tally.by_category),
        by_rule=dict(tally.by_rule),
        pmi=tally_pmi(observation.pmi),
        features=tally_features(observation.features),
        must_not_fire=list(must_not_fire or []),
        part_process_type=observation.part_process_type,
        engine_version=observation.engine_version or UNSTAMPED,
    )


# =============================================================================
# The comparison
# =============================================================================


@dataclass
class DriftResult:
    """What a fixture's re-run says about its baseline."""

    fixture: str = ""
    ok: bool = True
    #: One line per disagreement, in sorted key order so two runs of the
    #: same drift print identically and a diff of the reports is readable.
    mismatches: list[str] = field(default_factory=list)
    #: True when the fixture has no baseline at all. Not a failure -- a new
    #: fixture is unapproved, not broken -- but it is not a pass either, and
    #: the caller decides which of those it cares about.
    no_baseline: bool = False

    @property
    def status(self) -> str:
        if self.no_baseline:
            return "unapproved"
        return "ok" if self.ok else "drifted"


def check_drift(expected: BaselineSpec, observation: Observation) -> DriftResult:
    """Compare a run against its baseline and say what moved.

    Scalars are compared only when the baseline asserts them. The four maps
    are compared in both directions: every key the baseline lists must match,
    and every key the run produced must be listed. The second half is what
    makes an approval a complete statement rather than a floor -- without it
    a newly firing rule is invisible until somebody happens to notice a
    finding count they did not expect.
    """
    result = DriftResult(fixture=expected.fixture or observation.fixture)
    tally = tally_findings(observation.findings)

    # Reported as drift so it lands where every other mismatch does, but it
    # means something softer: the counts were approved against a different
    # build and may well still be right. The wording is the reference's.
    actual_engine = observation.engine_version or UNSTAMPED
    if expected.engine_version is None:
        result.ok = False
        result.mismatches.append(
            "engine_version: baseline records none (approved before engine "
            f"versioning; engine is now {actual_engine}) -- re-approve to record it"
        )
    elif expected.engine_version != actual_engine:
        result.ok = False
        result.mismatches.append(
            f"engine_version: baseline approved under {expected.engine_version}, "
            f"engine is {actual_engine} -- re-approve after reviewing"
        )

    def compare(name: str, want: int, got: int) -> None:
        if want == got:
            return
        result.ok = False
        delta = got - want
        sign = "+" if delta >= 0 else ""
        result.mismatches.append(f"{name}: expected {want}, got {got} ({sign}{delta})")

    if expected.feature_count is not None:
        compare("feature_count", expected.feature_count, len(observation.features))
    if expected.severity_error is not None:
        compare("severity.error", expected.severity_error, tally.error)
    if expected.severity_warn is not None:
        compare("severity.warn", expected.severity_warn, tally.warn)
    if expected.severity_info is not None:
        compare("severity.info", expected.severity_info, tally.info)

    _compare_map(
        result,
        compare,
        "by_category",
        expected.by_category,
        tally.by_category,
        "unexpected category fired {count} finding(s) (not listed in expected)",
    )
    _compare_map(
        result,
        compare,
        "by_rule",
        expected.by_rule,
        tally.by_rule,
        "unexpected rule fired {count} finding(s) (not listed in expected baseline)",
    )
    if expected.pmi is not None:
        _compare_map(
            result,
            compare,
            "pmi",
            expected.pmi,
            tally_pmi(observation.pmi),
            "unexpected PMI element appeared {count} time(s) "
            "(not listed in expected baseline)",
        )
    if expected.features is not None:
        _compare_map(
            result,
            compare,
            "features",
            expected.features,
            tally_features(observation.features),
            "unexpected feature signature appeared {count} time(s) "
            "(not listed in expected baseline)",
        )

    for rule in sorted(expected.must_not_fire):
        got = tally.by_rule.get(rule, 0)
        if got > 0:
            result.ok = False
            result.mismatches.append(
                f"must_not_fire.{rule}: expected 0, got {got} FIRED"
            )

    if expected.part_process_type is not None:
        actual = observation.part_process_type or "UNKNOWN"
        if expected.part_process_type != actual:
            result.ok = False
            result.mismatches.append(
                f"part_process.type: expected {expected.part_process_type}, "
                f"got {actual}"
            )

    return result


def _compare_map(
    result: DriftResult,
    compare,
    prefix: str,
    expected: dict[str, int],
    actual: dict[str, int],
    surplus_message: str,
) -> None:
    """One map, compared in both directions.

    Both halves walk their keys in sorted order. The reference engine gets
    that for free from ``std::map``; here it has to be asked for, and it is
    worth asking for -- a mismatch list whose order depends on the order the
    JSON happened to be written in cannot be diffed against a previous run.
    """
    for key in sorted(expected):
        compare(f"{prefix}.{key}", expected[key], actual.get(key, 0))
    for key in sorted(actual):
        if key in expected:
            continue
        result.ok = False
        result.mismatches.append(
            f"{prefix}.{key}: " + surplus_message.format(count=actual[key])
        )
