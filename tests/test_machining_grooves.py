# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for groove and seal-gland recognition.

The refusals matter more than the recognitions here. A cylinder sitting
between two shoulders is the signature of a groove *and* of a plain bore
between two counterbores, and the only thing separating them is which way the
step goes. Claiming the second as a groove would lose a real hole, so most of
these tests are about geometry the recognizer must decline.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.features import GROOVE_TYPES, FeatureType
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _cyl(x, y, z, radius, height) -> TopoDS_Shape:
    axis = gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1))
    return BRepPrimAPI_MakeCylinder(axis, radius, height).Shape()


def _box(p0, p1) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def block() -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(80.0, 80.0, 60.0).Shape()


def shaft() -> TopoDS_Shape:
    return _cyl(0, 0, 0, 20.0, 120.0)


def analyse(shape, prefs=None):
    return MachiningAnalyzer().execute(
        shape, FaceIndex(shape), EdgeIndex(shape), prefs=prefs or {}
    )


def grooves_in(shape):
    context = list(analyse(shape).values())[0]
    return context.recognition.of_type(*GROOVE_TYPES)


def rule_check(shape, rule, limit="N/A", prefs=None):
    check_class = get_check_class(rule)
    assert check_class is not None
    return check_class().run_check(
        analyse(shape, prefs),
        RuleLimit(target="N/A", limit=limit, binary_severity="WARNING"),
        rule,
        feedback=RuleFeedback(),
    )


# -- shapes -------------------------------------------------------------------


def _ring(z, outer, inner, height, x=0.0, y=0.0) -> TopoDS_Shape:
    """The tool that cuts an annular channel: a tube of material to remove."""
    return _cut(
        _cyl(x, y, z, outer, height), _cyl(x, y, z - 1.0, inner, height + 2.0)
    )


def make_oring_gland() -> TopoDS_Shape:
    """4 mm wide, 2 mm deep: squarely in the AS568 band."""
    return _cut(shaft(), _ring(40.0, 20.5, 18.0, 4.0))


def make_retaining_ring_groove() -> TopoDS_Shape:
    """1.5 wide, 1.2 deep: narrow and near-square."""
    return _cut(shaft(), _ring(40.0, 20.5, 18.8, 1.5))


def make_wide_groove() -> TopoDS_Shape:
    """12 wide, 3 deep: too broad to seal anything."""
    return _cut(shaft(), _ring(40.0, 20.5, 17.0, 12.0))


def make_bore_between_counterbores() -> TopoDS_Shape:
    """The trap. A plain bore counterbored from both ends.

    Topologically identical to a groove -- a cylinder between two shoulders,
    coaxial cylinders beyond both -- and it must not be claimed as one.
    """
    drilled = _cut(block(), _cyl(40, 40, -1, 6.0, 62.0))
    counterbored = _cut(drilled, _cyl(40, 40, -1, 12.0, 16.0))
    return _cut(counterbored, _cyl(40, 40, 45, 12.0, 16.0))


def make_internal_groove() -> TopoDS_Shape:
    """A relief turned into the middle of a through bore."""
    bored = _cut(block(), _cyl(40, 40, -1, 10.0, 62.0))
    return _cut(bored, _ring(25.0, 14.0, 10.0, 4.0, x=40.0, y=40.0))


def make_circular_face_gland() -> TopoDS_Shape:
    """An annular seal channel sunk into the end of a cap."""
    return _cut(_cyl(0, 0, 0, 40.0, 20.0), _ring(18.0, 27.0, 24.0, 4.0))


def make_square_gasket_loop() -> TopoDS_Shape:
    """A racetrack gasket channel round the top face, square-cornered."""
    return _cut(
        block(),
        _cut(_box((10, 10, 57), (70, 70, 61)), _box((14, 14, 56), (66, 66, 62))),
    )


def make_plain_pocket() -> TopoDS_Shape:
    return _cut(block(), _box((20, 20, 45), (60, 60, 61)))


# =============================================================================


class TestTurnedGrooves(unittest.TestCase):
    def test_plain_shaft_has_no_groove(self):
        self.assertEqual(grooves_in(shaft()), [])

    def test_plain_block_has_no_groove(self):
        self.assertEqual(grooves_in(block()), [])

    def test_oring_gland_is_classified_by_its_proportions(self):
        found = grooves_in(make_oring_gland())
        self.assertEqual([f.type for f in found], [FeatureType.O_RING_GLAND])
        self.assertAlmostEqual(found[0].number("width_mm"), 4.0, places=3)
        self.assertAlmostEqual(found[0].number("depth_mm"), 2.0, places=3)

    def test_narrow_square_groove_is_a_retaining_ring(self):
        found = grooves_in(make_retaining_ring_groove())
        self.assertEqual([f.type for f in found], [FeatureType.RETAINING_RING_GROOVE])

    def test_a_broad_groove_seals_nothing(self):
        found = grooves_in(make_wide_groove())
        self.assertEqual([f.type for f in found], [FeatureType.GROOVE])

    def test_an_external_groove_is_not_internal(self):
        self.assertFalse(grooves_in(make_oring_gland())[0].param("is_internal"))

    def test_two_grooves_on_one_shaft_are_two_features(self):
        turned = _cut(_cut(shaft(), _ring(30.0, 20.5, 18.0, 4.0)), _ring(70.0, 20.5, 18.0, 4.0))
        self.assertEqual(len(grooves_in(turned)), 2)

    def test_instance_ids_are_unique(self):
        turned = _cut(_cut(shaft(), _ring(30.0, 20.5, 18.0, 4.0)), _ring(70.0, 20.5, 18.0, 4.0))
        ids = [f.instance_id for f in grooves_in(turned)]
        self.assertEqual(len(set(ids)), len(ids))


