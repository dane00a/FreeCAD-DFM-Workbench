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
    from OCP.TopoDS import TopoDS
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
