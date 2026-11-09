# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Run fixtures and hold each one to the reference engine's approved golden.

``check_dfm_baselines.py`` asks whether this engine still says what it said
yesterday; ``check_parity.py`` compares that recording with the reference's.
This does the comparison the reference itself does: analyse a fixture, then
check the result against ``expected/<fixture>.json`` with the reference's
drift semantics -- the same field-by-field, both-directions comparison and
the same mismatch wording, from :func:`..baselines.spec.check_drift`. A
fixture that passes here would pass the reference's own gate.

    python tests/fixtures/check_golden.py                 # the whole corpus
    python tests/fixtures/check_golden.py through_hole sm_bracket
    python tests/fixtures/check_golden.py --match seam37
    python tests/fixtures/check_golden.py --save run.json
    python tests/fixtures/check_golden.py --save after.json --compare before.json
    python tests/fixtures/check_golden.py --jobs 8 --quiet

``--save`` writes each fixture's mismatch list; ``--compare`` reads an earlier
save and reports which fixtures were fixed and which were broken since. That
pair is what makes porting the reference one commit at a time safe: every
change is meant to fix some fixtures and break none.

Nothing is written to the reference tree or to ``dfm_baseline.json``. The
reference directory is found beside this repository, or at
``DFM_REFERENCE_EXPECTED``. Needs ``.venv311`` (Python with OCP).
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _path in (_REPO, os.path.join(_REPO, "tests", "stubs"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

DEFAULT_EXPECTED = os.path.abspath(
    os.path.join(
        _REPO,
        "..",
        "brooklineind-website",
        "apps",
        "geometry-service",
        "tests",
        "fixtures",
        "expected",
    )
)


def expected_dir() -> str:
    return os.environ.get("DFM_REFERENCE_EXPECTED") or DEFAULT_EXPECTED


def load_golden(name: str) -> Optional[dict]:
    path = os.path.join(expected_dir(), name + ".json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# -- worker -------------------------------------------------------------------

_registry = None


def _init_worker() -> None:
    global _registry
    import io
    import contextlib

    from freecad.DFM.core.baselines.corpus import load_registry

    with contextlib.redirect_stdout(io.StringIO()):
        _registry = load_registry()


def _run_one(name: str) -> dict:
    """Analyse one fixture and check it against its golden."""
    import io
    import contextlib

    from freecad.DFM.core.baselines.harness import default_process_for, observe_shape
    from freecad.DFM.core.baselines.spec import BaselineSpec, check_drift

    golden = load_golden(name)
    if golden is None:
        return {"name": name, "status": "no-golden", "mismatches": []}
    builder = _registry.get(name) if _registry else None
    if builder is None:
        return {"name": name, "status": "no-builder", "mismatches": []}
    started = time.time()
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            shape = builder()
            observation, _ = observe_shape(name, shape, default_process_for(name))
    except Exception as exc:  # one broken fixture must not stop the corpus
        return {
            "name": name,
            "status": "error",
            "mismatches": [f"{type(exc).__name__}: {exc}"],
            "seconds": round(time.time() - started, 1),
        }
    # Speak the reference's rule vocabulary: our enum names, lower-cased,
    # with the handful of deliberate renames translated back.
    from freecad.DFM.core.baselines.parity import alias_map

    to_reference = alias_map().to_reference
    for finding in observation.findings:
        rule = finding.get("rule_id") or ""
        finding["rule_id"] = to_reference.get(rule, rule.lower())
    result = check_drift(BaselineSpec.from_json(golden), observation)
    # A rule raising inside the harness prints rather than propagating; say so.
    raised = [line.strip() for line in log.getvalue().splitlines() if line.strip().startswith("!")]
    return {
        "name": name,
        "status": "ok" if result.ok else "drift",
        "mismatches": result.mismatches + [f"rule raised: {line}" for line in raised],
        "seconds": round(time.time() - started, 1),
    }


# -- driver -------------------------------------------------------------------


def corpus_names() -> list[str]:
    names = []
    for file in os.listdir(expected_dir()):
        if file.endswith(".json"):
            names.append(file[:-5])
    return sorted(names)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="fixtures (default: every golden)")
    parser.add_argument("--match", action="append", default=[], help="substring filter, repeatable")
    parser.add_argument(
        "--process",
        action="append",
        default=[],
        help="only fixtures whose golden part_process_type is this (MILLED, "
        "TURNED, MILL_TURN, SHEET_METAL), repeatable",
    )
    parser.add_argument(
        "--not-process",
        action="append",
        default=[],
        help="exclude fixtures whose golden part_process_type is this, repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="drop fixtures whose name contains this substring, repeatable",
    )
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--save", help="write per-fixture results to this JSON file")
    parser.add_argument("--compare", help="an earlier --save to diff against")
    parser.add_argument("--quiet", action="store_true", help="summary only, no mismatch lines")
    parser.add_argument("--slowest", type=int, default=0, help="list the N slowest fixtures")
    args = parser.parse_args(argv)

    if not os.path.isdir(expected_dir()):
        print(f"reference goldens not found at {expected_dir()}; set DFM_REFERENCE_EXPECTED")
        return 2

    names = args.names or corpus_names()
    if args.match:
        names = [n for n in names if any(m in n for m in args.match)]
    if args.exclude:
        names = [n for n in names if not any(m in n for m in args.exclude)]
    if args.process or args.not_process:
        def process_of(name: str) -> str:
            return (load_golden(name) or {}).get("part_process_type") or ""
        if args.process:
            names = [n for n in names if process_of(n) in args.process]
        if args.not_process:
            names = [n for n in names if process_of(n) not in args.not_process]
    if not names:
        print("no fixtures selected")
        return 2

    started = time.time()
    if args.jobs > 1 and len(names) > 1:
        # Longest-first is not known up front; small chunks keep workers busy.
        with multiprocessing.Pool(args.jobs, initializer=_init_worker) as pool:
            results = pool.map(_run_one, names, chunksize=1)
    else:
        _init_worker()
        results = [_run_one(n) for n in names]
    elapsed = time.time() - started

    by_status: dict[str, list[dict]] = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    if not args.quiet:
        for r in results:
            if r["status"] == "ok":
                continue
            print(f"  {r['status'].upper():<10} {r['name']}")
            for line in r["mismatches"]:
                print(f"             {line}")

    ok = len(by_status.get("ok", []))
    print()
    print(
        f"{ok} of {len(results)} match their golden"
        f"   drift {len(by_status.get('drift', []))}"
        f"   error {len(by_status.get('error', []))}"
        f"   no builder {len(by_status.get('no-builder', []))}"
        f"   ({elapsed:.0f} s, {args.jobs} jobs)"
    )
    if by_status.get("no-builder"):
        print("  no builder: " + ", ".join(r["name"] for r in by_status["no-builder"]))

    if args.slowest:
        for r in sorted(results, key=lambda r: -r.get("seconds", 0))[: args.slowest]:
            print(f"  {r.get('seconds', 0):>7.1f} s  {r['name']}")

    if args.compare:
        with open(args.compare, "r", encoding="utf-8") as handle:
            before = {r["name"]: r for r in json.load(handle)["results"]}
        fixed, broken, changed = [], [], []
        for r in results:
            prev = before.get(r["name"])
            if prev is None:
                continue
            if prev["status"] != "ok" and r["status"] == "ok":
                fixed.append(r["name"])
            elif prev["status"] == "ok" and r["status"] != "ok":
                broken.append(r["name"])
            elif prev["mismatches"] != r["mismatches"]:
                changed.append(r["name"])
        print(f"\nsince {args.compare}: fixed {len(fixed)}, broken {len(broken)}, changed {len(changed)}")
        for label, items in (("fixed", fixed), ("BROKEN", broken), ("changed", changed)):
            for name in items:
                print(f"  {label:<8} {name}")
                if label != "fixed":
                    for line in next(r for r in results if r["name"] == name)["mismatches"]:
                        print(f"             {line}")

    if args.save:
        with open(args.save, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {"expected_dir": expected_dir(), "results": sorted(results, key=lambda r: r["name"])},
                handle,
                indent=1,
            )

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
