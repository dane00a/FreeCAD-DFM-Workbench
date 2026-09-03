# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the boss and rib rules.

Everything a protrusion rule says depends on the ratio between two numbers the
recognizer measured, so the fixtures are built to make those ratios exact: a
24 mm boss standing 20 mm, a 3 mm web standing 25. A rule reading the wrong
dimension -- a rib's run instead of its height, a pad's long side instead of
its narrow one -- gets a plausible answer and the wrong verdict, and only
fixed proportions catch that.

The draft rule is the odd one out. It is about pulling a pattern out of a
mould, so on a milled part it has to stay silent no matter how square the rib
walls are.
"""

import math
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# =============================================================================
# Shapes
# =============================================================================


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def _box(p0, p1) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def block() -> TopoDS_Shape:
    """The 100 x 80 x 40 billet every boss stands on."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def plate() -> TopoDS_Shape:
    """The 120 x 80 x 10 plate every rib stands on."""
    return BRepPrimAPI_MakeBox(120.0, 80.0, 10.0).Shape()


def make_round_boss(diameter: float, height: float) -> TopoDS_Shape:
    """A spigot of the given size standing off the middle of the billet top."""
    spigot = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(50, 40, 40), gp_Dir(0, 0, 1)), diameter / 2.0, height
    )
    return _fuse(block(), spigot.Shape())


def make_leaning_boss(diameter: float, height: float) -> TopoDS_Shape:
    """The same spigot at 45 degrees, so no machine axis looks straight down it."""
    spigot = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(50, 40, 40), gp_Dir(1, 0, 1)), diameter / 2.0, height
    )
    return _fuse(block(), spigot.Shape())


def make_side_boss(diameter: float, height: float) -> TopoDS_Shape:
    """A spigot on the billet's side face.

    Routine work: the part gets stood on end and the boss is a top face again.
    """
    spigot = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(100, 40, 20), gp_Dir(1, 0, 0)), diameter / 2.0, height
    )
    return _fuse(block(), spigot.Shape())


def make_square_boss(side: float, height: float) -> TopoDS_Shape:
    """A square mounting pad standing off the billet top."""
    return make_rectangular_boss(side, side, height)


def make_rectangular_boss(width: float, length: float, height: float) -> TopoDS_Shape:
    """A mounting pad of the given plan size standing off the billet top."""
    return _fuse(
        block(),
        _box(
            (50 - width / 2.0, 40 - length / 2.0, 40),
            (50 + width / 2.0, 40 + length / 2.0, 40 + height),
        ),
    )


def make_rib(thickness: float, height: float) -> TopoDS_Shape:
    """One web standing off the plate, 60 mm long, with square walls."""
    x0 = 60.0 - thickness / 2.0
    return _fuse(plate(), _box((x0, 10, 10), (x0 + thickness, 70, 10 + height)))


def make_drafted_rib(thickness: float, height: float, draft_deg: float) -> TopoDS_Shape:
    """The same web with both walls leaning in by `draft_deg`, so it pulls."""
    shrink = height * math.tan(math.radians(draft_deg))
    x0 = 60.0 - thickness / 2.0
    x1 = 60.0 + thickness / 2.0
    z0, z1 = 10.0, 10.0 + height

    profile = BRepBuilderAPI_MakePolygon()
    for point in (
        (x0, 10.0, z0),
        (x1, 10.0, z0),
        (x1 - shrink, 10.0, z1),
        (x0 + shrink, 10.0, z1),
    ):
        profile.Add(gp_Pnt(*point))
    profile.Close()

    face = BRepBuilderAPI_MakeFace(profile.Wire()).Face()
    web = BRepPrimAPI_MakePrism(face, gp_Vec(0, 60, 0)).Shape()
    return _fuse(plate(), web)


# =============================================================================
# Harness
# =============================================================================


AS_CAST = {"MachiningBlankForm": "as_cast"}


