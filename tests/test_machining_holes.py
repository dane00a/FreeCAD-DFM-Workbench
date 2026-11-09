# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for hole recognition.

A cylindrical face is not automatically a hole, and most of these cases exist
to pin the things that are not: bosses, corner fillets, and blend bands all
present a cylinder to the recognizer and none of them is a bore. The
false-positive guards matter more than the positives here, because a hole
recognizer that also finds bosses poisons every hole rule downstream.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.machining.recognizers import HoleRecognizer
from freecad.DFM.core.utils.geometry import FaceIndex


# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def _cylinder(x, y, z, dx, dy, dz, radius, height) -> TopoDS_Shape:
    axis = gp_Ax2(gp_Pnt(x, y, z), gp_Dir(dx, dy, dz))
    return BRepPrimAPI_MakeCylinder(axis, radius, height).Shape()


def block() -> TopoDS_Shape:
    """The 80 x 60 x 30 block every hole case is cut into."""
    return BRepPrimAPI_MakeBox(80.0, 60.0, 30.0).Shape()


def holes_in(shape: TopoDS_Shape):
    graph = AagBuilder(shape, FaceIndex(shape)).build()
    return HoleRecognizer().recognize(graph, shape)


def types_in(shape: TopoDS_Shape) -> list[str]:
    return sorted(f.type for f in holes_in(shape))


# -- shapes -------------------------------------------------------------------


def make_through_hole() -> TopoDS_Shape:
    """A 12mm bore right through the block."""
    return _cut(block(), _cylinder(40, 30, -1, 0, 0, 1, 6.0, 40.0))


def make_blind_hole() -> TopoDS_Shape:
    """A 10mm bore stopping 15mm down, leaving a flat floor."""
    return _cut(block(), _cylinder(40, 30, 15, 0, 0, 1, 5.0, 20.0))


def make_drilled_blind_hole() -> TopoDS_Shape:
    """A blind hole with a 118 degree drill point, as a twist drill leaves."""
    bore = _cylinder(40, 30, 12, 0, 0, 1, 5.0, 20.0)
    # A 118 degree included angle is a 59 degree half angle; the cone runs
    # from the full radius down to the point.
    tip_height = 5.0 / 1.6643  # tan(59 degrees)
    cone = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(40, 30, 12), gp_Dir(0, 0, -1)), 5.0, 0.0, tip_height
    ).Shape()
    return _cut(_cut(block(), bore), cone)


def make_counterbore() -> TopoDS_Shape:
    """A 8mm bore through, opened to 16mm for the last 8mm."""
    inner = _cylinder(40, 30, -1, 0, 0, 1, 4.0, 40.0)
    outer = _cylinder(40, 30, 22, 0, 0, 1, 8.0, 10.0)
    return _cut(_cut(block(), inner), outer)


def make_countersink() -> TopoDS_Shape:
    """A through bore with a 90 degree conical entry."""
    bore = _cylinder(40, 30, -1, 0, 0, 1, 4.0, 40.0)
    cone = BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(40, 30, 22), gp_Dir(0, 0, 1)), 4.0, 12.0, 8.0)
    return _cut(_cut(block(), bore), cone.Shape())


def make_boss() -> TopoDS_Shape:
    """A cylindrical boss standing on the block. Not a hole."""
    return _fuse(block(), _cylinder(40, 30, 30, 0, 0, 1, 10.0, 15.0))


def make_pocket_with_corner_fillets() -> TopoDS_Shape:
    """A pocket whose corners are radiused. The corner arcs are not holes."""
    cavity = BRepPrimAPI_MakeBox(gp_Pnt(15, 15, 12), gp_Pnt(65, 45, 31)).Shape()
    pocket = _cut(block(), cavity)
    filleted = BRepFilletAPI_MakeFillet(pocket)
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from freecad.DFM.core.utils.occt import TopoDS
    from OCP.BRep import BRep_Tool

    explorer = TopExp_Explorer(pocket, TopAbs_EDGE)
    added = 0
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        curve = BRep_Tool.Curve_s(edge, 0.0, 0.0)
        if curve is None:
            continue
        first, last = BRep_Tool.Range_s(edge)
        mid = curve.Value((first + last) * 0.5)
        # The four vertical corners of the cavity.
        if 12.0 < mid.Z() < 30.0 and mid.X() in (15.0, 65.0) and mid.Y() in (15.0, 45.0):
            filleted.Add(4.0, edge)
            added += 1
    if added == 0:
        return pocket
    filleted.Build()
    return filleted.Shape() if filleted.IsDone() else pocket


