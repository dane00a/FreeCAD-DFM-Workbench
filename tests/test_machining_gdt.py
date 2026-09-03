# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the GD&T rules.

These rules are dormant: nothing supplies tolerance or finish callouts today,
so on a real part every one of them is silent. That is easy to test and worth
almost nothing on its own -- a rule that has never been shown to fire is not
ported, it is decoration.

So the tests come in two halves. The first proves the silence is honest: a
machined block with no annotations produces no findings from any of the seven.
The second installs an annotation source through the same entry point a future
PMI importer would use, and proves each rule fires on the right input, with
the right severity, and says something a machinist can act on. When the
annotations arrive, the logic is already known to work.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import CONTEXT_KEY, MachiningAnalyzer
from freecad.DFM.core.machining.annotations import (
    AnnotationSet,
    Datum,
    FeatureControlFrame,
    MaterialCondition,
    SurfaceFinish,
    SurfaceFinishNote,
    ToleranceCategory,
    ToleranceType,
    annotation_source,
    annotations_for,
    parse_ra_um,
    set_annotation_source,
)
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.models import Severity
from freecad.DFM.core.processes.process import RuleFeedback, RuleLimit
from freecad.DFM.core.registries import get_check_class
from freecad.DFM.core.rules import Rulebook
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# Every rule this module covers, so the dormancy tests cannot fall behind the
# implementation by forgetting one.
GDT_RULES = (
    Rulebook.GDT_TOLERANCE_ACHIEVABLE,
    Rulebook.GDT_DATUM_VALID,
    Rulebook.GDT_DATUM_UNRESOLVED,
    Rulebook.GDT_SURFACE_FINISH_CONFLICT,
    Rulebook.GDT_FEATURE_TOLERANCE_MISMATCH,
    Rulebook.NOTE_SURFACE_FINISH_DEMANDING,
    Rulebook.SURFACE_FINISH_PER_FACE_DEMANDING,
)


# =============================================================================
# Shapes
# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    operation = BRepAlgoAPI_Cut(a, b)
    operation.Build()
    return operation.Shape()


def machined_block() -> TopoDS_Shape:
    """A 120 x 90 x 50 block with a pocket and a drilled hole.

    Deliberately carries one feature of size (the hole) and one thing that is
    not (the pocket), because the mismatch rule turns on exactly that
    distinction.
    """
    block = BRepPrimAPI_MakeBox(120.0, 90.0, 50.0).Shape()
    pocket = BRepPrimAPI_MakeBox(gp_Pnt(20, 20, 30), gp_Pnt(100, 70, 51)).Shape()
    drill = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(110, 80, -1), gp_Dir(0, 0, 1)), 4.0, 60.0
    ).Shape()
    return _cut(_cut(block, pocket), drill)


def small_cube() -> TopoDS_Shape:
    """A 12 mm cube: every face is 144 mm2, under the 200 mm2 datum minimum.

    Nothing on it is big enough to establish a datum on, which is what the
    datum-validity rule is looking for.
    """
    return BRepPrimAPI_MakeBox(12.0, 12.0, 12.0).Shape()


# =============================================================================
# Harness
# =============================================================================


def analyse(shape, prefs=None):
    face_index, edge_index = FaceIndex(shape), EdgeIndex(shape)
    return MachiningAnalyzer().execute(shape, face_index, edge_index, prefs=prefs or {})


def context_of(shape, prefs=None):
    return analyse(shape, prefs)[CONTEXT_KEY]


def feature_of(context, feature_type):
    """The first recognized feature of a type, so tests need no face ids."""
    for feature in context.recognition.features:
        if feature.type == feature_type:
            return feature
    raise AssertionError(f"no {feature_type} was recognized on this part")


def rule_check(shape, rule, build=None, limit="N/A", severity="WARNING", prefs=None):
    """Run one rule, optionally with an annotation source installed.

    `build` is exactly what a PMI importer would hand to
    :func:`set_annotation_source`: a callable taking the machining context and
    returning the callouts for that part.
    """
    data = analyse(shape, prefs)
    check_class = get_check_class(rule)
    assert check_class is not None, f"{rule.name} has no registered check"

    if build is not None:
        set_annotation_source(build)
    try:
        return check_class().run_check(
            data,
            RuleLimit(target="N/A", limit=limit, binary_severity=severity),
            rule,
            feedback=RuleFeedback(),
        )
    finally:
        set_annotation_source(None)


