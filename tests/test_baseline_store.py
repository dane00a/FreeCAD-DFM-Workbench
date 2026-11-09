# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the two ends of the baseline: what goes in, and where it lives.

The comparison itself is covered next door on hand-written counts. What is
left is the pair of translations either side of it -- the workbench's own
findings and features reduced to those counts, and the counts written to
disk and read back -- and both have a way of going wrong that the
comparison cannot catch, because a translation that loses something loses it
from both sides at once and the two still agree.
"""

import json
import os
import tempfile
import unittest

from freecad.DFM.core.baselines.observe import observe
from freecad.DFM.core.baselines.spec import check_drift, spec_from_observation
from freecad.DFM.core.baselines.store import BaselineStore
from freecad.DFM.core.models import CheckResult, Severity
from freecad.DFM.core.rules import Rulebook


def result(rule, severity=Severity.WARNING):
    return CheckResult(
        rule_id=rule,
        severity=severity,
        overview="",
        message="the wall is 0.8 mm thick at the boss",
        ignore=False,
        value=0.8,
        limit=1.5,
    )


class FakeFeature:
    def __init__(self, kind, **parameters):
        self.type = kind
        self.parameters = parameters


class FakeProcess:
    def __init__(self, value):
        self.value = value


class FakeContext:
    """Only the two attributes the observer reads, so that the test does not
    have to build an adjacency graph to check a dictionary."""

    def __init__(self, features, process_type="MILLED"):
        self.recognition = type("R", (), {"features": features})()
        self.part_process = type("P", (), {"type": FakeProcess(process_type)})()


# =============================================================================


class ObserveTests(unittest.TestCase):
    def test_a_finding_is_reduced_to_severity_family_and_rule(self):
        observation = observe("part", [result(Rulebook.MIN_WALL_THICKNESS)])
        self.assertEqual(
            observation.findings,
            [
                {
                    "severity": "warn",
                    "category": Rulebook.MIN_WALL_THICKNESS.value.family.value,
                    "rule_id": "MIN_WALL_THICKNESS",
                }
            ],
        )

    def test_the_measured_value_and_message_are_not_recorded(self):
        """Neither is durable, and one of them is a dimension off a part
        that is deliberately not in this repository."""
        record = observe("part", [result(Rulebook.MIN_WALL_THICKNESS)]).findings[0]
        self.assertEqual(set(record), {"severity", "category", "rule_id"})

    def test_severities_are_spelled_the_way_the_baseline_stores_them(self):
        severities = [
            observe("p", [result(Rulebook.SEALED_VOID, s)]).findings[0]["severity"]
            for s in (Severity.ERROR, Severity.WARNING, Severity.INFO, Severity.SUCCESS)
        ]
        self.assertEqual(severities, ["error", "warn", "info", "info"])

    def test_a_run_with_no_machining_context_records_no_features(self):
        """A process built entirely of draft and thickness rules never makes
        one, and saying so plainly beats guessing at a census."""
        observation = observe("part", [], context=None)
        self.assertEqual(observation.features, [])
        self.assertEqual(observation.part_process_type, "UNKNOWN")

    def test_features_carry_their_whole_parameter_map(self):
        """Filtering here as well as in the tally would give one decision two
        homes, and eventually two answers."""
        context = FakeContext([FakeFeature("BEND", is_hem=True, radius_mm=2.0)])
        observation = observe("part", [], context=context)
        self.assertEqual(
            observation.features,
            [{"type": "BEND", "parameters": {"is_hem": True, "radius_mm": 2.0}}],
        )
        self.assertEqual(observation.part_process_type, "MILLED")

    def test_an_absent_annotation_source_still_produces_a_tracked_block(self):
        """Nothing supplies PMI today. The empty block is written anyway, so
        that the day something does, one fixture drifts rather than all of
        them at once."""
        spec = spec_from_observation(observe("part", []))
        self.assertEqual(spec.pmi, {})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "dfm_baseline.json")

    def observation(self, fixture, rule=Rulebook.MIN_WALL_THICKNESS):
        return observe(fixture, [result(rule)], process="CNC Milling")

    def test_a_missing_file_is_an_empty_store_rather_than_an_error(self):
        self.assertEqual(len(BaselineStore(self.path)), 0)

    def test_approving_one_fixture_leaves_the_others_alone(self):
        """The trap `generate_fixtures.py` avoids next door: re-recording one
        recipe must not quietly drop the two hundred that were not run."""
        store = BaselineStore(self.path)
        store.approve(spec_from_observation(self.observation("a")))
        store.approve(spec_from_observation(self.observation("b")))
        store.save()

        again = BaselineStore(self.path)
        again.approve(spec_from_observation(self.observation("a", Rulebook.SEALED_VOID)))
        again.save()

        self.assertEqual(BaselineStore(self.path).names(), ["a", "b"])

    def test_a_saved_baseline_matches_the_run_it_came_from(self):
        observation = self.observation("a")
        store = BaselineStore(self.path)
        store.approve(spec_from_observation(observation))
        store.save()
        self.assertTrue(check_drift(BaselineStore(self.path).get("a"), observation).ok)

    def test_the_key_is_the_authority_on_the_name(self):
        """A hand-edited file whose entry disagrees with its key must not
        produce a spec that looks up under one name and reports another."""
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"a": {"fixture": "something_else"}}, handle)
        self.assertEqual(BaselineStore(self.path).get("a").fixture, "a")

    def test_forgetting_a_baseline_makes_the_fixture_unapproved_again(self):
        store = BaselineStore(self.path)
        store.approve(spec_from_observation(self.observation("a")))
        self.assertTrue(store.forget("a"))
        self.assertFalse(store.forget("a"))
        store.save()
        self.assertIsNone(BaselineStore(self.path).get("a"))

    def test_the_file_is_written_sorted(self):
        """One large file only stays diffable if a change to one fixture
        touches one fixture's lines."""
        store = BaselineStore(self.path)
        for name in ("zeta", "alpha", "mid"):
            store.approve(spec_from_observation(self.observation(name)))
        store.save()
        with open(self.path, encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(list(written), ["alpha", "mid", "zeta"])


if __name__ == "__main__":
    unittest.main()