def make_cross_drilled() -> TopoDS_Shape:
    """A vertical bore crossed by a horizontal one."""
    vertical = _cylinder(40, 30, -1, 0, 0, 1, 5.0, 40.0)
    horizontal = _cylinder(-1, 30, 15, 1, 0, 0, 4.0, 90.0)
    return _cut(_cut(block(), vertical), horizontal)


def make_opposed_blind_holes() -> TopoDS_Shape:
    """Two blind holes on one axis, drilled from opposite faces.

    They line up exactly and are still two holes, separated by solid
    material. Merging them would report one deep bore that nobody drills.
    """
    top = _cylinder(40, 30, 22, 0, 0, 1, 4.0, 12.0)
    bottom = _cylinder(40, 30, -2, 0, 0, 1, 4.0, 10.0)
    return _cut(_cut(block(), top), bottom)


# =============================================================================


class TestHoleRecognition(unittest.TestCase):
    def test_through_hole(self):
        holes = holes_in(make_through_hole())
        self.assertEqual(len(holes), 1)
        self.assertEqual(holes[0].type, FeatureType.THROUGH_HOLE)
        self.assertTrue(holes[0].param("is_through"))
        self.assertAlmostEqual(holes[0].number("diameter_mm"), 12.0, places=3)
        self.assertAlmostEqual(holes[0].number("depth_mm"), 30.0, places=3)

    def test_blind_hole(self):
        holes = holes_in(make_blind_hole())
        self.assertEqual(len(holes), 1)
        self.assertEqual(holes[0].type, FeatureType.BLIND_HOLE)
        self.assertFalse(holes[0].param("is_through"))
        self.assertAlmostEqual(holes[0].number("depth_mm"), 15.0, places=3)

    def test_blind_hole_with_a_flat_floor_is_flagged_flat(self):
        self.assertTrue(holes_in(make_blind_hole())[0].param("flat_bottom"))

    def test_drilled_blind_hole_is_not_flat_bottomed(self):
        # A twist drill always leaves a cone, so a drill-pointed hole must
        # not be reported as flat-bottomed.
        holes = holes_in(make_drilled_blind_hole())
        self.assertTrue(holes)
        self.assertFalse(holes[0].param("flat_bottom"))

    def test_counterbore(self):
        holes = holes_in(make_counterbore())
        self.assertEqual(len(holes), 1)
        self.assertEqual(holes[0].type, FeatureType.COUNTERBORE)
        self.assertAlmostEqual(holes[0].number("diameter_mm"), 8.0, places=3)
        self.assertAlmostEqual(holes[0].number("outer_diameter_mm"), 16.0, places=3)

    def test_counterbore_is_seeded_from_the_inner_bore(self):
        # Seeded from the outer cylinder instead, the counterbore would be
        # emitted as a standalone blind hole before the inner bore could
        # claim it.
        holes = holes_in(make_counterbore())
        self.assertEqual(len(holes), 1, "the outer bore was reported separately")

    def test_countersink(self):
        holes = holes_in(make_countersink())
        self.assertEqual(len(holes), 1)
        self.assertEqual(holes[0].type, FeatureType.COUNTERSINK)
        self.assertAlmostEqual(holes[0].number("included_angle"), 90.0, delta=2.0)

    def test_axis_is_recorded(self):
        axis = holes_in(make_through_hole())[0].param("axis")
        self.assertEqual(len(axis), 3)
        self.assertAlmostEqual(abs(axis[2]), 1.0, places=6)

    def test_faces_are_recorded_for_highlighting(self):
        hole = holes_in(make_through_hole())[0]
        self.assertGreaterEqual(len(hole.faces), 1)
        self.assertTrue(all(isinstance(f, int) and f >= 1 for f in hole.faces))
        self.assertEqual(hole.geometry_refs[0][0], "Face")


class TestNotHoles(unittest.TestCase):
    """Cylinders that must not be mistaken for bores."""

    def test_plain_block_has_no_holes(self):
        self.assertEqual(holes_in(block()), [])

    def test_boss_is_not_a_hole(self):
        # The discriminator is orientation: a bore's face is stored reversed
        # because its outward normal points at the axis; a boss's is not.
        self.assertEqual(holes_in(make_boss()), [])

    def test_external_fillet_is_not_a_hole(self):
        rod = _cylinder(0, 0, 0, 0, 0, 1, 15.0, 60.0)
        self.assertEqual(holes_in(rod), [])

    def test_pocket_corner_fillets_are_not_holes(self):
        # A corner fillet is tangent to both pocket walls, so its axis sits
        # exactly one radius from each. A hole drilled at a corner does not.
        holes = holes_in(make_pocket_with_corner_fillets())
        self.assertEqual(
            holes, [], f"corner fillets reported as {[h.type for h in holes]}"
        )


