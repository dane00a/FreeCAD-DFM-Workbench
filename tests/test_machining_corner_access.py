# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Whether a sharp corner is one a cutter can get to.

Tested against the two tests themselves rather than through the rule, so a
change in what the recognizers claim cannot quietly move what these say. Both
answers are physical -- rays are fired at real solids -- so the fixtures are
built to have an unambiguous right answer: a corner in open air, a corner at
the bottom of a slot narrower than a tool holder, a floor line that runs out
to daylight, one that ends in a wall.
"""

import math
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.TopoDS import TopoDS_Shape
from OCP.gp import gp_Pnt

from freecad.DFM.core.checks.machining.corner_access import (
    ShapeProbe,
    is_cardinal,
    is_cutter_formed,
    is_reachable,
    second_smallest_extent,
)
from freecad.DFM.core.machining.aag import Concavity
from freecad.DFM.core.machining.aag_builder import AagBuilder
from OCP.gp import gp_Dir


def _box(a, b) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*a), gp_Pnt(*b)).Shape()


def _fuse(*shapes) -> TopoDS_Shape:
    out = shapes[0]
    for shape in shapes[1:]:
        out = BRepAlgoAPI_Fuse(out, shape).Shape()
    return out


def _cut(a, b) -> TopoDS_Shape:
    return BRepAlgoAPI_Cut(a, b).Shape()


_MINIMUM_FEATURE_MM = 0.5


class CornerCase:
    """One solid, with its concave corners already worked out."""

    def __init__(self, shape):
        self.shape = shape
        self.graph = AagBuilder(shape).build()
        self.probe = ShapeProbe(shape, self.graph)

    def square_corners(self):
        """Every concave edge at roughly a right angle."""
        found = []
        for edge in self.graph.edges:
            if edge.concavity is not Concavity.CONCAVE:
                continue
            deviation = abs(edge.dihedral_angle - math.pi)
            if not 1.3090 < deviation < 1.8326:
                continue
            if not (
                self.graph.has_node(edge.face_id_a)
                and self.graph.has_node(edge.face_id_b)
            ):
                continue
            found.append(
                (
                    edge,
                    self.graph.node(edge.face_id_a),
                    self.graph.node(edge.face_id_b),
                    deviation,
                )
            )
        return found

    def reachable(self):
        return [
            is_reachable(self.probe, edge, a, b)
            for edge, a, b, _ in self.square_corners()
        ]

    def cutter_formed(self):
        return [
            is_cutter_formed(self.probe, edge, a, b, deviation, _MINIMUM_FEATURE_MM)
            for edge, a, b, deviation in self.square_corners()
        ]


class ReachabilityTests(unittest.TestCase):
    def test_an_open_outside_corner_is_reachable(self):
        """The inside angle of an L bracket, with nothing near it."""
        case = CornerCase(
            _fuse(_box((0, 0, 0), (80, 80, 10)), _box((0, 0, 0), (10, 80, 60)))
        )
        verdicts = case.reachable()
        self.assertTrue(verdicts, "the bracket should have a square inside corner")
        self.assertTrue(all(verdicts), "an open corner is reachable from three sides")

    def test_a_corner_at_the_bottom_of_a_narrow_canyon_is_not(self):
        """Two tall ribs four millimetres apart.

        The floor between them is a corner a cutter cannot be brought to
        squarely: anything long enough to reach the bottom is thicker than
        the gap, which is the whole manufacturing concern.
        """
        case = CornerCase(
            _fuse(
                _box((0, 0, 0), (80, 80, 10)),
                _box((20, 0, 0), (26, 80, 60)),
                _box((30, 0, 0), (36, 80, 60)),
            )
        )
        verdicts = case.reachable()
        self.assertTrue(verdicts)
        self.assertIn(False, verdicts, "the canyon floor corners are confined")

    def test_moving_the_wall_away_makes_the_same_corner_reachable(self):
        """The geometry of the corner is identical; only its neighbours move.

        This is the point of the whole test: what changes the answer is what
        is standing nearby, not the shape of the junction.
        """
        near = CornerCase(
            _fuse(
                _box((0, 0, 0), (80, 80, 10)),
                _box((20, 0, 0), (26, 80, 60)),
                _box((30, 0, 0), (36, 80, 60)),
            )
        )
        far = CornerCase(
            _fuse(
                _box((0, 0, 0), (80, 80, 10)),
                _box((0, 0, 0), (6, 80, 60)),
                _box((70, 0, 0), (76, 80, 60)),
            )
        )
        self.assertIn(False, near.reachable())
        self.assertNotIn(False, far.reachable())

    def test_a_plain_block_has_no_concave_corner_to_judge(self):
        self.assertEqual(CornerCase(_box((0, 0, 0), (40, 40, 40))).square_corners(), [])


class CutterFormedTests(unittest.TestCase):
    def test_a_through_channel_floor_line_is_cutter_formed(self):
        """A slot open at both ends: the cutter runs straight through it."""
        case = CornerCase(_cut(_box((0, 0, 0), (80, 80, 40)), _box((-1, 30, 20), (81, 45, 41))))
        verdicts = case.cutter_formed()
        self.assertTrue(verdicts)
        self.assertIn(True, verdicts, "a run-through floor line is formed in passing")

    def test_a_closed_pocket_floor_line_is_not(self):
        """The same cut, stopped short of both ends.

        Now every floor line terminates in a wall, and the trihedral corner
        where they meet can never be sharp -- the tool is round. So the
        floor line is not free after all.
        """
        case = CornerCase(_cut(_box((0, 0, 0), (80, 80, 40)), _box((15, 30, 20), (65, 45, 41))))
        verdicts = case.cutter_formed()
        self.assertTrue(verdicts)
        self.assertNotIn(
            True, verdicts, "a closed pocket's floor lines end in walls"
        )

    def test_a_curved_corner_is_never_called_cutter_formed(self):
        """Only straight plane-to-plane junctions qualify."""
        case = CornerCase(
            _fuse(_box((0, 0, 0), (60, 60, 10)), _box((20, 20, 10), (40, 40, 40)))
        )
        for edge, a, b, deviation in case.square_corners():
            if edge.edge_curve_type == "line":
                continue
            self.assertFalse(
                is_cutter_formed(case.probe, edge, a, b, deviation, _MINIMUM_FEATURE_MM)
            )

    def test_a_shallow_junction_is_not_a_right_angle(self):
        case = CornerCase(
            _fuse(_box((0, 0, 0), (80, 80, 10)), _box((0, 0, 0), (10, 80, 60)))
        )
        edge, a, b, _ = case.square_corners()[0]
        self.assertFalse(
            is_cutter_formed(case.probe, edge, a, b, 0.4, _MINIMUM_FEATURE_MM),
            "0.4 radians off flat is a shallow bend, not a corner",
        )


class HelperTests(unittest.TestCase):
    def test_cardinal_directions_are_recognized(self):
        self.assertTrue(is_cardinal(gp_Dir(0.0, 0.0, 1.0)))
        self.assertTrue(is_cardinal(gp_Dir(-1.0, 0.0, 0.0)))
        self.assertFalse(is_cardinal(gp_Dir(0.0, 0.5, 0.866)))
        self.assertFalse(is_cardinal(None))

    def test_the_flat_dimension_of_a_face_is_ignored(self):
        """A planar face is zero thick, and that says nothing about tooling."""
        case = CornerCase(_box((0, 0, 0), (40, 8, 25)))
        extents = sorted(second_smallest_extent(n) for n in case.graph.nodes)
        self.assertTrue(all(e > 0.0 for e in extents))
        self.assertEqual(round(max(extents), 3), 25.0)

    def test_a_probe_finds_every_face_of_the_solid(self):
        shape = _box((0, 0, 0), (10, 10, 10))
        probe = ShapeProbe(shape, AagBuilder(shape).build())
        self.assertIsNotNone(probe.face(1))
        self.assertIsNotNone(probe.face(6))
        self.assertIsNone(probe.face(0), "graph ids count from one")
        self.assertIsNone(probe.face(7))

    def test_a_ray_into_open_air_escapes_and_one_into_the_part_does_not(self):
        shape = _box((0, 0, 0), (10, 10, 10))
        probe = ShapeProbe(shape, AagBuilder(shape).build())
        outside = gp_Pnt(5.0, 5.0, 20.0)
        self.assertTrue(probe.escapes(outside, gp_Dir(0.0, 0.0, 1.0), 100.0))
        self.assertFalse(probe.escapes(outside, gp_Dir(0.0, 0.0, -1.0), 100.0))

    def test_a_ray_stops_looking_at_the_range_it_was_given(self):
        """Cutter reach, not part scale: distant clutter is not obstruction."""
        shape = _box((0, 0, 0), (10, 10, 10))
        probe = ShapeProbe(shape, AagBuilder(shape).build())
        far = gp_Pnt(5.0, 5.0, 200.0)
        self.assertTrue(probe.escapes(far, gp_Dir(0.0, 0.0, -1.0), 25.0))
        self.assertFalse(probe.escapes(far, gp_Dir(0.0, 0.0, -1.0), 500.0))


if __name__ == "__main__":
    unittest.main()
