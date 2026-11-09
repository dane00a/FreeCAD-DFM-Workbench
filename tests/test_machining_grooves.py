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

    def test_the_groove_leaves_the_bore_to_the_hole(self):
        # The groove is its own band and two shoulders. The bore either side
        # is what the groove interrupts and belongs to the hole: the groove
        # used to claim those flanks as well, which put one face under a
        # groove and a hole at once (reference 70efab906 changed this; the
        # groove here used to hold all five faces).
        context = list(analyse(make_internal_groove()).values())[0]
        grooves = context.recognition.of_type(FeatureType.GROOVE)
        self.assertEqual(len(grooves), 1)
        self.assertEqual(len(grooves[0].faces), 3)
        holes = context.recognition.of_type(FeatureType.THROUGH_HOLE)
        self.assertEqual(len(holes), 1)
        self.assertFalse(set(holes[0].faces) & set(grooves[0].faces))


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

    def test_a_bore_crossing_the_loop_is_not_one_of_its_corners(self):
        # A bolt hole drilled through the channel floor has its axis along
        # the floor normal, like a corner radius, but runs the depth of the
        # part rather than the depth of the channel. This one breaks through
        # the inner wall and leaves a web against the outer one.
        from freecad.DFM.core.machining import AagBuilder

        shape = _cut(make_square_gasket_loop(), _cyl(14.5, 40, -1, 3.0, 70.0))
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        found = grooves_in(shape)
        self.assertEqual(len(found), 1)
        for face_id in found[0].faces:
            node = graph.node(face_id)
            self.assertNotAlmostEqual(node.cyl_radius, 3.0, places=3)


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


class TestReliefInterruptedPort(unittest.TestCase):
    """A tapped port with a relief undercut part-way down is one hole.

    Four M6 ports on a circle: a 5 mm tap drill with a 118 degree point, a
    6.5 x 1.5 relief undercut part-way down and a 0.5 x 45 entry chamfer.
    Each used to come back as a countersink (entry half), a blind hole (far
    half) and a groove that also claimed both halves' bore faces, and the
    pattern pass made two patterns where there is one.
    """

    @staticmethod
    def make_ports() -> TopoDS_Shape:
        import math

        from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone

        part = _box((-40, -40, 0), (40, 40, 20))
        cone_h = 2.5 / math.tan(math.radians(59.0))
        up = gp_Dir(0, 0, 1)
        for i in range(4):
            th = i * math.pi / 2.0
            cx, cy = 25.0 * math.cos(th), 25.0 * math.sin(th)
            part = _cut(part, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, -0.1), up), 2.5, 11.2).Shape())
            part = _cut(part, BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(cx, cy, 11.1), up), 2.5, 0.0, cone_h).Shape())
            ring = _cut(
                BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, 6.5), up), 3.25, 1.5).Shape(),
                BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, 6.4), up), 2.5, 1.7).Shape(),
            )
            part = _cut(part, ring)
            part = _cut(part, BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(cx, cy, 0.0), up), 3.0, 2.5, 0.5).Shape())
        return part

    def test_each_port_is_one_hole_and_its_groove_claims_only_itself(self):
        from collections import Counter

        from freecad.DFM.core.machining import AagBuilder
        from freecad.DFM.core.machining.aag import SurfaceType

        shape = self.make_ports()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        context = list(analyse(shape).values())[0]
        hole_types = {
            FeatureType.COUNTERSINK,
            FeatureType.BLIND_HOLE,
            FeatureType.THROUGH_HOLE,
            FeatureType.COUNTERBORE,
            FeatureType.BREAKTHROUGH_HOLE,
        }
        ports = grooves = patterns = 0
        owners: Counter = Counter()
        for feature in context.recognition.features:
            if feature.type == FeatureType.PATTERN:
                patterns += 1
                continue
            owners.update(feature.faces)
            if feature.type in GROOVE_TYPES:
                grooves += 1
            if feature.type in hole_types and abs((feature.number("diameter_mm") or 0.0) - 5.0) < 0.1:
                ports += 1
                self.assertFalse(feature.param("is_through"))
                self.assertTrue(feature.param("relief_interrupted"))
                cylinders = [
                    f for f in feature.faces
                    if graph.node(f).surface_type is SurfaceType.CYLINDER
                ]
                self.assertEqual(len(cylinders), 2)  # both halves of the bore
        self.assertEqual(ports, 4)
        self.assertEqual(grooves, 4)
        self.assertEqual(patterns, 1)
        for face_id, count in owners.items():
            self.assertEqual(count, 1, f"face {face_id}")