def analyse(shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def rule_check(shape, rule, target="N/A", limit="N/A", severity="WARNING", prefs=None):
    data = analyse(shape, prefs)
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


# =============================================================================


class TestBossHeightRatio(unittest.TestCase):
    RULE = Rulebook.BOSS_HEIGHT_RATIO

    def test_plain_billet_is_clean(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_a_stocky_boss_is_clean(self):
        # 24 mm across and 20 mm tall: shorter than it is wide.
        self.assertEqual(rule_check(make_round_boss(24.0, 20.0), self.RULE), [])

    def test_a_tall_boss_is_a_warning(self):
        # Five times its own diameter, past the 4x the shop is happy with.
        findings = rule_check(make_round_boss(10.0, 50.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 5.0, places=3)

    def test_a_very_tall_boss_is_an_error(self):
        findings = rule_check(make_round_boss(6.0, 60.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.ERROR])
        self.assertAlmostEqual(findings[0].value, 10.0, places=3)

    def test_a_pad_is_judged_on_its_narrow_side(self):
        # A 12 x 30 pad standing 22 mm bends about its 12 mm side, so the
        # ratio is 1.8 and not the 0.7 the long side would give.
        findings = rule_check(
            make_rectangular_boss(12.0, 30.0, 22.0), self.RULE, target="1.5"
        )
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 22.0 / 12.0, places=3)

    def test_the_thresholds_are_configurable(self):
        shape = make_round_boss(10.0, 50.0)  # five times its diameter
        self.assertEqual(rule_check(shape, self.RULE, target="6.0"), [])
        self.assertEqual(
            severities(rule_check(shape, self.RULE, target="2.0", limit="4.0")),
            [Severity.ERROR],
        )


class TestBossWallThickness(unittest.TestCase):
    RULE = Rulebook.BOSS_WALL_THICKNESS

    def test_plain_billet_is_clean(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_a_normal_boss_is_clean(self):
        self.assertEqual(rule_check(make_round_boss(24.0, 20.0), self.RULE), [])

    def test_a_pin_thin_boss_is_a_warning(self):
        findings = rule_check(make_round_boss(2.5, 10.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 2.5, places=3)
        self.assertAlmostEqual(findings[0].limit, 3.0, places=3)

    def test_a_square_pad_is_not_judged(self):
        # The rule is about a round post's section. A pad's stiffness is the
        # height ratio's business.
        self.assertEqual(rule_check(make_square_boss(20.0, 20.0), self.RULE), [])

    def test_the_minimum_is_configurable(self):
        shape = make_round_boss(6.0, 10.0)
        self.assertEqual(rule_check(shape, self.RULE), [])
        self.assertEqual(
            severities(rule_check(shape, self.RULE, target="10.0")), [Severity.WARNING]
        )


class TestBossUndercut(unittest.TestCase):
    RULE = Rulebook.BOSS_UNDERCUT

    def test_plain_billet_is_clean(self):
        self.assertEqual(rule_check(block(), self.RULE), [])

    def test_an_upright_boss_is_clean(self):
        self.assertEqual(rule_check(make_round_boss(24.0, 20.0), self.RULE), [])

    def test_a_side_boss_is_clean(self):
        # Along a machine axis, just not the one that was up when the part was
        # drawn. Stand it on end and it is an ordinary boss.
        self.assertEqual(rule_check(make_side_boss(12.0, 20.0), self.RULE), [])

    def test_a_leaning_boss_needs_another_setup(self):
        findings = rule_check(make_leaning_boss(12.0, 30.0), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 45.0, places=1)

    def test_the_finding_says_what_it_costs(self):
        message = rule_check(make_leaning_boss(12.0, 30.0), self.RULE)[0].message
        self.assertIn("setup", message)

    def test_a_square_pad_carries_no_axis(self):
        self.assertEqual(rule_check(make_square_boss(20.0, 20.0), self.RULE), [])

    def test_the_severity_follows_the_rule_config(self):
        shape = make_leaning_boss(12.0, 30.0)
        findings = rule_check(shape, self.RULE, severity="ERROR")
        self.assertEqual(severities(findings), [Severity.ERROR])


class TestRibHeightAspect(unittest.TestCase):
    RULE = Rulebook.RIB_HEIGHT_ASPECT

    def test_bare_plate_is_clean(self):
        self.assertEqual(rule_check(plate(), self.RULE), [])

    def test_a_stout_rib_is_clean(self):
        # 4 mm thick and 12 mm tall: three times its thickness.
        self.assertEqual(rule_check(make_rib(4.0, 12.0), self.RULE), [])

    def test_a_rib_at_the_limit_is_clean(self):
        # Exactly five times, which is the threshold rather than past it.
        self.assertEqual(rule_check(make_rib(4.0, 20.0), self.RULE), [])

    def test_a_slender_rib_is_a_warning(self):
        findings = rule_check(make_rib(3.0, 25.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 25.0 / 3.0, places=3)

    def test_the_ratio_uses_height_not_length(self):
        # The web runs 60 mm and stands 25. Reading the run as the height
        # would give 20:1 and a much louder finding than the rib deserves.
        findings = rule_check(make_rib(3.0, 25.0), self.RULE)
        self.assertLess(findings[0].value, 10.0)

    def test_the_thresholds_are_configurable(self):
        shape = make_rib(3.0, 25.0)  # 8.3 times its thickness
        self.assertEqual(rule_check(shape, self.RULE, target="10.0"), [])
        self.assertEqual(
            severities(rule_check(shape, self.RULE, target="4.0", limit="6.0")),
            [Severity.ERROR],
        )


class TestRibDraftAngle(unittest.TestCase):
    RULE = Rulebook.RIB_DRAFT_ANGLE

    def test_a_milled_rib_is_not_asked_for_draft(self):
        # Square walls are what an end mill produces, and there is nothing to
        # pull the part out of.
        self.assertEqual(rule_check(make_rib(3.0, 25.0), self.RULE), [])

    def test_an_as_cast_rib_without_draft_is_a_note(self):
        findings = rule_check(make_rib(3.0, 25.0), self.RULE, prefs=AS_CAST)
        self.assertEqual(severities(findings), [Severity.INFO])
        self.assertAlmostEqual(findings[0].value, 0.0, places=3)
        self.assertAlmostEqual(findings[0].limit, 1.0, places=3)

    def test_a_drafted_rib_is_clean(self):
        # Three degrees a side, measured off the walls rather than assumed.
        self.assertEqual(
            rule_check(make_drafted_rib(5.0, 20.0, 3.0), self.RULE, prefs=AS_CAST), []
        )

    def test_the_draft_is_measured_not_assumed(self):
        # The same drafted rib against a mould that wants five degrees: the
        # rule has to report the three it found, not a flat zero.
        findings = rule_check(
            make_drafted_rib(5.0, 20.0, 3.0), self.RULE, limit="5.0", prefs=AS_CAST
        )
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].value, 3.0, places=1)

    def test_bare_plate_is_clean(self):
        self.assertEqual(rule_check(plate(), self.RULE, prefs=AS_CAST), [])

    def test_the_note_talks_about_the_mould(self):
        message = rule_check(make_rib(3.0, 25.0), self.RULE, prefs=AS_CAST)[0].message
        self.assertIn("pattern", message)


class TestReporting(unittest.TestCase):
    def test_a_well_proportioned_boss_trips_no_protrusion_rule(self):
        shape = make_round_boss(24.0, 20.0)
        for rule in (
            Rulebook.BOSS_HEIGHT_RATIO,
            Rulebook.BOSS_WALL_THICKNESS,
            Rulebook.BOSS_UNDERCUT,
            Rulebook.RIB_HEIGHT_ASPECT,
            Rulebook.RIB_DRAFT_ANGLE,
        ):
            self.assertEqual(rule_check(shape, rule), [], rule.name)

    def test_a_plain_billet_trips_no_protrusion_rule(self):
        for rule in (
            Rulebook.BOSS_HEIGHT_RATIO,
            Rulebook.BOSS_WALL_THICKNESS,
            Rulebook.BOSS_UNDERCUT,
            Rulebook.RIB_HEIGHT_ASPECT,
            Rulebook.RIB_DRAFT_ANGLE,
        ):
            self.assertEqual(rule_check(block(), rule), [], rule.name)

    def test_findings_highlight_the_feature(self):
        finding = rule_check(make_rib(3.0, 25.0), Rulebook.RIB_HEIGHT_ASPECT)[0]
        self.assertTrue(finding.failing_geometry)
        self.assertTrue(all(kind == "Face" for kind, _ in finding.failing_geometry))


if __name__ == "__main__":
    unittest.main()
