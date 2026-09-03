# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the sheet-metal rules.

Every fixture is a folded part, because nothing else classifies as sheet metal
and every rule in the family stands down on any other verdict. The bracket
builders come from the recognizer tests, which is deliberate: a rule tested
against geometry the recognizers were never shown proves nothing about what
the workbench will actually report. Each test class asserts its own fixture
still classifies SHEET_METAL before asking anything else, so a fixture that
quietly stops being sheet fails loudly rather than passing by silence.

Two harnesses, and the difference matters. `sheet_context` runs the sheet
recognizers directly, the way the recognizer tests do; `analysed` runs the
whole analyzer. The full pipeline's resolver has no priority entries for the
sheet feature types yet, so a recognized notch loses to the step reading of the
same faces and a formed hood loses to a pocket. Until the resolver learns about
them, the rules that key on tabs, notches and formed features have to be tested
on the sheet pipeline; the bend rules are exercised through the analyzer as
well, which is what proves the wiring.
"""

import unittest

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import CONTEXT_KEY, MachiningAnalyzer
from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.config import MachiningConfig
from freecad.DFM.core.machining.context import MachiningContext
from freecad.DFM.core.machining.features import FeatureType, RecognitionResult
from freecad.DFM.core.machining.process_classifier import (
    PartProcessType,
    classify_part_process,
)
from freecad.DFM.core.machining.recognizers.bend_recognizer import BendRecognizer
from freecad.DFM.core.machining.recognizers.hole_recognizer import HoleRecognizer
from freecad.DFM.core.machining.recognizers.sheet_formed_recognizer import (
    SheetFormedRecognizer,
)
from freecad.DFM.core.machining.recognizers.sheet_outline_recognizer import (
    SheetOutlineRecognizer,
)
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex

# The decorators only run when the package is imported, and nothing else in
# this module reaches it.
import freecad.DFM.core.checks.sheet  # noqa: F401

from test_machining_sheet import (
    GAUGE,
    INNER_RADIUS,
    bracket,
    embossed_bracket,
    hemmed_bracket,
    notched_bracket,
    tabbed_bracket,
)


# =============================================================================
# Shape helpers
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


def _drill(x: float, y: float, radius: float) -> TopoDS_Shape:
    """A cutting cylinder straight down through the base of a bracket."""
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(x, y, -1.0), gp_Dir(0, 0, 1)), radius, 5.0
    ).Shape()


def _fillet_edges_at_z(shape: TopoDS_Shape, z: float, radius: float) -> TopoDS_Shape:
    """Round every edge lying wholly in one horizontal plane."""
    fillet = BRepFilletAPI_MakeFillet(shape)
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        curve = BRepAdaptor_Curve(edge)
        start = curve.Value(curve.FirstParameter())
        end = curve.Value(curve.LastParameter())
        if abs(start.Z() - z) < 1e-6 and abs(end.Z() - z) < 1e-6:
            fillet.Add(radius, edge)
    fillet.Build()
    return fillet.Shape()


# =============================================================================
# Fixtures
# =============================================================================


def open_bend_end() -> TopoDS_Shape:
    """A bracket whose flange stops halfway, with the blank running on past it.

    The fold covers 30 of the 50 mm, and the flat carries straight on over the
    full width of the fold footprint. That is what an unrelieved bend
    termination is, and the tear starts where the fold runs out.
    """
    return _fuse(bracket(width=30.0), _box((0, 30, 0), (60, 50, 2)))


def relieved_bend_end() -> TopoDS_Shape:
    """The same part with a relief slit cut across the fold at the free end."""
    return _cut(open_bend_end(), _box((-1, 29, -1), (6, 33, 3)))


def tray(size: float = 60.0, height: float = 25.0) -> TopoDS_Shape:
    """A four-sided pan: four bends off one base, meeting at four corners.

    Built as an outer solid with rounded bottom edges minus an inner one with
    its own smaller radius, so every fold arrives as a genuine coaxial cylinder
    pair one gauge apart rather than as a fillet.
    """
    outer = _fillet_edges_at_z(_box((0, 0, 0), (size, size, height)), 0.0, INNER_RADIUS + GAUGE)
    inner = _fillet_edges_at_z(
        _box((GAUGE, GAUGE, GAUGE), (size - GAUGE, size - GAUGE, height + 1.0)),
        GAUGE,
        INNER_RADIUS,
    )
    return _cut(outer, inner)


def relieved_tray() -> TopoDS_Shape:
    """The same pan with all four corners cut back clear of both bend zones."""
    shape = tray()
    for x0, y0 in ((-1, -1), (53, -1), (-1, 53), (53, 53)):
        shape = _cut(shape, _box((x0, y0, -1), (x0 + 8, y0 + 8, 40)))
    return shape


def walled_bracket() -> TopoDS_Shape:
    """A bracket with a second wall stood on the base at a square corner.

    One fold is modelled properly and the other is not, which is the case the
    sharp-fold rule exists for: the square junction carries no bend geometry
    for anything else to check.
    """
    return _fuse(bracket(), _box((55, 0, 2), (57, 50, 30)))


def sharp_shell() -> TopoDS_Shape:
    """An L of constant thickness with a square fold: sheet drawn as a solid."""
    return _fuse(_box((0, 0, 0), (60, 50, 2)), _box((0, 0, 2), (2, 50, 40)))


def machined_sharp_shell() -> TopoDS_Shape:
    """The same L with a flat-floored pocket cut into it.

    The pocket has solid stock behind its floor, which is exactly what formed
    sheet does not have, so the classifier reverts the part to milled -- and
    that is the one state the sheet-intent advisory speaks in.
    """
    return _cut(sharp_shell(), _box((20, 20, 1), (30, 30, 3)))


def thin_notch_bracket() -> TopoDS_Shape:
    """A bracket with a 1.5 mm bite out of the free edge of its base."""
    return _cut(bracket(), _box((50, 24, -1), (61, 25.5, 3)))


def thin_tab_bracket() -> TopoDS_Shape:
    """A bracket whose base ends in a 3 mm finger between two wide cuts."""
    shape = _cut(bracket(), _box((44, 10, -1), (61, 23.5, 3)))
    return _cut(shape, _box((44, 26.5, -1), (61, 40, 3)))


def emboss_bracket(depth: float = 5.0, x0: float = 25.0, x1: float = 41.0) -> TopoDS_Shape:
    """A bracket with one square emboss drawn out of the back of its base."""
    raised = _fuse(bracket(), _box((x0, 17.0, -depth), (x1, 33.0, 0.0)))
    return _cut(raised, _box((x0 + 2, 19.0, -depth + 2), (x1 - 2, 31.0, 3.0)))


def two_emboss_bracket(gap: float) -> TopoDS_Shape:
    """A bracket carrying two embosses with a chosen web of flat between them."""
    shape = bracket()
    shape = _cut(_fuse(shape, _box((20, 17, -5), (32, 33, 0))), _box((22, 19, -3), (30, 31, 3)))
    shape = _cut(
        _fuse(shape, _box((32 + gap, 17, -5), (44 + gap, 33, 0))),
        _box((34 + gap, 19, -3), (42 + gap, 31, 3)),
    )
    return shape


def lanced_bracket(height: float = 4.0) -> TopoDS_Shape:
    """A bracket with a bridge lance: a hood sheared open at both ends."""
    raised = _fuse(bracket(), _box((10, 17, 2), (50, 33, 2 + height)))
    return _cut(raised, _box((12, 15, -1), (48, 35, height)))


def countersunk_bracket(depth: float) -> TopoDS_Shape:
    """A bracket with one countersunk hole of the given cone depth."""
    shape = _cut(bracket(), _drill(30.0, 25.0, 2.0))
    cone = BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(30.0, 25.0, GAUGE - depth), gp_Dir(0, 0, 1)), 2.0, 4.0, depth
    ).Shape()
    return _cut(shape, cone)


def milled_block() -> TopoDS_Shape:
    """A plain block. Nothing here was formed, so no sheet rule may speak."""
    return BRepPrimAPI_MakeBox(80.0, 50.0, 30.0).Shape()


# =============================================================================
# Harness
# =============================================================================

_SHEET_PIPELINE = (
    HoleRecognizer,
    BendRecognizer,
    SheetOutlineRecognizer,
    SheetFormedRecognizer,
)


def sheet_context(shape: TopoDS_Shape, config=None) -> MachiningContext:
    """A context built from the sheet recognizers, as the rules see one."""
    config = config or MachiningConfig()
    face_index = FaceIndex(shape)
    graph = AagBuilder(shape, face_index).build()
    process = classify_part_process(graph, config.thresholds, shape)

    features = []
    for recognizer_class in _SHEET_PIPELINE:
        recognizer = recognizer_class()
        recognizer.config = config
        recognizer.part_process = process
        features.extend(recognizer.recognize(graph, shape, set(), list(features)))

    return MachiningContext(
        shape=shape,
        graph=graph,
        face_index=face_index,
        config=config,
        part_process=process,
        recognition=RecognitionResult(features=features),
    )


def analysed(shape: TopoDS_Shape, prefs=None) -> MachiningContext:
    """A context from the whole analyzer, resolver and all."""
    data = MachiningAnalyzer().execute(
        shape, FaceIndex(shape), EdgeIndex(shape), prefs=prefs or {}
    )
    return data[CONTEXT_KEY]


def run(context: MachiningContext, rule: Rulebook, **limits):
    """Run the check registered for a rule against a prepared context."""
    check_class = get_check_class(rule)
    assert check_class is not None, f"no check registered for {rule.name}"
    config = RuleLimit(
        target=limits.get("target", ""),
        limit=limits.get("limit", ""),
        binary_severity=limits.get("binary_severity", "ERROR"),
        min_value=limits.get("min_value", ""),
        max_value=limits.get("max_value", ""),
    )
    return check_class().run_check(
        {CONTEXT_KEY: context}, config, rule, feedback=RuleFeedback()
    )


def check(shape: TopoDS_Shape, rule: Rulebook, **limits):
    """Analyse a shape on the sheet pipeline and run one rule over it."""
    return run(sheet_context(shape), rule, **limits)


def severities(findings):
    return [finding.severity for finding in findings]


class SheetCase(unittest.TestCase):
    """Shared assertion: a fixture has to be sheet before anything else means anything."""

    def assert_sheet(self, context, gauge: float = GAUGE):
        self.assertIs(context.part_process.type, PartProcessType.SHEET_METAL)
        self.assertAlmostEqual(context.part_process.sheet_thickness_mm, gauge, places=3)
        return context


# =============================================================================
# The gate
# =============================================================================


class TestSheetGate(SheetCase):
    """Every rule but one stands down on a part that was not formed."""

    def test_no_sheet_rule_speaks_about_a_milled_block(self):
        context = sheet_context(milled_block())
        self.assertIsNot(context.part_process.type, PartProcessType.SHEET_METAL)
        for rule in Rulebook:
            if not rule.name.startswith("SHEET_"):
                continue
            if rule is Rulebook.SHEET_INTENT_SHARP_CORNERS:
                continue  # the one rule that is about milled parts
            with self.subTest(rule=rule.name):
                self.assertEqual(run(context, rule), [])

    def test_the_bracket_is_sheet_metal(self):
        self.assert_sheet(sheet_context(bracket()))

    def test_every_local_fixture_is_sheet_metal(self):
        for name, shape in (
            ("open bend end", open_bend_end()),
            ("relieved bend end", relieved_bend_end()),
            ("tray", tray()),
            ("relieved tray", relieved_tray()),
            ("walled bracket", walled_bracket()),
            ("thin notch", thin_notch_bracket()),
            ("thin tab", thin_tab_bracket()),
            ("emboss", emboss_bracket()),
            ("lance", lanced_bracket()),
            ("countersunk", countersunk_bracket(1.9)),
        ):
            with self.subTest(fixture=name):
                self.assert_sheet(sheet_context(shape))


# =============================================================================
# Bends
# =============================================================================


class TestBendRadius(SheetCase):
    RULE = Rulebook.SHEET_BEND_RADIUS_SMALL

    def test_a_generous_radius_is_clean(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_under_one_gauge_warns(self):
        findings = check(bracket(inner_radius=1.5), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 0.75, places=2)

    def test_under_half_a_gauge_is_an_error(self):
        findings = check(bracket(inner_radius=0.6), self.RULE)
        self.assertEqual(severities(findings), [Severity.ERROR])

    def test_a_hem_is_left_to_the_hem_rule(self):
        """A hem folds to nearly nothing by design; a hemming die owns it."""
        context = self.assert_sheet(sheet_context(hemmed_bracket()))
        hems = [f for f in context.recognition.features if f.param("is_hem")]
        self.assertEqual(len(hems), 1)
        self.assertLess(hems[0].number("inner_radius_mm"), GAUGE)
        self.assertEqual(run(context, self.RULE), [])

    def test_the_configured_factors_move_the_verdict(self):
        """A shop that will not run one gauge, and one that will run half."""
        # Three millimetres in two-gauge stock: one and a half gauges, and
        # comfortable by default.
        self.assertEqual(check(bracket(), self.RULE), [])
        # Ask for two gauges and the same bend no longer clears it.
        findings = check(bracket(), self.RULE, target="2.0", limit="1.0")
        self.assertEqual(severities(findings), [Severity.WARNING])
        # Accept half a gauge and a bend that warned by default goes quiet.
        self.assertEqual(
            check(bracket(inner_radius=1.5), self.RULE, target="0.5", limit="0.25"), []
        )


class TestFlangeShort(SheetCase):
    RULE = Rulebook.SHEET_FLANGE_SHORT

    def test_a_long_flange_is_clean(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_a_flange_the_die_cannot_grip_warns(self):
        findings = check(bracket(leg_z=12.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        # Four gauges plus the bend radius: 4 x 2 + 3.
        self.assertAlmostEqual(findings[0].limit, 11.0, places=3)
        self.assertLess(findings[0].value, findings[0].limit)

    def test_the_finding_points_at_the_short_panel(self):
        findings = check(bracket(leg_z=12.0), self.RULE)
        self.assertEqual(len(findings[0].failing_geometry), 1)
        self.assertEqual(findings[0].failing_geometry[0][0], "Face")

    def test_a_configured_figure_is_read_as_millimetres(self):
        """The rule is declared in millimetres, so a shop enters millimetres.

        The upright stands 35 mm off the bend line, which clears the default
        11 mm comfortably. A shop whose die needs 40 mm says so and gets the
        finding.
        """
        findings = check(bracket(), self.RULE, limit="40.0")
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].limit, 40.0, places=3)


class TestBendAngle(SheetCase):
    RULE = Rulebook.SHEET_BEND_ANGLE_EXTREME

    def test_a_right_angle_bend_is_clean(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_a_hem_in_heavy_gauge_draws_the_advisory(self):
        """The ceiling is banded, and 2 mm stock sits just above 14 gauge.

        A hem folds to 180 degrees, which is more than the brake reaches in
        this material -- and hems are deliberately not excluded, because a
        heavy-gauge hem is precisely the fold the rule exists to flag.
        """
        findings = check(hemmed_bracket(), self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        self.assertAlmostEqual(findings[0].value, 180.0, places=1)
        self.assertLess(findings[0].limit, 180.0)

    def test_a_configured_ceiling_overrides_the_band(self):
        self.assertEqual(check(hemmed_bracket(), self.RULE, limit="180.0"), [])


class TestDiagonalBend(SheetCase):
    RULE = Rulebook.SHEET_BEND_LONGER_THAN_BODY

    def test_a_bend_along_the_blank_is_clean(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_a_bend_spanning_the_whole_blank_is_clean(self):
        """Equal by construction, and the epsilon is what keeps it quiet.

        A 100 mm bend on a 100 mm blank is the most ordinary part in the shop.
        Without slack, float noise off the bounding box alone would decide it.
        """
        self.assertEqual(check(bracket(width=100.0), self.RULE), [])

    def test_a_bend_longer_than_the_part_warns(self):
        context = self.assert_sheet(sheet_context(bracket()))
        bend = context.recognition.of_type(FeatureType.BEND)[0]
        extent = max(context.plane_bbox_dims())
        bend.parameters["length_mm"] = extent + 10.0

        findings = run(context, self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, extent + 10.0, places=3)

    def test_thin_stock_is_exempt(self):
        """Gauge numbering runs backwards, and this rule is about heavy material."""
        context = self.assert_sheet(
            sheet_context(bracket(gauge=1.0, inner_radius=1.5)), gauge=1.0
        )
        bend = context.recognition.of_type(FeatureType.BEND)[0]
        bend.parameters["length_mm"] = max(context.plane_bbox_dims()) + 10.0
        self.assertEqual(run(context, self.RULE), [])


class TestClosedFlangeLoop(SheetCase):
    RULE = Rulebook.SHEET_CLOSED_FLANGE_LOOP

    def test_an_open_profile_is_clean(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_a_tray_is_still_an_open_profile(self):
        """Four flanges off one base is a star, not a ring."""
        self.assertEqual(check(tray(), self.RULE), [])

    def test_two_bends_joining_the_same_pair_of_panels_close_a_loop(self):
        context = self.assert_sheet(sheet_context(hemmed_bracket()))
        bends = context.recognition.of_type(FeatureType.BEND)
        self.assertEqual(len(bends), 2)
        first, second = bends
        second.parameters["panel_a"] = first.param("panel_a")
        second.parameters["panel_b"] = first.param("panel_b")

        findings = run(context, self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])


class TestHemDimensions(SheetCase):
    RULE = Rulebook.SHEET_HEM_DIMENSIONS

    def test_a_generous_return_is_clean(self):
        self.assertEqual(check(hemmed_bracket(), self.RULE), [])

    def test_a_short_return_warns(self):
        findings = check(hemmed_bracket(return_z=34.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        # Above 14 gauge the requirement is four times the material.
        self.assertAlmostEqual(findings[0].limit, 4.0 * GAUGE, places=3)

    def test_a_plain_bend_is_not_a_hem(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_a_closed_hem_on_heavy_stock_warns(self):
        context = self.assert_sheet(sheet_context(hemmed_bracket()))
        hem = [f for f in context.recognition.features if f.param("is_hem")][0]
        # A fold radius under half the gauge is a closed hem, and the cracking
        # only starts on material over 2 mm.
        hem.parameters["inner_radius_mm"] = 0.4
        context.part_process.sheet_thickness_mm = 3.0

        findings = run(context, self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])


class TestBendRelief(SheetCase):
    RULE = Rulebook.SHEET_BEND_RELIEF_MISSING

    def test_a_bend_running_the_full_edge_is_clean(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_a_bend_stopping_mid_panel_warns(self):
        findings = check(open_bend_end(), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_a_relief_slit_settles_it(self):
        """The test is for metal, not for a recognized notch.

        Reliefs are cut in every shape a laser will draw, and none of them
        reads back reliably as a feature. Probing the solid is right for all
        of them.
        """
        self.assertEqual(check(relieved_bend_end(), self.RULE), [])


class TestCornerRelief(SheetCase):
    RULE = Rulebook.SHEET_CORNER_RELIEF_MISSING

    def test_a_single_bend_has_no_corner(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_a_pan_with_uncut_corners_warns_at_each_one(self):
        findings = check(tray(), self.RULE)
        self.assertEqual(len(findings), 4)
        self.assertEqual(set(severities(findings)), {Severity.WARNING})

    def test_cutting_the_corners_back_settles_it(self):
        self.assertEqual(check(relieved_tray(), self.RULE), [])


# =============================================================================
# Holes
# =============================================================================


class TestHoleSmall(SheetCase):
    RULE = Rulebook.SHEET_HOLE_SMALL

    def test_a_hole_wider_than_the_gauge_is_clean(self):
        self.assertEqual(check(_cut(bracket(), _drill(30.0, 25.0, 4.0)), self.RULE), [])

    def test_a_hole_under_the_gauge_warns(self):
        findings = check(_cut(bracket(), _drill(30.0, 25.0, 0.5)), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 0.5, places=2)  # 1.0 mm in 2.0 mm

    def test_a_configured_multiple_moves_the_floor(self):
        shape = _cut(bracket(), _drill(30.0, 25.0, 4.0))
        findings = check(shape, self.RULE, limit="5.0")
        self.assertEqual(severities(findings), [Severity.WARNING])


class TestHoleNearBend(SheetCase):
    RULE = Rulebook.SHEET_HOLE_NEAR_BEND

    def test_a_hole_clear_of_the_fold_is_clean(self):
        self.assertEqual(check(_cut(bracket(), _drill(30.0, 25.0, 4.0)), self.RULE), [])

    def test_a_hole_inside_the_deformation_zone_warns(self):
        findings = check(_cut(bracket(), _drill(11.0, 25.0, 3.0)), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        # Two and a half gauges plus the bend radius: 2.5 x 2 + 3.
        self.assertAlmostEqual(findings[0].limit, 8.0, places=3)

    def test_the_clearance_is_measured_to_the_hole_edge(self):
        findings = check(_cut(bracket(), _drill(11.0, 25.0, 3.0)), self.RULE)
        # Centre 6 mm out from the tangent line, less a 3 mm radius.
        self.assertAlmostEqual(findings[0].value, 3.0, places=2)

    def test_a_part_with_no_bends_says_nothing(self):
        context = self.assert_sheet(sheet_context(_cut(bracket(), _drill(11.0, 25.0, 3.0))))
        context.recognition.features = [
            f for f in context.recognition.features if f.type != FeatureType.BEND
        ]
        self.assertEqual(run(context, self.RULE), [])


class TestHolePitch(SheetCase):
    RULE = Rulebook.SHEET_HOLE_PITCH

    def test_a_single_hole_has_no_pitch(self):
        self.assertEqual(check(_cut(bracket(), _drill(30.0, 25.0, 4.0)), self.RULE), [])

    def test_a_thin_web_between_two_holes_warns(self):
        shape = _cut(_cut(bracket(), _drill(25.0, 25.0, 3.0)), _drill(32.0, 25.0, 3.0))
        findings = check(shape, self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 1.0, places=2)
        self.assertAlmostEqual(findings[0].limit, 2.0 * GAUGE, places=3)

    def test_well_spaced_holes_are_clean(self):
        shape = _cut(_cut(bracket(), _drill(20.0, 25.0, 3.0)), _drill(45.0, 25.0, 3.0))
        self.assertEqual(check(shape, self.RULE), [])

    def test_a_configured_figure_is_read_as_millimetres(self):
        shape = _cut(_cut(bracket(), _drill(20.0, 25.0, 3.0)), _drill(45.0, 25.0, 3.0))
        findings = check(shape, self.RULE, limit="25.0")
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].limit, 25.0, places=3)


class TestCountersinkDepth(SheetCase):
    RULE = Rulebook.SHEET_COUNTERSINK_DEEP

    def test_a_shallow_countersink_is_clean(self):
        self.assertEqual(check(countersunk_bracket(0.8), self.RULE), [])

    def test_a_countersink_leaving_a_knife_edge_warns(self):
        findings = check(countersunk_bracket(1.9), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertIn("land", findings[0].message)

    def test_the_depth_is_read_off_the_cone(self):
        findings = check(countersunk_bracket(1.9), self.RULE)
        self.assertAlmostEqual(findings[0].value, 1.9, places=2)


class TestMachinedFeature(SheetCase):
    RULE = Rulebook.SHEET_MACHINED_FEATURE

    def test_a_plain_hole_is_not_a_secondary_operation(self):
        self.assertEqual(check(_cut(bracket(), _drill(30.0, 25.0, 4.0)), self.RULE), [])

    def test_a_tapped_hole_draws_the_note(self):
        context = self.assert_sheet(sheet_context(_cut(bracket(), _drill(30.0, 25.0, 2.0))))
        holes = context.recognition.of_type(FeatureType.THROUGH_HOLE)
        self.assertEqual(len(holes), 1)
        holes[0].type = FeatureType.THREADED_HOLE

        findings = run(context, self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        self.assertAlmostEqual(findings[0].value, GAUGE, places=3)


# =============================================================================
# Outline
# =============================================================================


class TestTabNarrow(SheetCase):
    RULE = Rulebook.SHEET_TAB_NARROW

    def test_a_six_millimetre_tab_is_clean(self):
        context = self.assert_sheet(sheet_context(tabbed_bracket()))
        self.assertEqual(len(context.recognition.of_type(FeatureType.TAB)), 1)
        self.assertEqual(run(context, self.RULE), [])

    def test_a_three_millimetre_tab_warns(self):
        """The floor is absolute as well as gauge-relative.

        Two gauges is 4 mm here, and the absolute floor is 3.2 mm, so the
        larger of the two governs -- handling damage does not scale with the
        thickness.
        """
        findings = check(thin_tab_bracket(), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].limit, 4.0, places=3)

    def test_a_slender_tab_warns_on_its_aspect(self):
        context = self.assert_sheet(sheet_context(tabbed_bracket()))
        tab = context.recognition.of_type(FeatureType.TAB)[0]
        tab.parameters["aspect"] = 9.0

        findings = run(context, self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 9.0, places=3)


class TestNotchNarrow(SheetCase):
    RULE = Rulebook.SHEET_NOTCH_NARROW

    def test_a_wide_notch_is_clean(self):
        context = self.assert_sheet(sheet_context(notched_bracket()))
        self.assertEqual(len(context.recognition.of_type(FeatureType.NOTCH)), 1)
        self.assertEqual(run(context, self.RULE), [])

    def test_a_notch_under_one_gauge_warns(self):
        findings = check(thin_notch_bracket(), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].limit, GAUGE, places=3)

    def test_a_notch_exactly_one_gauge_wide_is_left_alone(self):
        """The epsilon is doing real work at this threshold.

        A 2.0 mm notch in 2.0 mm material sits precisely on the limit, and a
        STEP round trip that delivered 1.999 would otherwise decide it.
        """
        context = self.assert_sheet(sheet_context(notched_bracket()))
        notch = context.recognition.of_type(FeatureType.NOTCH)[0]
        notch.parameters["width_mm"] = GAUGE - 0.005
        self.assertEqual(run(context, self.RULE), [])

    def test_a_deep_narrow_notch_warns_on_its_depth(self):
        context = self.assert_sheet(sheet_context(notched_bracket()))
        notch = context.recognition.of_type(FeatureType.NOTCH)[0]
        width = notch.number("width_mm")
        notch.parameters["length_mm"] = width * 12.0

        findings = run(context, self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].limit, width * 10.0, places=3)


# =============================================================================
# Formed features
# =============================================================================


class TestFormedFeature(SheetCase):
    RULE = Rulebook.SHEET_FORMED_FEATURE

    def test_a_plain_bracket_has_nothing_formed(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_every_formed_feature_is_listed(self):
        findings = check(embossed_bracket(), self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        self.assertIn("emboss", findings[0].overview)

    def test_two_forms_give_two_lines(self):
        self.assertEqual(len(check(two_emboss_bracket(10.0), self.RULE)), 2)


class TestEmbossDepth(SheetCase):
    RULE = Rulebook.SHEET_EMBOSS_DEEP

    def test_a_shallow_draw_is_clean(self):
        self.assertEqual(check(emboss_bracket(5.0), self.RULE), [])

    def test_a_draw_past_three_gauges_warns(self):
        findings = check(emboss_bracket(9.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 4.5, places=2)  # 9 mm in 2 mm
        self.assertAlmostEqual(findings[0].limit, 3.0, places=3)

    def test_a_lance_is_judged_by_the_louver_rule_instead(self):
        context = self.assert_sheet(sheet_context(lanced_bracket(8.0)))
        formed = context.recognition.of_type(FeatureType.SHEET_FORMED)
        self.assertEqual([f.param("subtype") for f in formed], ["lance"])
        self.assertEqual(run(context, self.RULE), [])


class TestLouverHeight(SheetCase):
    RULE = Rulebook.SHEET_LOUVER_TALL

    def test_a_low_hood_is_clean(self):
        self.assertEqual(check(lanced_bracket(4.0), self.RULE), [])

    def test_a_hood_past_the_die_range_warns(self):
        findings = check(lanced_bracket(8.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        # A quarter inch at 14 gauge, scaled: 6.35 / 1.897.
        self.assertAlmostEqual(findings[0].limit, 6.35 / 1.897, places=3)

    def test_an_emboss_is_judged_by_the_draw_rule_instead(self):
        self.assertEqual(check(emboss_bracket(9.0), self.RULE), [])


class TestFormedPitch(SheetCase):
    RULE = Rulebook.SHEET_FORMED_PITCH

    def test_one_form_has_no_pitch(self):
        self.assertEqual(check(emboss_bracket(5.0), self.RULE), [])

    def test_forms_sharing_a_thin_web_warn(self):
        findings = check(two_emboss_bracket(3.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].value, 3.0, places=2)
        self.assertAlmostEqual(findings[0].limit, 2.0 * GAUGE, places=3)

    def test_well_spaced_forms_are_clean(self):
        self.assertEqual(check(two_emboss_bracket(10.0), self.RULE), [])

    def test_a_configured_figure_is_read_as_millimetres(self):
        findings = check(two_emboss_bracket(10.0), self.RULE, limit="15.0")
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertAlmostEqual(findings[0].limit, 15.0, places=3)


class TestFormedNearBend(SheetCase):
    RULE = Rulebook.SHEET_FORMED_NEAR_BEND

    def test_a_form_out_in_the_panel_is_clean(self):
        self.assertEqual(check(emboss_bracket(5.0), self.RULE), [])

    def test_a_form_inside_the_bend_zone_warns(self):
        findings = check(emboss_bracket(5.0, x0=8.0, x1=22.0), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])
        # Three gauges plus the bend radius: 3 x 2 + 3.
        self.assertAlmostEqual(findings[0].limit, 9.0, places=3)

    def test_one_finding_per_form_however_many_bends_it_is_near(self):
        findings = check(emboss_bracket(5.0, x0=8.0, x1=22.0), self.RULE)
        self.assertEqual(len(findings), 1)


# =============================================================================
# The part as a whole
# =============================================================================


class TestThicknessRange(SheetCase):
    RULE = Rulebook.SHEET_THICKNESS_OUT_OF_RANGE

    def test_two_millimetre_stock_is_in_range(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_above_the_steel_ceiling_draws_an_advisory(self):
        shape = bracket(gauge=4.0, inner_radius=4.0)
        context = self.assert_sheet(sheet_context(shape), gauge=4.0)
        findings = run(context, self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        self.assertAlmostEqual(findings[0].limit, 3.175, places=3)

    def test_an_undeclared_material_is_judged_against_steel_and_says_so(self):
        shape = bracket(gauge=4.0, inner_radius=4.0)
        findings = run(sheet_context(shape), self.RULE)
        self.assertIn("No material was declared", findings[0].message)

    def test_declared_aluminium_raises_the_ceiling(self):
        config = MachiningConfig(material_family="aluminium")
        shape = bracket(gauge=4.0, inner_radius=4.0)
        context = self.assert_sheet(sheet_context(shape, config=config), gauge=4.0)
        self.assertEqual(run(context, self.RULE), [])

    def test_declared_steel_names_the_material(self):
        config = MachiningConfig(material_family="steel")
        shape = bracket(gauge=4.0, inner_radius=4.0)
        findings = run(sheet_context(shape, config=config), self.RULE)
        self.assertIn("the declared material", findings[0].message)

    def test_a_configured_range_overrides_both_ends(self):
        shape = bracket(gauge=4.0, inner_radius=4.0)
        context = sheet_context(shape)
        self.assertEqual(run(context, self.RULE, min_value="0.3", max_value="8.0"), [])


class TestFeatureComplexity(SheetCase):
    RULE = Rulebook.SHEET_FEATURE_COMPLEXITY

    def test_the_census_always_reports(self):
        findings = check(bracket(), self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        # One cut profile plus one brake stroke.
        self.assertAlmostEqual(findings[0].value, 2.0, places=3)

    def test_a_hem_counts_as_two_strokes(self):
        findings = check(hemmed_bracket(), self.RULE)
        # Cut profile, one stroke for the corner, two for the hem.
        self.assertAlmostEqual(findings[0].value, 4.0, places=3)

    def test_a_forming_station_is_counted(self):
        findings = check(emboss_bracket(5.0), self.RULE)
        self.assertAlmostEqual(findings[0].value, 3.0, places=3)
        self.assertIn("forming station", findings[0].message)

    def test_a_configured_ceiling_raises_the_tone(self):
        findings = check(hemmed_bracket(), self.RULE, target="1.0", limit="2.0")
        self.assertEqual(severities(findings), [Severity.ERROR])


class TestSharpFold(SheetCase):
    RULE = Rulebook.SHEET_SHARP_FOLD

    def test_a_properly_modelled_bend_is_clean(self):
        self.assertEqual(check(bracket(), self.RULE), [])

    def test_a_square_junction_between_two_skins_warns(self):
        findings = check(walled_bracket(), self.RULE)
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_the_finding_names_both_panels(self):
        findings = check(walled_bracket(), self.RULE)
        self.assertEqual(len(findings[0].failing_geometry), 2)

    def test_one_fold_gives_one_finding(self):
        """Both skins of the joining slab meet the panel, so the fold arrives
        twice and has to collapse to a single line."""
        self.assertEqual(len(check(walled_bracket(), self.RULE)), 1)


class TestIntentSharpCorners(SheetCase):
    RULE = Rulebook.SHEET_INTENT_SHARP_CORNERS

    def test_it_says_nothing_about_a_part_that_is_already_sheet(self):
        """The sharp-fold rule owns that case; one process voice per part."""
        context = sheet_context(sharp_shell())
        self.assertIs(context.part_process.type, PartProcessType.SHEET_METAL)
        self.assertEqual(run(context, self.RULE), [])

    def test_a_milled_shell_with_square_folds_draws_the_advisory(self):
        context = analysed(machined_sharp_shell())
        self.assertIs(context.part_process.type, PartProcessType.MILLED)
        findings = run(context, self.RULE)
        self.assertEqual(severities(findings), [Severity.INFO])
        self.assertAlmostEqual(findings[0].value, GAUGE, places=2)

    def test_an_ordinary_milled_block_is_clean(self):
        context = analysed(milled_block())
        self.assertIsNot(context.part_process.type, PartProcessType.SHEET_METAL)
        self.assertEqual(run(context, self.RULE), [])


# =============================================================================
# End to end
# =============================================================================


class TestThroughTheAnalyzer(SheetCase):
    """The bend rules, run over the analyzer's own output.

    Bends survive the resolver because nothing else claims a coaxial cylinder
    pair one gauge apart. This is what proves the family is wired up rather
    than merely importable.
    """

    def test_the_analyzer_classifies_the_bracket_as_sheet(self):
        self.assert_sheet(analysed(bracket()))

    def test_a_tight_radius_reports_through_the_analyzer(self):
        findings = run(analysed(bracket(inner_radius=1.5)), Rulebook.SHEET_BEND_RADIUS_SMALL)
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_a_short_flange_reports_through_the_analyzer(self):
        findings = run(analysed(bracket(leg_z=12.0)), Rulebook.SHEET_FLANGE_SHORT)
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_the_census_reports_through_the_analyzer(self):
        findings = run(analysed(bracket()), Rulebook.SHEET_FEATURE_COMPLEXITY)
        self.assertEqual(severities(findings), [Severity.INFO])

    def test_machining_rules_stay_quiet_on_a_sheet_part(self):
        """The mirror of the sheet gate: a formed part is not machined stock."""
        context = self.assert_sheet(analysed(bracket()))
        for rule in (
            Rulebook.THIN_WALL,
            Rulebook.POCKET_DEPTH_RATIO,
            Rulebook.FEATURE_COMPLEXITY,
        ):
            check_class = get_check_class(rule)
            if check_class is None:
                continue
            with self.subTest(rule=rule.name):
                self.assertEqual(run(context, rule), [])


if __name__ == "__main__":
    unittest.main()