def severities(findings):
    return [f.severity for f in findings]


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


def frames(*items):
    """An annotation source supplying just these feature control frames."""
    return lambda context: AnnotationSet(frames=list(items))


# =============================================================================


class TestAnnotationEntryPoint(unittest.TestCase):
    """The seam a future PMI importer plugs into."""

    def tearDown(self):
        set_annotation_source(None)

    def test_nothing_is_installed_by_default(self):
        self.assertIsNone(annotation_source())

    def test_a_part_carries_no_callouts_today(self):
        callouts = annotations_for(context_of(machined_block()))
        self.assertTrue(callouts.is_empty)

    def test_an_installed_source_is_consulted(self):
        context = context_of(machined_block())
        set_annotation_source(lambda ctx: AnnotationSet(datums=[Datum("d1", "A")]))
        self.assertEqual(len(annotations_for(context).datums), 1)

    def test_a_broken_source_does_not_take_the_analysis_down(self):
        # A half-written PMI translator must fail closed: no callouts, not a
        # traceback out of the middle of a rule run.
        def explode(context):
            raise RuntimeError("bad PMI")

        context = context_of(machined_block())
        set_annotation_source(explode)
        self.assertTrue(annotations_for(context).is_empty)

    def test_the_category_follows_from_the_characteristic(self):
        frame = FeatureControlFrame("fcf_1", type=ToleranceType.FLATNESS)
        self.assertEqual(frame.category, ToleranceCategory.FORM)
        frame = FeatureControlFrame("fcf_2", type=ToleranceType.POSITION)
        self.assertEqual(frame.category, ToleranceCategory.LOCATION)

    def test_an_explicit_category_wins(self):
        frame = FeatureControlFrame(
            "fcf_1", type="something_new", category=ToleranceCategory.PROFILE
        )
        self.assertEqual(frame.category, ToleranceCategory.PROFILE)

    def test_only_cited_datum_letters_count_as_referenced(self):
        callouts = AnnotationSet(
            frames=[FeatureControlFrame("fcf_1", datum_refs=["A", "B"])],
            datums=[Datum("d1", "A"), Datum("d2", "C")],
        )
        self.assertEqual(callouts.cited_datum_labels(), {"A", "B"})


class TestRaParsing(unittest.TestCase):
    """Reading a finish value out of drawing text."""

    def test_metric_callout(self):
        self.assertAlmostEqual(parse_ra_um("ALL SURFACES Ra 0.8"), 0.8)

    def test_metric_callout_with_a_unit(self):
        self.assertAlmostEqual(parse_ra_um("Ra0.4 um"), 0.4)

    def test_an_explicit_microinch_suffix_converts(self):
        self.assertAlmostEqual(parse_ra_um("FINISH Ra 63 uin"), 63 * 0.0254)

    def test_a_bare_large_number_is_read_as_microinches(self):
        # Nobody mills to Ra 32 um; 32 uin is a common ground finish.
        self.assertAlmostEqual(parse_ra_um("Ra 32"), 32 * 0.0254)

    def test_text_without_a_callout_gives_nothing(self):
        self.assertEqual(parse_ra_um("DEBURR AND BREAK SHARP EDGES"), 0.0)

    def test_ra_inside_a_word_is_not_a_callout(self):
        self.assertEqual(parse_ra_um("EXTRA 5 HOLES THIS SIDE"), 0.0)

    def test_empty_text(self):
        self.assertEqual(parse_ra_um(""), 0.0)


