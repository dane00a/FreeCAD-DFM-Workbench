# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for undercut detection and the rules that read it.

The interesting cases are the near misses. A channel that runs out of the
part is reachable from its ends even though it looks enclosed from above; a
chamber closed at both ends is not. Getting that distinction wrong in either
direction is expensive: too eager and every slot is an undercut, too shy and
genuinely unmachinable geometry ships.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.machining.recognizers.reachability import Reachability
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


def _cavity(p0, p1) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def block() -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(100.0, 80.0, 50.0).Shape()


def analyse(shape, prefs=None):
    return MachiningAnalyzer().execute(
        shape, FaceIndex(shape), EdgeIndex(shape), prefs=prefs or {}
    )


def undercuts_in(shape):
    context = list(analyse(shape).values())[0]
    return context.recognition.of_type(FeatureType.UNDERCUT)


def rule_check(shape, rule, severity="WARNING", prefs=None):
    data = analyse(shape, prefs)
    check_class = get_check_class(rule)
    assert check_class is not None
    return check_class().run_check(
        data,
        RuleLimit(target="N/A", limit="N/A", binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


# -- shapes -------------------------------------------------------------------


def make_closed_chamber() -> TopoDS_Shape:
    """A wide chamber under a narrow neck, closed at both ends.

    Nothing reaches the chamber walls: the neck is too narrow to admit a
    cutter that could swing out, and the ends are walled in.
    """
    necked = _cut(block(), _cavity((45, 20, 25), (55, 60, 51)))
    return _cut(necked, _cavity((30, 15, 10), (70, 65, 26)))


def make_through_chamber() -> TopoDS_Shape:
    """The same chamber run right through the part.

    A T-slot cutter enters from the end, so this is awkward but reachable.
    """
    necked = _cut(block(), _cavity((45, -1, 25), (55, 81, 51)))
    return _cut(necked, _cavity((30, -1, 10), (70, 81, 26)))


def make_open_pocket() -> TopoDS_Shape:
    return _cut(block(), _cavity((20, 20, 25), (80, 60, 51)))


def make_through_hole() -> TopoDS_Shape:
    drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, -1), gp_Dir(0, 0, 1)), 6.0, 60.0)
    return _cut(block(), drill.Shape())


# =============================================================================


class TestReachability(unittest.TestCase):
    def test_an_outer_face_is_reachable(self):
        shape = block()
        reach = Reachability(shape)
        graph = list(analyse(shape).values())[0].graph
        top = max(graph.nodes, key=lambda n: n.centroid.Z())
        self.assertTrue(reach.reachable_from_any_cardinal(top.centroid, top.outward_normal))

    def test_a_pocket_floor_is_reachable_from_above(self):
        shape = make_open_pocket()
        reach = Reachability(shape)
        graph = list(analyse(shape).values())[0].graph
        floor = min(
            (n for n in graph.nodes if n.outward_normal and n.outward_normal.Z() > 0.9),
            key=lambda n: n.centroid.Z(),
        )
        self.assertTrue(reach.reachable_from_any_cardinal(floor.centroid, floor.outward_normal))


class TestUndercutRecognition(unittest.TestCase):
    def test_plain_block_has_no_undercut(self):
        self.assertEqual(undercuts_in(block()), [])

    def test_open_pocket_has_no_undercut(self):
        self.assertEqual(undercuts_in(make_open_pocket()), [])

    def test_a_hole_is_not_an_undercut(self):
        # A bore is unreachable from the side by nature. Saying so about
        # every hole would bury the findings that matter, and the hole rules
        # cover access anyway.
        self.assertEqual(undercuts_in(make_through_hole()), [])

    def test_closed_chamber_is_undercut(self):
        found = undercuts_in(make_closed_chamber())
        self.assertTrue(found, "a chamber closed on every side should be unreachable")

    def test_chamber_open_at_both_ends_is_not(self):
        # A cutter enters from the end, so this is awkward rather than
        # impossible. Reporting it would be a false positive on every T-slot.
        self.assertEqual(undercuts_in(make_through_chamber()), [])

    def test_undercuts_record_the_blocked_direction(self):
        for undercut in undercuts_in(make_closed_chamber()):
            self.assertEqual(len(undercut.param("blocked_tad")), 3)


class TestAccessRules(unittest.TestCase):
    def test_undercut_is_reported(self):
        findings = rule_check(make_closed_chamber(), Rulebook.UNDERCUT_PRESENT)
        self.assertTrue(findings)
        self.assertEqual(findings[0].severity, Severity.WARNING)

    def test_the_message_suggests_a_way_in(self):
        message = rule_check(make_closed_chamber(), Rulebook.UNDERCUT_PRESENT)[0].message
        self.assertTrue(
            any(term in message for term in ("dovetail", "EDM", "grooving", "flipped"))
        )

    def test_open_geometry_is_clean(self):
        self.assertEqual(rule_check(make_open_pocket(), Rulebook.UNDERCUT_PRESENT), [])

    def test_five_axis_has_no_undercuts(self):
        # The tool can tilt, so most of these stop being undercuts at all.
        self.assertEqual(
            rule_check(
                make_closed_chamber(),
                Rulebook.UNDERCUT_PRESENT,
                prefs={"MachiningMachineMode": "5axis"},
            ),
            [],
        )

    def test_reachable_part_is_never_blocked(self):
        self.assertEqual(rule_check(make_open_pocket(), Rulebook.TOOL_ACCESS_BLOCKED), [])


if __name__ == "__main__":
    unittest.main()
