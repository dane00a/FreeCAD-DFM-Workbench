# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Measure this engine against the one it was ported from, and rank the gap.

``check_dfm_baselines.py`` next door asks whether the rules still say what
they said yesterday. It cannot ask whether what they say is *right*, because
the only thing it compares against is a recording this engine made of
itself: every number in ``dfm_baseline.json`` could disagree with the
reference engine and the harness would stay green forever. This is the other
half. It reads the same recording and compares it against the reference's
own approved answers -- ``expected/<fixture>.json``, one per fixture, in the
schema this workbench deliberately kept key for key -- and prints what is
left, largest first.

    python tests/fixtures/check_parity.py --verify
    python tests/fixtures/check_parity.py --verify --top 0
    python tests/fixtures/check_parity.py torture_machinists_maze thin_sheet
    python tests/fixtures/check_parity.py --list
    python tests/fixtures/check_parity.py --show optical_periscope_housing
    python tests/fixtures/check_parity.py --aliases

Nothing here runs a rule or opens a shape, which is the point: it compares
two files and takes about a second, so it can be run after every change
rather than only when somebody remembers. It also never writes -- not to
``dfm_baseline.json``, which the recorder owns, and emphatically not to the
reference tree, which belongs to another repository and is the standard
being measured against.

**The report's most useful column is the last one.** Every difference is
graded against ``docs/parity-tasks.md``: a difference that document already
names is backlog somebody has looked at and priced, and a difference it does
not name appeared afterwards. The two need completely different responses,
and a flat list of 300 numbers hides which is which.