class TestDormantOnRealParts(unittest.TestCase):
    """Every rule stands down on a part with no annotations.

    Which is every part today. The point of testing it is that the silence
    must come from there being nothing to judge -- so the same rules are run
    again below with callouts supplied, and they speak.
    """

    def tearDown(self):
        set_annotation_source(None)

    def test_every_rule_is_registered(self):
        for rule in GDT_RULES:
            with self.subTest(rule=rule.name):
                self.assertIsNotNone(get_check_class(rule))

    def test_every_rule_is_silent_on_a_machined_block(self):
        shape = machined_block()
        for rule in GDT_RULES:
            with self.subTest(rule=rule.name):
                self.assertEqual(rule_check(shape, rule), [])

    def test_every_rule_is_silent_on_a_small_cube(self):
        # The cube has no face large enough to be a datum, which is the one
        # geometric condition any of these rules could trip on by itself.
        shape = small_cube()
        for rule in GDT_RULES:
            with self.subTest(rule=rule.name):
                self.assertEqual(rule_check(shape, rule), [])

    def test_every_rule_is_silent_when_the_source_supplies_nothing(self):
        shape = machined_block()
        for rule in GDT_RULES:
            with self.subTest(rule=rule.name):
                self.assertEqual(rule_check(shape, rule, build=lambda ctx: AnnotationSet()), [])


# =============================================================================
# Tolerance capability
# =============================================================================


class TestToleranceAchievable(unittest.TestCase):
    RULE = Rulebook.GDT_TOLERANCE_ACHIEVABLE

    def tearDown(self):
        set_annotation_source(None)

    def test_a_tight_position_tolerance_is_flagged(self):
        # 0.005 mm against the 0.05 mm a 3-axis machine holds.
        findings = rule_check(
            machined_block(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.POSITION, tolerance_value_mm=0.005
                )
            ),
        )
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_an_ordinary_position_tolerance_passes(self):
        findings = rule_check(
            machined_block(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.POSITION, tolerance_value_mm=0.1
                )
            ),
        )
        self.assertEqual(findings, [])

    def test_form_is_judged_against_the_form_budget(self):
        # 0.01 mm flatness is under the 0.025 mm form budget but over the
        # 0.05 mm position one, so the two families must not share a limit.
        tight = rule_check(
            machined_block(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.FLATNESS, tolerance_value_mm=0.01
                )
            ),
        )
        self.assertEqual(severities(tight), [Severity.WARNING])
        loose = rule_check(
            machined_block(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.FLATNESS, tolerance_value_mm=0.03
                )
            ),
        )
        self.assertEqual(loose, [])

    def test_a_better_machine_holds_more(self):
        # The same 0.02 mm position callout: out of reach on a 3-axis
        # machine, comfortable on a 5-axis one.
        build = frames(
            FeatureControlFrame(
                "fcf_1", type=ToleranceType.POSITION, tolerance_value_mm=0.02
            )
        )
        three = rule_check(machined_block(), self.RULE, build)
        five = rule_check(
            machined_block(), self.RULE, build, prefs={"MachiningMachineMode": "5axis"}
        )
        self.assertEqual(severities(three), [Severity.WARNING])
        self.assertEqual(five, [])

    def test_a_configured_limit_overrides_the_machine_table(self):
        # A shop that probes its datums can promise tighter than the generic
        # defaults, and the editor field is how it says so.
        build = frames(
            FeatureControlFrame(
                "fcf_1", type=ToleranceType.POSITION, tolerance_value_mm=0.005
            )
        )
        self.assertEqual(rule_check(machined_block(), self.RULE, build, limit="0.001"), [])

    def test_a_frame_with_no_zone_is_skipped(self):
        findings = rule_check(
            machined_block(),
            self.RULE,
            frames(FeatureControlFrame("fcf_1", type=ToleranceType.POSITION)),
        )
        self.assertEqual(findings, [])

    def test_the_finding_names_the_machine_and_both_numbers(self):
        def build(context):
            hole = feature_of(context, FeatureType.THROUGH_HOLE)
            return AnnotationSet(
                frames=[
                    FeatureControlFrame(
                        "fcf_1",
                        type=ToleranceType.POSITION,
                        tolerance_value_mm=0.005,
                        feature_id=hole.instance_id,
                    )
                ]
            )

        finding = only(rule_check(machined_block(), self.RULE, build))
        self.assertIn("position", finding.message)
        self.assertIn("through hole h_0", finding.message)
        self.assertIn("0.005 mm", finding.message)
        self.assertIn("0.050 mm", finding.message)
        self.assertIn("3-axis", finding.message)
        self.assertEqual(finding.unit, "mm")
        self.assertEqual(finding.value, 0.005)
        self.assertEqual(finding.limit, 0.05)

    def test_the_finding_points_at_the_features_faces(self):
        def build(context):
            hole = feature_of(context, FeatureType.THROUGH_HOLE)
            return AnnotationSet(
                frames=[
                    FeatureControlFrame(
                        "fcf_1",
                        type=ToleranceType.POSITION,
                        tolerance_value_mm=0.005,
                        feature_id=hole.instance_id,
                    )
                ]
            )

        context = context_of(machined_block())
        expected = [("Face", i) for i in sorted(feature_of(context, "THROUGH_HOLE").faces)]
        finding = only(rule_check(machined_block(), self.RULE, build))
        self.assertEqual(finding.failing_geometry, expected)

    def test_findings_come_out_in_a_stable_order(self):
        build = frames(
            FeatureControlFrame(
                "fcf_b", type=ToleranceType.POSITION, tolerance_value_mm=0.004
            ),
            FeatureControlFrame(
                "fcf_a", type=ToleranceType.POSITION, tolerance_value_mm=0.003
            ),
        )
        findings = rule_check(machined_block(), self.RULE, build)
        self.assertEqual([f.value for f in findings], [0.003, 0.004])


