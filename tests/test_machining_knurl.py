# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The knurl recognizer's measurements, and the marking note channel."""

import math
import unittest

from freecad.DFM.core.machining.features import FeatureInstance, FeatureType
from freecad.DFM.core.machining.recognizers.knurl_recognizer import (
    azimuth_span_deg,
    drop_knurl_absorbed_features,
    note_names_knurl,
    pitch_from_positions,
)
from freecad.DFM.core.machining.recognizers.marking.priors import marking_keyword_in


class PitchTests(unittest.TestCase):
    def test_flank_pairs_give_the_pitch_not_the_groove_width(self):
        # Two flanks 0.35 apart every 2.5 mm: the gaps alternate, and the
        # pitch must come out as the line spacing whatever the parity.
        positions = []
        for i in range(10):
            positions += [i * 2.5, i * 2.5 + 0.35]
        pitch, cv, lines, width = pitch_from_positions(positions, 0.0)
        self.assertAlmostEqual(pitch, 2.5, places=6)
        self.assertAlmostEqual(cv, 0.0, places=6)
        self.assertEqual(lines, 10)
        self.assertAlmostEqual(width, 0.35, places=6)

    def test_too_few_lines_is_no_spectrum(self):
        self.assertIsNone(pitch_from_positions([0.0, 0.35, 2.5, 2.85, 5.0, 5.35], 0.0))

    def test_an_interrupted_ring_has_the_closed_form_cv(self):
        # 18 lines at 15 degrees on a full circle: the bare arc enters the
        # spacing set once, so cv = 360 / (lines * pitch_deg) - 1.
        r = 7.0
        period = 2.0 * math.pi * r
        step = period * 15.0 / 360.0
        positions = [i * step for i in range(18)]
        pitch, cv, lines, _ = pitch_from_positions(positions, period)
        self.assertEqual(lines, 18)
        self.assertAlmostEqual(cv, 360.0 / (18 * 15.0) - 1.0, places=6)


class AzimuthTests(unittest.TestCase):
    def test_a_closed_band_reads_360_at_any_pitch(self):
        period = 100.0
        arcs = [(i * 10.0 - 1.0, i * 10.0 + 9.5) for i in range(10)]
        self.assertAlmostEqual(azimuth_span_deg(arcs, period), 360.0)

    def test_an_interruption_reads_its_own_width(self):
        period = 360.0
        arcs = [(a, a + 10.0) for a in range(0, 300, 10)]
        self.assertAlmostEqual(azimuth_span_deg(arcs, period), 300.0)


class AbsorptionTests(unittest.TestCase):
    def test_a_slot_inside_a_knurl_is_retired_and_a_marking_is_not(self):
        knurl = FeatureInstance("kn_0", FeatureType.KNURL, list(range(1, 21)))
        slot = FeatureInstance("s_0", FeatureType.SLOT, [3, 4, 5, 6, 99])
        keyway = FeatureInstance("s_1", FeatureType.SLOT, [3, 98, 99])
        marking = FeatureInstance("mk_0", FeatureType.MARKING_TEXT, [1, 2, 3])
        features = [slot, keyway, marking]
        drop_knurl_absorbed_features(features, [knurl])
        self.assertEqual([f.instance_id for f in features], ["s_1", "mk_0"])


class NoteTests(unittest.TestCase):
    def test_knurl_is_named_not_parsed(self):
        self.assertTrue(note_names_knurl("DIAMOND KNURL 0.8 PITCH"))
        self.assertTrue(note_names_knurl("serrated grip"))
        self.assertFalse(note_names_knurl("BREAK ALL EDGES"))

    def test_marking_keywords_match_the_operation(self):
        self.assertEqual(marking_keyword_in("Laser etch per spec"), "ETCH")
        self.assertEqual(marking_keyword_in("ENGRAVED LOGO"), "ENGRAVE")
        self.assertEqual(marking_keyword_in("SEE REMARKS ON SHEET 2"), "")
        self.assertEqual(marking_keyword_in("MARKET GRADE MATERIAL"), "")
        # IDENT is a stem: it must not match IDENTIFIED inside boilerplate.
        self.assertEqual(
            marking_keyword_in("FEATURES IDENTIFIED AS STATISTICALLY TOLERANCED"), ""
        )
        self.assertEqual(marking_keyword_in("PARTS TO BE IDENTICAL"), "")
        self.assertNotEqual(marking_keyword_in("IDENTIFICATION PLATE PER SPEC"), "")
        self.assertNotEqual(marking_keyword_in("IDENT PLATE REQUIRED"), "")


if __name__ == "__main__":
    unittest.main()
