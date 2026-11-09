# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Run the rulebook over a shape, with nothing else in the way.

:class:`~...app.analysis_runner.AnalysisRunner` is what the machinist gets:
it takes a ``Part.Shape`` off a document, reads the shop's preferences,
asks the part what blank it is cut from, reports progress and can be
aborted. All of that is right for a person sitting in front of it and wrong
for a baseline, because every one of those inputs is a way for the recorded
answer to depend on whose machine recorded it.

So this is the same walk over the same registries with all of it taken
away. No document, no user preferences, no per-part declarations -- the
shop defaults, the shipped process definitions, and the shape. Two
developers running the corpus get the same numbers, and a baseline that
disagrees disagrees because a rule changed.

The one thing it does not take away is the process YAML the workbench
ships, which is read straight off disk rather than through
:class:`~..registries.process_registry.ProcessRegistry`. The registry
layers the user's own process definitions over the shipped ones, which is
exactly the behaviour a machinist wants and exactly the behaviour that
would let a developer's edited copy of CNC Milling quietly rewrite the
corpus.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import yaml

from OCP.gp import gp_Dir
from OCP.TopoDS import TopoDS_Shape

from ..aggregation import aggregate_findings
from ..machining.annotations import annotations_for
from ..models import CheckResult
from ..processes.process import Process, RuleFeedback, RuleLimit
from ..registries.analyzers_registry import get_analyzer_class
from ..registries.checks_registry import get_check_class
from ..utils.geometry import EdgeIndex, FaceIndex
from . import corpus
from .observe import observe
from .spec import Observation


#: Where the shipped process definitions live.
_PROCESS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "processes")
)

#: The process a fixture is analysed under when nobody has said otherwise.
DEFAULT_PROCESS = "CNC Milling"

#: And for the sheet fixtures.
SHEET_PROCESS = "Sheet Metal Fabrication"

#: The reference engine's own answer for each fixture, mapped onto the
#: processes this workbench ships. ``fixture_processes.json`` is derived from
#: the ``part_process_type`` recorded in the reference corpus's approved
#: baselines -- the golden standard rather than a guess -- and covers the 150
#: fixtures it states one for, the twelve imported NIST parts among them.
#: Names only: no counts, no geometry.
#:
#: Like ``import_reference_geometry.py``, the file cannot be regenerated
#: without the reference corpus, which is not distributed. It is committed for
#: that reason.
#:
#: ``MILL_TURN`` maps to milling rather than turning on purpose. A mill-turn
#: part is held and cut both ways, and ``cnc_turning.yaml`` says in as many
#: words that it drops the vise workholding rules because a turned part is
#: gripped in a chuck. Milling is the superset, so it is the safer reading of
#: a part that sees both.
_PROCESS_MAP_FILE = "fixture_processes.json"
_process_map: Optional[dict] = None

#: The material row every fixture is judged against. "Default" is the base
#: layer every process defines and no real alloy overrides, which keeps the
#: recorded limits the shipped ones.
DEFAULT_MATERIAL = "Default"


def default_process_for(fixture: str) -> str:
    """Which process a fixture should be analysed under, absent a baseline.

    The reference's answer first, then the ``sm_`` prefix for the fixtures it
    says nothing about. The prefix on its own is not trustworthy and the
    corpus proves it: ``sheet_metal_bracket`` and ``sheet_metal_channel`` are
    sheet parts that do not carry the prefix, while ``sm_lookalike_billet``
    and ``sm_hybrid_milled_pocket`` are milled parts named to look like sheet
    -- they exist precisely to catch a classifier that reads the name instead
    of the geometry, and a prefix rule walks straight into them.

    This only decides the *first* recording. Once a fixture is approved the
    process is written into its baseline and the re-run uses that, so a
    fixture guessed wrong is fixed once with ``--process`` and stays fixed.
    """
    global _process_map
    if _process_map is None:
        path = os.path.join(corpus.FIXTURES_DIR, _PROCESS_MAP_FILE)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                _process_map = json.load(handle)
        except (OSError, ValueError):
            # An installation without the working tree has no map and no
            # corpus to use it on. The prefix still answers for the panel's
            # process combo, which asks before it knows a fixture is missing.
            _process_map = {}
    known = _process_map.get(fixture)
    if known:
        return known
    return SHEET_PROCESS if fixture.startswith("sm_") else DEFAULT_PROCESS