class TestInterruptedAndOpposedBores(unittest.TestCase):
    def test_cross_drilled_bore_is_one_through_hole(self):
        # The crossing bore splits the horizontal cylinder in two. They are
        # fragments of one hole, not two holes.
        holes = holes_in(make_cross_drilled())
        self.assertEqual(len(holes), 2, "expected one vertical and one horizontal bore")
        for hole in holes:
            self.assertEqual(hole.type, FeatureType.THROUGH_HOLE)

    def test_interrupted_bore_records_its_longest_run(self):
        horizontal = [
            h for h in holes_in(make_cross_drilled()) if abs(h.param("axis")[0]) > 0.9
        ]
        self.assertEqual(len(horizontal), 1)
        hole = horizontal[0]
        # The drill crosses the void, so the full span is 80mm, but the
        # longest uninterrupted run is what decides how hard it is to drill.
        self.assertAlmostEqual(hole.number("depth_mm"), 80.0, delta=1.0)
        self.assertLess(hole.number("max_contiguous_depth_mm"), 80.0)
        self.assertGreater(hole.number("max_void_mm"), 0.0)

    def test_opposed_blind_holes_stay_separate(self):
        holes = holes_in(make_opposed_blind_holes())
        self.assertEqual(
            len(holes), 2, "two holes drilled from opposite faces were merged into one"
        )
        for hole in holes:
            self.assertEqual(hole.type, FeatureType.BLIND_HOLE)
            self.assertLess(hole.number("depth_mm"), 20.0)


if __name__ == "__main__":
    unittest.main()


# =============================================================================


from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex


def hole_check(shape, rule, target="N/A", limit="N/A", severity="WARNING"):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    data = MachiningAnalyzer().execute(shape, face_index, edge_index, prefs={})
    check_class = get_check_class(rule)
    assert check_class is not None
    return check_class().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


def _severities(findings):
    return [f.severity for f in findings]


def make_shaft_with_axial_bore(bore_radius: float, depth: float) -> TopoDS_Shape:
    """A turned bar with a bore down its own axis: boring-bar work."""
    bar = _cylinder(0, 0, 0, 0, 0, 1, 20.0, 100.0)
    return _cut(bar, _cylinder(0, 0, 100.0 - depth, 0, 0, 1, bore_radius, depth + 1.0))


class TestBoreEndingInRelief(unittest.TestCase):
    """A bore that ends in a relief undercut is blind, not through.

    A 70 mm bore down to a floor 18 mm in, with a 71.5 x 3 relief ring at
    its bottom reaching 1 mm below the floor: the bore passes through the
    relief's shoulder ring, and the first pass counted that ring as a second
    opening. The 28 mm rod bore through the same floor, with a 32 mm seat at
    the bottom face split by an O-ring gland, is the control and must stay
    through.
    """

    @staticmethod
    def make_end_cap() -> TopoDS_Shape:
        part = _cylinder(0, 0, 0, 0, 0, 1, 40.0, 50.0)
        part = _cut(part, _cylinder(0, 0, 32.0, 0, 0, 1, 35.0, 18.1))
        part = _cut(
            part,
            _cut(
                _cylinder(0, 0, 31.0, 0, 0, 1, 35.75, 3.0),
                _cylinder(0, 0, 30.9, 0, 0, 1, 35.0, 3.2),
            ),
        )
        part = _cut(part, _cylinder(0, 0, -0.001, 0, 0, 1, 16.0, 6.001))
        part = _cut(part, _cylinder(0, 0, 6.0, 0, 0, 1, 14.0, 44.1))
        part = _cut(
            part,
            _cut(
                _cylinder(0, 0, 1.65, 0, 0, 1, 17.83, 2.7),
                _cylinder(0, 0, 1.55, 0, 0, 1, 16.0, 2.9),
            ),
        )
        return part

    def test_the_relieved_bore_is_blind_and_the_rod_bore_through(self):
        shape = self.make_end_cap()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        bore70 = rod28 = 0
        for feature in HoleRecognizer().recognize(graph, shape):
            diameter = feature.number("diameter_mm") or 0.0
            if abs(diameter - 70.0) < 0.2:
                bore70 += 1
                self.assertFalse(feature.param("is_through"))
                self.assertTrue(feature.param("ends_in_relief"))
                # Top face to the relief's far end.
                self.assertAlmostEqual(feature.number("depth_mm"), 19.0, delta=0.2)
                self.assertAlmostEqual(feature.number("relief_diameter_mm"), 71.5, delta=0.1)
                # Reached from one end only: signed, pointing at the opening.
                self.assertTrue(feature.param("axis_signed"))
                self.assertGreater(feature.param("axis")[2], 0.9)
                # The relief band and its shoulders stay unclaimed.
                for face_id in feature.faces:
                    node = graph.node(face_id)
                    if node.surface_type.name == "CYLINDER":
                        self.assertAlmostEqual(node.cyl_radius, 35.0, delta=0.1)
            elif abs(diameter - 28.0) < 0.2:
                rod28 += 1
                self.assertTrue(feature.param("is_through"))
                self.assertFalse(feature.param("ends_in_relief"))
        self.assertEqual(bore70, 1)
        self.assertEqual(rod28, 1)


