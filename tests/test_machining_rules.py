# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the machining rules that need only the adjacency graph.

Each rule gets three kinds of case, following the discipline the reference
engine settled on: something that should fire, something similar that should
not, and a pair straddling the threshold. The false-positive guards matter
most -- a rule that cries wolf on ordinary geometry gets switched off, and
then it protects nobody.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
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


def _analyse(shape: TopoDS_Shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def check(shape: TopoDS_Shape, rule: Rulebook, target="N/A", limit="N/A", severity="ERROR", prefs=None):
    """Run one rule over one shape and return its findings."""
    data = _analyse(shape, prefs)
    check_class = get_check_class(rule)
    assert check_class is not None, f"no check registered for {rule.name}"
    return check_class().run_check(
        data,
        RuleLimit(target=target, limit=limit, binary_severity=severity),
        rule,
        feedback=RuleFeedback(),
    )


def severities(findings) -> list:
    return [f.severity for f in findings]


# -- shapes -------------------------------------------------------------------


def make_ordinary_block() -> TopoDS_Shape:
    """A well-proportioned block. Nothing here should trip any rule."""
    return BRepPrimAPI_MakeBox(80.0, 50.0, 30.0).Shape()


def make_thin_plate() -> TopoDS_Shape:
    """200 x 120 x 2.5: broad, thin and floppy."""
    return BRepPrimAPI_MakeBox(200.0, 120.0, 2.5).Shape()


def make_slender_bar() -> TopoDS_Shape:
    """300 long on a 10 mm square section: 30:1, and not a plate."""
    return BRepPrimAPI_MakeBox(300.0, 10.0, 10.0).Shape()


def make_shaft(radius: float, length: float) -> TopoDS_Shape:
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), radius, length).Shape()


def make_tiny_cube() -> TopoDS_Shape:
    """12 mm cube: below vise scale in every direction."""
    return BRepPrimAPI_MakeBox(12.0, 12.0, 12.0).Shape()


def make_buried_cavity() -> TopoDS_Shape:
    """A block with a void that never reaches the surface."""
    inner = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(40, 25, 8), gp_Dir(0, 0, 1)), 6.0, 14.0)
    return _cut(make_ordinary_block(), inner.Shape())


def make_open_pocket() -> TopoDS_Shape:
    """A block with a pocket open to the top -- reachable, so not a void."""
    cavity = BRepPrimAPI_MakeBox(gp_Pnt(10, 10, 10), gp_Pnt(70, 40, 31)).Shape()
    return _cut(make_ordinary_block(), cavity)


def make_hollow_tray(wall_mm: float) -> TopoDS_Shape:
    """An open-topped tray with walls of the given thickness."""
    outer = BRepPrimAPI_MakeBox(100.0, 100.0, 40.0).Shape()
    inner = BRepPrimAPI_MakeBox(
        gp_Pnt(wall_mm, wall_mm, wall_mm), gp_Pnt(100.0 - wall_mm, 100.0 - wall_mm, 41.0)
    ).Shape()
    return _cut(outer, inner)


# =============================================================================