# =============================================================================
# Datums
# =============================================================================


class TestDatumValid(unittest.TestCase):
    RULE = Rulebook.GDT_DATUM_VALID

    def tearDown(self):
        set_annotation_source(None)

    def test_a_part_with_no_usable_flats_cannot_carry_three_datums(self):
        findings = rule_check(
            small_cube(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.POSITION, datum_refs=["A", "B", "C"]
                )
            ),
        )
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_a_block_has_flats_enough(self):
        findings = rule_check(
            machined_block(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.POSITION, datum_refs=["A", "B", "C"]
                )
            ),
        )
        self.assertEqual(findings, [])

    def test_a_frame_citing_no_datum_is_not_this_rules_business(self):
        findings = rule_check(
            small_cube(),
            self.RULE,
            frames(FeatureControlFrame("fcf_1", type=ToleranceType.FLATNESS)),
        )
        self.assertEqual(findings, [])

    def test_the_severity_follows_the_rule_configuration(self):
        build = frames(
            FeatureControlFrame("fcf_1", type=ToleranceType.POSITION, datum_refs=["A"])
        )
        findings = rule_check(small_cube(), self.RULE, build, severity="ERROR")
        self.assertEqual(severities(findings), [Severity.ERROR])

    def test_the_finding_lists_the_datums_and_the_shortfall(self):
        build = frames(
            FeatureControlFrame(
                "fcf_1", type=ToleranceType.POSITION, datum_refs=["A", "B", "C"]
            )
        )
        finding = only(rule_check(small_cube(), self.RULE, build))
        self.assertIn("A, B, C", finding.message)
        self.assertIn("3 datums", finding.message)
        self.assertIn("200 mm2", finding.message)
        self.assertIn("machined pads", finding.message)
        self.assertEqual(finding.value, 0.0)
        self.assertEqual(finding.limit, 3.0)