class TestRimChamferIsNotACountersink(unittest.TestCase):
    """A part-axis rim chamfer is not a bolt hole's countersink.

    A 6 mm foot bore on a bolt circle breaks out through the flange's rim
    chamfer -- a cone on the part's axis, parallel to the bore and centred
    on none of it -- and overshoots 1 mm into the hub past its own
    countersink, leaving a crescent of wall there.
    """

    @staticmethod
    def make_flange() -> TopoDS_Shape:
        import math

        part = _cylinder(0, 0, -10, 0, 0, 1, 40.0, 10.0)
        part = _fuse(part, _cylinder(0, 0, 0, 0, 0, 1, 32.0, 20.0))
        ax = gp_Ax2(gp_Pnt(0, 0, -10), gp_Dir(0, 0, 1))
        cone_ring = _cut(
            BRepPrimAPI_MakeCylinder(ax, 41.0, 4.0).Shape(),
            BRepPrimAPI_MakeCone(ax, 34.97, 40.0, 4.0).Shape(),
        )
        part = _cut(part, cone_ring)
        cx = 33.0 * math.cos(math.pi / 4.0)
        cy = 33.0 * math.sin(math.pi / 4.0)
        part = _cut(part, _cylinder(cx, cy, -11.0, 0, 0, 1, 3.0, 12.0))
        sink = BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(cx, cy, 0.0), gp_Dir(0, 0, -1)), 6.5, 3.0, 2.94)
        return _cut(part, sink.Shape())

    def test_the_bore_owns_its_own_cone_only_and_is_one_hole(self):
        shape = self.make_flange()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        bores = 0
        for feature in HoleRecognizer().recognize(graph, shape):
            if abs((feature.number("diameter_mm") or 0.0) - 6.0) > 0.1:
                continue
            bores += 1
            own_cone = False
            for face_id in feature.faces:
                node = graph.node(face_id)
                if node.surface_type.name != "CONE":
                    continue
                rim = max(node.cone_r0, node.cone_r1)
                self.assertLess(rim, 20.0)  # never the rim chamfer
                if 6.0 < rim < 7.0:
                    own_cone = True
            self.assertTrue(own_cone)
            self.assertEqual(feature.type, FeatureType.COUNTERSINK)
            # Folding the crescent in must not hide it: the tool ran past
            # its exit and scored the hub wall.
            self.assertEqual(feature.param("breakout_scar_kind"), "overshoot_arc")
            self.assertGreater(feature.number("breakout_overshoot_mm"), 0.3)
            self.assertGreater(feature.number("breakout_scar_area_mm2"), 0.0)
        # One bore, not one per fragment: the crescent past the countersink
        # belongs to the same drilling.
        self.assertEqual(bores, 1)


class TestAnnularReliefIsNotABore(unittest.TestCase):
    def test_a_web_recess_around_a_hub_is_not_a_bore(self):
        # The outer wall of an annular relief looks exactly like a wide bore
        # whose floor misses the axis. What separates them is the island: a
        # coaxial convex wall standing alongside it over a ring floor.
        part = _cylinder(0, 0, 0, 0, 0, 1, 110.0, 45.0)
        ring = _cut(
            _cylinder(0, 0, 25.5, 0, 0, 1, 87.5, 19.6),
            _cylinder(0, 0, 25.4, 0, 0, 1, 70.0, 19.8),
        )
        part = _cut(part, ring)
        part = _cut(part, _cylinder(0, 0, -0.1, 0, 0, 1, 50.0, 45.2))

        hub_bores = 0
        for feature in holes_in(part):
            diameter = feature.number("diameter_mm") or 0.0
            self.assertGreater(abs(diameter - 175.0), 1.0, feature.type)
            if abs(diameter - 100.0) < 0.5:
                hub_bores += 1
        self.assertEqual(hub_bores, 1)