Needs nothing but the standard library and this repository. The reference
directory is found beside the workbench by default; point ``--reference`` or
``DFM_REFERENCE_EXPECTED`` at it if it lives somewhere else, and if it is
not checked out at all this exits saying so rather than reporting a corpus
in perfect agreement with nothing.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import sys
from collections import defaultdict
from typing import Iterable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
# Same path dance as check_dfm_baselines.py, and for the same reason: the
# stub has to win a name clash against a real FreeCAD if one happens to be
# importable. Nothing in this file needs FreeCAD, but `freecad.DFM.core.rules`
# is reached through the package and the package's __init__ may.
for _path in (_REPO, os.path.join(_REPO, "tests", "stubs"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from freecad.DFM.core.baselines.parity import (  # noqa: E402
    COMPARED_FIELDS,
    COUNTING_CAVEAT,
    REFERENCE_RULE_IDS,
    RENAME_NOTES,
    RENAMED_RULES,
    ROW_COUNTED_FIELDS,
    SKIPPED_FIELDS,
    UNPAIRED_OURS,
    UNPAIRED_REFERENCE,
    AliasMap,
    Difference,
    ParityResult,
    alias_map,
    compare,
    coverage,
)
from freecad.DFM.core.baselines.spec import BaselineSpec  # noqa: E402
from freecad.DFM.core.baselines.store import BaselineStore  # noqa: E402


#: The reference's approved answers: the copy committed beside the fixtures
#: (tests/fixtures/reference_expected). Overridable, to compare against a
#: newer reference while resynchronising.
DEFAULT_REFERENCE = os.path.join(_REPO, "tests", "fixtures", "reference_expected")


#: The backlog this report grades against.
PARITY_TASKS = os.path.join(_REPO, "docs", "parity-tasks.md")

#: How a difference relates to what is already written down. The three are
#: worth different amounts of somebody's attention and the report says which
#: is which rather than making the reader cross-reference 300 lines by hand.
CATALOGUED = "catalogued"  # this rule, on this fixture, is named in the doc
KNOWN = "known"  # the rule is named, but not against this fixture
NEW = "NEW"  # nothing in the doc mentions it


# =============================================================================
# Loading the two sides
# =============================================================================


def load_reference(directory: str) -> dict[str, BaselineSpec]:
    """Read the reference goldens.

    Parsed straight into :class:`BaselineSpec`, because the two schemas are
    the same schema -- ``spec.py`` was written from ``expected_checker.cpp``
    key for key, and the absent/empty distinction that makes the comparison
    correct is already implemented there. Writing a second parser for the
    same JSON would be two chances to get the optional fields wrong instead
    of one.

    A golden is keyed by its filename rather than by its ``fixture`` field,
    matching how the reference's own loader addresses them: it builds the
    path from the fixture name and never reads the field back.
    """
    specs: dict[str, BaselineSpec] = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as handle:
            spec = BaselineSpec.from_json(json.load(handle))
        spec.fixture = name
        specs[name] = spec
    return specs


def reference_state(directory: str, specs: dict[str, BaselineSpec]) -> str:
    """One line recording which reference snapshot a run measured against.

    The reference corpus is live: it gains fixtures and rules while this is
    being worked on, and a parity number with no note of what it was
    measured against is a number nobody can reproduce or argue with. There
    is no git access to that tree from here, so a file count and a date is
    what can honestly be claimed -- which is enough to notice that two runs
    a week apart were not looking at the same thing.
    """
    stamp = datetime.date.today().isoformat()
    return f"{len(specs)} goldens, read {stamp}, from {directory}"


# =============================================================================
# Grading against docs/parity-tasks.md
# =============================================================================


class Catalogue:
    """What ``docs/parity-tasks.md`` already knows about.

    The document is prose with tables in it, not a data file, so this reads
    it the way a person does: every backticked token is a candidate, and a
    token is a rule if it names one and a fixture if it names one. A rule
    and a fixture on the same line -- which is what every table row in the
    document is -- pair up; a rule in a section heading pairs with every
    fixture named anywhere under that heading, which is how ``§1a`` and
    ``§5`` are written.

    **This is a heuristic and it is allowed to be.** Grading wrong in the
    generous direction buries a new difference in the backlog, so the
    classification is deliberately tight: an exact rule-and-fixture pair is
    ``catalogued``, a rule named anywhere is only ``known``, and anything
    else is ``NEW`` and printed in full no matter how the report is
    truncated. The cost of a false ``NEW`` is somebody reading a paragraph
    of the document; the cost of a false ``catalogued`` is a regression
    filed under a decision that was made about something else.
    """

    #: Tokens in the document are backticked. Nothing else is reliable:
    #: fixture names appear in table cells, in headings and mid-sentence,
    #: and the only thing they have in common is the backticks.
    _TOKEN = re.compile(r"`([A-Za-z0-9_]+)`")
    _HEADING = re.compile(r"^(#+)\s")

    def __init__(
        self,
        text: str,
        rule_names: Iterable[str],
        fixture_names: Iterable[str],
        aliases: AliasMap,
    ):
        self.rules: set[str] = set()
        self.pairs: set[tuple[str, str]] = set()
        self.fixtures: set[str] = set()
        self.feature_types: set[str] = set()

        known_rules = set(rule_names)
        known_fixtures = set(fixture_names)

        section_rules: set[str] = set()
        section_fixtures: set[str] = set()

        def close_section() -> None:
            for rule in section_rules:
                for fixture in section_fixtures:
                    self.pairs.add((rule, fixture))

        for line in text.splitlines():
            tokens = self._TOKEN.findall(line)
            heading = self._HEADING.match(line)
            if heading:
                # A heading ends the previous section's scope. Doing this
                # before reading the new heading's own tokens keeps `§1a`'s
                # rule from adopting `§1b`'s fixtures, which would quietly
                # mark half of §1b catalogued under the wrong cause.
                close_section()
                section_rules = set()
                section_fixtures = set()

            line_rules: set[str] = set()
            line_fixtures: set[str] = set()
            for token in tokens:
                our_name = self._as_rule(token, known_rules, aliases)
                if our_name is not None:
                    line_rules.add(our_name)
                    self.rules.add(our_name)
                    continue
                if token in known_fixtures:
                    line_fixtures.add(token)
                    self.fixtures.add(token)
                    continue
                # The recognition table in the document names feature types
                # bare -- THROUGH_CAVITY, PATTERN, FILLET -- which the
                # features block spells `type_THROUGH_CAVITY`.
                if token.isupper():
                    self.feature_types.add(token)

            for rule in line_rules:
                for fixture in line_fixtures:
                    self.pairs.add((rule, fixture))

            if heading:
                section_rules = line_rules
                section_fixtures = set()
            else:
                section_fixtures |= line_fixtures

        close_section()

    @staticmethod
    def _as_rule(
        token: str, known_rules: set[str], aliases: AliasMap
    ) -> Optional[str]:
        """Our name for a token that names a rule, or ``None``.

        The document writes rule names in the reference's lower-case
        spelling throughout, including the six renames it tabulates, so
        every lookup goes through the alias map. Without that, `hole_deep_risk`
        in the status table would not match `HOLE_DEPTH_RATIO` in the report
        and the largest catalogued differences would all grade as new.
        """
        upper = token.upper()
        if upper in known_rules:
            return upper
        our_name = aliases.translate(token.lower())
        if our_name is not None and our_name in known_rules:
            return our_name
        return None

    def grade(self, difference: Difference) -> str:
        """Where a difference sits relative to the written backlog.

        Rule differences grade on the rule, which is what the document is
        organised by. Feature differences grade on the feature type, which
        is what its recognition table is organised by. Everything else --
        severities, category totals, feature counts -- has no per-rule
        identity to grade on, so it falls back to whether the fixture is
        discussed at all, and that fallback is the weakest thing in this
        file: a fixture named once for an unrelated reason will mark its
        severity difference ``known``. Read those two fields' grades as a
        hint and not as a verdict.
        """
        if difference.field == "by_rule":
            if (difference.key, difference.fixture) in self.pairs:
                return CATALOGUED
            return KNOWN if difference.key in self.rules else NEW
        if difference.field == "features":
            kind = difference.key.split("_", 1)[-1] if difference.key else ""
            return KNOWN if kind in self.feature_types else NEW
        return KNOWN if difference.fixture in self.fixtures else NEW


def load_catalogue(
    path: str, rule_names: Iterable[str], fixture_names: Iterable[str], aliases: AliasMap
) -> Optional[Catalogue]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return Catalogue(handle.read(), rule_names, fixture_names, aliases)


# =============================================================================
# Reporting
# =============================================================================


def _bar(magnitude: int, worst: int, width: int = 24) -> str:
    """A crude magnitude bar, so the shape of the tail is visible at a glance.

    The ranked tables are the point of this report and a column of numbers
    does not show that four rules carry most of the gap and thirty carry one
    apiece. Rounded up rather than down, so a difference of one is never
    drawn as nothing at all.
    """
    if worst <= 0:
        return ""
    filled = max(1, round(magnitude * width / worst))
    return "#" * filled


def print_alias_report(cov, verbose: bool) -> None:
    """The alias map's coverage, in both directions.

    Printed on every run rather than behind a flag. An alias map is the one
    part of a parity harness that can make the numbers look *better* by
    being wrong -- a rule that fails to translate is a rule not compared --
    so its coverage belongs next to the figures it produced, not a flag
    away.
    """
    mapped = len(cov.mapped)
    print(f"alias map: {mapped} reference rules translated")
    if verbose:
        for reference_id, our_name in sorted(cov.mapped.items()):
            if reference_id in RENAMED_RULES:
                print(f"    {reference_id:<34} -> {our_name}")
                print(f"    {'':<34}    renamed: {RENAME_NOTES[reference_id]}")
            else:
                print(f"    {reference_id:<34} -> {our_name}")

    for label, ids, reasons in (
        ("reference", cov.reference_unmapped_expected, UNPAIRED_REFERENCE),
        ("ours", cov.ours_unmapped_expected, UNPAIRED_OURS),
    ):
        if not ids:
            continue
        print(f"  {len(ids)} {label}-side rule(s) with no counterpart, understood:")
        for rule_id in ids:
            print(f"    {rule_id}")
            print(f"        {reasons[rule_id]}")

    # The loud half. An identifier nobody has explained is either a port
    # that has not happened or a rename nobody wrote down, and both stay
    # invisible for exactly as long as the tooling is willing to shrug.
    for label, ids in (
        ("REFERENCE", cov.reference_unmapped_unexplained),
        ("OURS", cov.ours_unmapped_unexplained),
    ):
        if ids:
            print(f"  !! {len(ids)} UNMAPPED {label} RULE(S), unexplained:")
            for rule_id in ids:
                print(f"     !! {rule_id}")
    if cov.unknown_to_snapshot:
        print(
            f"  !! {len(cov.unknown_to_snapshot)} reference rule(s) not in the "
            "committed snapshot -- the reference has grown rules:"
        )
        for rule_id in cov.unknown_to_snapshot:
            print(f"     !! {rule_id}")


def print_ranked_by_rule(
    results: list[ParityResult], catalogue: Optional[Catalogue], top: int
) -> None:
    """Every rule that disagrees, worst first, with its net direction.

    Net and gross are both shown because they answer different questions. A
    rule at net 0 and gross 8 fires four times too often on one part and
    four times too seldom on another, which is a measurement difference;
    one at net −8 and gross 8 is missing entirely in one direction, which is
    usually a port that has not happened. Ranking on gross puts the first
    kind where somebody will see it.
    """
    net: dict[str, int] = defaultdict(int)
    gross: dict[str, int] = defaultdict(int)
    fixtures: dict[str, set[str]] = defaultdict(set)
    ours_only: dict[str, int] = defaultdict(int)
    reference_only: dict[str, int] = defaultdict(int)
    grades: dict[str, set[str]] = defaultdict(set)
    for result in results:
        for difference in result.differences:
            if difference.field != "by_rule":
                continue
            net[difference.key] += difference.delta
            gross[difference.key] += difference.magnitude
            fixtures[difference.key].add(difference.fixture)
            if difference.one_sided:
                if difference.reference == 0:
                    ours_only[difference.key] += 1
                else:
                    reference_only[difference.key] += 1
            if catalogue is not None:
                grades[difference.key].add(catalogue.grade(difference))

    if not gross:
        print("\nby rule: no differences")
        return

    # Ranked on the one-sided part count first, because that is the half of
    # the difference the counting convention cannot explain away, and gross
    # only as the tie-break. Ranking on gross alone puts whichever rule
    # happened to fire on the most heavily patterned part at the top of a
    # list of work to do, which is exactly the wrong instruction.
    def rank(rule: str) -> tuple:
        return (
            -(ours_only[rule] + reference_only[rule]),
            -gross[rule],
            rule,
        )

    order = sorted(gross, key=rank)
    shown = order if top <= 0 else order[:top]
    worst = max(gross.values())

    print(f"\nby rule -- {len(order)} rule(s) disagree, most one-sided first")
    print(
        "  'one-sided' counts parts where the rule fires on exactly one side. "
        "Those\n  are real. A gross with no one-sided parts behind it may be "
        "nothing but the\n  reference having collapsed a patterned part into "
        "one row.\n  'status' is the worst grade over the rule's parts, so "
        "NEW means at least one\n  part is not in the document, not that the "
        "rule is unknown to it."
    )
    print(
        f"  {'rule':<34} {'net':>6} {'gross':>6} {'parts':>6} "
        f"{'ours-only':>10} {'ref-only':>9}  {'status':<11}"
    )
    for rule in shown:
        seen = grades.get(rule, set())
        if catalogue is None:
            grade = "-"
        elif NEW in seen:
            grade = NEW
        elif KNOWN in seen:
            grade = KNOWN
        else:
            grade = CATALOGUED
        delta = net[rule]
        sign = "+" if delta >= 0 else ""
        print(
            f"  {rule:<34} {sign + str(delta):>6} {gross[rule]:>6} "
            f"{len(fixtures[rule]):>6} {ours_only[rule]:>10} "
            f"{reference_only[rule]:>9}  {grade:<11} {_bar(gross[rule], worst)}"
        )
    if len(order) > len(shown):
        print(f"  ... {len(order) - len(shown)} more (--top 0 for all)")
    one_sided_parts = sum(ours_only.values()) + sum(reference_only.values())
    print(
        f"  {sum(gross.values())} occurrences of disagreement, over "
        f"{one_sided_parts} rule-and-part pairs where one side is silent"
    )


def print_ranked_by_fixture(
    results: list[ParityResult], catalogue: Optional[Catalogue], top: int
) -> None:
    """Every fixture that disagrees, worst first.

    Grouped separately from the rule table because the two point at
    different work. A rule table says which rule to read; a fixture table
    says which part to open, and four parts carrying a third of the gap is
    the kind of fact only this view shows.
    """
    def one_sided(result: ParityResult) -> int:
        return sum(1 for d in result.differences if d.one_sided)

    ranked = sorted(
        (r for r in results if r.differences),
        key=lambda r: (-one_sided(r), -r.magnitude, r.fixture),
    )
    if not ranked:
        print("\nby fixture: no differences")
        return
    shown = ranked if top <= 0 else ranked[:top]
    worst = max(r.magnitude for r in ranked)

    print(f"\nby fixture -- {len(ranked)} fixture(s) disagree, most one-sided first")
    print(f"  {'fixture':<40} {'gross':>6} {'fields':>7} {'one-sided':>10}")
    for result in shown:
        fields = len({d.label for d in result.differences})
        flag = ""
        if catalogue is not None and any(
            catalogue.grade(d) == NEW for d in result.differences
        ):
            flag = "  <- has new"
        print(
            f"  {result.fixture:<40} {result.magnitude:>6} {fields:>7} "
            f"{one_sided(result):>10}  {_bar(result.magnitude, worst)}{flag}"
        )
    if len(ranked) > len(shown):
        print(f"  ... {len(ranked) - len(shown)} more (--top 0 for all)")


def print_new_since_doc(
    results: list[ParityResult], catalogue: Optional[Catalogue]
) -> None:
    """Differences the backlog document does not mention.

    Printed in full and never truncated by ``--top``. The ranked tables
    above are a picture of a backlog that somebody has already measured,
    argued about and in one case deliberately parked; this section is the
    part nobody has seen, and it is the only thing in this report that can
    represent a regression rather than an unfinished port. Burying it under
    a ``--top`` cut would defeat the entire cross-reference.
    """
    if catalogue is None:
        print("\nnew since docs/parity-tasks.md: not graded (document not found)")
        return

    fresh = [
        d
        for result in results
        for d in result.differences
        if catalogue.grade(d) == NEW
    ]
    if not fresh:
        print("\nnew since docs/parity-tasks.md: none")
        return

    # `by_rule` and `features` carry an identity the document is organised
    # by, so their grade is a real answer. The other three do not: a
    # category total is the sum of its rules and a severity total is the sum
    # of its findings, so they cannot be new independently of the rules
    # underneath them, and grading them on "is the fixture mentioned"
    # produces a long list that says nothing the rule list does not say
    # better. They are counted here and not enumerated.
    graded = [d for d in fresh if d.field in ("by_rule", "features")]
    derivative = [d for d in fresh if d.field not in ("by_rule", "features")]

    print(
        f"\nnew since docs/parity-tasks.md -- {len(graded)} rule and feature "
        "difference(s) the document does not name"
    )
    print(
        "  These are the ones worth reading. A rule difference is new when "
        "the\n  document never names that rule; a feature difference when it "
        "never names\n  that feature type. Truncation never applies here."
    )
    if not graded:
        print("  (none)")
    by_field: dict[str, list[Difference]] = defaultdict(list)
    for difference in graded:
        by_field[difference.field].append(difference)
    for field_name in ("by_rule", "features"):
        entries = by_field.get(field_name)
        if not entries:
            continue
        entries = sorted(entries, key=lambda d: (-d.magnitude, d.fixture, d.key))
        print(f"\n  {field_name}: {len(entries)}")
        for difference in entries:
            # A fixture the document never mentions at all almost certainly
            # postdates it -- the corpus it measured was 211 parts and has
            # grown since -- so its differences were never anybody's backlog
            # rather than having escaped it.
            unseen = (
                ""
                if difference.fixture in catalogue.fixtures
                else "  (fixture not named in the document)"
            )
            print(f"    {difference.fixture:<40} {difference.describe()}{unseen}")

    if derivative:
        counts: dict[str, int] = defaultdict(int)
        for difference in derivative:
            counts[difference.field] += 1
        summary = ", ".join(f"{k} {counts[k]}" for k in sorted(counts))
        print(
            f"\n  not enumerated: {len(derivative)} derivative difference(s) "
            f"({summary}).\n  A category total is the sum of its rules and a "
            "severity total the sum of its\n  findings, so these follow from "
            "the rule differences above and from the\n  severity declarations "
            "in the process YAML. --show a fixture to read them."
        )


def print_untranslatable(results: list[ParityResult]) -> None:
    """Reference rules the alias map could not read, per fixture.

    Kept out of every count above on purpose. An untranslatable rule is a
    hole in the instrument and not a measurement: reporting it as a −4 would
    put it in the ranked table and invite somebody to go and close a rule
    that may not exist here under any name.
    """
    offenders = [(r.fixture, r.untranslatable) for r in results if r.untranslatable]
    if not offenders:
        return
    print(f"\n!! {len(offenders)} fixture(s) carry reference rules that did not translate")
    print("   These are NOT counted in any figure above -- they were not compared.")
    for fixture, rules in sorted(offenders):
        for rule_id, count in sorted(rules.items()):
            print(f"   !! {fixture:<40} {rule_id} x{count}")


# =============================================================================
# Entry point
# =============================================================================


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "names", nargs="*", help="fixtures to compare (default: all shared)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="compare the corpus and rank what disagrees (the default)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="one line per fixture saying whether it agrees, and stop",
    )
    parser.add_argument(
        "--show", metavar="FIXTURE", help="every difference on one fixture, and stop"
    )
    parser.add_argument(
        "--aliases",
        action="store_true",
        help="print the rule alias map in full and stop",
    )
    parser.add_argument(
        "--reference",
        default=os.environ.get("DFM_REFERENCE_EXPECTED") or DEFAULT_REFERENCE,
        help="the reference engine's expected/ directory (default: beside this repo)",
    )
    parser.add_argument(
        "--baseline", help="read this file instead of dfm_baseline.json"
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="rows per ranked table; 0 for all (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    aliases = alias_map()
    from freecad.DFM.core.rules import Rulebook

    our_rule_names = [rule.name for rule in Rulebook]

    if args.aliases:
        cov = coverage(aliases, REFERENCE_RULE_IDS, our_rule_names)
        print_alias_report(cov, verbose=True)
        return 0 if cov.ok else 1

    if not os.path.isdir(args.reference):
        # Refusing rather than reporting nothing. A corpus compared against
        # an empty directory agrees with it perfectly, and a green parity
        # run that measured nothing is worse than a red one.
        print(f"reference goldens not found: {args.reference}")
        print("pass --reference, or set DFM_REFERENCE_EXPECTED")
        return 2

    store = BaselineStore(args.baseline)
    reference = load_reference(args.reference)
    if not reference:
        print(f"no goldens in {args.reference}")
        return 2

    ours_names = set(store.names())
    shared = sorted(ours_names & set(reference))
    if args.names:
        shared = [n for n in args.names if n in shared]
        missing = [n for n in args.names if n not in shared]
        for name in missing:
            print(f"  ?  {name}: not in both corpora")

    results = [compare(store.get(n), reference[n], aliases) for n in shared]
    by_name = {r.fixture: r for r in results}

    if args.show:
        result = by_name.get(args.show)
        if result is None:
            print(f"{args.show}: not compared (missing from one side?)")
            return 1
        print(f"{result.fixture}: {len(result.differences)} difference(s)")
        for difference in sorted(
            result.differences, key=lambda d: (d.field, d.key)
        ):
            print(f"    {difference.describe()}")
        if result.not_asserted:
            print(
                "  reference states no opinion on: "
                + ", ".join(sorted(result.not_asserted))
            )
        for rule_id, count in sorted(result.untranslatable.items()):
            print(f"  !! untranslatable reference rule {rule_id} x{count}")
        return 0 if result.ok else 1

    if args.list:
        for result in results:
            status = "ok" if result.ok else f"{result.magnitude:>4} occurrences"
            print(f"{result.fixture:<40} {status}")
        agreed = sum(1 for r in results if r.ok)
        print(f"\n{agreed} of {len(results)} identical")
        return 0 if agreed == len(results) else 1

    # --- the full report -------------------------------------------------

    print(f"reference snapshot: {reference_state(args.reference, reference)}")
    print(f"ours: {len(store)} recorded baselines")
    print(
        f"compared: {len(shared)} fixture(s) present in both "
        f"({len(ours_names - set(reference))} only here, "
        f"{len(set(reference) - ours_names)} only there)"
    )

    # The union, not the observed set alone. A reference rule that exists
    # but never fires over its own corpus is mapped perfectly well and must
    # not be reported as a hole; a rule that fires but is not in the
    # committed snapshot is the reference having grown one, and
    # `unknown_to_snapshot` says so separately.
    observed_reference_rules = {
        rule for spec in reference.values() for rule in spec.by_rule
    }
    cov = coverage(
        aliases, REFERENCE_RULE_IDS | observed_reference_rules, our_rule_names
    )
    print()
    print_alias_report(cov, verbose=False)

    catalogue = load_catalogue(
        PARITY_TASKS, our_rule_names, set(store.names()) | set(reference), aliases
    )

    print()
    print_field_summary(results)
    print_process_types(store, reference, shared)
    print_ranked_by_rule(results, catalogue, args.top)
    print_ranked_by_fixture(results, catalogue, args.top)
    print_new_since_doc(results, catalogue)
    print_untranslatable(results)

    print("\nnot compared:")
    for field_name, why in sorted(SKIPPED_FIELDS.items()):
        print(f"  {field_name}: {why}")

    identical = sum(1 for r in results if r.ok)
    print(f"\n{identical} of {len(results)} fixture(s) identical on every field compared")
    return 0 if identical == len(results) and cov.ok else 1


def print_field_summary(results: list[ParityResult]) -> None:
    """How many fixtures agree, field by field.

    The headline number for a port is not the total occurrences of
    disagreement -- that is dominated by whichever rule happens to fire most
    -- but how many parts come out identical on each axis. A field where 198
    of 211 parts agree and one where 124 do are in completely different
    states of repair, and one summed figure hides that.
    """
    compared: dict[str, int] = defaultdict(int)
    agreed: dict[str, int] = defaultdict(int)
    occurrences: dict[str, int] = defaultdict(int)

    for result in results:
        touched = {d.field for d in result.differences}
        silent = set(result.not_asserted)
        for field_name in COMPARED_FIELDS:
            # `severity` arrives in not_asserted as severity.error and so
            # on, so a field counts as unasserted only when every axis under
            # it is. Nothing in the corpus states one severity and not the
            # others, but a comparison that assumed so would miscount
            # silently rather than fail.
            if field_name == "severity":
                if len([s for s in silent if s.startswith("severity.")]) == 3:
                    continue
            elif field_name in silent:
                continue
            compared[field_name] += 1
            if field_name not in touched:
                agreed[field_name] += 1
        for difference in result.differences:
            occurrences[difference.field] += difference.magnitude

    print(f"{'field':<16} {'identical':>12} {'compared':>10} {'occurrences':>12}")
    for field_name in COMPARED_FIELDS:
        total = compared[field_name]
        mark = " *" if field_name in ROW_COUNTED_FIELDS else ""
        if not total:
            print(f"{field_name:<16} {'-':>12} {0:>10}   never asserted")
            continue
        print(
            f"{field_name:<16} {agreed[field_name]:>12} {total:>10} "
            f"{occurrences[field_name]:>12}{mark}"
        )
    print(f"\n  * {COUNTING_CAVEAT}")


def print_process_types(
    store: BaselineStore, reference: dict[str, BaselineSpec], names: Iterable[str]
) -> None:
    """Process classification, counted on its own.

    Kept out of the ranked tables because it is categorical and has no
    magnitude, and reported anyway because it is the single most
    consequential thing either engine decides: every rule that runs on a
    part, and every limit those rules read, comes from the process the
    classifier picked. A part the two engines send to different machines is
    not comparable on any other axis in this report, and a run that did not
    say so would be quietly comparing two different questions.
    """
    agree = disagree = 0
    disagreements: list[str] = []
    for name in names:
        theirs = reference[name].part_process_type
        if theirs is None:
            continue
        mine = store.get(name).part_process_type or "UNKNOWN"
        if mine == theirs:
            agree += 1
        else:
            disagree += 1
            disagreements.append(f"    {name:<40} ours {mine}, reference {theirs}")
    total = agree + disagree
    print(f"\npart_process_type: {agree} of {total} agree where the reference states one")
    for line in sorted(disagreements):
        print(line)


if __name__ == "__main__":
    raise SystemExit(main())
