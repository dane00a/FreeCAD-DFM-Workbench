# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for thread callouts read from STEP presentation text and aspect names.

The text layer runs on small synthetic STEP files, with the part's bores and
its entity-to-face lookup handed in as plain data. The end-to-end read runs on
the PMI fixtures and skips when a file is not installed.
"""

import os
import unittest

from freecad.DFM.core.machining.annotations import ThreadCallout
from freecad.DFM.core.machining.pmi.step_text import StepTextIndex
from freecad.DFM.core.machining.pmi.thread_text import (
    Bore,
    detect_designation,
    extract_text_callouts,
    merge_text_callouts,
)

STEP_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "step_pmi")


def step_path(name):
    return os.path.join(STEP_DIR, name + ".step")


def needs(name):
    return unittest.skipUnless(
        os.path.isfile(step_path(name)), f"{name}.step is not installed"
    )


def index_of(body: str) -> StepTextIndex:
    index = StepTextIndex()
    index.parse("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n" + body + "\nENDSEC;\n")
    return index


def lookup(table):
    """faces_for over a fixed table; ids absent from it are not entities."""
    return lambda step_id: table.get(step_id)


# A "2X M12x1.75-6H" callout drawn the CATIA/NX way: the text hangs off a
# validation property, the callout is tied to a DIMENSIONAL_SIZE, and the
# dimension applies to a composite group of two bore aspects.
_PRESENTATION = r"""
#1=PRODUCT_DEFINITION_SHAPE('','',#2);
#10=SHAPE_ASPECT('bore 1','',#1,.T.);
#11=SHAPE_ASPECT('bore 2','',#1,.T.);
#12=COMPOSITE_GROUP_SHAPE_ASPECT('2X','',#1,.T.);
#13=SHAPE_ASPECT_RELATIONSHIP('','',#12,#10);
#14=SHAPE_ASPECT_RELATIONSHIP('','',#12,#11);
#20=GEOMETRIC_ITEM_SPECIFIC_USAGE('','',#10,#90,#100);
#21=GEOMETRIC_ITEM_SPECIFIC_USAGE('','',#11,#90,#101);
#30=DIMENSIONAL_SIZE(#12,'diameter');
#31=DRAUGHTING_MODEL_ITEM_ASSOCIATION('','',#30,#91,#32);
#32=DRAUGHTING_CALLOUT('',(#95));
#33=CHARACTERIZED_ITEM_WITHIN_REPRESENTATION('','',#32,#92);
#34=PROPERTY_DEFINITION('pmi validation property','',#33);
#35=PROPERTY_DEFINITION_REPRESENTATION(#34,#36);
#36=REPRESENTATION('',(#37),#93);
#37=DESCRIPTIVE_REPRESENTATION_ITEM('equivalent unicode string','DIM\w2X M12x1.75-6H');
"""

# OCCT's own writer: the callout is the name of the aspect.
_ASPECT_NAMES = r"""
#40=SHAPE_ASPECT('M8x1.25 - 6H','',#1,.T.);
#41=GEOMETRIC_ITEM_SPECIFIC_USAGE('','',#40,#90,#102);
#42=SHAPE_ASPECT('M8x1.25 - 6H','',#1,.T.);
#43=GEOMETRIC_ITEM_SPECIFIC_USAGE('','',#42,#90,#103);
#44=SHAPE_ASPECT('0.03 MMC to A|B|C','',#1,.T.);
#45=GEOMETRIC_ITEM_SPECIFIC_USAGE('','',#44,#90,#104);
"""

# A callout pointing at a cosmetic thread cylinder rather than the bore: two
# circles on the axis of a bore at x=10, z from 0 down to -25. The same curve
# set brackets 18 mm of thread.
_COSMETIC = r"""
#210=SHAPE_ASPECT('M12x1.75 - 6H','',#1,.T.);
#211=GEOMETRIC_ITEM_SPECIFIC_USAGE('','',#210,#90,#200);
#200=GEOMETRIC_CURVE_SET('Thread Cylinder.1',(#201,#202));
#201=CIRCLE('',#203,6.);
#203=AXIS2_PLACEMENT_3D('',#204,#205,#206);
#204=CARTESIAN_POINT('',(10.,0.,-2.));
#202=CIRCLE('',#207,6.);
#207=AXIS2_PLACEMENT_3D('',#208,#205,#206);
#208=CARTESIAN_POINT('',(10.,0.,-20.));
#205=DIRECTION('',(0.,0.,1.));
#206=DIRECTION('',(1.,0.,0.));
"""

_BORE = Bore(
    origin=(10.0, 0.0, 0.0),
    direction=(0.0, 0.0, 1.0),
    radius=5.1,
    bounds=(4.9, -5.1, -25.0, 15.1, 5.1, 0.0),
    face_id=9,
)
# Coaxial with the first, but well below it: the callout is not its.
_LOWER_BORE = Bore(
    origin=(10.0, 0.0, 0.0),
    direction=(0.0, 0.0, 1.0),
    radius=5.1,
    bounds=(4.9, -5.1, -80.0, 15.1, 5.1, -60.0),
    face_id=11,
)


class DesignationTest(unittest.TestCase):
    def test_metric(self):
        self.assertEqual(detect_designation("DIM\\w4X M12x1.75-6H"), "M12x1.75")
        self.assertEqual(detect_designation("M6x1.0 - 6H"), "M6x1")
        self.assertEqual(detect_designation("M8"), "M8")

    def test_imperial_needs_its_series(self):
        self.assertEqual(detect_designation("1/4-20 unc"), "1/4-20 UNC")
        # The leading word boundary cannot sit before a '#', so a numbered
        # size loses its hash -- as the reference's regex does.
        self.assertEqual(detect_designation("#10-32 UNF"), "10-32 UNF")
        self.assertEqual(detect_designation("1/4-20"), "")

    def test_words_are_not_threads(self):
        self.assertEqual(detect_designation("0.03 MMC to A|B|C"), "")
        self.assertEqual(detect_designation("ROOM20"), "")
        self.assertEqual(detect_designation(""), "")


class PresentationTextTest(unittest.TestCase):
    def test_the_callout_walks_up_to_its_dimension_and_down_to_each_bore(self):
        found = extract_text_callouts(
            index_of(_PRESENTATION), [], lookup({100: [5], 101: [6]})
        )
        self.assertEqual(sorted(found), [5, 6])
        for callout in found.values():
            self.assertEqual(callout.designation, "M12x1.75")
            self.assertEqual(callout.nominal_mm, 12.0)
            self.assertEqual(callout.pitch_mm, 1.75)
            self.assertEqual(callout.source, "presentation_text")
            self.assertEqual(callout.thread_depth_mm, 0.0)

    def test_a_callout_with_no_dimension_behind_it_says_nothing(self):
        orphan = "#37=DESCRIPTIVE_REPRESENTATION_ITEM('x','M12x1.75');"
        self.assertEqual(extract_text_callouts(index_of(orphan), [], lookup({})), {})


class AspectNameTest(unittest.TestCase):
    def test_an_aspect_named_for_a_thread_threads_its_face(self):
        found = extract_text_callouts(
            index_of(_PRESENTATION + _ASPECT_NAMES),
            [],
            lookup({100: [5], 101: [6], 102: [6], 103: [8], 104: [10]}),
        )
        # Face 6 is named by both: the presentation text wins.
        self.assertEqual(found[6].designation, "M12x1.75")
        self.assertEqual(found[6].source, "presentation_text")
        self.assertEqual(found[8].designation, "M8x1.25")
        self.assertEqual(found[8].source, "shape_aspect_name")
        # A tolerance aspect is not a thread.
        self.assertNotIn(10, found)

    def test_an_id_that_is_no_entity_is_skipped(self):
        found = extract_text_callouts(index_of(_ASPECT_NAMES), [], lookup({103: [8]}))
        self.assertEqual(sorted(found), [8])


class CosmeticThreadTest(unittest.TestCase):
    def test_the_thread_cylinder_finds_the_bore_on_its_axis(self):
        found = extract_text_callouts(
            index_of(_COSMETIC), [_BORE, _LOWER_BORE], lookup({200: []})
        )
        self.assertEqual(sorted(found), [9])
        self.assertEqual(found[9].designation, "M12x1.75")

    def test_thread_planes_give_the_tapped_depth(self):
        found = extract_text_callouts(index_of(_COSMETIC), [_BORE], lookup({200: []}))
        self.assertAlmostEqual(found[9].thread_depth_mm, 18.0)

    def test_an_off_axis_bore_is_not_claimed(self):
        elsewhere = Bore(
            origin=(40.0, 0.0, 0.0),
            direction=(0.0, 0.0, 1.0),
            radius=5.1,
            bounds=(34.9, -5.1, -25.0, 45.1, 5.1, 0.0),
            face_id=12,
        )
        found = extract_text_callouts(index_of(_COSMETIC), [elsewhere], lookup({200: []}))
        self.assertEqual(found, {})


class MergeTest(unittest.TestCase):
    def test_semantic_designations_win_but_take_the_modelled_depth(self):
        shared = ThreadCallout("M10x1.5", 10.0, 1.5, source="semantic_name")
        semantic = {1: shared, 2: shared}
        text = {
            1: ThreadCallout("M12x1.75", 12.0, 1.75, 25.0, "presentation_text"),
            3: ThreadCallout("M8x1.25", 8.0, 1.25, 0.0, "shape_aspect_name"),
        }
        merged = merge_text_callouts(semantic, text)
        self.assertEqual(merged[1].designation, "M10x1.5")
        self.assertEqual(merged[1].thread_depth_mm, 25.0)
        # The other face of the same dimension keeps no depth.
        self.assertEqual(merged[2].thread_depth_mm, 0.0)
        self.assertEqual(merged[3].designation, "M8x1.25")


class FixtureTest(unittest.TestCase):
    @staticmethod
    def threads_of(name):
        from freecad.DFM.core.baselines.step_fixtures import read_step
        from freecad.DFM.core.machining.pmi import read_step_pmi

        path = step_path(name)
        return read_step_pmi(path, read_step(path)).threads

    @needs("nist_ctc_04")
    def test_nist_ctc_04_four_m12_holes_from_presentation_text(self):
        threads = self.threads_of("nist_ctc_04")
        m12 = [c for c in threads.values() if c.designation == "M12x1.75"]
        self.assertGreaterEqual(len(m12), 4)
        self.assertTrue(all(c.source == "presentation_text" for c in m12))
        # 25 mm of modelled thread in a 30 mm bore.
        self.assertTrue(any(abs(c.thread_depth_mm - 25.0) < 1e-6 for c in m12))

    @needs("pmi_torture_gauntlet")
    def test_torture_gauntlet_threads_from_aspect_names(self):
        threads = self.threads_of("pmi_torture_gauntlet")
        designations = {c.designation for c in threads.values()}
        self.assertTrue(
            {"M5x0.8", "M6x1", "M8x1.25", "M10x1.5", "M12x1.75"} <= designations
        )
        self.assertTrue(all(c.source == "shape_aspect_name" for c in threads.values()))


if __name__ == "__main__":
    unittest.main()


class ExternalThreadFromPmiTest(unittest.TestCase):
    """A callout on the outside of a shaft reaches the external-thread pass."""

    @staticmethod
    def run_with(callout_for_side):
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
        from OCP.GeomAbs import GeomAbs_Cylinder
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

        from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
        from freecad.DFM.core.machining.annotations import AnnotationSet
        from freecad.DFM.core.machining.features import FeatureType
        from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex

        shaft = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 6.0, 30.0
        ).Shape()
        faces = FaceIndex(shaft)
        side = next(
            i
            for i in range(1, len(faces) + 1)
            if BRepAdaptor_Surface(faces.face_at(i)).GetType() == GeomAbs_Cylinder
        )
        annotations = AnnotationSet()
        annotations.threads = {side: callout_for_side}
        context = list(
            MachiningAnalyzer()
            .execute(shaft, faces, EdgeIndex(shaft), prefs={}, annotations=annotations)
            .values()
        )[0]
        return side, context.recognition.of_type(FeatureType.EXTERNAL_THREAD)

    def test_a_callout_of_the_shaft_diameter_is_its_thread(self):
        side, found = self.run_with(
            ThreadCallout("M12x1.75", 12.0, 1.75, source="shape_aspect_name")
        )
        self.assertEqual(len(found), 1)
        thread = found[0]
        self.assertIn(side, thread.faces)
        self.assertEqual(thread.param("thread_designation"), "M12x1.75")
        self.assertEqual(thread.param("thread_evidence"), "pmi")
        self.assertAlmostEqual(thread.number("length_mm"), 30.0)

    def test_a_callout_of_another_diameter_is_not(self):
        _, found = self.run_with(
            ThreadCallout("M20x2.5", 20.0, 2.5, source="presentation_text")
        )
        self.assertEqual(found, [])
