# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the feature census.

The census is the analysis handed to somebody who is not going to open the
workbench -- an estimator with a spreadsheet. What matters is that it has the
same columns every time, so this month's part can be compared with last
month's, and that a parameter a feature does not have comes out blank rather
than as a zero somebody will later average.
"""

import csv
import os
import tempfile
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.census import (
    CENSUS_COLUMNS,
    census_rows,
    census_summary,
    write_census,
)
from freecad.DFM.core.machining.features import FeatureInstance
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


def sample_features() -> list[FeatureInstance]:
    return [
        FeatureInstance(
            instance_id="h_0",
            type="THROUGH_HOLE",
            faces=[3, 5, 7],
            parameters={"diameter_mm": 6.0, "depth_mm": 40.0, "is_through": True},
        ),
        FeatureInstance(
            instance_id="p_0",
            type="POCKET",
            faces=[9, 10],
            parameters={"width_mm": 20.0, "depth_mm": 12.0, "corner_radius_mm": 0.0},
        ),
    ]


def drilled_block():
    block = BRepPrimAPI_MakeBox(80.0, 80.0, 40.0).Shape()
    drill = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(40, 40, -1), gp_Dir(0, 0, 1)), 6.0, 42.0
    ).Shape()
    operation = BRepAlgoAPI_Cut(block, drill)
    operation.Build()
    return operation.Shape()


class TestCensusShape(unittest.TestCase):
    def test_the_header_is_the_declared_columns(self):
        self.assertEqual(census_rows(sample_features())[0], list(CENSUS_COLUMNS))

    def test_every_row_has_every_column(self):
        rows = census_rows(sample_features())
        for row in rows:
            self.assertEqual(len(row), len(CENSUS_COLUMNS))

    def test_the_columns_do_not_vary_with_the_part(self):
        # A census whose shape changes per part cannot be put beside last
        # month's, which is the only thing anyone does with one.
        first = census_rows(sample_features())[0]
        second = census_rows([sample_features()[0]])[0]
        self.assertEqual(first, second)
        self.assertEqual(census_rows([])[0], first)

    def test_an_absent_parameter_is_blank_not_zero(self):
        # A pocket has no diameter. Writing 0.00 there invites somebody to
        # average it into a mean hole size.
        rows = census_rows(sample_features())
        diameter = CENSUS_COLUMNS.index("diameter_mm")
        self.assertEqual(rows[2][diameter], "")

    def test_booleans_read_as_words(self):
        rows = census_rows(sample_features())
        through = CENSUS_COLUMNS.index("is_through")
        self.assertEqual(rows[1][through], "yes")

    def test_faces_are_listed(self):
        rows = census_rows(sample_features())
        faces = CENSUS_COLUMNS.index("faces")
        self.assertEqual(rows[1][faces], "3 5 7")
        self.assertEqual(rows[1][CENSUS_COLUMNS.index("face_count")], "3")


class TestCensusSummary(unittest.TestCase):
    def test_the_commonest_kind_comes_first(self):
        features = sample_features() + [
            FeatureInstance(instance_id="h_1", type="THROUGH_HOLE", faces=[11])
        ]
        self.assertTrue(census_summary(features).startswith("2 through hole"))

    def test_an_empty_part_says_so(self):
        self.assertEqual(census_summary([]), "no features recognized")


class TestCensusFile(unittest.TestCase):
    def test_a_written_census_reads_back(self):
        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        try:
            write_census(path, sample_features())
            with open(path, newline="", encoding="utf-8") as opened:
                rows = list(csv.reader(opened))
        finally:
            os.unlink(path)
        self.assertEqual(rows[0], list(CENSUS_COLUMNS))
        self.assertEqual(len(rows), 3)


class TestCensusFromAnalysis(unittest.TestCase):
    def test_a_real_analysis_produces_a_census(self):
        shape = drilled_block()
        context = list(
            MachiningAnalyzer()
            .execute(shape, FaceIndex(shape), EdgeIndex(shape), prefs={})
            .values()
        )[0]
        rows = census_rows(context.recognition.features)
        self.assertGreater(len(rows), 1, "a drilled block has at least one feature")
        self.assertIn("hole", census_summary(context.recognition.features))

    def test_face_ids_in_the_census_exist_on_the_model(self):
        # The census is only useful if its face ids point at the same faces
        # the viewport highlights.
        shape = drilled_block()
        face_index = FaceIndex(shape)
        context = list(
            MachiningAnalyzer()
            .execute(shape, face_index, EdgeIndex(shape), prefs={})
            .values()
        )[0]
        for feature in context.recognition.features:
            for face_id in feature.faces:
                self.assertGreaterEqual(face_id, 1)
                self.assertLessEqual(face_id, len(face_index))


if __name__ == "__main__":
    unittest.main()