class TestDatumUnresolved(unittest.TestCase):
    RULE = Rulebook.GDT_DATUM_UNRESOLVED

    def tearDown(self):
        set_annotation_source(None)

    @staticmethod
    def _callouts(datums, refs=("B",)):
        return lambda context: AnnotationSet(
            frames=[
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.POSITION, datum_refs=list(refs)
                )
            ],
            datums=datums,
        )

    def test_a_faceless_datum_that_something_cites_is_flagged(self):
        findings = rule_check(
            machined_block(), self.RULE, self._callouts([Datum("d2", "B")])
        )
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_a_datum_bound_to_a_face_is_fine(self):
        findings = rule_check(
            machined_block(), self.RULE, self._callouts([Datum("d2", "B", face_ids=[1])])
        )
        self.assertEqual(findings, [])

    def test_a_datum_target_is_exempt(self):
        # A target is anchored by its own placement point, so carrying no
        # face is correct for one rather than a failure to resolve.
        findings = rule_check(
            machined_block(),
            self.RULE,
            self._callouts([Datum("d2", "B", is_target=True)]),
        )
        self.assertEqual(findings, [])

    def test_a_datum_nothing_references_is_left_alone(self):
        # It locates nothing, so nothing is at risk. Flagging it is noise.
        findings = rule_check(
            machined_block(), self.RULE, self._callouts([Datum("d9", "Z")], refs=("B",))
        )
        self.assertEqual(findings, [])

    def test_the_finding_names_the_tolerances_left_adrift(self):
        finding = only(
            rule_check(machined_block(), self.RULE, self._callouts([Datum("d2", "B")]))
        )
        self.assertIn("Datum B", finding.message)
        self.assertIn("1 tolerance (fcf_1)", finding.message)
        self.assertIn("can be inspected as drawn", finding.message)
        self.assertEqual(finding.failing_geometry, [])

    def test_several_orphaned_tolerances_are_counted(self):
        def build(context):
            return AnnotationSet(
                frames=[
                    FeatureControlFrame("fcf_2", datum_refs=["B"]),
                    FeatureControlFrame("fcf_1", datum_refs=["B"]),
                ],
                datums=[Datum("d2", "B")],
            )

        finding = only(rule_check(machined_block(), self.RULE, build))
        self.assertIn("2 tolerances (fcf_1, fcf_2)", finding.message)


# =============================================================================
# Finish implied by a tolerance
# =============================================================================


class TestSurfaceFinishConflict(unittest.TestCase):
    RULE = Rulebook.GDT_SURFACE_FINISH_CONFLICT

    def tearDown(self):
        set_annotation_source(None)

    def _flatness(self, value):
        return rule_check(
            machined_block(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.FLATNESS, tolerance_value_mm=value
                )
            ),
        )

    def test_a_lapping_tolerance_is_an_error(self):
        self.assertEqual(severities(self._flatness(0.003)), [Severity.ERROR])

    def test_a_grinding_tolerance_is_a_warning(self):
        self.assertEqual(severities(self._flatness(0.01)), [Severity.WARNING])

    def test_a_millable_tolerance_passes(self):
        self.assertEqual(self._flatness(0.05), [])

    def test_a_profile_tolerance_counts_too(self):
        findings = rule_check(
            machined_block(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1",
                    type=ToleranceType.PROFILE_OF_A_SURFACE,
                    tolerance_value_mm=0.01,
                )
            ),
        )
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_a_location_tolerance_says_nothing_about_finish(self):
        findings = rule_check(
            machined_block(),
            self.RULE,
            frames(
                FeatureControlFrame(
                    "fcf_1", type=ToleranceType.POSITION, tolerance_value_mm=0.001
                )
            ),
        )
        self.assertEqual(findings, [])

    def test_the_grinding_finding_asks_for_grind_stock(self):
        finding = only(self._flatness(0.01))
        self.assertIn("flatness", finding.message)
        self.assertIn("grinding", finding.message)
        self.assertIn("grind stock", finding.message)

    def test_the_lapping_finding_names_a_separate_machine(self):
        finding = only(self._flatness(0.003))
        self.assertIn("lapping or super-finishing", finding.message)
        self.assertIn("separate machine", finding.message)


# =============================================================================
# Tolerance on the wrong kind of feature
# =============================================================================


