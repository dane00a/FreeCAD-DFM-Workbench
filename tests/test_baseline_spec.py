# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the fixture-baseline comparison.

There is no geometry in this file on purpose. The comparison works on
counts, and every trap in it is a matter of bookkeeping rather than of
shape: a block that is absent versus a block that is empty, a map checked in
one direction versus both, a boolean that Python will also call an integer.
Building a solid to exercise any of those would only make the failures
harder to read.

The cases that matter most are the ones where a baseline could quietly stop
checking. A rule that fires without being listed, a feature re-classified
under a different name, a block the recorder forgot to write -- each of
those leaves every other number identical, which is exactly why they need
tests of their own.
"""

import unittest

from freecad.DFM.core.baselines.spec import (
    ENGINE_VERSION,
    BaselineSpec,
    Observation,
    check_drift,
    spec_from_observation,
    tally_features,
    tally_findings,
    tally_pmi,
)


def finding(rule, category="hole", severity="warn"):
    return {"rule_id": rule, "category": category, "severity": severity}


def feature(kind, **parameters):
    return {"type": kind, "parameters": parameters}


def observation(findings=(), features=(), pmi=None, process_type="MILLED"):
    return Observation(
        fixture="part",
        findings=list(findings),
        features=list(features),
        pmi=pmi or {},
        part_process_type=process_type,
        process="CNC Milling",
        material="Default",
    )


#: The version stamp every current baseline carries. A spec without one is a
#: pre-versioning approval and drifts on that alone, so tests about some
#: other block start from a stamped spec to keep the two questions apart.
STAMPED = {"engine_version": ENGINE_VERSION}


def approve(obs):
    """The baseline that approving a run would write."""
    return spec_from_observation(obs)


# =============================================================================


class TallyTests(unittest.TestCase):
    def test_severity_buckets_default_to_info(self):
        """A severity the baseline has no bucket for still has to be counted.

        Dropping it would make the three severity numbers stop adding up to
        the finding count, and a total that does not reconcile is worse than
        a finding in the wrong bucket.
        """
        tally = tally_findings(
            [
                finding("A", severity="error"),
                finding("B", severity="warn"),
                finding("C", severity="info"),
                finding("D", severity="something_new"),
            ]
        )
        self.assertEqual((tally.error, tally.warn, tally.info), (1, 1, 2))
        self.assertEqual(tally.total, 4)

    def test_blank_rule_and_category_are_not_counted(self):
        """An empty key would collect every unlabelled finding under ''."""
        tally = tally_findings([{"severity": "warn", "category": "", "rule_id": ""}])
        self.assertEqual(tally.by_rule, {})
        self.assertEqual(tally.by_category, {})
        self.assertEqual(tally.warn, 1)

    def test_features_count_types_and_discriminants(self):
        counts = tally_features(
            [
                feature("BEND", is_hem=False),
                feature("BEND", is_hem=True),
                feature("SHEET_FORMED", subtype="louver", open_edges=1),
            ]
        )
        self.assertEqual(
            counts,
            {
                "type_BEND": 2,
                "type_SHEET_FORMED": 1,
                "is_hem_false": 1,
                "is_hem_true": 1,
                "subtype_louver": 1,
                "open_edges_1": 1,
            },
        )

    def test_booleans_are_spelled_as_words_not_as_integers(self):
        """`True` is an `int` in Python, and `is_hem_1` would be wrong.

        Wrong quietly, too: it would still compare equal to itself, so the
        baseline would pass while carrying a key the reference engine never
        writes and no one could read.
        """
        self.assertEqual(tally_features([feature("BEND", is_hem=True)]), {
            "type_BEND": 1,
            "is_hem_true": 1,
        })

    def test_float_discriminants_are_skipped(self):
        """A float would give every geometry tweak a key of its own."""
        counts = tally_features([feature("SHEET_FORMED", subtype=1.5, open_edges=2)])
        self.assertEqual(counts, {"type_SHEET_FORMED": 1, "open_edges_2": 1})

    def test_dimensions_and_face_ids_never_reach_a_key(self):
        """The corpus is not distributable, and this is what keeps it that way."""
        counts = tally_features(
            [feature("BLIND_HOLE", diameter_mm=6.35, depth_mm=40.0, host_face=17)]
        )
        self.assertEqual(counts, {"type_BLIND_HOLE": 1})

    def test_pmi_kinds(self):
        counts = tally_pmi(
            {
                "annotations": [
                    {"type": "position", "tolerance_value_mm": 0.1},
                    {"type": "dimension_linear", "tolerance_value_mm": 0.05},
                    {"type": "dimension_linear", "tolerance_value_mm": 0.0},
                    {"type": "note"},
                    # An unclassified presentation label counts as a note,
                    # as in the reference. The key set is exactly the
                    # reference's: the old "surface_finish" key is gone.
                    {"type": "unknown"},
                ],
                "datums": [
                    {"is_target": False, "face_ids": [3]},
                    {"is_target": False, "face_ids": []},
                    {"is_target": True, "face_ids": []},
                ],
            }
        )
        self.assertEqual(
            counts,
            {
                "tol_position": 1,
                "dimension_valued": 1,
                "dimension_drawing_only": 1,
                "note": 2,
                "datum_resolved": 1,
                "datum_unresolved": 1,
                "datum_target": 1,
            },
        )


# =============================================================================


class NoDriftTests(unittest.TestCase):
    def test_a_run_matches_the_baseline_taken_from_it(self):
        obs = observation(
            findings=[finding("HOLE_DEPTH_RATIO"), finding("THIN_WALL", "part", "error")],
            features=[feature("BLIND_HOLE"), feature("POCKET", subtype="open")],
        )
        result = check_drift(approve(obs), obs)
        self.assertTrue(result.ok)
        self.assertEqual(result.mismatches, [])
        self.assertEqual(result.status, "ok")

    def test_an_empty_baseline_asserts_nothing_it_can_leave_out(self):
        """A spec parsed from `{}` asserts no count it is allowed to omit.

        The scalars and the two optional blocks all stand down. What does
        *not* stand down is the rule and category surplus check, and that
        asymmetry is deliberate: a baseline is a complete statement of
        acceptable output, so every rule not enumerated is implicitly
        forbidden. A fixture with no findings records no `by_rule` at all,
        and the day one starts firing is the day it has to be looked at.
        """
        quiet = observation(features=[feature("POCKET")])
        self.assertTrue(check_drift(BaselineSpec.from_json(dict(STAMPED)), quiet).ok)

        noisy = observation(findings=[finding("ANYTHING")])
        self.assertFalse(check_drift(BaselineSpec.from_json(dict(STAMPED)), noisy).ok)


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.before = observation(
            findings=[finding("HOLE_DEPTH_RATIO"), finding("HOLE_DEPTH_RATIO")],
            features=[feature("BLIND_HOLE"), feature("BLIND_HOLE")],
        )
        self.spec = approve(self.before)

    def test_feature_count_drift(self):
        after = observation(
            findings=self.before.findings, features=[feature("BLIND_HOLE")]
        )
        result = check_drift(self.spec, after)
        self.assertFalse(result.ok)
        self.assertIn("feature_count: expected 2, got 1 (-1)", result.mismatches)

    def test_severity_drift_reports_the_delta_with_its_sign(self):
        after = observation(
            findings=[
                finding("HOLE_DEPTH_RATIO", severity="error"),
                finding("HOLE_DEPTH_RATIO"),
            ],
            features=self.before.features,
        )
        result = check_drift(self.spec, after)
        self.assertIn("severity.error: expected 0, got 1 (+1)", result.mismatches)
        self.assertIn("severity.warn: expected 2, got 1 (-1)", result.mismatches)

    def test_a_rule_that_stops_firing_drifts(self):
        after = observation(findings=[], features=self.before.features)
        result = check_drift(self.spec, after)
        self.assertIn("by_rule.HOLE_DEPTH_RATIO: expected 2, got 0 (-2)", result.mismatches)
        self.assertIn("by_category.hole: expected 2, got 0 (-2)", result.mismatches)

    def test_process_classification_is_categorical(self):
        after = observation(
            findings=self.before.findings,
            features=self.before.features,
            process_type="TURNED",
        )
        result = check_drift(self.spec, after)
        self.assertIn(
            "part_process.type: expected MILLED, got TURNED", result.mismatches
        )

    def test_a_missing_classification_reads_as_unknown(self):
        after = observation(
            findings=self.before.findings, features=self.before.features
        )
        after.part_process_type = ""
        result = check_drift(self.spec, after)
        self.assertIn(
            "part_process.type: expected MILLED, got UNKNOWN", result.mismatches
        )

    def test_mismatches_come_out_in_a_fixed_order(self):
        """Two runs of the same drift have to print the same lines in the
        same order, or one report cannot be diffed against the last.

        Within each half the keys are sorted; the halves themselves stay in
        the order the reference engine emits them, everything the baseline
        listed before everything it did not.
        """
        after = observation(
            findings=[finding("ZZZ", "zeta"), finding("AAA", "alpha")],
            features=self.before.features,
        )
        rule_lines = [
            m for m in check_drift(self.spec, after).mismatches if m.startswith("by_rule.")
        ]
        self.assertEqual(
            rule_lines,
            [
                "by_rule.HOLE_DEPTH_RATIO: expected 2, got 0 (-2)",
                "by_rule.AAA: unexpected rule fired 1 finding(s) "
                "(not listed in expected baseline)",
                "by_rule.ZZZ: unexpected rule fired 1 finding(s) "
                "(not listed in expected baseline)",
            ],
        )


class BidirectionalTests(unittest.TestCase):
    """An approval is a complete statement, not a floor.

    Everything here would pass if the comparison only walked the baseline's
    own keys, which is the failure mode that lets a newly firing rule sit
    unnoticed in a green corpus for months.
    """

    def test_an_unlisted_rule_that_fires_is_drift(self):
        spec = approve(observation(findings=[finding("KNOWN")]))
        after = observation(findings=[finding("KNOWN"), finding("SURPRISE", "part")])
        result = check_drift(spec, after)
        self.assertFalse(result.ok)
        self.assertIn(
            "by_rule.SURPRISE: unexpected rule fired 1 finding(s) "
            "(not listed in expected baseline)",
            result.mismatches,
        )
        self.assertIn(
            "by_category.part: unexpected category fired 1 finding(s) "
            "(not listed in expected)",
            result.mismatches,
        )

    def test_a_reclassified_feature_is_drift_even_when_nothing_else_moves(self):
        """The case the feature block exists for.

        A louver read as a lance fires the same rules, at the same
        severities, over the same number of features. Every other block in
        the baseline is byte-identical.
        """
        before = observation(
            findings=[finding("SHEET_FORM_DEPTH", "sheet")],
            features=[feature("SHEET_FORMED", subtype="louver")],
        )
        after = observation(
            findings=[finding("SHEET_FORM_DEPTH", "sheet")],
            features=[feature("SHEET_FORMED", subtype="lance")],
        )
        spec = approve(before)
        self.assertEqual(spec.feature_count, 1)

        result = check_drift(spec, after)
        self.assertFalse(result.ok)
        self.assertIn("features.subtype_louver: expected 1, got 0 (-1)", result.mismatches)
        self.assertIn(
            "features.subtype_lance: unexpected feature signature appeared 1 time(s) "
            "(not listed in expected baseline)",
            result.mismatches,
        )

    def test_an_unlisted_pmi_element_is_drift(self):
        spec = approve(observation(pmi={"datums": [{"is_target": True}]}))
        after = observation(
            pmi={
                "datums": [{"is_target": True}],
                "annotations": [{"type": "flatness", "tolerance_value_mm": 0.02}],
            }
        )
        result = check_drift(spec, after)
        self.assertIn(
            "pmi.tol_flatness: unexpected PMI element appeared 1 time(s) "
            "(not listed in expected baseline)",
            result.mismatches,
        )


class MustNotFireTests(unittest.TestCase):
    def test_a_forbidden_rule_that_fires_is_named(self):
        spec = approve(observation())
        spec.must_not_fire = ["SEALED_VOID"]
        result = check_drift(spec, observation(findings=[finding("SEALED_VOID", "part")] * 3))
        self.assertFalse(result.ok)
        self.assertIn("must_not_fire.SEALED_VOID: expected 0, got 3 FIRED", result.mismatches)

    def test_a_forbidden_rule_that_stays_silent_is_fine(self):
        """Other rules firing as recorded does not trip the negative one."""
        quiet = observation(findings=[finding("OTHER")])
        spec = approve(quiet)
        spec.must_not_fire = ["SEALED_VOID"]
        self.assertTrue(check_drift(spec, quiet).ok)

    def test_hand_authored_entries_survive_re_recording(self):
        """The reference engine drops these on every save.

        Losing a deliberately written negative assertion because somebody
        approved an unrelated change is a cost with nothing on the other
        side of it, so approving carries them forward instead.
        """
        recorded = spec_from_observation(
            observation(), notes="held to 0.02", must_not_fire=["SEALED_VOID"]
        )
        self.assertEqual(recorded.notes, "held to 0.02")
        self.assertEqual(recorded.must_not_fire, ["SEALED_VOID"])


class LegacyBaselineTests(unittest.TestCase):
    """A block added after a baseline was written must not drift it.

    Both `pmi` and `features` were added to the schema after baselines
    existed. If absence read as "expected nothing", every older file would
    have gone red at once and the whole corpus would have been re-approved
    in one blind sweep -- which is the same as having no baselines at all.
    """

    def test_an_absent_features_block_is_not_checked(self):
        spec = BaselineSpec.from_json(
            {"fixture": "part", "feature_count": 2, "by_rule": {}, **STAMPED}
        )
        self.assertIsNone(spec.features)
        result = check_drift(
            spec, observation(features=[feature("POCKET"), feature("BOSS")])
        )
        self.assertTrue(result.ok, result.mismatches)

    def test_an_absent_pmi_block_is_not_checked(self):
        spec = BaselineSpec.from_json({"fixture": "part", **STAMPED})
        self.assertIsNone(spec.pmi)
        result = check_drift(
            spec, observation(pmi={"datums": [{"is_target": False, "face_ids": [1]}]})
        )
        self.assertTrue(result.ok)

    def test_an_empty_block_is_a_statement_and_is_checked(self):
        """Present-and-empty is the opposite of absent: tracked, found bare."""
        spec = BaselineSpec.from_json({"fixture": "part", "features": {}})
        self.assertEqual(spec.features, {})
        result = check_drift(spec, observation(features=[feature("POCKET")]))
        self.assertFalse(result.ok)

    def test_approving_always_writes_both_blocks(self):
        """Re-approving is the only way a legacy fixture gains them."""
        spec = spec_from_observation(observation())
        self.assertEqual(spec.pmi, {})
        self.assertEqual(spec.features, {})
        self.assertIn("pmi", spec.to_json())
        self.assertIn("features", spec.to_json())


class RoundTripTests(unittest.TestCase):
    def test_absent_and_empty_survive_a_round_trip(self):
        """If `to_json` wrote nulls, one reload would erase the distinction."""
        legacy = BaselineSpec.from_json({"fixture": "part"})
        self.assertIsNone(BaselineSpec.from_json(legacy.to_json()).pmi)

        tracked = spec_from_observation(observation())
        self.assertEqual(BaselineSpec.from_json(tracked.to_json()).pmi, {})

    def test_every_asserted_number_survives_a_round_trip(self):
        original = spec_from_observation(
            observation(
                findings=[finding("A", "hole", "error"), finding("B", "part", "info")],
                features=[feature("BEND", is_hem=True)],
            ),
            notes="why this part looks like this",
            must_not_fire=["C"],
        )
        again = BaselineSpec.from_json(original.to_json())
        self.assertEqual(again.to_json(), original.to_json())
        self.assertTrue(check_drift(again, observation(
            findings=[finding("A", "hole", "error"), finding("B", "part", "info")],
            features=[feature("BEND", is_hem=True)],
        )).ok)

    def test_the_process_the_run_used_is_recorded_but_never_asserted(self):
        """It is an input, not an output. A baseline cannot drift from it."""
        spec = approve(observation())
        self.assertEqual(spec.process, "CNC Milling")
        after = observation()
        after.process = "Sheet Metal Fabrication"
        self.assertTrue(check_drift(spec, after).ok)


if __name__ == "__main__":
    unittest.main()


class EngineVersionTests(unittest.TestCase):
    """A baseline says which engine it was approved under, as the reference's do."""

    def test_approving_stamps_the_engine_version(self):
        spec = approve(observation())
        self.assertEqual(spec.engine_version, ENGINE_VERSION)
        self.assertEqual(spec.to_json()["engine_version"], ENGINE_VERSION)

    def test_an_unversioned_baseline_asks_to_be_re_approved(self):
        """An approval with no stamp could date from anywhere in history."""
        result = check_drift(BaselineSpec.from_json({"fixture": "part"}), observation())
        self.assertFalse(result.ok)
        self.assertEqual(
            result.mismatches,
            [
                "engine_version: baseline records none (approved before engine "
                f"versioning; engine is now {ENGINE_VERSION}) -- re-approve to record it"
            ],
        )

    def test_a_different_engine_version_is_drift(self):
        spec = BaselineSpec.from_json({"fixture": "part", "engine_version": "1.9.0"})
        result = check_drift(spec, observation())
        self.assertEqual(
            result.mismatches,
            [
                "engine_version: baseline approved under 1.9.0, "
                f"engine is {ENGINE_VERSION} -- re-approve after reviewing"
            ],
        )


