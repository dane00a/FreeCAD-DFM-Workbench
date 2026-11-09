# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Run the rules over the corpus, and check they still say what they said.

``generate_fixtures.py`` answers "are these still the same parts". This
answers the question that only matters once they are: do the same parts
still get the same answers. A recognizer rewritten to find pockets a better
way, a threshold moved by half a millimetre, a rule taught a new exemption
-- each of those is meant to change some findings and no others, and the
difference between the two is invisible until someone counts.

So `dfm_baseline.json` records, per fixture, how many findings the analysis
produced, at what severities, from which rules and families, over which
recognized features, and how the part was classified. Counts only: no face
ids, no dimensions, nothing that could put a commercial part back in this
repository. Re-run the corpus and anything that moved is printed as a line
saying what it was and what it is now.

    python tests/fixtures/check_dfm_baselines.py --verify
    python tests/fixtures/check_dfm_baselines.py --verify deep_hole sm_bracket
    python tests/fixtures/check_dfm_baselines.py --record-baseline deep_hole
    python tests/fixtures/check_dfm_baselines.py --record-baseline
    python tests/fixtures/check_dfm_baselines.py --list
    python tests/fixtures/check_dfm_baselines.py --show deep_hole

Approving is a merge: recording one fixture leaves the other two hundred
and twenty-four alone, and the notes and ``must_not_fire`` entries someone
wrote by hand survive being re-recorded. A fixture with no baseline is
reported as unapproved rather than as a failure -- a new recipe has not gone
wrong, it has just never been read by anyone -- but ``--verify`` still exits
non-zero on drift, so this is usable as the regression gate it is meant to
be.

Twelve of the fixtures are imported STEP files rather than recipes, and
those files are gitignored, so a fresh clone does not have them. Their
baselines are here regardless. Each one is reported as a skip on its own
line and counted apart from the rest, because the only thing worse than
not checking twelve parts is not checking them and printing a clean run.

