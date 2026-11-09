# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for reading semantic PMI out of STEP files.

Two kinds. The STEP-text index and the seam that carries callouts into the
analysis are tested on synthetic input, so they run anywhere. The XDE reader
is tested against the PMI fixtures in ``tests/fixtures/step_pmi`` -- the files
are not distributed, so each of those tests skips when its file is absent --
and the expected numbers are the reference engine's own ``gdt.json`` for the
same file.
"""

import os
import unittest

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from freecad.DFM.core.analyzers.machining_analyzer import CONTEXT_KEY, MachiningAnalyzer
from freecad.DFM.core.baselines.observe import pmi_record
from freecad.DFM.core.baselines.spec import tally_pmi
from freecad.DFM.core.machining.annotations import (
    AnnotationSet,
    Datum,
    FeatureControlFrame,
    ToleranceType,
    annotations_for,
    set_annotation_source,
)
from freecad.DFM.core.machining.pmi.step_text import StepTextIndex
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex

STEP_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "step_pmi")


def step_path(name):
    return os.path.join(STEP_DIR, name + ".step")


def needs(name):
    return unittest.skipUnless(
        os.path.isfile(step_path(name)), f"{name}.step is not installed"
    )


_SAMPLE = """ISO-10303-21;
HEADER;
FILE_NAME('#99=NOT_AN_ENTITY;',$);
ENDSEC;
DATA;
#1=DATUM('A face; with #2 in it','',#5,.F.,'A');
#2=(
LENGTH_MEASURE_WITH_UNIT()
MEASURE_WITH_UNIT(LENGTH_MEASURE(0.75),#3)
REPRESENTATION_ITEM('it''s')
);
#3=(CONVERSION_BASED_UNIT('INCH',#4)LENGTH_UNIT()NAMED_UNIT(#6));
#4=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#7);
#7=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));
ENDSEC;
END-ISO-10303-21;
"""


class TestStepTextIndex(unittest.TestCase):
    def setUp(self):
        self.index = StepTextIndex()
        self.index.parse(_SAMPLE)

    def test_the_header_is_not_read(self):
        self.assertIsNone(self.index.get(99))

    def test_a_literal_hides_its_hashes_and_semicolons(self):
        datum = self.index.get(1)
        self.assertEqual(datum.type, "DATUM")
        self.assertEqual(datum.refs, [5])
        self.assertEqual(datum.strings[0], "A face; with #2 in it")
        self.assertEqual(datum.strings[-1], "A")

    def test_a_complex_instance_has_no_type_and_keeps_its_arguments(self):
        measure = self.index.get(2)
        self.assertEqual(measure.type, "")
        self.assertEqual(measure.reals, [0.75])
        self.assertEqual(measure.refs, [3])
        self.assertEqual(measure.strings, ["it's"])

    def test_referrers(self):
        self.assertEqual(self.index.referrers(3), [2])

    def test_a_unit_resolves_to_millimetres(self):
        from freecad.DFM.core.machining.pmi.xde import measure_value_mm, unit_to_mm

        self.assertAlmostEqual(unit_to_mm(self.index, 7), 1.0)
        self.assertAlmostEqual(unit_to_mm(self.index, 3), 25.4)
        self.assertAlmostEqual(measure_value_mm(self.index, 2), 0.75 * 25.4)


class TestAnnotationsReachTheAnalysis(unittest.TestCase):
    """The seam: callouts handed to the analyzer come out of the context."""

    def tearDown(self):
        set_annotation_source(None)

    @staticmethod
    def _context(**kwargs):
        shape = BRepPrimAPI_MakeBox(50.0, 40.0, 30.0).Shape()
        data = MachiningAnalyzer().execute(
            shape, FaceIndex(shape), EdgeIndex(shape), prefs={}, **kwargs
        )
        return data[CONTEXT_KEY]

    def test_carried_callouts_are_what_the_rules_read(self):
        carried = AnnotationSet(datums=[Datum("datum_1", "A", face_ids=[1])])
        self.assertIs(annotations_for(self._context(annotations=carried)), carried)

    def test_carried_callouts_outrank_an_installed_source(self):
        carried = AnnotationSet(datums=[Datum("datum_1", "A")])
        set_annotation_source(lambda ctx: AnnotationSet())
        self.assertIs(annotations_for(self._context(annotations=carried)), carried)

    def test_a_document_part_behaves_as_before(self):
        self.assertTrue(annotations_for(self._context()).is_empty)


class TestTallyShape(unittest.TestCase):
    def test_notes_and_finishes_are_not_counted_but_unclassified_labels_are(self):
        callouts = AnnotationSet(
            frames=[
                FeatureControlFrame("gdt_1", type=ToleranceType.PROFILE_OF_A_SURFACE,
                                    tolerance_value_mm=0.5),
                FeatureControlFrame("gdt_2", type=ToleranceType.UNKNOWN),
                FeatureControlFrame("gdt_3", type=ToleranceType.DIMENSION_DIAMETER,
                                    tolerance_value_mm=12.0),
            ],
        )
        self.assertEqual(
            tally_pmi(pmi_record(callouts)),
            {"tol_profile_surface": 1, "note": 1, "dimension_valued": 1},
        )


class _FixtureCase(unittest.TestCase):
    NAME = ""
    _cache = {}

    @classmethod
    def gdt(cls):
        if cls.NAME not in cls._cache:
            from freecad.DFM.core.baselines.step_fixtures import read_step
            from freecad.DFM.core.machining.pmi.xde import read_gdt

            path = step_path(cls.NAME)
            cls._cache[cls.NAME] = read_gdt(path, read_step(path))
        return cls._cache[cls.NAME]


@needs("pmi_flatness_cover_plate")
class TestFlatnessCoverPlate(_FixtureCase):
    NAME = "pmi_flatness_cover_plate"

    def test_the_two_tolerances_and_their_datum(self):
        result = self.gdt()
        self.assertEqual(
            [(a.type, round(a.tolerance_value, 4), a.datum_refs) for a in result.annotations],
            [("flatness", 0.015, []), ("parallelism", 0.05, ["A"])],
        )
        self.assertEqual([(d.label, len(d.face_ids)) for d in result.datums], [("A", 1)])

    def test_the_toleranced_face_is_a_face_of_the_analysis_shape(self):
        from freecad.DFM.core.baselines.step_fixtures import read_step

        shape = read_step(step_path(self.NAME))
        count = len(FaceIndex(shape))
        for annotation in self.gdt().annotations:
            for face_id in annotation.face_ids:
                self.assertTrue(1 <= face_id <= count)


@needs("nist_ctc_01")
class TestCtc01(_FixtureCase):
    NAME = "nist_ctc_01"

    def test_tolerance_values_lost_by_the_kernel_are_recovered(self):
        # OCCT 7.8 reads every magnitude here as zero (they are complex
        # measure entities); the reference's 8.0 reads these.
        values = [round(a.tolerance_value, 3) for a in self.gdt().annotations
                  if a.category not in ("dimension", "other")]
        self.assertEqual(values, [0.75, 0.75, 1.25, 0.5, 1.5, 0.2])

    def test_two_face_datums_bind_through_the_item_usage(self):
        datums = {d.label: len(d.face_ids) for d in self.gdt().datums}
        self.assertEqual(datums, {"A": 1, "B": 2, "C": 2})

    def test_drawing_only_dimensions_survive_and_duplicates_do_not(self):
        from freecad.DFM.core.machining.pmi.gdt_result import to_annotation_set

        counts = tally_pmi(pmi_record(to_annotation_set(self.gdt())))
        self.assertEqual(counts["dimension_valued"], 8)
        self.assertEqual(counts["dimension_drawing_only"], 5)


@needs("nist_ftc_06")
class TestFtc06(_FixtureCase):
    NAME = "nist_ftc_06"

    def test_dimensions_are_millimetres(self):
        # Inch measures carrying a precision qualifier: 7.8 leaves them in
        # inches, 8.0 converts. 0.5 in is the first dimension.
        first = next(a for a in self.gdt().annotations if a.category == "dimension")
        self.assertAlmostEqual(first.tolerance_value, 12.7, places=3)

    def test_targets_the_importer_dropped_are_targets(self):
        targets = sum(1 for d in self.gdt().datums if d.is_target)
        self.assertEqual(targets, 6)


if __name__ == "__main__":
    unittest.main()