class TestInterruptedFaceGland(unittest.TestCase):
    """A face gland ring cut into arcs by a cavity is still one gland.

    An ID65 / OD70 x 2 ring around a 60 x 60 cavity whose corners reach past
    it. Judged one wall fragment at a time each arc failed the full-ring
    gate, nothing claimed the ring and the hole pass called its outer wall a
    65 mm counterbore with a 70 mm outer.
    """

    @staticmethod
    def make_ringed_cavity() -> TopoDS_Shape:
        part = _box((0, 0, 0), (75, 75, 45))
        part = _cut(part, _box((7.5, 7.5, 13), (67.5, 67.5, 45.001)))
        ring = _cut(_cyl(37.5, 37.5, 43, 35.0, 2.1), _cyl(37.5, 37.5, 43, 32.5, 2.2))
        return _cut(part, ring)

    def test_the_ring_is_one_groove_and_no_hole(self):
        from freecad.DFM.core.machining import AagBuilder
        from freecad.DFM.core.machining.recognizers import GrooveRecognizer, HoleRecognizer

        shape = self.make_ringed_cavity()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        holes = HoleRecognizer().recognize(graph, shape)
        grooves = GrooveRecognizer().recognize(graph, shape, prior=holes)

        glands = [
            g
            for g in grooves
            if g.param("gland_orientation") == "face"
            and abs((g.number("groove_diameter_mm") or 0.0) - 70.0) <= 0.1
        ]
        self.assertEqual(len(glands), 1)
        self.assertAlmostEqual(glands[0].number("inner_diameter_mm"), 65.0, delta=0.1)
        self.assertAlmostEqual(glands[0].number("width_mm"), 2.5, delta=0.1)
        self.assertGreater(glands[0].param("ring_segments") or 1, 1)

        # A 65 or 70 mm hole here is the gland read as a counterbore.
        for hole in holes:
            diameter = hole.number("diameter_mm") or 0.0
            self.assertGreater(abs(diameter - 65.0), 0.5, hole.type)
            self.assertGreater(abs(diameter - 70.0), 0.5, hole.type)


class TestDin472Catalogue(unittest.TestCase):
    """A retaining-ring groove is identified by the catalogue.

    A ring groove and an O-ring gland are the same shape, and at real
    hardware sizes their proportions overlap; a ring groove is cut to a
    published row because the ring is a bought part, and every gland misses
    its bore's row by millimetres.
    """

    @staticmethod
    def grooved_bore(body_r, bore_r, groove_r, z, width) -> TopoDS_Shape:
        part = _cyl(0, 0, 0, body_r, 30.0)
        part = _cut(part, _cyl(0, 0, -0.1, bore_r, 30.2))
        ring = _cut(_cyl(0, 0, z, groove_r, width), _cyl(0, 0, z - 0.1, bore_r, width + 0.2))
        return _cut(part, ring)

    @staticmethod
    def grooves_at(shape, diameter):
        from freecad.DFM.core.machining import AagBuilder
        from freecad.DFM.core.machining.recognizers import GrooveRecognizer

        graph = AagBuilder(shape, FaceIndex(shape)).build()
        return [
            g
            for g in GrooveRecognizer().recognize(graph, shape)
            if abs((g.number("groove_diameter_mm") or 0.0) - diameter) <= 0.1
        ]

    def test_a_din472_row_match_is_a_retaining_ring_groove(self):
        # 58.0 x 2.2 in a 55 bore: ratio 1.47, inside the gland band, so
        # only the catalogue can tell what this is.
        found = self.grooves_at(self.grooved_bore(50.0, 27.5, 29.0, 10.0, 2.2), 58.0)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].type, FeatureType.RETAINING_RING_GROOVE)

    def test_an_as568_gland_in_a_listed_bore_is_still_a_gland(self):
        # 10.8 x 2.4 in an 8 bore, where DIN 472 says 8.4 x 0.9.
        found = self.grooves_at(self.grooved_bore(20.0, 4.0, 5.4, 12.0, 2.4), 10.8)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].type, FeatureType.O_RING_GLAND)


if __name__ == "__main__":
    unittest.main()
