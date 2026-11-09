# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the pass that collapses identical findings.

No geometry here either. Aggregation never looks at a shape -- it compares
what the rules already decided -- and every way it can go wrong is a matter
of which two findings are called the same one. Building solids to produce
those pairs would put the interesting difference three layers away from the
assertion.

The cases worth writing down are the two failures that would be expensive
to notice later. Merging too eagerly hides a finding: the machinist is told
about one thin wall and never learns about the second one at a different
thickness, and nothing in the panel or the baselines says a finding went
missing. Merging too timidly only leaves the noise that was already there.
So most of what follows is a list of single fields that must keep two
findings apart, and each of them is one line because the point is the
coverage, not the case.
"""

import unittest

from freecad.DFM.core.aggregation import aggregate_findings
from freecad.DFM.core.models import CheckResult, GeometryRef, Severity
from freecad.DFM.core.rules import Rulebook


def finding(
    rule=Rulebook.MIN_WALL_THICKNESS,
    severity=Severity.WARNING,
    message="This wall is thin.",
    value=1.5,
    limit=2.0,
    unit="mm",
    comparison="<",
    ignore=False,
    overview="thin wall",
    faces=(),
    refs=(),
):
    return CheckResult(
        rule_id=rule,
        severity=severity,
        overview=overview,
        message=message,
        ignore=ignore,
        value=value,
        limit=limit,
        comparison=comparison,
        unit=unit,
        failing_geometry=[("Face", i) for i in faces],
        refs=[GeometryRef(type="Face", index=i - 1, label=f"Face{i}") for i in refs],
    )


class MergeTests(unittest.TestCase):
    def test_identical_findings_become_one(self):
        merged = aggregate_findings([finding(faces=(1,)), finding(faces=(2,))])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].occurrence_count, 2)
        self.assertEqual(merged[0].message, "2x: This wall is thin.")

    def test_forty_eight_identical_findings_become_one(self):
        """The motivating case, in miniature: a patterned part.

        Written out because the count is the whole product claim and an
        off-by-one in it -- crediting the surviving finding to itself, say
        -- is invisible in a pair.
        """
        merged = aggregate_findings([finding(faces=(i,)) for i in range(1, 49)])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].occurrence_count, 48)
        self.assertEqual(merged[0].message, "48x: This wall is thin.")
        self.assertEqual(len(merged[0].failing_geometry), 48)

    def test_the_surviving_finding_is_the_first_one(self):
        first = finding(faces=(1,))
        merged = aggregate_findings([first, finding(faces=(2,))])

        self.assertIs(merged[0], first)

    def test_unmerged_findings_keep_their_order(self):
        merged = aggregate_findings(
            [
                finding(message="c", faces=(1,)),
                finding(message="a", faces=(2,)),
                finding(message="b", faces=(3,)),
            ]
        )

        self.assertEqual([f.message for f in merged], ["c", "a", "b"])


class GeometryMergeTests(unittest.TestCase):
    def test_failing_geometry_accumulates(self):
        merged = aggregate_findings([finding(faces=(1, 2)), finding(faces=(3, 4))])

        self.assertEqual(
            merged[0].failing_geometry,
            [("Face", 1), ("Face", 2), ("Face", 3), ("Face", 4)],
        )

    def test_failing_geometry_is_deduplicated(self):
        """Two rules can name the same face, and the face is still one face."""
        merged = aggregate_findings([finding(faces=(1, 2)), finding(faces=(2, 3))])

        self.assertEqual(
            merged[0].failing_geometry, [("Face", 1), ("Face", 2), ("Face", 3)]
        )

    def test_failing_geometry_order_is_the_order_it_arrived(self):
        """A set would answer this differently on different runs.

        The face list is what the viewport highlights and what the CSV
        exports, so an unstable order turns an exported report into
        something nobody can diff against yesterday's.
        """
        merged = aggregate_findings([finding(faces=(9, 4)), finding(faces=(7, 4, 1))])

        self.assertEqual(
            merged[0].failing_geometry,
            [("Face", 9), ("Face", 4), ("Face", 7), ("Face", 1)],
        )

    def test_refs_merge_deduplicated_and_in_order(self):
        """Refs are resolved after aggregation in the application, but the
        baseline panel and the history both hand around findings that
        already carry them, so the merge has to cope with either."""
        merged = aggregate_findings([finding(refs=(9, 4)), finding(refs=(7, 4))])

        self.assertEqual([r.label for r in merged[0].refs], ["Face9", "Face4", "Face7"])

    def test_refs_dedupe_by_what_they_point_at(self):
        """`GeometryRef` is a plain dataclass, so two built from the same
        face are equal but not the same object. Identity would keep both."""
        merged = aggregate_findings([finding(refs=(3,)), finding(refs=(3,))])

        self.assertEqual(len(merged[0].refs), 1)


class SeparationTests(unittest.TestCase):
    """Every field that has to keep two findings apart, one line each."""

    def assert_kept_apart(self, first, second):
        merged = aggregate_findings([first, second])
        self.assertEqual(len(merged), 2)
        self.assertEqual([f.occurrence_count for f in merged], [1, 1])

    def test_different_value(self):
        self.assert_kept_apart(finding(value=1.5), finding(value=1.9))

    def test_different_limit(self):
        self.assert_kept_apart(finding(limit=2.0), finding(limit=3.0))

    def test_different_unit(self):
        self.assert_kept_apart(finding(unit="mm"), finding(unit="deg"))

    def test_different_comparison(self):
        self.assert_kept_apart(finding(comparison="<"), finding(comparison=">"))

    def test_different_message(self):
        self.assert_kept_apart(finding(message="thin here"), finding(message="thin there"))

    def test_different_severity(self):
        self.assert_kept_apart(
            finding(severity=Severity.WARNING), finding(severity=Severity.ERROR)
        )

    def test_different_rule(self):
        self.assert_kept_apart(
            finding(rule=Rulebook.MIN_WALL_THICKNESS), finding(rule=Rulebook.MIN_DRAFT_ANGLE)
        )

    def test_different_overview(self):
        """The title is the row's own label in the tree, and the closest
        thing this model has to the per-finding category the reference
        keeps in its key to stop a rule's slot findings being filed under
        Pocket. A merge that took the first title and dropped the second
        would print a row the viewport contradicts."""
        self.assert_kept_apart(finding(overview="thin wall"), finding(overview="thin rib"))

    def test_a_muted_finding_does_not_absorb_an_unmuted_one(self):
        """Nothing sets `ignore` before this pass runs today. The day
        something does, a merge across it would mute a finding the
        machinist never touched -- silently, since the row that would have
        carried it no longer exists."""
        self.assert_kept_apart(finding(ignore=True), finding(ignore=False))


class RoundingTests(unittest.TestCase):
    def test_noise_below_the_fourth_decimal_still_merges(self):
        """The case the reference wrote its rounding for: the same nominal
        channel through two boolean operations, coming back differing in
        the ninth decimal place."""
        merged = aggregate_findings(
            [
                finding(value=1.000000200000028, faces=(1,)),
                finding(value=1.000000200000029, faces=(2,)),
            ]
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].occurrence_count, 2)

    def test_a_difference_at_the_fourth_decimal_does_not_merge(self):
        merged = aggregate_findings([finding(value=1.0001), finding(value=1.0002)])

        self.assertEqual(len(merged), 2)

    def test_a_measurement_that_is_not_a_number_is_left_alone(self):
        """`value` and `limit` are typed float and are allowed to be None
        by a check that had nothing to measure. Rounding one would raise in
        the middle of an analysis that had otherwise succeeded."""
        merged = aggregate_findings([finding(value=None), finding(value=None)])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].occurrence_count, 2)


class UntouchedTests(unittest.TestCase):
    def test_a_lone_finding_gains_nothing(self):
        merged = aggregate_findings([finding(message="only one", faces=(1,))])

        self.assertEqual(merged[0].message, "only one")
        self.assertEqual(merged[0].occurrence_count, 1)

    def test_a_lone_finding_among_merged_ones_gains_nothing(self):
        """The prefix is applied in a second pass over the groups, which is
        where a loop that wrote it unconditionally would show up."""
        merged = aggregate_findings(
            [finding(message="a"), finding(message="a"), finding(message="b")]
        )

        self.assertEqual([f.message for f in merged], ["2x: a", "b"])
        self.assertEqual([f.occurrence_count for f in merged], [2, 1])

    def test_running_the_pass_twice_changes_nothing(self):
        """The GUI and the corpus reach this by different routes, and a
        third caller added later would be one refactor away from putting
        two of them in series. A second pass must not turn "48x:" into
        "1x: 48x:" or anything else."""
        once = aggregate_findings([finding(faces=(1,)), finding(faces=(2,))])
        twice = aggregate_findings(once)

        self.assertEqual(len(twice), 1)
        self.assertEqual(twice[0].occurrence_count, 2)
        self.assertEqual(twice[0].message, "2x: This wall is thin.")

    def test_an_empty_run_produces_nothing(self):
        self.assertEqual(aggregate_findings([]), [])

    def test_the_default_occurrence_count_is_one(self):
        """Every check constructs a `CheckResult` without mentioning it."""
        self.assertEqual(finding().occurrence_count, 1)


if __name__ == "__main__":
    unittest.main()