class BendFamilyTests(unittest.TestCase):
    def test_bend_family_is_a_discriminant(self):
        """Which fold geometry a bend is decides which sheet rules can see it."""
        counts = tally_features(
            [
                feature("BEND", is_hem=False, bend_family="cylindrical"),
                feature("BEND", is_hem=False, bend_family="conical"),
            ]
        )
        self.assertEqual(counts["bend_family_cylindrical"], 1)
        self.assertEqual(counts["bend_family_conical"], 1)
        self.assertEqual(counts["type_BEND"], 2)


class InertMetricsTests(unittest.TestCase):
    """The reference keeps a retired `metrics` block, unasserted and unchanged."""

    BLOCK = {
        "flat_area_mm2": {
            "value": 1234.5,
            "tol": 0.0001,
            "res": 0.001,
            "source": "measured (--batch-fixtures); not independently verified",
        }
    }

    def test_metrics_survive_a_round_trip_byte_for_byte(self):
        spec = BaselineSpec.from_json({"fixture": "part", "metrics": self.BLOCK})
        self.assertEqual(spec.to_json()["metrics"], self.BLOCK)

    def test_metrics_are_never_compared(self):
        spec = BaselineSpec.from_json(
            {"fixture": "part", "metrics": self.BLOCK, **STAMPED}
        )
        self.assertTrue(check_drift(spec, observation()).ok)

    def test_approving_never_creates_a_metrics_block(self):
        self.assertNotIn("metrics", approve(observation()).to_json())