class TestPartSlenderness(unittest.TestCase):
    RULE = Rulebook.PART_ASPECT_RATIO

    def test_ordinary_block_is_not_slender(self):
        self.assertEqual(check(make_ordinary_block(), self.RULE), [])

    def test_thin_plate_is_reported_as_a_plate(self):
        findings = check(make_thin_plate(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertIn("plate", findings[0].overview)

    def test_slender_bar_is_reported_as_a_bar(self):
        findings = check(make_slender_bar(), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertIn("bar", findings[0].overview)

    def test_plate_and_bar_get_different_advice(self):
        plate = check(make_thin_plate(), self.RULE)[0].message
        bar = check(make_slender_bar(), self.RULE)[0].message
        self.assertIn("warp", plate)
        self.assertIn("deflect", bar)

    def test_turned_part_measures_length_over_diameter(self):
        findings = check(make_shaft(8.0, 200.0), self.RULE)
        self.assertEqual(len(findings), 1)
        # 200 long on a 16mm diameter is 12.5:1, not the 8.8:1 a bounding
        # box would give by touching the cylinder at its corners.
        self.assertAlmostEqual(findings[0].value, 12.5, places=1)

    def test_stubby_turned_part_is_not_slender(self):
        self.assertEqual(check(make_shaft(25.0, 60.0), self.RULE), [])

    def test_disc_is_never_slender(self):
        # Wider than it is long: no amount of diameter makes a disc floppy.
        self.assertEqual(check(make_shaft(50.0, 5.0), self.RULE), [])

    def test_turning_thresholds_grade_warn_then_error(self):
        warn = check(make_shaft(5.0, 55.0), self.RULE)  # 5.5:1
        error = check(make_shaft(5.0, 95.0), self.RULE)  # 9.5:1
        self.assertEqual(severities(warn), [Severity.WARNING])
        self.assertEqual(severities(error), [Severity.ERROR])

    def test_material_limits_override_the_defaults(self):
        findings = check(make_ordinary_block(), self.RULE, target="2.0", limit="2.5")
        self.assertEqual(severities(findings), [Severity.ERROR])  # 80/30 = 2.7


class TestMaterialRemoval(unittest.TestCase):
    RULE = Rulebook.MATERIAL_REMOVAL

    def test_solid_block_wastes_nothing(self):
        self.assertEqual(check(make_ordinary_block(), self.RULE), [])

    def test_hollow_tray_wastes_most_of_the_billet(self):
        findings = check(make_hollow_tray(5.0), self.RULE)
        self.assertEqual(len(findings), 1)
        self.assertGreater(findings[0].value, 70.0)

    def test_turned_part_is_priced_from_round_bar(self):
        # A plain cylinder is the whole bar, so nothing is wasted -- pricing
        # it from a bounding box would claim about 21% removal.
        self.assertEqual(check(make_shaft(20.0, 60.0), self.RULE), [])

    def test_thresholds_grade_warn_then_error(self):
        warn = check(make_hollow_tray(5.0), self.RULE, target="70", limit="95")
        error = check(make_hollow_tray(5.0), self.RULE, target="50", limit="60")
        self.assertEqual(severities(warn), [Severity.WARNING])
        self.assertEqual(severities(error), [Severity.ERROR])


class TestSealedVoid(unittest.TestCase):
    RULE = Rulebook.SEALED_VOID

    def test_buried_cavity_is_an_error(self):
        findings = check(make_buried_cavity(), self.RULE)
        self.assertEqual(severities(findings), [Severity.ERROR])

    def test_open_pocket_is_not_a_sealed_void(self):
        self.assertEqual(check(make_open_pocket(), self.RULE), [])

    def test_solid_block_has_no_void(self):
        self.assertEqual(check(make_ordinary_block(), self.RULE), [])

    def test_through_hole_is_not_a_void(self):
        drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(40, 25, -1), gp_Dir(0, 0, 1)), 6.0, 40.0)
        self.assertEqual(check(_cut(make_ordinary_block(), drill.Shape()), self.RULE), [])


class TestThinWall(unittest.TestCase):
    RULE = Rulebook.THIN_WALL

    def test_ordinary_block_has_no_thin_wall(self):
        self.assertEqual(check(make_ordinary_block(), self.RULE), [])

    def test_thin_plate_is_a_thin_wall(self):
        findings = check(make_thin_plate(), self.RULE)
        self.assertTrue(findings)
        self.assertAlmostEqual(findings[0].value, 2.5, places=2)

    def test_pocket_walls_are_not_a_wall_between_them(self):
        # The two facing walls of a pocket are close together with nothing but
        # air in between. Reporting that as a thin wall is the classic false
        # positive this rule has to avoid.
        cavity = BRepPrimAPI_MakeBox(gp_Pnt(10, 10, 10), gp_Pnt(12, 40, 31)).Shape()
        findings = check(_cut(make_ordinary_block(), cavity), self.RULE)
        for finding in findings:
            self.assertGreater(
                finding.value, 2.5, "a gap across a cavity was reported as material"
            )

    def test_thick_walled_tray_passes(self):
        self.assertEqual(check(make_hollow_tray(8.0), self.RULE), [])

    def test_thin_walled_tray_fires(self):
        self.assertTrue(check(make_hollow_tray(1.0), self.RULE))

    def test_threshold_pair_straddles_the_warn_limit(self):
        # A narrow footprint on purpose: at 16mm across, neither thickness is
        # broad enough to trip the aspect path, so this isolates the absolute
        # threshold. A wider plate would legitimately fire either way.
        below = check(BRepPrimAPI_MakeBox(16.0, 16.0, 1.4).Shape(), self.RULE, target="1.5", limit="0.8")
        above = check(BRepPrimAPI_MakeBox(16.0, 16.0, 1.6).Shape(), self.RULE, target="1.5", limit="0.8")
        self.assertEqual(severities(below), [Severity.WARNING])
        self.assertEqual(above, [])

    def test_broad_thin_section_fires_on_aspect_alone(self):
        # 1.6mm is above the absolute target, but 60mm across it is still a
        # panel that will drum. This is the aspect path doing its job.
        findings = check(BRepPrimAPI_MakeBox(60.0, 60.0, 1.6).Shape(), self.RULE, target="1.5", limit="0.8")
        self.assertEqual(severities(findings), [Severity.WARNING])
        self.assertIn("aspect ratio", findings[0].message)

    def test_thick_section_never_fires_on_aspect(self):
        # Stiffness scales with the cube of thickness, so a 6mm wall is rigid
        # however broad it is; without the cap this would fire at 50:1.
        self.assertEqual(
            check(BRepPrimAPI_MakeBox(300.0, 300.0, 6.0).Shape(), self.RULE, target="1.5", limit="0.8"),
            [],
        )

    def test_threshold_pair_straddles_the_error_limit(self):
        below = check(BRepPrimAPI_MakeBox(60.0, 60.0, 0.7).Shape(), self.RULE, target="1.5", limit="0.8")
        above = check(BRepPrimAPI_MakeBox(60.0, 60.0, 0.9).Shape(), self.RULE, target="1.5", limit="0.8")
        self.assertEqual(severities(below), [Severity.ERROR])
        self.assertEqual(severities(above), [Severity.WARNING])


class TestWorkholding(unittest.TestCase):
    def test_ordinary_block_has_a_datum_face(self):
        self.assertEqual(check(make_ordinary_block(), Rulebook.NO_DATUM_FACE), [])

    def test_tiny_cube_has_no_face_large_enough(self):
        # A 12mm cube's largest flat is 144mm2, under the 200mm2 minimum.
        self.assertTrue(check(make_tiny_cube(), Rulebook.NO_DATUM_FACE))

    def test_ordinary_block_has_opposed_clamping_faces(self):
        self.assertEqual(check(make_ordinary_block(), Rulebook.NO_PARALLEL_DATUM_PAIR), [])

    def test_ordinary_block_is_thick_enough_to_clamp(self):
        self.assertEqual(check(make_ordinary_block(), Rulebook.THIN_CLAMPING_DIMENSION), [])

    def test_thin_plate_is_too_thin_to_clamp(self):
        findings = check(make_thin_plate(), Rulebook.THIN_CLAMPING_DIMENSION)
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_clamping_limit_is_configurable(self):
        plate = BRepPrimAPI_MakeBox(200.0, 120.0, 4.0).Shape()
        self.assertEqual(check(plate, Rulebook.THIN_CLAMPING_DIMENSION, limit="3.0"), [])
        self.assertTrue(check(plate, Rulebook.THIN_CLAMPING_DIMENSION, limit="6.0"))

    def test_small_part_gets_a_holding_note(self):
        findings = check(make_tiny_cube(), Rulebook.SMALL_PART_HOLDING)
        self.assertEqual(severities(findings), [Severity.INFO])

    def test_normal_part_gets_no_holding_note(self):
        self.assertEqual(check(make_ordinary_block(), Rulebook.SMALL_PART_HOLDING), [])

    def test_small_parts_do_not_also_collect_vise_warnings(self):
        # Below vise scale the vise rules stand down, so the holding note is
        # the only thing said rather than three overlapping complaints.
        self.assertEqual(check(make_tiny_cube(), Rulebook.NO_PARALLEL_DATUM_PAIR), [])
        self.assertEqual(check(make_tiny_cube(), Rulebook.THIN_CLAMPING_DIMENSION), [])

    def test_workholding_rules_stand_down_on_turned_parts(self):
        # A turned part is gripped by its diameter in a chuck, so vise
        # reasoning does not apply to it at all.
        shaft = make_shaft(20.0, 60.0)
        for rule in (
            Rulebook.NO_DATUM_FACE,
            Rulebook.NO_PARALLEL_DATUM_PAIR,
            Rulebook.THIN_CLAMPING_DIMENSION,
            Rulebook.SMALL_PART_HOLDING,
        ):
            with self.subTest(rule=rule.name):
                self.assertEqual(check(shaft, rule), [])


class TestFindingShape(unittest.TestCase):
    """Findings have to be usable by the existing results panel."""

    def test_geometry_references_are_one_based_face_ids(self):
        findings = check(make_thin_plate(), Rulebook.THIN_WALL)
        self.assertTrue(findings)
        for kind, index in findings[0].failing_geometry:
            self.assertEqual(kind, "Face")
            self.assertGreaterEqual(index, 1)

    def test_findings_carry_a_measured_value_and_limit(self):
        finding = check(make_thin_plate(), Rulebook.THIN_WALL)[0]
        self.assertGreater(finding.value, 0.0)
        self.assertGreater(finding.limit, 0.0)
        self.assertEqual(finding.unit, "mm")

    def test_messages_are_written_out_not_left_as_templates(self):
        finding = check(make_thin_plate(), Rulebook.THIN_WALL)[0]
        self.assertNotIn("{measured}", finding.message)
        self.assertGreater(len(finding.message), 40)

    def test_process_feedback_overrides_the_default_message(self):
        data = _analyse(make_thin_plate())
        check_class = get_check_class(Rulebook.THIN_WALL)
        assert check_class is not None
        findings = check_class().run_check(
            data,
            RuleLimit(target="1.5", limit="0.8", binary_severity="ERROR"),
            Rulebook.THIN_WALL,
            feedback=RuleFeedback(warning_msg="Wall is {measured} against {target}."),
        )
        self.assertTrue(findings[0].message.startswith("Wall is 2.50mm against 1.50mm"))


if __name__ == "__main__":
    unittest.main()