class TestFeatureToleranceMismatch(unittest.TestCase):
    RULE = Rulebook.GDT_FEATURE_TOLERANCE_MISMATCH

    def tearDown(self):
        set_annotation_source(None)

    @staticmethod
    def _position_on(feature_type, **kwargs):
        def build(context):
            feature = feature_of(context, feature_type)
            return AnnotationSet(
                frames=[
                    FeatureControlFrame(
                        "fcf_1",
                        type=ToleranceType.POSITION,
                        tolerance_value_mm=0.1,
                        feature_id=feature.instance_id,
                        **kwargs,
                    )
                ]
            )

        return build

    def test_position_on_a_hole_is_ordinary_practice(self):
        findings = rule_check(
            machined_block(), self.RULE, self._position_on(FeatureType.THROUGH_HOLE)
        )
        self.assertEqual(findings, [])

    def test_position_on_a_pocket_is_questioned(self):
        findings = rule_check(
            machined_block(), self.RULE, self._position_on(FeatureType.POCKET)
        )
        self.assertEqual(severities(findings), [Severity.WARNING])

    def test_a_form_tolerance_on_a_pocket_is_not_this_rules_business(self):
        def build(context):
            pocket = feature_of(context, FeatureType.POCKET)
            return AnnotationSet(
                frames=[
                    FeatureControlFrame(
                        "fcf_1",
                        type=ToleranceType.FLATNESS,
                        tolerance_value_mm=0.1,
                        feature_id=pocket.instance_id,
                    )
                ]
            )

        self.assertEqual(rule_check(machined_block(), self.RULE, build), [])

    def test_the_plain_case_suggests_the_control_that_was_wanted(self):
        finding = only(
            rule_check(machined_block(), self.RULE, self._position_on(FeatureType.POCKET))
        )
        self.assertIn("recognized as a pocket", finding.message)
        self.assertIn("feature of size", finding.message)
        self.assertIn("profile of a surface", finding.message)

    def test_a_material_modifier_makes_the_claim_sharper(self):
        # MMC on something with no size is not merely unusual: it is invalid
        # per ASME Y14.5, so the likelier story is a mis-recognized feature.
        finding = only(
            rule_check(
                machined_block(),
                self.RULE,
                self._position_on(
                    FeatureType.POCKET, material_condition=MaterialCondition.MMC
                ),
            )
        )
        self.assertIn("MMC", finding.overview)
        self.assertIn("MMC modifier", finding.message)
        self.assertIn("read wrongly", finding.message)

    def test_the_severity_follows_the_rule_configuration(self):
        findings = rule_check(
            machined_block(),
            self.RULE,
            self._position_on(FeatureType.POCKET),
            severity="ERROR",
        )
        self.assertEqual(severities(findings), [Severity.ERROR])


# =============================================================================
# Surface finish called out directly
# =============================================================================


class TestNoteSurfaceFinishDemanding(unittest.TestCase):
    RULE = Rulebook.NOTE_SURFACE_FINISH_DEMANDING

    def tearDown(self):
        set_annotation_source(None)

    def _note(self, text, face_ids=()):
        def build(context):
            return AnnotationSet(
                notes=[SurfaceFinishNote("n_1", text=text, face_ids=list(face_ids))]
            )

        return rule_check(machined_block(), self.RULE, build)

    def test_a_lapped_finish_is_an_error(self):
        self.assertEqual(severities(self._note("ALL SURFACES Ra 0.05")), [Severity.ERROR])

    def test_a_ground_finish_is_a_warning(self):
        self.assertEqual(severities(self._note("ALL SURFACES Ra 0.4")), [Severity.WARNING])

    def test_a_finish_just_under_the_mill_ceiling_is_information(self):
        self.assertEqual(severities(self._note("Ra 1.2 UNLESS NOTED")), [Severity.INFO])

    def test_an_ordinary_finish_passes(self):
        self.assertEqual(self._note("MACHINED SURFACES Ra 3.2"), [])

    def test_a_note_with_no_callout_passes(self):
        self.assertEqual(self._note("DEBURR AND BREAK SHARP EDGES"), [])

    def test_a_microinch_callout_is_converted_before_judging(self):
        # Ra 16 uin is 0.4 um -- a grinding finish, not a comfortable one.
        self.assertEqual(severities(self._note("FINISH Ra 16 uin")), [Severity.WARNING])

    def test_the_finding_quotes_the_note_and_says_what_it_costs(self):
        finding = only(self._note("ALL SURFACES Ra 0.4"))
        self.assertIn("ALL SURFACES Ra 0.4", finding.message)
        self.assertIn("part-wide note", finding.message)
        self.assertIn("grinding", finding.message)
        self.assertEqual(finding.unit, "um")
        self.assertEqual(finding.value, 0.4)
        self.assertEqual(finding.limit, 1.6)

    def test_a_note_bound_to_faces_highlights_them(self):
        finding = only(self._note("Ra 0.4", face_ids=[4, 2]))
        self.assertEqual(finding.failing_geometry, [("Face", 2), ("Face", 4)])
        self.assertIn("on 2 faces", finding.message)