class TestGrooveRefusals(unittest.TestCase):
    def test_a_bore_between_counterbores_is_not_a_groove(self):
        # The step goes the wrong way: the material either side of a bore
        # groove is smaller, and here it is larger. Claiming this would lose
        # the hole.
        self.assertEqual(grooves_in(make_bore_between_counterbores()), [])

    def test_the_hole_survives(self):
        # It comes out a through hole rather than a counterbore, and both
        # readings are of the same geometry: the seat is recorded on it
        # either way. Counterbored from both ends, the bore's shoulders are
        # each wide enough to be a face the tool came in through, so once
        # the whole stack is considered together the hole plainly runs from
        # one side of the block to the other -- which is the more useful
        # thing to know about it than that one end has a seat.
        context = list(analyse(make_bore_between_counterbores()).values())[0]
        bores = context.recognition.of_type(
            FeatureType.THROUGH_HOLE, FeatureType.COUNTERBORE
        )
        self.assertEqual(len(bores), 1)
        self.assertAlmostEqual(bores[0].number("outer_diameter_mm"), 24.0, places=3)

    def test_a_counterbore_alone_is_not_a_groove(self):
        drilled = _cut(block(), _cyl(40, 40, -1, 6.0, 62.0))
        self.assertEqual(grooves_in(_cut(drilled, _cyl(40, 40, 45, 12.0, 16.0))), [])

    def test_a_plain_pocket_is_not_a_gland(self):
        self.assertEqual(grooves_in(make_plain_pocket()), [])


class TestInternalGrooves(unittest.TestCase):
    def test_a_groove_in_a_bore_is_found(self):
        found = grooves_in(make_internal_groove())
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].param("is_internal"))

    def test_the_groove_carries_the_bore_it_is_cut_in(self):
        # Grooves and holes overlap deliberately: the groove is part of the
        # bore's geometry as well as a feature in its own right, so skipping
        # faces the hole recognizer claimed would lose every internal groove.
        #
        # Which of the two survives is the resolver's call, and the groove
        # family deliberately outranks the bore family: a groove owns its
        # band, both shoulders and the bore either side of it, which is a
        # superset of what the bore recognizer sees, so the bore is contained
        # and drops out. Here that is the whole part -- there is nothing to
        # the bore but the groove and its two stubs -- so the groove is left
        # holding all five faces. On a real part the bore reaches an end of
        # its own past the groove and both features survive.
        context = list(analyse(make_internal_groove()).values())[0]
        grooves = context.recognition.of_type(FeatureType.GROOVE)
        self.assertEqual(len(grooves), 1)
        self.assertEqual(len(grooves[0].faces), 5)


class TestFaceGlands(unittest.TestCase):
    def test_a_circular_face_gland_is_found(self):
        found = grooves_in(make_circular_face_gland())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].param("gland_shape"), "circular")
        self.assertAlmostEqual(found[0].number("width_mm"), 3.0, places=3)

    def test_a_plain_cap_has_no_gland(self):
        self.assertEqual(grooves_in(_cyl(0, 0, 0, 40.0, 20.0)), [])

    def test_a_gasket_loop_is_found(self):
        found = grooves_in(make_square_gasket_loop())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].param("gland_shape"), "loop")
        self.assertAlmostEqual(found[0].number("width_mm"), 4.0, places=3)


class TestGrooveRules(unittest.TestCase):
    def test_a_square_gasket_loop_is_reported(self):
        findings = rule_check(make_square_gasket_loop(), Rulebook.GROOVE_SQUARE_CORNER)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.WARNING)

    def test_every_corner_of_the_loop_is_counted(self):
        message = rule_check(
            make_square_gasket_loop(), Rulebook.GROOVE_SQUARE_CORNER
        )[0].message
        self.assertIn("4 square corners", message)

    def test_the_message_offers_the_alternative_process(self):
        message = rule_check(
            make_square_gasket_loop(), Rulebook.GROOVE_SQUARE_CORNER
        )[0].message
        self.assertIn("EDM", message)

    def test_a_turned_groove_has_no_plan_view_corners(self):
        # Every groove has square floor-to-wall edges by the cutter's own
        # geometry. Reporting those would flag every groove ever cut.
        self.assertEqual(
            rule_check(make_oring_gland(), Rulebook.GROOVE_SQUARE_CORNER), []
        )

    def test_a_plain_pocket_is_clean(self):
        self.assertEqual(
            rule_check(make_plain_pocket(), Rulebook.GROOVE_SQUARE_CORNER), []
        )

    def test_relief_rule_ignores_a_groove_with_no_thread(self):
        self.assertEqual(
            rule_check(make_oring_gland(), Rulebook.THREAD_RELIEF_WIDTH), []
        )


if __name__ == "__main__":
    unittest.main()