class TestOwnWallIsNotAnOvershoot(unittest.TestCase):
    def test_a_bore_whose_wall_is_a_partial_arc_reports_no_scar(self):
        # A bore breaking out through the side of the part comes back as one
        # arc spanning its whole depth, the same shape a seam-split wall
        # presents; with only its countersink cone as the full circle it
        # used to read as an overshoot nearly as long as the hole.
        part = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(60, 60, 30)).Shape()
        part = _cut(part, _cylinder(55, 30, -0.1, 0, 0, 1, 10.0, 30.2))
        sink = BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(55, 30, 30.0), gp_Dir(0, 0, -1)), 13.0, 10.0, 3.0)
        part = _cut(part, sink.Shape())

        graph = AagBuilder(part, FaceIndex(part)).build()
        bores = halves = 0
        for feature in HoleRecognizer().recognize(graph, part):
            if abs((feature.number("diameter_mm") or 0.0) - 20.0) > 0.5:
                continue
            bores += 1
            halves += sum(
                1 for f in feature.faces if graph.node(f).surface_type.name == "CYLINDER"
            )
            self.assertNotIn("breakout_scar_kind", feature.parameters)
            self.assertNotIn("breakout_scar_faces", feature.parameters)
        self.assertEqual(bores, 1)
        self.assertGreaterEqual(halves, 1)


class TestStackedCounterbore(unittest.TestCase):
    def test_two_seats_over_one_bore_are_one_counterbore(self):
        # 20 x 6 over 12 x 10 over a 6 mm through bore. The 20 is reachable
        # only across the 12's own shoulder, so it used to come back as a
        # second feature; and the merged through bore was relabelled a plain
        # through hole carrying counterbore parameters.
        part = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(60, 60, 40)).Shape()
        part = _cut(part, _cylinder(30, 30, 34.0, 0, 0, 1, 10.0, 7.0))
        part = _cut(part, _cylinder(30, 30, 24.0, 0, 0, 1, 6.0, 11.0))
        part = _cut(part, _cylinder(30, 30, -1.0, 0, 0, 1, 3.0, 42.0))

        graph = AagBuilder(part, FaceIndex(part)).build()
        holes = HoleRecognizer().recognize(graph, part)
        self.assertEqual(len(holes), 1)
        hole = holes[0]
        self.assertEqual(hole.type, FeatureType.COUNTERBORE)
        self.assertTrue(hole.param("is_through"))
        self.assertAlmostEqual(hole.number("diameter_mm"), 6.0, delta=0.1)
        self.assertAlmostEqual(hole.number("outer_diameter_mm"), 12.0, delta=0.1)
        self.assertEqual(hole.param("counterbore_levels"), 2)
        stack = hole.param("counterbore_stack")
        self.assertEqual(len(stack), 2)
        self.assertAlmostEqual(stack[0]["diameter_mm"], 12.0, delta=0.1)
        self.assertAlmostEqual(stack[0]["depth_mm"], 10.0, delta=0.2)
        self.assertAlmostEqual(stack[1]["diameter_mm"], 20.0, delta=0.1)
        self.assertAlmostEqual(stack[1]["depth_mm"], 6.0, delta=0.2)
        walls = [f for f in hole.faces if graph.node(f).surface_type.name == "CYLINDER"]
        self.assertEqual(len(walls), 3)


class TestHoleDepthRatio(unittest.TestCase):
    RULE = Rulebook.HOLE_DEPTH_RATIO

    def test_shallow_hole_is_clean(self):
        self.assertEqual(hole_check(make_through_hole(), self.RULE), [])

    def test_deep_hole_warns(self):
        deep = _cut(block(), _cylinder(40, 30, -1, 0, 0, 1, 2.0, 40.0))  # 7.5x
        self.assertEqual(_severities(hole_check(deep, self.RULE)), [Severity.WARNING])

    def test_very_deep_hole_errors(self):
        deeper = _cut(block(), _cylinder(40, 30, -1, 0, 0, 1, 1.2, 40.0))  # 12.5x
        self.assertEqual(_severities(hole_check(deeper, self.RULE)), [Severity.ERROR])

    def test_threshold_pair(self):
        under = _cut(block(), _cylinder(40, 30, -1, 0, 0, 1, 2.6, 40.0))  # 5.8x
        over = _cut(block(), _cylinder(40, 30, -1, 0, 0, 1, 2.4, 40.0))  # 6.3x
        self.assertEqual(hole_check(under, self.RULE, "6.0", "10.0"), [])
        self.assertEqual(
            _severities(hole_check(over, self.RULE, "6.0", "10.0")), [Severity.WARNING]
        )

    def test_axial_bore_in_a_turned_part_uses_boring_limits(self):
        # 5:1 is comfortable for a drill and already marginal for a boring
        # bar, so the same proportions must read differently on a lathe.
        shaft = make_shaft_with_axial_bore(5.0, 50.0)  # 50 deep on 10 dia
        findings = hole_check(shaft, self.RULE)
        self.assertTrue(findings, "a 5:1 bored hole should be reported on a lathe")
        self.assertIn("boring bar", findings[0].message)