Needs FreeCAD's Python, or any interpreter with cadquery-ocp installed and
`tests/stubs` on the path. It never opens FreeCAD.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
# The stub has to win a name clash against a real FreeCAD if one happens to
# be importable, for the same reason `tests/conftest.py` puts it first: the
# core is exercised here against the stub's narrower Console and Vector.
for _path in (_REPO, os.path.join(_REPO, "tests", "stubs"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from freecad.DFM.core.baselines.corpus import (  # noqa: E402
    load_registry,
    missing_step_fixtures,
)
from freecad.DFM.core.baselines.harness import (  # noqa: E402
    DEFAULT_MATERIAL,
    default_process_for,
    observe_shape,
)
from freecad.DFM.core.baselines.spec import check_drift, spec_from_observation  # noqa: E402
from freecad.DFM.core.baselines.store import BaselineStore  # noqa: E402


def summarise(spec) -> str:
    """One line describing what a baseline says, for the listing."""
    errors = spec.severity_error or 0
    warnings = spec.severity_warn or 0
    return (
        f"{spec.part_process_type or 'UNKNOWN':<11} "
        f"{spec.feature_count or 0:>4} features  "
        f"{errors:>3} error  {warnings:>3} warn  "
        f"{len(spec.by_rule):>3} rules"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "names", nargs="*", help="fixtures to run (default: all registered)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="run each fixture and report anything that moved from its baseline",
    )
    parser.add_argument(
        "--record-baseline",
        action="store_true",
        help="approve what the rules say today as the new expected answer",
    )
    parser.add_argument(
        "--list", action="store_true", help="list the approved baselines and stop"
    )
    parser.add_argument(
        "--show", metavar="FIXTURE", help="print one baseline as stored and stop"
    )
    parser.add_argument(
        "--process",
        help="analyse under this process instead of the one the baseline "
        "recorded (sheet fixtures default to Sheet Metal Fabrication, the "
        "rest to CNC Milling)",
    )
    parser.add_argument(
        "--material",
        default=DEFAULT_MATERIAL,
        help="material row to judge against (default: %(default)s)",
    )
    parser.add_argument(
        "--baseline", help="read and write this file instead of dfm_baseline.json"
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="free-text note to store with the fixtures being recorded",
    )
    args = parser.parse_args(argv)

    store = BaselineStore(args.baseline)

    if args.show:
        spec = store.get(args.show)
        if spec is None:
            print(f"{args.show}: no baseline recorded")
            return 1
        print(json.dumps(spec.to_json(), indent=2, sort_keys=True))
        return 0

    if args.list:
        for name in store.names():
            print(f"{name:<40} {summarise(store.get(name))}")
        print(f"\n{len(store)} approved")
        return 0

    if not (args.verify or args.record_baseline):
        parser.error("nothing to do: pass --verify, --record-baseline or --list")

    registry = load_registry()
    if not registry:
        print("no fixture recipes found; is this a working tree?")
        return 1

    # An imported part whose STEP file is not installed still has a
    # baseline, and a whole-corpus run has to walk it in order to say so.
    # Leaving it out of the default list would make twelve unchecked parts
    # look exactly like twelve checked ones.
    absent_steps = set(missing_step_fixtures())
    names = args.names or sorted(set(registry) | (absent_steps & set(store.names())))
    approved = drifted = unapproved = failed = recorded = skipped = 0

    for name in names:
        builder = registry.get(name)
        if builder is None:
            if name in absent_steps:
                skipped += 1
                print(
                    f"  s  {name}: STEP file not installed "
                    f"(tests/fixtures/step_pmi/{name}.step); baseline not checked"
                )
                continue
            print(f"  ?  {name}: no builder registered")
            failed += 1
            continue

        previous = store.get(name)
        # The recorded process is what the re-run uses, so that a fixture
        # someone deliberately moved onto the brake stays there. An explicit
        # --process overrides it, which is how it got moved in the first
        # place.
        process = args.process or (previous.process if previous else None)
        process = process or default_process_for(name)

        try:
            shape = builder()
            observation, _ = observe_shape(name, shape, process, args.material)
        except Exception as exc:  # a fixture that will not analyse must name itself
            print(f"  !  {name}: {type(exc).__name__}: {exc}")
            failed += 1
            continue

        if args.verify:
            if previous is None:
                unapproved += 1
                print(f"  -  {name}: no baseline yet")
            else:
                result = check_drift(previous, observation)
                if result.ok:
                    approved += 1
                else:
                    drifted += 1
                    print(f"  X  {name}: {len(result.mismatches)} changed")
                    for line in result.mismatches:
                        print(f"        {line}")

        if args.record_baseline:
            # Notes and negative assertions are hand-authored and cannot be
            # derived from a run, so they come forward from whatever is
            # being replaced unless --notes says otherwise.
            notes = args.notes if args.notes is not None else (previous.notes if previous else "")
            store.approve(
                spec_from_observation(
                    observation,
                    notes=notes,
                    must_not_fire=previous.must_not_fire if previous else None,
                )
            )
            recorded += 1

    if args.record_baseline:
        store.save()
        print(f"\nrecorded {recorded} of {len(store)} to {os.path.basename(store.path)}")

    print(f"\nran {len(names) - failed - skipped} of {len(names)}")
    if args.verify:
        print(f"matched: {approved}   drifted: {drifted}", end="")
        print(f"   unapproved: {unapproved}" if unapproved else "")
        if skipped:
            # Said once more at the bottom, where someone who scrolled
            # past the per-fixture lines will still meet it. A skip is
            # not a pass and the summary must not read like one.
            print(
                f"skipped: {skipped} imported fixture(s) whose STEP file is "
                "not installed; their baselines were not checked"
            )
        if failed:
            print(f"failed to run: {failed}")
    return 1 if (failed or drifted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
