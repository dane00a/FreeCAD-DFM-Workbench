# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the pocket and slot rules.

Several of these limits come from the tool library rather than from the
material, so the tests check that the answer moves when the tooling does: a
pocket too narrow for a shop with 6mm cutters is fine for one with 1mm
cutters, and the rule has to say so.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.config import MachiningConfig, ToolEntry
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
    """The 120 x 90 x 50 block every cavity is cut into."""
    return BRepPrimAPI_MakeBox(120.0, 90.0, 50.0).Shape()


def analyse(shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def rule_check(shape, rule, target="N/A", limit="N/A", severity="WARNING"):
    data = analyse(shape)
    check_class = get_check_class(rule)
    assert check_class is not None
    return check_class().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


def severities(findings):
    return [f.severity for f in findings]


# -- shapes -------------------------------------------------------------------


def make_shallow_pocket() -> TopoDS_Shape:
    """80 x 50 and 20mm deep: comfortable proportions."""
    return _cut(block(), _cavity((20, 20, 30), (100, 70, 51)))


def make_deep_pocket() -> TopoDS_Shape:
    """25 wide and 45 deep: under two times its width."""
    return _cut(block(), _cavity((48, 30, 5), (73, 62, 51)))


def make_very_deep_pocket() -> TopoDS_Shape:
    """6 wide and 45 deep: seven and a half times its width."""
    return _cut(block(), _cavity((57, 30, 5), (63, 62, 51)))


def make_narrow_pocket() -> TopoDS_Shape:
    """1.5mm across and nearly square, so it stays a pocket.

    Elongate it and it becomes a slot instead, which is the right answer for
    a long thin cavity but not what this rule is about.
    """
    return _cut(block(), _cavity((59.25, 44, 40), (60.75, 46.5, 51)))


def make_slot(width, depth, length=None) -> TopoDS_Shape:
    """A channel of the given width and depth, run across the block."""
    half = width / 2.0
    start = -1.0 if length is None else (90.0 - length) / 2.0
    end = 91.0 if length is None else start + length
    return _cut(block(), _cavity((60 - half, start, 50 - depth), (60 + half, end, 51)))


# =============================================================================


class TestPocketDepthRatio(unittest.TestCase):
    RULE = Rulebook.POCKET_DEPTH_RATIO

    def test_shallow_pocket_is_clean(self):
        self.assertEqual(rule_check(make_shallow_pocket(), self.RULE), [])

    def test_moderately_deep_pocket_is_clean(self):
        self.assertEqual(rule_check(make_deep_pocket(), self.RULE), [])

    def test_very_deep_pocket_is_an_error(self):
        self.assertEqual(
            severities(rule_check(make_very_deep_pocket(), self.RULE)), [Severity.ERROR]
        )

    def test_threshold_pair(self):
        # 45mm deep on 12mm wide is 3.75 times; on 10mm wide it is 4.5.
        under = _cut(block(), _cavity((54, 30, 5), (66, 62, 51)))
        over = _cut(block(), _cavity((55, 30, 5), (65, 62, 51)))
        self.assertEqual(rule_check(under, self.RULE, "4.0", "6.0"), [])
        self.assertEqual(
            severities(rule_check(over, self.RULE, "4.0", "6.0")), [Severity.WARNING]
        )


class TestPocketCornerRadius(unittest.TestCase):
    RULE = Rulebook.POCKET_CORNER_RADIUS

    def test_square_cornered_pocket_is_reported(self):
        findings = rule_check(make_shallow_pocket(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertIn("square", findings[0].overview)

    def test_the_message_names_the_process_needed(self):
        # A closed pocket needs sinker EDM; a through cut can be wired.
        message = rule_check(make_shallow_pocket(), self.RULE)[0].message
        self.assertIn("sinker EDM", message)

    def test_the_message_suggests_a_workable_radius(self):
        message = rule_check(make_shallow_pocket(), self.RULE)[0].message
        self.assertIn("would let it be milled", message)


class TestPocketNarrowOpening(unittest.TestCase):
    RULE = Rulebook.POCKET_NARROW_OPENING

    def test_ordinary_pocket_is_clean(self):
        self.assertEqual(rule_check(make_shallow_pocket(), self.RULE), [])

    def test_pocket_narrower_than_any_cutter_is_reported(self):
        findings = rule_check(make_narrow_pocket(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertLess(findings[0].value, findings[0].limit)

    def test_the_answer_depends_on_the_tool_library(self):
        # The same 8mm pocket is fine for a shop with small cutters and
        # impossible for one whose smallest end mill is 6mm.
        # Kept nearly square so it stays a pocket rather than a slot.
        pocket = _cut(block(), _cavity((56, 38, 35), (64, 52, 51)))
        self.assertEqual(rule_check(pocket, self.RULE), [])

        data = analyse(pocket)
        coarse = MachiningConfig()
        coarse.tool_library = [
            ToolEntry(
                type="end_mill", min_diameter_mm=6.0, max_diameter_mm=6.0, unit="metric"
            )
        ]
        list(data.values())[0].config = coarse
        check_class = get_check_class(self.RULE)
        findings = check_class().run_check(
            data,
            RuleLimit(target="N/A", limit="N/A", binary_severity="WARNING"),
            self.RULE,
            feedback=RuleFeedback(),
        )
        self.assertEqual(len(findings), 1, "a 6mm-cutter shop cannot clear an 8mm pocket")


class TestSlotRules(unittest.TestCase):
    def test_shallow_slot_is_clean(self):
        self.assertEqual(
            rule_check(make_slot(width=20.0, depth=10.0), Rulebook.SLOT_DEPTH_RATIO), []
        )

    def test_deep_slot_is_reported(self):
        shape = make_slot(width=8.0, depth=40.0)  # five times its width
        self.assertEqual(
            severities(rule_check(shape, Rulebook.SLOT_DEPTH_RATIO)), [Severity.WARNING]
        )

    def test_slot_depth_limit_is_configurable(self):
        shape = make_slot(width=8.0, depth=32.0)  # four times its width
        self.assertTrue(rule_check(shape, Rulebook.SLOT_DEPTH_RATIO, limit="3.0"))
        self.assertEqual(rule_check(shape, Rulebook.SLOT_DEPTH_RATIO, limit="5.0"), [])

    def test_long_deep_slot_reports_overhang(self):
        self.assertTrue(
            rule_check(make_slot(width=6.0, depth=30.0), Rulebook.SLOT_OVERHANG)
        )

    def test_long_but_shallow_slot_does_not(self):
        # A long groove is fine when the tool is short. Both conditions have
        # to hold together, which is why this rule is separate from depth.
        self.assertEqual(
            rule_check(make_slot(width=10.0, depth=8.0), Rulebook.SLOT_OVERHANG), []
        )

    def test_deep_but_short_slot_does_not(self):
        shape = make_slot(width=10.0, depth=30.0, length=40.0)
        self.assertEqual(rule_check(shape, Rulebook.SLOT_OVERHANG), [])


if __name__ == "__main__":
    unittest.main()