_process_cache: dict[str, Process] = {}


def load_process(name: str) -> Optional[Process]:
    """The shipped definition of one process, by the name inside the file.

    Cached, because the corpus run asks for the same two processes two
    hundred times and parsing YAML that often is the slowest thing in an
    otherwise geometry-bound job.
    """
    if not _process_cache:
        for entry in sorted(os.listdir(_PROCESS_DIR)):
            if not entry.endswith(".yaml"):
                continue
            with open(os.path.join(_PROCESS_DIR, entry), encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            if isinstance(data, dict):
                process = Process.from_yaml(data)
                _process_cache[process.name] = process
    return _process_cache.get(name)


def process_names() -> list[str]:
    """Every process a fixture can be recorded under."""
    load_process("")
    return sorted(_process_cache)


def resolve_rule_config(process: Process, material: Any, rule_id: Any) -> RuleLimit:
    """The limits one rule runs at: the Default row, then the material's own.

    The same layering the application does. Kept here rather than imported
    from it because the runner that owns it cannot be imported without
    FreeCAD, and a headless corpus run that could not be done headlessly
    would defeat its own purpose.
    """
    config = RuleLimit(target="N/A", limit="N/A", binary_severity=None)

    default_material = process.materials.get("Default")
    if default_material and rule_id in default_material.rule_limits:
        base = default_material.rule_limits[rule_id]
        config.target = base.target
        config.limit = base.limit
        config.binary_severity = base.binary_severity

    if material and rule_id in material.rule_limits:
        override = material.rule_limits[rule_id]
        if override.target not in ("N/A", "", None):
            config.target = override.target
        if override.limit not in ("N/A", "", None):
            config.limit = override.limit
        if override.binary_severity is not None:
            config.binary_severity = override.binary_severity

    if config.binary_severity is None:
        config.binary_severity = "ERROR"

    return config


class RunOutput:
    """One analysis: its findings, and the analysis they were read from.

    The context is handed back alongside the findings because the caller
    usually wants both -- the baseline counts the recognized features, and
    the panel shows them next to the findings they explain.
    """

    def __init__(
        self,
        findings: list[CheckResult],
        context: Any,
        process: str,
        material: str,
    ):
        self.findings = findings
        self.context = context
        self.process = process
        self.material = material


def run_rules(
    shape: TopoDS_Shape,
    process_name: str = DEFAULT_PROCESS,
    material_name: str = DEFAULT_MATERIAL,
    prefs: Optional[dict] = None,
    annotations: Any = None,
) -> RunOutput:
    """Every active rule of one process, over one shape.

    ``annotations`` are the part's tolerance and finish callouts, when the
    shape came with any (a STEP file's PMI); they reach every analyzer, and
    the machining context carries them to the GD&T rules.

    Analyzers are cached by id across the rules that share them, which is
    not an optimisation so much as a requirement: building the adjacency
    graph and recognizing features is most of the cost, and every machining
    rule wants the same one.

    A check that raises is allowed to lose its own findings and nothing
    else. A corpus run exists to show what moved across two hundred parts,
    and one rule throwing on one fixture should show up as that rule's
    counts going to zero, not as the run stopping.
    """
    prefs = dict(prefs or {})
    process = load_process(process_name)
    if process is None:
        raise ValueError(f"no shipped process named {process_name!r}")
    material = process.materials.get(material_name)

    face_index = FaceIndex(shape)
    edge_index = EdgeIndex(shape)
    pull_direction = gp_Dir(0, 0, 1)

    analyzer_cache: dict[str, Any] = {}
    findings: list[CheckResult] = []

    for rule_id in process.active_rules:
        check_class = get_check_class(rule_id)
        if check_class is None:
            continue
        check_instance = check_class()
        analyzer_id = check_instance.required_analyzer_id

        if analyzer_id not in analyzer_cache:
            analyzer_class = get_analyzer_class(analyzer_id)
            if analyzer_class is None:
                continue
            analyzer_cache[analyzer_id] = analyzer_class().execute(
                shape,
                face_index,
                edge_index,
                prefs=prefs,
                annotations=annotations,
                **{"PULL_DIRECTION": pull_direction},
            )

        config = resolve_rule_config(process, material, rule_id)
        feedback = process.rule_feedback.get(rule_id) or RuleFeedback()
        try:
            findings.extend(
                check_instance.run_check(
                    analyzer_cache[analyzer_id],
                    config,
                    rule_id,
                    feedback=feedback,
                    prefs=prefs,
                )
            )
        except Exception as exc:  # a failing rule must name itself, not stop the run
            print(f"  !  {rule_id.name}: {type(exc).__name__}: {exc}")

    # The same collapse the panel does, from the same module, for the
    # reason the panel's copy exists at all: a baseline that counted raw
    # occurrences while the panel showed merged rows would have the two
    # halves of this workbench disagreeing about what the engine found,
    # and the corpus is only worth running because it agrees.
    context = _machining_context(analyzer_cache)
    return RunOutput(aggregate_findings(findings), context, process.name, material_name)


def _machining_context(analyzer_cache: dict[str, Any]) -> Any:
    """The machining analysis, if this process ran one.

    A process whose rules are all draft and thickness never builds one, and
    saying so with ``None`` lets the caller record an empty feature census
    rather than invent one.
    """
    data = analyzer_cache.get("MACHINING_ANALYZER")
    if not data:
        return None
    return next(iter(data.values()), None)


#: Customer intent the reference's batch harness declares for four fixtures,
#: copied from ``fixture_analysis_options`` in its ``src/main.cpp``. These
#: are declarations rather than anything either engine can read off the
#: geometry -- that a part arrives as a casting, that the shop runs it in
#: precision mode, that it is steel -- so running these fixtures on defaults
#: asks the two engines different questions. Every other fixture runs on
#: defaults on both sides. Keys are the prefs ``MachiningConfig`` reads.
FIXTURE_DECLARED_PREFS: dict[str, dict[str, Any]] = {
    # The drafted plinth against undrafted thin ribs: the casting/rib draft
    # contrast case.
    "torture_casting_ribfield": {"MachiningBlankForm": "as_cast"},
    # A revolved hub with no three mutually perpendicular datum faces: the
    # no_orthogonal_datum_trio bait, which only precision mode asks about.
    "torture_freeform_leviathan": {"MachiningPrecisionMode": True},
    # Declared as-cast with no modelled draft anywhere.
    "as_cast_no_draft": {"MachiningBlankForm": "as_cast"},
    # 4.5 mm declared steel: over the steel gauge ceiling, under aluminium's.
    "sm_gauge_steel_ceiling": {"MachiningMaterialFamily": "steel"},
}


def declared_prefs_for(fixture: str) -> dict[str, Any]:
    """The prefs a fixture's declared intent sets, or none for the rest."""
    return dict(FIXTURE_DECLARED_PREFS.get(fixture, {}))


def _fixture_annotations(fixture: str) -> Any:
    """The PMI an imported fixture carries, or None for a recipe."""
    from . import step_fixtures

    if not step_fixtures.is_step_fixture(fixture):
        return None
    return step_fixtures.pmi(fixture)


def observe_shape(
    fixture: str,
    shape: TopoDS_Shape,
    process_name: Optional[str] = None,
    material_name: str = DEFAULT_MATERIAL,
    annotations: Any = None,
) -> tuple[Observation, RunOutput]:
    """Analyse one fixture and reduce the result to baseline records.

    Both halves come back: the observation is what the baseline is compared
    against, and the run is what the panel shows a developer who wants to
    know *why* it moved.

    A fixture imported from a STEP file brings its PMI with it unless the
    caller hands some in; a recipe fixture has none. The PMI block is
    recorded from those callouts whether or not the process built a
    machining context, because the reference tallies it for every part.
    """
    process_name = process_name or default_process_for(fixture)
    if annotations is None:
        annotations = _fixture_annotations(fixture)
    run = run_rules(
        shape, process_name, material_name, declared_prefs_for(fixture), annotations
    )
    if annotations is None and run.context:
        annotations = annotations_for(run.context)
    observation = observe(
        fixture=fixture,
        findings=run.findings,
        context=run.context,
        annotations=annotations,
        process=run.process,
        material=run.material,
    )
    return observation, run