class TestHoleFlatBottom(unittest.TestCase):
    RULE = Rulebook.HOLE_FLAT_BOTTOM

    def test_flat_bottomed_blind_hole_is_reported(self):
        self.assertTrue(hole_check(make_blind_hole(), self.RULE))

    def test_drill_pointed_hole_is_not_reported(self):
        self.assertEqual(hole_check(make_drilled_blind_hole(), self.RULE), [])

    def test_through_hole_has_no_bottom(self):
        self.assertEqual(hole_check(make_through_hole(), self.RULE), [])

    def test_shallow_wide_recess_is_a_spot_face(self):
        # Wider than it is deep, and wide enough for a rigid cutter: one
        # plunge, not a concern.
        spot = _cut(block(), _cylinder(40, 30, 26, 0, 0, 1, 8.0, 10.0))
        self.assertEqual(hole_check(spot, self.RULE), [])

    def test_large_bore_is_expected_to_be_faced_flat(self):
        big = _cut(block(), _cylinder(40, 30, 10, 0, 0, 1, 9.0, 25.0))
        self.assertEqual(hole_check(big, self.RULE), [])


class TestHoleEdgeDistance(unittest.TestCase):
    RULE = Rulebook.HOLE_EDGE_DISTANCE

    def test_central_hole_is_clean(self):
        self.assertEqual(hole_check(make_through_hole(), self.RULE), [])

    def test_hole_near_the_edge_is_reported(self):
        near = _cut(block(), _cylinder(3.5, 30, -1, 0, 0, 1, 3.0, 40.0))
        findings = hole_check(near, self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 0.5, places=2)

    def test_limit_is_configurable(self):
        hole = _cut(block(), _cylinder(8.0, 30, -1, 0, 0, 1, 3.0, 40.0))  # 5mm wall
        self.assertEqual(hole_check(hole, self.RULE, limit="2.0"), [])
        self.assertTrue(hole_check(hole, self.RULE, limit="8.0"))


def make_counterbore_stack_plate() -> TopoDS_Shape:
    """Four counterbore stations: a clean three-level stack (the control), a
    seat 1 mm from the Y=0 face over a bore 8 mm clear of it, two seats
    leaving a 1 mm web where their bores leave 15, and a double-ended seat."""
    part = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(140, 90, 30)).Shape()
    cuts = [
        (30.0, 60.0, -0.1, 3.0, 30.2), (30.0, 60.0, 22.0, 6.0, 8.2), (30.0, 60.0, 25.0, 10.0, 5.2),
        (80.0, 11.0, -0.1, 3.0, 30.2), (80.0, 11.0, 25.0, 10.0, 5.2),
        (115.0, 45.0, -0.1, 3.0, 30.2), (115.0, 45.0, 25.0, 10.0, 5.2),
        (115.0, 66.0, -0.1, 3.0, 30.2), (115.0, 66.0, 25.0, 10.0, 5.2),
        (30.0, 25.0, -0.1, 4.0, 30.2), (30.0, 25.0, 26.0, 8.0, 4.2), (30.0, 25.0, -0.1, 8.0, 4.2),
    ]
    for x, y, z, radius, height in cuts:
        part = _cut(part, _cylinder(x, y, z, 0, 0, 1, radius, height))
    return part


class TestProximityMeasuresTheSeat(unittest.TestCase):
    """A counterbore's seat is wider than its bore, so on any question of how
    close the hole comes to something the seat is the answer."""

    def test_edge_distance_reports_the_seat(self):
        findings = hole_check(make_counterbore_stack_plate(), Rulebook.HOLE_EDGE_DISTANCE)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 1.0, delta=0.1)
        self.assertIn("20.0 mm seat", findings[0].message)

    def test_web_thickness_reports_the_seats(self):
        findings = hole_check(make_counterbore_stack_plate(), Rulebook.HOLE_WEB_THICKNESS)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 1.0, delta=0.1)


class TestHoleWebThickness(unittest.TestCase):
    RULE = Rulebook.HOLE_WEB_THICKNESS

    def test_well_spaced_holes_are_clean(self):
        spaced = _cut(
            _cut(block(), _cylinder(20, 30, -1, 0, 0, 1, 4.0, 40.0)),
            _cylinder(60, 30, -1, 0, 0, 1, 4.0, 40.0),
        )
        self.assertEqual(hole_check(spaced, self.RULE), [])

    def test_thin_web_between_holes_is_reported(self):
        close = _cut(
            _cut(block(), _cylinder(30, 30, -1, 0, 0, 1, 4.0, 40.0)),
            _cylinder(38.5, 30, -1, 0, 0, 1, 4.0, 40.0),
        )
        findings = hole_check(close, self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 0.5, places=2)

    def test_crossing_holes_are_not_a_web(self):
        # Non-parallel bores leave no web of constant thickness; that is the
        # intersecting-holes rule's concern instead.
        self.assertEqual(hole_check(make_cross_drilled(), self.RULE), [])