class TestSurfaceFinishPerFaceDemanding(unittest.TestCase):
    RULE = Rulebook.SURFACE_FINISH_PER_FACE_DEMANDING

    def tearDown(self):
        set_annotation_source(None)

    @staticmethod
    def _requirement(ra_um, face_ids):
        return lambda context: AnnotationSet(
            surface_finishes=[
                SurfaceFinish("sf_1", ra_um=ra_um, face_ids=list(face_ids), source="AP242")
            ]
        )

    def _on_the_pocket(self, ra_um=0.4):
        def build(context):
            pocket = feature_of(context, FeatureType.POCKET)
            return AnnotationSet(
                surface_finishes=[
                    SurfaceFinish(
                        "sf_1",
                        ra_um=ra_um,
                        face_ids=sorted(pocket.faces),
                        source="AP242",
                    )
                ]
            )

        return build

    def test_a_demanding_finish_on_a_pocket_is_reported_against_it(self):
        finding = only(rule_check(machined_block(), self.RULE, self._on_the_pocket()))
        self.assertEqual(finding.severity, Severity.WARNING)
        self.assertIn("pocket p_0", finding.message)
        self.assertIn("Ra 0.40 um", finding.message)

    def test_the_highlight_is_the_faces_the_finish_applies_to(self):
        context = context_of(machined_block())
        expected = [("Face", i) for i in sorted(feature_of(context, "POCKET").faces)]
        finding = only(rule_check(machined_block(), self.RULE, self._on_the_pocket()))
        self.assertEqual(finding.failing_geometry, expected)

    def test_an_ordinary_finish_passes(self):
        self.assertEqual(rule_check(machined_block(), self.RULE, self._on_the_pocket(3.2)), [])

    def test_the_bands_match_the_note_rule(self):
        for ra_um, severity in ((0.05, Severity.ERROR), (0.4, Severity.WARNING), (1.2, Severity.INFO)):
            with self.subTest(ra_um=ra_um):
                findings = rule_check(machined_block(), self.RULE, self._on_the_pocket(ra_um))
                self.assertEqual(severities(findings), [severity])

    def test_a_requirement_with_no_face_linkage_still_gets_said_once(self):
        finding = only(rule_check(machined_block(), self.RULE, self._requirement(0.4, [])))
        self.assertIn("no face linkage resolved", finding.message)
        self.assertEqual(finding.failing_geometry, [])

    def test_a_finish_on_a_face_no_feature_owns_is_still_reported(self):
        # A callout on a plain outside wall costs exactly what one on a
        # pocket costs, so it cannot be dropped for want of a feature.
        context = context_of(machined_block())
        claimed = {f for feature in context.recognition.features for f in feature.faces}
        loose = sorted(n.face_id for n in context.graph.nodes if n.face_id not in claimed)
        self.assertTrue(loose, "the block should have faces no feature claims")

        finding = only(
            rule_check(machined_block(), self.RULE, self._requirement(0.4, loose[:1]))
        )
        self.assertIn("1 face of the part", finding.message)
        self.assertEqual(finding.failing_geometry, [("Face", loose[0])])

    def test_a_finish_spanning_two_features_is_reported_against_each(self):
        def build(context):
            pocket = feature_of(context, FeatureType.POCKET)
            hole = feature_of(context, FeatureType.THROUGH_HOLE)
            return AnnotationSet(
                surface_finishes=[
                    SurfaceFinish(
                        "sf_1",
                        ra_um=0.4,
                        face_ids=sorted(set(pocket.faces) | set(hole.faces)),
                    )
                ]
            )

        findings = rule_check(machined_block(), self.RULE, build)
        self.assertEqual(len(findings), 2)
        # Sorted by instance id, so the hole comes before the pocket.
        self.assertIn("through hole h_0", findings[0].message)
        self.assertIn("pocket p_0", findings[1].message)


if __name__ == "__main__":
    unittest.main()
