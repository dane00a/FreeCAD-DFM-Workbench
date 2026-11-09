# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the parity comparison against the reference engine.

There is no geometry here and no reference repository either. Both are
deliberate. The comparison works on two recordings and every trap in it is
bookkeeping: a field that is absent versus one that is present and zero, a
rule spelled one way on each side, a map walked in one direction when it
needed two. And the reference tree is a sibling checkout that is not on
every machine, so a suite that reached for it would skip its most important
assertions on exactly the machines where nobody would notice -- which is why
``REFERENCE_RULE_IDS`` is a committed snapshot and these tests read that
instead.

Two failure modes are worth more attention than the rest, because both make
the numbers look *better* rather than worse and neither announces itself.

An **alias that does not resolve** removes a rule from the comparison
entirely: whatever it would have disagreed about is now not measured, and
the report's totals go down. Every assertion about totality below exists for
that.

An **absent field read as a zero** invents agreement where there was no
claim, or invents disagreement where there was no output. The reference
schema omits what it does not assert and that has to survive the round trip
through the comparison, on each of the five fields independently, because
they do not all have the same rule.
"""

import unittest

from freecad.DFM.core.baselines.parity import (
    REFERENCE_RULE_IDS,
    RENAME_NOTES,
    RENAMED_RULES,
    UNPAIRED_OURS,
    UNPAIRED_REFERENCE,
    alias_map,
    compare,
    coverage,
    translate_by_rule,
)
from freecad.DFM.core.baselines.spec import BaselineSpec
from freecad.DFM.core.rules import Rulebook


OUR_RULE_NAMES = [rule.name for rule in Rulebook]


def ours(**fields) -> BaselineSpec:
    """One of our baselines, built the way the store builds one."""
    return BaselineSpec.from_json({"fixture": "part", **fields})


def theirs(**fields) -> BaselineSpec:
    """A reference golden, parsed the way check_parity.py parses one.

    Deliberately the same constructor. The two schemas are one schema, and
    a test that built the reference side through some other path would stop
    exercising the absent-field handling that is the whole point.
    """
    return BaselineSpec.from_json({"fixture": "part", **fields})


def labels(result) -> set:
    return {d.label for d in result.differences}


def by_label(result) -> dict:
    return {d.label: d for d in result.differences}


# =============================================================================
# The alias map, and whether anything can fall through it
# =============================================================================


class AliasMapTests(unittest.TestCase):
    def setUp(self):
        self.aliases = alias_map(OUR_RULE_NAMES)

    def test_case_is_handled_mechanically(self):
        """The ordinary case is derived, not tabulated.

        If this ever needed an entry per rule, the table would go stale the
        first time somebody added a rule and did not think to update it.
        """
        self.assertEqual(self.aliases.translate("thin_wall"), "THIN_WALL")
        self.assertEqual(self.aliases.translate("sharp_internal_edge"), "SHARP_INTERNAL_EDGE")

    def test_every_rename_names_a_real_rule(self):
        """A rename pointing at nothing silently stops comparing that rule."""
        for reference_id, our_name in RENAMED_RULES.items():
            self.assertIn(our_name, OUR_RULE_NAMES, f"{reference_id} -> {our_name}")

    def test_every_rename_carries_a_reason(self):
        """Why a name changed is the part nobody can reconstruct later."""
        self.assertEqual(set(RENAME_NOTES), set(RENAMED_RULES))
        for note in RENAME_NOTES.values():
            self.assertTrue(note.strip())

    def test_renames_win_in_both_directions(self):
        """MATERIAL_REMOVAL has a valid mechanical twin the reference never emits.

        Lower-casing it gives `material_removal`, which is a perfectly good
        identifier and is not the one the reference uses. The reverse map
        has to prefer the rename or a report would translate our rule back
        into a spelling that appears in no golden.
        """
        self.assertEqual(self.aliases.translate("material_removal_high"), "MATERIAL_REMOVAL")
        self.assertEqual(self.aliases.to_reference["MATERIAL_REMOVAL"], "material_removal_high")

    def test_unknown_rule_translates_to_nothing(self):
        """Never a guess, and never a pass-through.

        Returning the identifier unchanged would be the tempting thing to do
        and would file an unknown reference rule under a rule of ours that
        does not exist, where it would compare against zero forever.
        """
        self.assertIsNone(self.aliases.translate("rule_that_does_not_exist"))


class AliasCoverageTests(unittest.TestCase):
    """Totality: nothing on either side may go unaccounted for."""

    def setUp(self):
        self.aliases = alias_map(OUR_RULE_NAMES)
        self.coverage = coverage(self.aliases, REFERENCE_RULE_IDS, OUR_RULE_NAMES)

    def test_the_committed_snapshot_is_fully_accounted_for(self):
        """Every reference rule either maps or has a written reason not to.

        This is the assertion that makes the alias map worth having. An
        unmapped reference rule is a whole rule's worth of findings that the
        comparison never sees, and it would show up in the report as
        improved parity.
        """
        self.assertEqual(self.coverage.reference_unmapped_unexplained, [])
        self.assertEqual(
            set(self.coverage.reference_unmapped_expected), set(UNPAIRED_REFERENCE)
        )

    def test_every_rule_of_ours_is_accounted_for(self):
        """The other direction, which is the one that gets forgotten.

        A rule of ours the reference cannot express is a rule nothing over
        there will ever corroborate, and that is worth knowing before
        somebody reads a green parity run as a finished port.
        """
        self.assertEqual(self.coverage.ours_unmapped_unexplained, [])
        self.assertEqual(set(self.coverage.ours_unmapped_expected), set(UNPAIRED_OURS))

    def test_the_two_unpaired_tables_do_not_overlap_the_mapped_ones(self):
        """An entry in both places is a stale note nobody noticed.

        It would read as an explained absence for a rule that is in fact
        being compared perfectly well, which is how a table stops being
        trustworthy.
        """
        for reference_id in UNPAIRED_REFERENCE:
            self.assertNotIn(reference_id, self.coverage.mapped)
        claimed = set(self.coverage.mapped.values())
        for our_name in UNPAIRED_OURS:
            self.assertNotIn(our_name, claimed)

    def test_the_snapshot_agrees_with_itself(self):
        self.assertEqual(self.coverage.unknown_to_snapshot, [])
        self.assertTrue(self.coverage.ok)

    def test_an_unexplained_reference_rule_is_reported(self):
        """The loud path, exercised rather than assumed.

        Every other test here asserts that nothing is currently unmapped,
        which would pass just as happily if the reporting were broken.
        """
        cov = coverage(
            self.aliases,
            set(REFERENCE_RULE_IDS) | {"some_new_reference_rule"},
            OUR_RULE_NAMES,
        )
        self.assertIn("some_new_reference_rule", cov.reference_unmapped_unexplained)
        self.assertIn("some_new_reference_rule", cov.unknown_to_snapshot)
        self.assertFalse(cov.ok)

    def test_an_unexplained_rule_of_ours_is_reported(self):
        cov = coverage(self.aliases, REFERENCE_RULE_IDS, OUR_RULE_NAMES + ["BRAND_NEW_RULE"])
        self.assertIn("BRAND_NEW_RULE", cov.ours_unmapped_unexplained)
        self.assertFalse(cov.ok)

    def test_a_rule_the_reference_never_fires_is_still_mapped(self):
        """Existing and firing are different questions.

        The reference has rules that never fire over its own corpus -- the
        GD&T ones, for a start, since nothing in either corpus carries a
        feature control frame. Computing coverage from the rules observed in
        the goldens rather than from the rules that exist would report every
        one of those as a hole in the alias map.
        """
        self.assertIn("gdt_datum_valid", self.coverage.mapped)


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.aliases = alias_map(OUR_RULE_NAMES)

    def test_a_by_rule_map_comes_back_in_our_spelling(self):
        translated, untranslatable = translate_by_rule(
            self.aliases, {"thin_wall": 4, "hole_deep_risk": 2}
        )
        self.assertEqual(translated, {"THIN_WALL": 4, "HOLE_DEPTH_RATIO": 2})
        self.assertEqual(untranslatable, {})

    def test_what_will_not_translate_is_returned_and_not_dropped(self):
        """Silence here is the failure. A rule the map cannot read has to
        reach the caller, because dropping it makes the totals look better
        precisely when the instrument is least trustworthy."""
        translated, untranslatable = translate_by_rule(
            self.aliases, {"thin_wall": 4, "rule_from_the_future": 9}
        )
        self.assertEqual(translated, {"THIN_WALL": 4})
        self.assertEqual(untranslatable, {"rule_from_the_future": 9})

    def test_two_reference_rules_mapping_to_one_of_ours_are_summed(self):
        """No current entry does this; a future rename could.

        Losing half a rule's occurrences to a dictionary key collision is
        the quiet wrong answer this whole module exists to prevent, so the
        arithmetic is pinned before anybody needs it.
        """
        aliases = alias_map(["THIN_WALL"])
        aliases.to_ours["legacy_thin_wall"] = "THIN_WALL"
        translated, _ = translate_by_rule(
            aliases, {"thin_wall": 3, "legacy_thin_wall": 2}
        )
        self.assertEqual(translated, {"THIN_WALL": 5})


# =============================================================================
# Absent, empty and zero
# =============================================================================


class AbsentFieldTests(unittest.TestCase):
    """The reference's silences, honoured field by field.

    Each of the five has its own rule and they are not the same rule, which
    is why there is a test per field rather than one test with a loop.
    """

    def test_an_absent_feature_count_is_no_opinion(self):
        result = compare(ours(feature_count=12), theirs())
        self.assertEqual(result.differences, [])
        self.assertIn("feature_count", result.not_asserted)

    def test_a_feature_count_of_zero_is_an_opinion(self):
        """Present and zero is a claim: the reference found no features.

        The distinction that makes the whole format survivable. Conflating
        it with absence would let a recognizer that finds twelve features on
        a part the reference reads as empty pass as agreement.
        """
        result = compare(ours(feature_count=12), theirs(feature_count=0))
        self.assertEqual(by_label(result)["feature_count"].delta, 12)
        self.assertNotIn("feature_count", result.not_asserted)

    def test_absent_severities_are_skipped_individually(self):
        """The severity block is three independent assertions, not one.

        Nothing in either corpus states one severity and omits the others,
        and a comparison that assumed so would be wrong silently rather
        than loudly the first time something did.
        """
        result = compare(
            ours(severity={"error": 1, "warn": 2, "info": 3}),
            theirs(severity={"warn": 2}),
        )
        self.assertEqual(result.differences, [])
        severities = {s for s in result.not_asserted if s.startswith("severity.")}
        self.assertEqual(severities, {"severity.error", "severity.info"})

    def test_an_absent_features_block_is_no_opinion(self):
        """182 of the reference's goldens predate the block.

        Reading absence as "this part has no features" would manufacture a
        disagreement on every one of them, and the resulting report would be
        useless on the exact axis that catches a recognizer renaming things.
        """
        result = compare(
            ours(feature_count=3, features={"type_HOLE": 3}),
            theirs(feature_count=3),
        )
        self.assertEqual(result.differences, [])
        self.assertIn("features", result.not_asserted)

    def test_an_empty_features_block_is_an_opinion(self):
        """Present and empty says the recognizers found nothing to classify."""
        result = compare(ours(features={"type_HOLE": 3}), theirs(features={}))
        self.assertEqual(by_label(result)["features.type_HOLE"].delta, 3)
        self.assertNotIn("features", result.not_asserted)

    def test_an_absent_by_rule_map_still_compares(self):
        """by_rule is not optional, and this is where it matters.

        The reference's writer omits an empty map and its checker still
        walks the other side of it looking for surplus rules, so absence
        there means "this part published nothing" rather than "no opinion".
        Skipping it would let a part where the reference is entirely silent
        and we publish three findings pass as agreement -- which is the
        single most over-reporting case a parity harness could miss.
        """
        result = compare(ours(by_rule={"THIN_WALL": 3}), theirs())
        self.assertEqual(by_label(result)["by_rule.THIN_WALL"].delta, 3)
        self.assertNotIn("by_rule", result.not_asserted)

    def test_an_absent_by_category_map_still_compares(self):
        result = compare(ours(by_category={"hole": 2}), theirs())
        self.assertEqual(by_label(result)["by_category.hole"].delta, 2)


class ComparisonTests(unittest.TestCase):
    def test_identical_recordings_agree(self):
        recording = {
            "feature_count": 4,
            "severity": {"error": 0, "warn": 2, "info": 1},
            "by_category": {"hole": 2, "part": 1},
            "by_rule": {"THIN_WALL": 2, "FEATURE_COMPLEXITY": 1},
            "features": {"type_HOLE": 4},
        }
        reference = dict(recording)
        reference["by_rule"] = {"thin_wall": 2, "feature_complexity": 1}
        result = compare(ours(**recording), theirs(**reference))
        self.assertEqual(result.differences, [])
        self.assertTrue(result.ok)

    def test_the_alias_map_is_applied_to_by_rule(self):
        """Without this the six renames read as total disagreement twice over.

        Compared literally, `hole_deep_risk` is a rule the reference fires
        and we never do, and `HOLE_DEPTH_RATIO` is a rule we fire and it
        never does -- so one agreeing rule becomes two differences whose
        magnitudes add rather than cancel.
        """
        result = compare(
            ours(by_rule={"HOLE_DEPTH_RATIO": 7}), theirs(by_rule={"hole_deep_risk": 7})
        )
        self.assertEqual(result.differences, [])

    def test_a_rule_only_we_fire_is_a_difference(self):
        result = compare(ours(by_rule={"THIN_WALL": 3}), theirs(by_rule={}))
        difference = by_label(result)["by_rule.THIN_WALL"]
        self.assertEqual((difference.ours, difference.reference), (3, 0))
        self.assertTrue(difference.one_sided)

    def test_a_rule_only_they_fire_is_a_difference(self):
        """The direction a one-way walk loses.

        A rule that fires in the reference and nowhere here is a port that
        has not happened, and it is invisible to any comparison that only
        iterates our own keys.
        """
        result = compare(ours(by_rule={}), theirs(by_rule={"thin_wall": 3}))
        difference = by_label(result)["by_rule.THIN_WALL"]
        self.assertEqual((difference.ours, difference.reference), (0, 3))
        self.assertTrue(difference.one_sided)

    def test_a_count_difference_is_not_one_sided(self):
        """The distinction the counting convention forces.

        The reference collapses identical findings into one row; we publish
        one per occurrence. That can inflate a count on our side but it can
        never take one to zero, so a difference where both sides fire is
        suspect and a difference where one is silent is not.
        """
        result = compare(
            ours(by_rule={"THIN_WALL": 8}), theirs(by_rule={"thin_wall": 3})
        )
        self.assertFalse(by_label(result)["by_rule.THIN_WALL"].one_sided)

    def test_the_delta_runs_from_the_reference_towards_us(self):
        """Positive means we report more, matching docs/parity-tasks.md.

        Its status table reads `thin_wall | 103 | 99 | +4`. A report whose
        signs ran the other way would be read against that table and
        misunderstood every time.
        """
        result = compare(
            ours(by_rule={"THIN_WALL": 103}), theirs(by_rule={"thin_wall": 99})
        )
        difference = by_label(result)["by_rule.THIN_WALL"]
        self.assertEqual(difference.delta, 4)
        self.assertEqual(difference.magnitude, 4)
        self.assertIn("+4", difference.describe())

    def test_feature_kinds_are_compared_and_not_only_the_count(self):
        """A recognizer can re-label everything and move no total at all.

        Four holes read as four pockets leaves feature_count, every finding
        count and every severity identical. This block is the only thing in
        the report that can see it.
        """
        result = compare(
            ours(feature_count=4, features={"type_POCKET": 4}),
            theirs(feature_count=4, features={"type_HOLE": 4}),
        )
        self.assertEqual(
            labels(result), {"features.type_POCKET", "features.type_HOLE"}
        )
        self.assertEqual(by_label(result)["features.type_HOLE"].delta, -4)

    def test_an_untranslatable_reference_rule_is_not_a_difference(self):
        """A hole in the instrument is not a measurement.

        Reporting it as a −4 would put it in the ranked table and send
        somebody off to close a rule that may not exist here under any name.
        It has to be visible and it has to be visible as something else.
        """
        result = compare(
            ours(by_rule={}), theirs(by_rule={"some_reference_only_rule": 4})
        )
        self.assertEqual(result.differences, [])
        self.assertEqual(result.untranslatable, {"some_reference_only_rule": 4})
        self.assertFalse(result.ok)

    def test_magnitude_sums_the_absolute_differences(self):
        """Ranking is on gross, so opposite differences must not cancel."""
        result = compare(
            ours(by_rule={"THIN_WALL": 5, "SLOT_OVERHANG": 0}),
            theirs(by_rule={"thin_wall": 2, "slot_overhang": 3}),
        )
        self.assertEqual(result.magnitude, 6)


if __name__ == "__main__":
    unittest.main()