class TestHoleIntersecting(unittest.TestCase):
    RULE = Rulebook.HOLE_INTERSECTING

    def test_separate_holes_do_not_intersect(self):
        spaced = _cut(
            _cut(block(), _cylinder(20, 30, -1, 0, 0, 1, 4.0, 40.0)),
            _cylinder(60, 30, -1, 0, 0, 1, 4.0, 40.0),
        )
        self.assertEqual(hole_check(spaced, self.RULE), [])

    def test_cross_drilled_pair_is_reported(self):
        findings = hole_check(make_cross_drilled(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertIn("deflect", findings[0].message)

    def test_a_network_collapses_to_one_note(self):
        # A manifold is a deliberate design, not twenty accidents. Past the
        # threshold the finding becomes a single informational note.
        shape = block()
        # One gallery along the block, fed by five drillings from the top
        # that each stop in it: five intersections, as a manifold has.
        # Built so that no bore is cut into pieces. The reference measures
        # each hole along its seed cylinder alone, so a passage chopped into
        # fragments by the bores crossing it (as this case once was, two
        # galleries through four passages) meets only the crossings on its
        # own fragment, and the network never reaches the threshold.
        shape = _cut(shape, _cylinder(40, -1, 10, 0, 1, 0, 4.0, 62.0))
        for y in (10, 20, 30, 40, 50):
            shape = _cut(shape, _cylinder(40, y, 10, 0, 0, 1, 2.0, 25.0))
        findings = hole_check(shape, self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.INFO)
        self.assertIn("manifold", findings[0].message)


# -- countersink or lead-in chamfer ------------------------------------------


def bore_with_entry_cone(bore_r: float, rim_r: float, cone_h: float) -> TopoDS_Shape:
    """A bore through a 20 mm plate, entered through a coaxial cone that opens
    to ``rim_r`` over ``cone_h``."""
    plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(50, 50, 20)).Shape()
    bore = _cylinder(25, 25, -5, 0, 0, 1, bore_r, 30.0)
    # Wide end at the z=20 face, narrowing inward to meet the bore.
    cone = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(25, 25, 20), gp_Dir(0, 0, -1)), rim_r, bore_r, cone_h
    ).Shape()
    return _cut(plate, _fuse(bore, cone))


class TestCountersinkOrLeadIn(unittest.TestCase):
    """A countersink and a lead-in chamfer are the same cone; what separates
    them is what set its size. A chamfer is sized by the edge, a countersink by
    the head it swallows (about half a bore of growth per side)."""

    def test_half_millimetre_lead_in_is_not_a_countersink(self):
        # 10 mm bore, 11 mm rim: 0.05 x bore of growth, an edge break.
        holes = holes_in(bore_with_entry_cone(5.0, 5.5, 0.5))
        types = [h.type for h in holes]
        self.assertNotIn(FeatureType.COUNTERSINK, types)
        self.assertIn(FeatureType.THROUGH_HOLE, types)
        # The cone is released so the blend recognizer can call it a chamfer.
        for hole in holes:
            if hole.type == FeatureType.THROUGH_HOLE:
                self.assertEqual(len(hole.faces), 1)

    def test_oversized_lead_in_is_still_not_a_seat(self):
        # 14 mm bore opening to 19.27: too big for a deburr, far too small to
        # seat an M14 head.
        holes = holes_in(bore_with_entry_cone(7.0, 9.635, 3.38))
        self.assertNotIn(FeatureType.COUNTERSINK, [h.type for h in holes])

    def test_released_lead_in_is_a_conical_chamfer(self):
        from freecad.DFM.core.machining.recognizers import BlendRecognizer

        shape = bore_with_entry_cone(7.0, 9.635, 3.38)
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        chamfers = [
            f for f in BlendRecognizer().recognize(graph, shape)
            if f.type == FeatureType.CHAMFER
        ]
        self.assertEqual(len(chamfers), 1)
        # A bore's lead-in rides with its hole for operation counting.
        self.assertTrue(chamfers[0].param("bore_lead_in"))


# -- blended bore mouths ------------------------------------------------------


