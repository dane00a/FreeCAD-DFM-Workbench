# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the whole-part rules.

The sharp-edge rule is almost entirely suppression, so that is what these
tests are about. Every cavity on a part meets its host face at a sharp
concave corner, every bore meets the surface it enters -- and all of those
are reported by the rule that owns the feature. A sharp-edge finding on a
plain pocket would be a duplicate, and enough duplicates make the whole
result panel worthless.

What must survive is the corner nobody else speaks for -- the inside angle
of an L bracket. That makes the rule's count on a part a reading of how
completely the part was recognized, which is worth knowing when interpreting
it.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
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


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def _box(p0, p1) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def block() -> TopoDS_Shape:
    return _box((0, 0, 0), (80, 80, 40))


def l_bracket() -> TopoDS_Shape:
    """Two slabs meeting at a right angle, with nothing else on the part.

    The inside corner is genuinely sharp and no feature owns it, so this is
    the case the rule exists for.
    """
    return _fuse(_box((0, 0, 0), (80, 80, 10)), _box((0, 0, 0), (10, 80, 60)))


def square_pocket() -> TopoDS_Shape:
    return _cut(block(), _box((20, 20, 25), (60, 60, 41)))


def crossing_slots() -> TopoDS_Shape:
    first = _cut(block(), _box((-1, 35, 30), (81, 45, 41)))
    return _cut(first, _box((35, -1, 30), (45, 81, 41)))


def drilled_block() -> TopoDS_Shape:
    drill = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(40, 40, -1), gp_Dir(0, 0, 1)), 8.0, 42.0
    ).Shape()
    return _cut(block(), drill)


def sub_tool_slot() -> TopoDS_Shape:
    """A slot two tenths wide -- narrower than any cutter in the library."""
    return _cut(block(), _box((20, 39.8, 35), (60, 40.0, 41)))


def rule_check(shape, rule, limit="N/A", target="N/A"):
    check_class = get_check_class(rule)
    assert check_class is not None, rule
    data = MachiningAnalyzer().execute(
        shape, FaceIndex(shape), EdgeIndex(shape), prefs={}
    )
    return check_class().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity="WARNING"),
        rule,
        feedback=RuleFeedback(),
    )


# =============================================================================


class TestMinimumFeatureSize(unittest.TestCase):
    RULE = Rulebook.MINIMUM_FEATURE_SIZE

    def test_a_sub_tool_slot_is_reported(self):
        findings = rule_check(sub_tool_slot(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.WARNING)

    def test_the_message_names_another_process(self):
        message = rule_check(sub_tool_slot(), self.RULE)[0].message
        self.assertIn("EDM", message)

    def test_an_ordinary_pocket_passes(self):
        self.assertEqual(rule_check(square_pocket(), self.RULE), [])

    def test_a_plain_block_passes(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_an_ordinary_hole_passes(self):
        self.assertEqual(rule_check(drilled_block(), self.RULE), [])


class TestSharpInternalEdge(unittest.TestCase):
    RULE = Rulebook.SHARP_INTERNAL_EDGE

    def test_an_unclaimed_inside_corner_is_reported(self):
        findings = rule_check(l_bracket(), self.RULE)
        self.assertTrue(findings, "the inside angle of an L bracket is a sharp corner")

    def test_the_message_offers_a_way_out(self):
        message = rule_check(l_bracket(), self.RULE)[0].message
        self.assertTrue(any(term in message for term in ("radius", "EDM", "relief")))

    def test_a_recognized_cavity_speaks_for_its_own_corners(self):
        # Two crossing slots read as one through-cavity, and the cavity rules
        # report its corners with the context to say something useful about
        # them. A per-edge duplicate would only crowd them out.
        self.assertEqual(rule_check(crossing_slots(), self.RULE), [])

    def test_the_rule_reports_only_what_nothing_else_claims(self):
        # This is the shape of the rule: it fires on the residue left after
        # every recognizer has taken what it understands. Its count on a part
        # is therefore a reading of how well the part was recognized, which
        # is worth knowing when interpreting it.
        findings = rule_check(l_bracket(), self.RULE)
        context = list(
            MachiningAnalyzer()
            .execute(
                l_bracket(),
                FaceIndex(l_bracket()),
                EdgeIndex(l_bracket()),
                prefs={},
            )
            .values()
        )[0]
        self.assertEqual(context.recognition.features, [])
        self.assertTrue(findings)

    def test_a_pocket_does_not_double_report(self):
        # A pocket's corners are square too, but the corner-radius rule
        # already says so with tooling context. Reporting them again per
        # edge is how a results panel becomes unreadable.
        self.assertEqual(rule_check(square_pocket(), self.RULE), [])

    def test_a_bore_rim_is_not_a_corner_to_cut(self):
        # A drill necessarily leaves a concave rim where it breaks the
        # surface. That is the shape of the hole, not a corner anyone has to
        # form.
        self.assertEqual(rule_check(drilled_block(), self.RULE), [])

    def test_a_plain_block_is_clean(self):
        self.assertEqual(rule_check(block(), self.RULE), [])


class TestFeatureComplexity(unittest.TestCase):
    RULE = Rulebook.FEATURE_COMPLEXITY

    def test_a_simple_part_is_not_complex(self):
        self.assertEqual(rule_check(square_pocket(), self.RULE), [])

    def test_a_busy_part_is_reported(self):
        # Thirty-six drillings: nothing individually wrong, which is the
        # whole point of the rule.
        shape = block()
        for row in range(6):
            for column in range(6):
                drill = BRepPrimAPI_MakeCylinder(
                    gp_Ax2(gp_Pnt(10 + row * 12, 10 + column * 12, -1), gp_Dir(0, 0, 1)),
                    2.0,
                    42.0,
                ).Shape()
                shape = _cut(shape, drill)
        findings = rule_check(shape, self.RULE, target="10", limit="60")
        self.assertTrue(findings)
        self.assertIn("distinct features", findings[0].message)

    def test_the_shop_can_set_its_own_threshold(self):
        # A shop that programs quickly raises the bar and stops hearing
        # about it.
        self.assertEqual(
            rule_check(crossing_slots(), self.RULE, target="500", limit="900"), []
        )


class TestMarkingRules(unittest.TestCase):
    def test_an_unmarked_part_reports_nothing(self):
        self.assertEqual(rule_check(block(), Rulebook.PART_MARKING), [])
        self.assertEqual(
            rule_check(block(), Rulebook.RAISED_TEXT_MACHINED_FACE), []
        )

    def test_a_plain_pocket_is_not_text(self):
        self.assertEqual(rule_check(square_pocket(), Rulebook.PART_MARKING), [])


if __name__ == "__main__":
    unittest.main()