def ring_with_blended_bore_mouth(
    bore_r: float,
    bore_h: float,
    outer_r: float,
    fillet_r: float,
    break_r: float,
    sweep_end_deg: float,
) -> TopoDS_Shape:
    """A disc with a bore whose mouth is blended, cut as a revolved void.

    With ``break_r`` the rim is rounded by ``fillet_r`` through
    ``sweep_end_deg`` and then broken by ``break_r``, landing tangent on the
    top face: two tori between the bore wall and the face it opens onto.
    Without it, one ninety-degree rim fillet. The void is revolved and cut,
    because a bore straight out of a revolve is stored forward and the hole
    test wants the reversed face a cut leaves.
    """
    import math

    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Ax1

    th = math.radians(sweep_end_deg)
    c1r, c1z = bore_r + fillet_r, bore_h
    p1r, p1z = c1r + fillet_r * math.cos(th), c1z + fillet_r * math.sin(th)
    m1 = (math.pi + th) * 0.5
    if break_r > 0.0:
        c2r, c2z = p1r + break_r * math.cos(th), p1z + break_r * math.sin(th)
        top_r, top_z = c2r, c2z - break_r
        m2 = (math.atan2(p1z - c2z, p1r - c2r) - math.pi * 0.5) * 0.5
    else:
        top_r, top_z = c1r, c1z + fillet_r

    lo, hi = -1.0, top_z + 1.0

    def line(a, b):
        return BRepBuilderAPI_MakeEdge(gp_Pnt(a[0], 0, a[1]), gp_Pnt(b[0], 0, b[1])).Edge()

    def arc(a, m, b):
        curve = GC_MakeArcOfCircle(
            gp_Pnt(a[0], 0, a[1]), gp_Pnt(m[0], 0, m[1]), gp_Pnt(b[0], 0, b[1])
        ).Value()
        return BRepBuilderAPI_MakeEdge(curve).Edge()

    wire = BRepBuilderAPI_MakeWire()
    wire.Add(line((0, lo), (bore_r, lo)))
    wire.Add(line((bore_r, lo), (bore_r, bore_h)))
    wire.Add(arc((bore_r, bore_h), (c1r + fillet_r * math.cos(m1), c1z + fillet_r * math.sin(m1)), (p1r, p1z)))
    if break_r > 0.0:
        wire.Add(arc((p1r, p1z), (c2r + break_r * math.cos(m2), c2z + break_r * math.sin(m2)), (top_r, top_z)))
    wire.Add(line((top_r, top_z), (top_r, hi)))
    wire.Add(line((top_r, hi), (0, hi)))
    wire.Add(line((0, hi), (0, lo)))
    profile = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    void = BRepPrimAPI_MakeRevol(profile, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))).Shape()
    disc = _cylinder(0, 0, 0, 0, 0, 1, outer_r, top_z)
    return _cut(disc, void)


class TestBlendedBoreMouth(unittest.TestCase):
    def test_a_mouth_blended_twice_is_still_a_hole(self):
        # Two tori stand between the bore and its top face, and the top face
        # is too small to pass as an opening on area: the bore's own blend
        # has to be seen punching that face's inner wire.
        holes = holes_in(ring_with_blended_bore_mouth(6.0, 1.0, 13.0, 2.0, 0.4, 25.0))
        self.assertEqual([h.type for h in holes], [FeatureType.THROUGH_HOLE])
        self.assertAlmostEqual(holes[0].number("diameter_mm"), 12.0, delta=0.12)
        self.assertAlmostEqual(holes[0].number("entry_fillet_radius_mm"), 2.0, places=3)

    def test_one_rim_fillet_is_unchanged(self):
        holes = holes_in(ring_with_blended_bore_mouth(6.0, 1.0, 16.0, 2.0, 0.0, 90.0))
        self.assertEqual([h.type for h in holes], [FeatureType.THROUGH_HOLE])


class TestPmiThread(unittest.TestCase):
    """A thread callout the PMI links to a bore wins over every other check."""

    def _holes_with_pmi(self, shape, designation="M8x1.25"):
        from freecad.DFM.core.machining.annotations import ThreadCallout
        from freecad.DFM.core.machining.thread_sources import ThreadEvidence

        graph = AagBuilder(shape, FaceIndex(shape)).build()
        bores = [
            n.face_id
            for n in graph.nodes
            if n.surface_type.name == "CYLINDER" and n.is_internal
        ]
        recognizer = HoleRecognizer()
        recognizer.thread_evidence = ThreadEvidence(
            pmi={
                face_id: ThreadCallout(designation, nominal_mm=8.0, pitch_mm=1.25)
                for face_id in bores
            }
        )
        return recognizer.recognize(graph, shape)

    def test_a_callout_on_a_through_bore_makes_it_tapped(self):
        holes = self._holes_with_pmi(make_through_hole())
        self.assertEqual([h.type for h in holes], [FeatureType.THREADED_HOLE])
        self.assertEqual(holes[0].param("thread_evidence"), "pmi")
        self.assertEqual(holes[0].param("thread_designation"), "M8x1.25")

    def test_a_counterbore_keeps_its_type_and_gains_the_thread(self):
        holes = self._holes_with_pmi(make_counterbore())
        self.assertEqual([h.type for h in holes], [FeatureType.COUNTERBORE])
        self.assertEqual(holes[0].param("thread_designation"), "M8x1.25")
