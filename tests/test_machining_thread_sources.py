# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the two thread sources that are not geometry.

A modelled helix is the only thread the workbench can measure. The other two
it has to be told about: a native FreeCAD document states which of its holes
are tapped, and an imported one can only be asked. Both end up saying the
same thing to the same recognizer, and both have to survive the model being
rebuilt underneath them.

The document reading itself needs a live FreeCAD, so it sits behind a seam --
a callable that hands over the Hole features already placed and aimed. These
tests feed that seam plain tuples and a stand-in object, which covers every
decision in the mapping without a document in sight. The real thing is
verified in ``tests/freecad/verify_in_freecad.py``.
"""

import unittest
from unittest import mock

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import MachiningAnalyzer
from freecad.DFM.core.machining.aag_builder import AagBuilder
from freecad.DFM.core.machining.features import FeatureInstance, FeatureType
from freecad.DFM.core.machining.recognizers import HoleRecognizer
from freecad.DFM.core.machining.thread_sources import (
    MODELLED_HELIX,
    NATIVE_DECLARATION,
    USER_CONFIRMED,
    BoreKey,
    Confirmation,
    ConfirmationStore,
    ThreadDeclaration,
    ThreadEvidence,
    ThreadFact,
    Transform,
    bore_key,
    candidates_for,
    copies_by_feature,
    declaration_from_hole,
    native_declarations,
    record_answers,
    resolve_declared_size,
    thread_evidence_for,
    transforms_of,
)
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# =============================================================================
# Stand-ins
# =============================================================================


class FakeHole:
    """A ``PartDesign::Hole`` reduced to the properties that matter.

    Everything the reader touches is a named property, so a plain object with
    the same names exercises the same code the document does.
    """

    TypeId = "PartDesign::Hole"

    def __init__(self, **properties):
        defaults = {
            "Threaded": True,
            "ThreadType": "ISOMetricProfile",
            "ThreadSize": "M6x1",
            "ThreadClass": "6H",
            "ThreadDepth": 0.0,
            "ThreadDepthType": "Hole Depth",
            "ThreadDirection": "Right",
            "ThreadDiameter": 6.0,
            "ModelThread": False,
            "Diameter": 5.0,
            "Depth": 15.0,
            "Label": "Hole",
            "Name": "Hole",
        }
        defaults.update(properties)
        for key, value in defaults.items():
            setattr(self, key, value)


class FakePattern:
    """A PartDesign transform feature, reduced to what places its copies.

    Only the properties a given pattern actually reads need setting. What is
    left off reads back the way FreeCAD's own default does -- no second
    direction, no custom spacings, dimensioned end to end -- which is how
    most patterns in most documents are set up.
    """

    def __init__(self, type_id, name="Pattern", **properties):
        self.TypeId = type_id
        self.Name = name
        self.Label = name
        self.TransformMode = "Features"
        self.Originals = []
        for key, value in properties.items():
            setattr(self, key, value)


class FakeResolver:
    """The document reading, already done.

    Turning a link into a line in space is the one part of the expansion that
    needs a live FreeCAD, so here the link is simply the name of an answer.
    A link with no answer stands for one that could not be resolved, which is
    what every unsupported datum and every broken reference comes to.
    """

    AXES = {
        "x": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        "y": ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        "z": ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    }
    PLANES = {
        "x=0": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        "y=0": ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    }

    def axis(self, link):
        return self.AXES.get(link)

    def plane(self, link):
        return self.PLANES.get(link)


def _places(transforms, point=(10.0, 0.0, 0.0)):
    """Where a set of transforms puts one hole, ready to compare."""
    return sorted(
        tuple(round(value, 6) + 0.0 for value in transform.apply_point(point))
        for transform in transforms
    )


class FakeStore:
    """The hidden document object the confirmations live in."""

    def __init__(self, records=()):
        self.DFMThreadRecords = list(records)


class FakeDocument:
    def __init__(self, objects=()):
        self.Objects = list(objects)
        self.added = []

    def addObject(self, type_id, name):
        holder = FakeStore()
        holder.TypeId = type_id
        holder.Name = name
        holder.Label = name
        holder.ViewObject = None
        # A fresh holder has no property until it is asked for one, the way
        # a real FeaturePython has none.
        del holder.DFMThreadRecords
        self.Objects.append(holder)
        self.added.append(holder)
        return holder


class FakeTarget:
    """The analysed object: a name, a document and whatever it is built from."""

    def __init__(self, name="Part", document=None, out_list=()):
        self.Name = name
        self.Label = name
        self.Document = document if document is not None else FakeDocument()
        self.OutList = list(out_list)


# =============================================================================
# Shapes
# =============================================================================


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def blind_tap_drill_hole() -> TopoDS_Shape:
    """A 5 mm blind bore in a plate: exactly an M6 tap drill, and no thread.

    The shape a real tapped hole arrives as. Nothing in it says thread, which
    is the whole difficulty.
    """
    block = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(40, 40, 20)).Shape()
    drill = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(20, 20, 8), gp_Dir(0, 0, 1)), 2.5, 20.0
    ).Shape()
    return _cut(block, drill)


def off_size_hole() -> TopoDS_Shape:
    """A 7.5 mm bore, which is no tap drill at all."""
    block = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(40, 40, 20)).Shape()
    drill = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(20, 20, 8), gp_Dir(0, 0, 1)), 3.75, 20.0
    ).Shape()
    return _cut(block, drill)


def _holes_with(shape, evidence):
    """Run the hole pass over a shape with the given evidence in hand."""
    graph = AagBuilder(shape, FaceIndex(shape)).build()
    recognizer = HoleRecognizer()
    recognizer.thread_evidence = evidence
    return recognizer.recognize(graph, shape, set(), [])


def _context(shape, target=None):
    data = MachiningAnalyzer().execute(
        shape, FaceIndex(shape), EdgeIndex(shape), prefs={}, target_object=target
    )
    return list(data.values())[0]


# The bore in the fixtures above, described the way a declaration describes it.
_BORE_ORIGIN = (20.0, 20.0, 8.0)
_BORE_AXIS = (0.0, 0.0, 1.0)


def _m6_declaration(**overrides) -> ThreadDeclaration:
    fields = {
        "designation": "M6x1.0",
        "nominal_mm": 6.0,
        "pitch_mm": 1.0,
        "positions": (_BORE_ORIGIN,),
        "direction": _BORE_AXIS,
        "bore_window": (5.0, 6.0),
        "depth_mm": None,
        "hand": "right",
        "declared_by": "Hole",
    }
    fields.update(overrides)
    return ThreadDeclaration(**fields)


# =============================================================================


class TestNamingADeclaredThread(unittest.TestCase):
    """Turning what FreeCAD spells into what the tables and rules use."""

    def test_a_metric_size_resolves_through_the_table(self):
        # FreeCAD writes "M6x1" and the table says "M6x1.0". One thread, and
        # it must not end up with two names depending on which source found it.
        self.assertEqual(
            resolve_declared_size("M6x1", "ISOMetricProfile", 6.0),
            ("M6x1.0", 6.0, 1.0),
        )

    def test_a_bare_metric_size_takes_the_coarse_pitch(self):
        self.assertEqual(
            resolve_declared_size("M8", "ISOMetricProfile", 8.0),
            ("M8x1.25", 8.0, 1.25),
        )

    def test_a_fine_metric_pitch_comes_from_the_size_text(self):
        # The tables are coarse only. Reading the pitch out of the callout is
        # what keeps an M8x1 from being reported as an M8x1.25.
        self.assertEqual(
            resolve_declared_size("M8x1", "ISOMetricFineProfile", 8.0),
            ("M8x1", 8.0, 1.0),
        )

    def test_a_unified_coarse_size_resolves_from_its_major_diameter(self):
        # FreeCAD spells the size "1/4" and the table "1/4-20 UNC". The major
        # diameter is what ties the two together.
        self.assertEqual(
            resolve_declared_size("1/4", "UNC", 6.35),
            ("1/4-20 UNC", 6.35, 1.27),
        )

    def test_a_fine_unified_thread_is_named_but_not_pitched(self):
        # A 1/4 UNF has the same major diameter as a 1/4-20 UNC and half
        # again the thread count. Quoting the coarse pitch would put the
        # run-out rule to work on a number nobody stated.
        designation, nominal, pitch = resolve_declared_size("1/4", "UNF", 6.35)
        self.assertEqual(designation, "1/4 UNF")
        self.assertAlmostEqual(nominal, 6.35)
        self.assertIsNone(pitch)

    def test_a_pipe_thread_is_not_dressed_up_as_a_machine_screw(self):
        # An NPT 1/8 measures about 10.2 across the crests, which is within
        # a tenth of an M10. Naming it one would be worse than not naming it.
        designation, _, pitch = resolve_declared_size("1/8", "NPT", 10.2)
        self.assertEqual(designation, "1/8 NPT")
        self.assertIsNone(pitch)

    def test_no_size_and_no_diameter_resolves_to_nothing(self):
        self.assertIsNone(resolve_declared_size("---", "None", None))


class TestReadingAHoleFeature(unittest.TestCase):
    """The mapping from a Hole feature's properties to a declaration."""

    def _declare(self, **properties):
        return declaration_from_hole(
            FakeHole(**properties), [_BORE_ORIGIN], _BORE_AXIS
        )

    def test_a_threaded_hole_declares_its_thread(self):
        declaration = self._declare()
        self.assertIsNotNone(declaration)
        self.assertEqual(declaration.designation, "M6x1.0")
        self.assertAlmostEqual(declaration.nominal_mm, 6.0)
        self.assertAlmostEqual(declaration.pitch_mm, 1.0)

    def test_an_unthreaded_hole_declares_nothing(self):
        self.assertIsNone(self._declare(Threaded=False))

    def test_a_hole_with_no_thread_profile_declares_nothing(self):
        # Threaded ticked and no profile chosen: the feature is half filled
        # in and there is no thread to name.
        self.assertIsNone(self._declare(ThreadType="None", ThreadSize="---"))

    def test_the_bore_window_spans_both_stated_diameters(self):
        # Which of Diameter and ThreadDiameter is the drill and which the
        # crest is not worth pinning down. The bore on the final shape is
        # somewhere between them either way.
        window = self._declare(Diameter=5.0, ThreadDiameter=6.0).bore_window
        self.assertAlmostEqual(window[0], 5.0)
        self.assertAlmostEqual(window[1], 6.0)

    def test_a_dimensioned_thread_depth_is_taken(self):
        declaration = self._declare(ThreadDepthType="Dimension", ThreadDepth=12.0)
        self.assertAlmostEqual(declaration.depth_mm, 12.0)

    def test_a_thread_tapped_to_the_hole_depth_states_no_length(self):
        # "Hole Depth" says the thread runs as far as the bore does, and the
        # rules already worst-case an unstated length to exactly that. Saying
        # it twice would only give the two statements a chance to disagree.
        self.assertIsNone(self._declare(ThreadDepthType="Hole Depth").depth_mm)

    def test_a_left_hand_thread_is_recorded_as_one(self):
        self.assertEqual(self._declare(ThreadDirection="Left").hand, "left")
        self.assertEqual(self._declare().hand, "right")

    def test_the_declaring_feature_is_named(self):
        self.assertEqual(self._declare(Label="Tapped M6").declared_by, "Tapped M6")


class TestMatchingADeclarationToABore(unittest.TestCase):
    """Which recognized bore a declared hole is about."""

    def test_a_bore_on_the_declared_centreline_matches(self):
        self.assertTrue(_m6_declaration().covers(5.0, _BORE_ORIGIN, _BORE_AXIS))

    def test_the_match_does_not_care_where_along_the_axis_it_is_asked(self):
        # A cylinder reports its axis through whatever point the kernel parked
        # there, and that point slides between rebuilds.
        self.assertTrue(_m6_declaration().covers(5.0, (20.0, 20.0, 99.0), _BORE_AXIS))

    def test_the_match_does_not_care_which_way_the_axis_points(self):
        self.assertTrue(_m6_declaration().covers(5.0, _BORE_ORIGIN, (0.0, 0.0, -1.0)))

    def test_a_bore_beside_the_declared_one_does_not_match(self):
        # The neighbouring hole in a pattern is the same size on a parallel
        # axis. Position is the only thing telling them apart.
        self.assertFalse(_m6_declaration().covers(5.0, (32.0, 20.0, 8.0), _BORE_AXIS))

    def test_a_bore_across_the_declared_one_does_not_match(self):
        self.assertFalse(_m6_declaration().covers(5.0, _BORE_ORIGIN, (1.0, 0.0, 0.0)))

    def test_a_bore_of_the_wrong_size_does_not_match(self):
        self.assertFalse(_m6_declaration().covers(8.5, _BORE_ORIGIN, _BORE_AXIS))

    def test_a_bore_inside_the_window_matches(self):
        # A modelled thread leaves the wall somewhere between the drill and
        # the crest, so the whole window has to count.
        self.assertTrue(_m6_declaration().covers(5.6, _BORE_ORIGIN, _BORE_AXIS))


class TestNamingABoreDurably(unittest.TestCase):
    """The key an answer is filed under, and what it survives."""

    def test_a_key_survives_the_axis_being_reported_differently(self):
        # Same line, reported through a different point and the other way
        # round. A rebuild does both, and the answer has to come back to the
        # same hole afterwards.
        first = bore_key("Part", 5.0, (20.0, 20.0, 8.0), (0.0, 0.0, 1.0))
        second = bore_key("Part", 5.0, (20.0, 20.0, -40.0), (0.0, 0.0, -1.0))
        self.assertTrue(first.matches(second))

    def test_a_key_round_trips_through_text(self):
        key = bore_key("Part", 5.0, (20.0, 20.0, 8.0), (0.0, 0.0, 1.0))
        restored = BoreKey.decode(key.encode())
        self.assertIsNotNone(restored)
        self.assertTrue(key.matches(restored))

    def test_a_different_hole_gets_a_different_key(self):
        here = bore_key("Part", 5.0, (20.0, 20.0, 8.0), (0.0, 0.0, 1.0))
        there = bore_key("Part", 5.0, (32.0, 20.0, 8.0), (0.0, 0.0, 1.0))
        resized = bore_key("Part", 6.8, (20.0, 20.0, 8.0), (0.0, 0.0, 1.0))
        elsewhere = bore_key("Other", 5.0, (20.0, 20.0, 8.0), (0.0, 0.0, 1.0))
        self.assertFalse(here.matches(there))
        self.assertFalse(here.matches(resized))
        self.assertFalse(here.matches(elsewhere))

    def test_a_degenerate_axis_gets_no_key(self):
        self.assertIsNone(bore_key("Part", 5.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
        self.assertIsNone(bore_key("Part", 0.0, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)))

    def test_rubbish_decodes_to_nothing(self):
        self.assertIsNone(BoreKey.decode("Part|5.0"))
        self.assertIsNone(BoreKey.decode(""))


class TestTheConfirmationStore(unittest.TestCase):
    """Keeping the answers, both kinds, across a save and a reload."""

    def setUp(self):
        self.key = bore_key("Part", 5.0, (20.0, 20.0, 8.0), (0.0, 0.0, 1.0))
        self.other = bore_key("Part", 5.0, (32.0, 20.0, 8.0), (0.0, 0.0, 1.0))

    def test_an_answer_comes_back(self):
        store = ConfirmationStore()
        store.remember(Confirmation(self.key, True, "M6x1.0"))
        record = store.verdict_for(self.key)
        self.assertIsNotNone(record)
        self.assertTrue(record.accepted)
        self.assertEqual(record.designation, "M6x1.0")

    def test_a_rejection_is_kept_as_carefully_as_a_confirmation(self):
        # Being asked twice about the same dowel hole is how a shop learns to
        # click past the question without reading it.
        store = ConfirmationStore()
        store.remember(Confirmation(self.key, False))
        record = store.verdict_for(self.key)
        self.assertIsNotNone(record)
        self.assertFalse(record.accepted)

    def test_an_unanswered_bore_has_no_verdict(self):
        store = ConfirmationStore()
        store.remember(Confirmation(self.key, True, "M6x1.0"))
        self.assertIsNone(store.verdict_for(self.other))

    def test_answering_again_replaces_the_answer(self):
        store = ConfirmationStore()
        store.remember(Confirmation(self.key, True, "M6x1.0"))
        store.remember(Confirmation(self.key, False))
        self.assertEqual(len(store), 1)
        self.assertFalse(store.verdict_for(self.key).accepted)

    def test_the_store_round_trips_through_the_document(self):
        store = ConfirmationStore()
        store.remember(Confirmation(self.key, True, "M6x1.0"))
        store.remember(Confirmation(self.other, False))
        restored = ConfirmationStore.decode(store.encode())
        self.assertEqual(len(restored), 2)
        self.assertTrue(restored.verdict_for(self.key).accepted)
        self.assertFalse(restored.verdict_for(self.other).accepted)

    def test_an_unreadable_record_is_dropped_rather_than_thrown(self):
        # A record written by a newer version, or one somebody edited by
        # hand. Losing an answer is a nuisance; losing the analysis is not.
        restored = ConfirmationStore.decode(["nonsense", "yes;M6x1.0;bad|key"])
        self.assertEqual(len(restored), 0)


class TestGatheringTheEvidence(unittest.TestCase):
    """The one entry point, and what it does with each kind of part."""

    def test_a_part_with_no_document_behind_it_has_no_evidence(self):
        evidence = thread_evidence_for(None)
        self.assertFalse(evidence)
        self.assertEqual(evidence.declarations, ())

    def test_the_declarations_come_through_the_seam(self):
        hole = FakeHole()
        target = FakeTarget(out_list=[hole])
        found = native_declarations(
            target, frames=lambda _t: [(hole, [_BORE_ORIGIN], _BORE_AXIS)]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].designation, "M6x1.0")

    def test_one_hole_feature_can_declare_several_holes(self):
        # A profile sketch with four circles drills four holes, and they are
        # all the same thread.
        hole = FakeHole()
        positions = [(x, 20.0, 8.0) for x in (10.0, 20.0, 30.0, 36.0)]
        found = native_declarations(
            FakeTarget(out_list=[hole]),
            frames=lambda _t: [(hole, positions, _BORE_AXIS)],
        )
        self.assertEqual(len(found[0].positions), 4)
        for position in positions:
            self.assertTrue(found[0].covers(5.0, position, _BORE_AXIS))

    def test_a_document_that_will_not_read_loses_no_analysis(self):
        # A document oddity is worth a warning and not worth the analysis.
        def explode(_target):
            raise RuntimeError("a datum went missing")

        with mock.patch(
            "freecad.DFM.core.machining.thread_sources._warn"
        ) as warned:
            self.assertEqual(native_declarations(FakeTarget(), frames=explode), [])
        self.assertTrue(warned.called)

    def test_the_confirmations_come_off_the_document(self):
        key = bore_key("Part", 5.0, _BORE_ORIGIN, _BORE_AXIS)
        store = FakeStore([Confirmation(key, True, "M6x1.0").encode()])
        evidence = thread_evidence_for(
            FakeTarget(document=FakeDocument([store])), frames=lambda _t: []
        )
        self.assertTrue(evidence)
        self.assertEqual(len(evidence.confirmations), 1)

    def test_a_declaration_outranks_a_confirmation(self):
        # The designer said M6 and somebody clicked M8. The document wins:
        # one of the two was written into the feature that makes the hole.
        key = bore_key("Part", 5.0, _BORE_ORIGIN, _BORE_AXIS)
        confirmations = ConfirmationStore(
            [Confirmation(key, True, "M8x1.25")]
        )
        evidence = ThreadEvidence(
            object_name="Part",
            declarations=(_m6_declaration(),),
            confirmations=confirmations,
        )
        fact = evidence.fact_for(5.0, _BORE_ORIGIN, _BORE_AXIS)
        self.assertEqual(fact.designation, "M6x1.0")
        self.assertEqual(fact.evidence, NATIVE_DECLARATION)

    def test_a_confirmation_becomes_a_fact(self):
        key = bore_key("Part", 5.0, _BORE_ORIGIN, _BORE_AXIS)
        evidence = ThreadEvidence(
            object_name="Part",
            confirmations=ConfirmationStore([Confirmation(key, True, "M6x1.0")]),
        )
        fact = evidence.fact_for(5.0, _BORE_ORIGIN, _BORE_AXIS)
        self.assertEqual(fact.designation, "M6x1.0")
        self.assertAlmostEqual(fact.pitch_mm, 1.0)
        self.assertEqual(fact.evidence, USER_CONFIRMED)

    def test_a_rejection_asserts_nothing(self):
        key = bore_key("Part", 5.0, _BORE_ORIGIN, _BORE_AXIS)
        evidence = ThreadEvidence(
            object_name="Part",
            confirmations=ConfirmationStore([Confirmation(key, False)]),
        )
        self.assertIsNone(evidence.fact_for(5.0, _BORE_ORIGIN, _BORE_AXIS))


class TestPlacingTheCopies(unittest.TestCase):
    """Where a transform feature puts the copies of what it repeats.

    Every number checked here was read back off FreeCAD 1.1.3, by building
    the pattern in a document and measuring the bores it left in the solid.
    A pattern is not a thing to reason out from the property names: whether a
    sweep is shared between the copies or between the gaps, and which way a
    reversed one runs, are decisions FreeCAD made and this has to match.
    """

    def setUp(self):
        self.resolver = FakeResolver()

    def _linear(self, **properties):
        return transforms_of(
            FakePattern("PartDesign::LinearPattern", **properties), self.resolver
        )

    def _polar(self, **properties):
        return transforms_of(
            FakePattern("PartDesign::PolarPattern", **properties), self.resolver
        )

    # -- a row --------------------------------------------------------------

    def test_a_row_shares_its_length_between_the_gaps(self):
        # Three holes over 40 mm is 20 mm apart, not 40. The length is
        # measured first to last, which is how a drawing dimensions a row.
        found = self._linear(Direction="x", Length=40.0, Occurrences=3)
        self.assertEqual(
            _places(found), [(10.0, 0.0, 0.0), (30.0, 0.0, 0.0), (50.0, 0.0, 0.0)]
        )

    def test_the_original_is_one_of_the_copies(self):
        # The hole the pattern repeats is already cut by the feature above
        # it, so the pattern's own list has to start where that one stands or
        # the declaration would miss the hole it was read off.
        found = self._linear(Direction="x", Length=40.0, Occurrences=3)
        self.assertIn((10.0, 0.0, 0.0), _places(found))

    def test_a_reversed_row_runs_the_other_way(self):
        found = self._linear(
            Direction="x", Length=40.0, Occurrences=3, Reversed=True
        )
        self.assertEqual(
            _places(found), [(-30.0, 0.0, 0.0), (-10.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
        )

    def test_a_row_dimensioned_by_gap_uses_the_offset(self):
        found = self._linear(
            Direction="x", Mode="Spacing", Offset=15.0, Occurrences=3
        )
        self.assertEqual(
            _places(found), [(10.0, 0.0, 0.0), (25.0, 0.0, 0.0), (40.0, 0.0, 0.0)]
        )

    def test_a_gap_left_unset_falls_back_to_the_offset(self):
        # FreeCAD writes -1 into the spacing list for a gap nobody has
        # touched, and means the ordinary offset by it.
        found = self._linear(
            Direction="x",
            Mode="Spacing",
            Offset=8.0,
            Occurrences=3,
            Spacings=[-1.0, 5.0],
        )
        self.assertEqual(
            _places(found), [(10.0, 0.0, 0.0), (18.0, 0.0, 0.0), (23.0, 0.0, 0.0)]
        )

    def test_a_repeating_run_of_uneven_gaps_is_refused(self):
        # An experimental setting, hidden behind a preference, whose reading
        # against the plain spacing list is not written down. A row placed
        # by guesswork would put the tap callout on the wrong bores.
        self.assertIsNone(
            self._linear(
                Direction="x",
                Mode="Spacing",
                Offset=10.0,
                Occurrences=4,
                SpacingPattern=[10.0, 20.0],
            )
        )

    def test_a_grid_repeats_the_row_rather_than_continuing_it(self):
        # Two across and two down is four holes. The second direction
        # multiplies the first: reading it as another leg of the same row
        # would leave the far corner undeclared.
        found = self._linear(
            Direction="x",
            Length=10.0,
            Occurrences=2,
            Direction2="y",
            Length2=20.0,
            Occurrences2=2,
        )
        self.assertEqual(
            _places(found),
            [
                (10.0, 0.0, 0.0),
                (10.0, 20.0, 0.0),
                (20.0, 0.0, 0.0),
                (20.0, 20.0, 0.0),
            ],
        )

    def test_a_second_direction_that_will_not_resolve_refuses_the_whole_grid(self):
        self.assertIsNone(
            self._linear(
                Direction="x",
                Length=10.0,
                Occurrences=2,
                Direction2="a datum nobody can find",
                Length2=20.0,
                Occurrences2=2,
            )
        )

    def test_a_row_of_one_is_the_hole_where_it_was_drawn(self):
        found = self._linear(Direction="x", Length=40.0, Occurrences=1)
        self.assertEqual(_places(found), [(10.0, 0.0, 0.0)])

    def test_a_direction_that_will_not_resolve_is_refused(self):
        # The whole reason for refusing: a direction guessed at does not
        # lose the copies, it puts them on whatever bores lie where they
        # were expected.
        self.assertIsNone(self._linear(Direction=None, Length=40.0, Occurrences=3))

    def test_a_way_of_dimensioning_this_does_not_know_is_refused(self):
        self.assertIsNone(
            self._linear(Direction="x", Mode="Something New", Occurrences=3)
        )

    # -- a bolt circle ------------------------------------------------------

    def test_a_full_turn_divides_by_the_holes_and_not_the_gaps(self):
        # The one case where the last copy would land back on the first.
        # Four holes round a full turn are 90 apart, not 120.
        found = self._polar(Axis="z", Angle=360.0, Occurrences=4)
        self.assertEqual(
            _places(found),
            [
                (-10.0, 0.0, 0.0),
                (0.0, -10.0, 0.0),
                (0.0, 10.0, 0.0),
                (10.0, 0.0, 0.0),
            ],
        )

    def test_a_part_turn_divides_by_the_gaps(self):
        found = self._polar(Axis="z", Angle=180.0, Occurrences=3)
        self.assertEqual(
            _places(found),
            [(-10.0, 0.0, 0.0), (0.0, 10.0, 0.0), (10.0, 0.0, 0.0)],
        )

    def test_a_reversed_bolt_circle_turns_the_other_way(self):
        found = self._polar(
            Axis="z", Angle=180.0, Occurrences=3, Reversed=True
        )
        self.assertEqual(
            _places(found),
            [(-10.0, 0.0, 0.0), (0.0, -10.0, 0.0), (10.0, 0.0, 0.0)],
        )

    def test_a_bolt_circle_dimensioned_by_gap_uses_the_offset(self):
        found = self._polar(
            Axis="z", Mode="Spacing", Offset=90.0, Occurrences=3
        )
        self.assertEqual(
            _places(found),
            [(-10.0, 0.0, 0.0), (0.0, 10.0, 0.0), (10.0, 0.0, 0.0)],
        )

    def test_an_axis_that_will_not_resolve_is_refused(self):
        self.assertIsNone(self._polar(Axis="a datum nobody can find", Angle=360.0, Occurrences=4))

    def test_a_bolt_circle_about_a_tilted_axis_aims_its_copies_differently(self):
        # Turning about anything but the drilling axis swings the copies out
        # of line with the original, which is why the declarations for one
        # Hole feature cannot always share a direction.
        found = self._polar(Axis="x", Angle=180.0, Occurrences=2)
        aims = [
            tuple(round(v, 6) + 0.0 for v in t.apply_direction((0.0, 0.0, 1.0)))
            for t in found
        ]
        self.assertEqual(aims, [(0.0, 0.0, 1.0), (0.0, 0.0, -1.0)])

    # -- a mirror -----------------------------------------------------------

    def test_a_mirror_keeps_the_original_and_adds_the_far_side(self):
        found = transforms_of(
            FakePattern("PartDesign::Mirrored", MirrorPlane="x=0"), self.resolver
        )
        self.assertEqual(_places(found), [(-10.0, 0.0, 0.0), (10.0, 0.0, 0.0)])

    def test_a_mirror_plane_that_will_not_resolve_is_refused(self):
        self.assertIsNone(
            transforms_of(
                FakePattern("PartDesign::Mirrored", MirrorPlane="a face that went"),
                self.resolver,
            )
        )

    # -- several at once ----------------------------------------------------

    def test_a_multi_transform_multiplies_its_stages(self):
        # A row of two turned three ways is six holes, not five. Each stage
        # acts on everything the ones before it made.
        row = FakePattern(
            "PartDesign::LinearPattern", Direction="x", Length=10.0, Occurrences=2
        )
        circle = FakePattern(
            "PartDesign::PolarPattern", Axis="z", Angle=360.0, Occurrences=3
        )
        found = transforms_of(
            FakePattern("PartDesign::MultiTransform", Transformations=[row, circle]),
            self.resolver,
        )
        self.assertEqual(len(found), 6)
        self.assertEqual(
            _places(found),
            [
                (-10.0, -17.320508, 0.0),
                (-10.0, 17.320508, 0.0),
                (-5.0, -8.660254, 0.0),
                (-5.0, 8.660254, 0.0),
                (10.0, 0.0, 0.0),
                (20.0, 0.0, 0.0),
            ],
        )

    def test_the_stages_are_applied_in_the_order_they_are_listed(self):
        # Turning a row is not the same as rowing a turn. Listing them the
        # other way round moves four of the six holes.
        row = FakePattern(
            "PartDesign::LinearPattern", Direction="x", Length=10.0, Occurrences=2
        )
        circle = FakePattern(
            "PartDesign::PolarPattern", Axis="z", Angle=360.0, Occurrences=3
        )
        first = transforms_of(
            FakePattern("PartDesign::MultiTransform", Transformations=[row, circle]),
            self.resolver,
        )
        second = transforms_of(
            FakePattern("PartDesign::MultiTransform", Transformations=[circle, row]),
            self.resolver,
        )
        self.assertNotEqual(_places(first), _places(second))
        # Turned first, the copies move out along the row they end up on;
        # rowed first, the whole row is swung round together.
        self.assertEqual(
            _places(second),
            [
                (-5.0, -8.660254, 0.0),
                (-5.0, 8.660254, 0.0),
                (5.0, -8.660254, 0.0),
                (5.0, 8.660254, 0.0),
                (10.0, 0.0, 0.0),
                (20.0, 0.0, 0.0),
            ],
        )

    def test_one_stage_that_cannot_be_read_refuses_all_of_them(self):
        # Half a MultiTransform is the worst possible answer: it puts real
        # copies in some of the right places and declares nothing at the
        # rest, which reads from the outside like a finished analysis.
        row = FakePattern(
            "PartDesign::LinearPattern", Direction="x", Length=10.0, Occurrences=2
        )
        lost = FakePattern(
            "PartDesign::PolarPattern", Axis="gone", Angle=360.0, Occurrences=3
        )
        self.assertIsNone(
            transforms_of(
                FakePattern(
                    "PartDesign::MultiTransform", Transformations=[row, lost]
                ),
                self.resolver,
            )
        )

    def test_a_scaled_stage_refuses_the_whole_transform(self):
        # A scaled copy of an M6 tapped hole is a bore no M6 tap fits. There
        # is no honest way to carry the callout onto it.
        row = FakePattern(
            "PartDesign::LinearPattern", Direction="x", Length=10.0, Occurrences=2
        )
        scaled = FakePattern("PartDesign::Scaled", Factor=1.5, Occurrences=2)
        self.assertIsNone(
            transforms_of(
                FakePattern(
                    "PartDesign::MultiTransform", Transformations=[row, scaled]
                ),
                self.resolver,
            )
        )

    def test_a_multi_transform_with_no_stages_moves_nothing(self):
        found = transforms_of(
            FakePattern("PartDesign::MultiTransform", Transformations=[]),
            self.resolver,
        )
        self.assertEqual(_places(found), [(10.0, 0.0, 0.0)])

    # -- what is not on the list --------------------------------------------

    def test_repeating_the_whole_shape_is_refused(self):
        # The copies are fused rather than cut, so a hole in one lands in
        # solid metal in the next and is filled in by it. Which bores come
        # out the far side depends on how the copies overlap, and that is
        # not something the properties can be made to say.
        self.assertIsNone(
            self._linear(
                Direction="x",
                Length=40.0,
                Occurrences=3,
                TransformMode="Whole shape",
            )
        )

    def test_a_kind_of_transform_this_does_not_know_is_refused(self):
        self.assertIsNone(
            transforms_of(
                FakePattern("PartDesign::Scaled", Factor=2.0, Occurrences=2),
                self.resolver,
            )
        )


class TestWhichHolesGetRepeated(unittest.TestCase):
    """Reading the document's transform features onto the holes they copy."""

    def setUp(self):
        self.resolver = FakeResolver()
        self.hole = FakeHole(Name="TappedHole", Label="TappedHole")

    def _row(self, name="Pattern", **properties):
        fields = {
            "Direction": "x",
            "Length": 40.0,
            "Occurrences": 3,
            "Originals": [self.hole],
        }
        fields.update(properties)
        return FakePattern("PartDesign::LinearPattern", name, **fields)

    def test_a_patterned_hole_gets_every_copy(self):
        copies, refused = copies_by_feature([self.hole, self._row()], self.resolver)
        self.assertEqual(refused, set())
        self.assertEqual(
            _places(copies["TappedHole"]),
            [(10.0, 0.0, 0.0), (30.0, 0.0, 0.0), (50.0, 0.0, 0.0)],
        )

    def test_an_unpatterned_hole_is_named_by_nothing(self):
        copies, refused = copies_by_feature([self.hole], self.resolver)
        self.assertEqual(copies, {})
        self.assertEqual(refused, set())

    def test_two_patterns_on_one_hole_both_count(self):
        # Nothing stops a second pattern repeating the same feature, and
        # both sets of copies are really cut.
        down = self._row("Down", Direction="y", Length=20.0, Occurrences=2)
        copies, _ = copies_by_feature(
            [self.hole, self._row(), down], self.resolver
        )
        self.assertIn((50.0, 0.0, 0.0), _places(copies["TappedHole"]))
        self.assertIn((10.0, 20.0, 0.0), _places(copies["TappedHole"]))

    def test_a_pattern_that_cannot_be_read_takes_the_hole_with_it(self):
        # The point of the whole exercise. One tapped hole reported out of
        # twelve is a worse answer than none, because it looks like an
        # answer.
        with mock.patch("freecad.DFM.core.machining.thread_sources._warn"):
            copies, refused = copies_by_feature(
                [self.hole, self._row(Direction="gone")], self.resolver
            )
        self.assertEqual(refused, {"TappedHole"})
        self.assertNotIn("TappedHole", copies)

    def test_the_refusal_is_said_out_loud(self):
        with mock.patch(
            "freecad.DFM.core.machining.thread_sources._warn"
        ) as warned:
            copies_by_feature([self.hole, self._row(Direction="gone")], self.resolver)
        self.assertTrue(warned.called)
        self.assertIn("TappedHole", warned.call_args[0][0])

    def test_a_stage_of_a_multi_transform_is_not_a_pattern_of_its_own(self):
        # The stages are linked from the MultiTransform and reachable in the
        # walk, but they repeat nothing on their own account. Counting them
        # twice would declare holes the part does not have.
        row = FakePattern(
            "PartDesign::LinearPattern",
            "Row",
            Direction="x",
            Length=10.0,
            Occurrences=2,
        )
        multi = FakePattern(
            "PartDesign::MultiTransform",
            "Multi",
            Transformations=[row],
            Originals=[self.hole],
        )
        copies, _ = copies_by_feature([self.hole, multi, row], self.resolver)
        self.assertEqual(
            _places(copies["TappedHole"]), [(10.0, 0.0, 0.0), (20.0, 0.0, 0.0)]
        )

    def test_repeating_the_whole_shape_drops_the_holes_it_would_have_copied(self):
        # A whole-shape transform names nothing, so the holes it affects are
        # everything cut before it. All of them lose their declaration
        # rather than half of them keeping one.
        whole = FakePattern(
            "PartDesign::Mirrored",
            "Halves",
            TransformMode="Whole shape",
            MirrorPlane="x=0",
            BaseFeature=self.hole,
        )
        with mock.patch("freecad.DFM.core.machining.thread_sources._warn"):
            copies, refused = copies_by_feature([self.hole, whole], self.resolver)
        self.assertEqual(refused, {"TappedHole"})
        self.assertEqual(copies, {})

    def test_a_scaled_hole_is_dropped_rather_than_declared(self):
        scaled = FakePattern(
            "PartDesign::Scaled",
            "Scaled",
            Factor=1.5,
            Occurrences=2,
            Originals=[self.hole],
        )
        with mock.patch("freecad.DFM.core.machining.thread_sources._warn"):
            _, refused = copies_by_feature([self.hole, scaled], self.resolver)
        self.assertEqual(refused, {"TappedHole"})

    def test_a_pattern_of_a_pattern_is_refused(self):
        # Stacking transforms is what a MultiTransform is for. Reached any
        # other way it is a shape this cannot account for.
        inner = FakePattern(
            "PartDesign::LinearPattern",
            "Inner",
            Direction="x",
            Length=10.0,
            Occurrences=2,
            Originals=[self.hole],
        )
        outer = FakePattern(
            "PartDesign::PolarPattern",
            "Outer",
            Axis="z",
            Angle=360.0,
            Occurrences=3,
            Originals=[inner],
        )
        with mock.patch("freecad.DFM.core.machining.thread_sources._warn"):
            _, refused = copies_by_feature(
                [self.hole, inner, outer], self.resolver
            )
        self.assertIn("Inner", refused)

    def test_an_absurd_number_of_copies_is_refused(self):
        # Past a few thousand the model is wrong rather than the reading,
        # and every position gets scanned against every bore on the part.
        with mock.patch("freecad.DFM.core.machining.thread_sources._warn"):
            copies, refused = copies_by_feature(
                [self.hole, self._row(Occurrences=9000)], self.resolver
            )
        self.assertEqual(refused, {"TappedHole"})
        self.assertEqual(copies, {})


class TestADeclarationReachesEveryCopy(unittest.TestCase):
    """The point of it all: one statement, every hole it was made about.

    Composing the copies with the document's own placements is the one part
    that needs a live FreeCAD, and it is proved in
    ``tests/freecad/verify_in_freecad.py``. What that composition is handed
    is checked here.
    """

    def test_a_bolt_circle_of_tapped_holes_is_declared_all_the_way_round(self):
        hole = FakeHole(Name="TappedHole")
        circle = FakePattern(
            "PartDesign::PolarPattern",
            "BoltCircle",
            Axis="z",
            Angle=360.0,
            Occurrences=12,
            Originals=[hole],
        )
        copies, refused = copies_by_feature([hole, circle], FakeResolver())
        self.assertEqual(refused, set())

        # What the frames seam hands over: every copy of the one profile
        # circle, all on the same drilling axis, under the one declaration.
        drilled = (25.0, 0.0, 8.0)
        positions = [t.apply_point(drilled) for t in copies["TappedHole"]]
        found = native_declarations(
            FakeTarget(out_list=[hole]),
            frames=lambda _t: [(hole, positions, _BORE_AXIS)],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].designation, "M6x1.0")
        self.assertEqual(len(found[0].positions), 12)
        for position in positions:
            self.assertTrue(
                found[0].covers(5.0, position, _BORE_AXIS),
                f"the hole at {position} was left undeclared",
            )

    def test_a_hole_beside_the_circle_is_still_not_declared(self):
        # Expanding the copies must not turn the declaration into a blanket
        # over the whole part.
        hole = FakeHole(Name="TappedHole")
        circle = FakePattern(
            "PartDesign::PolarPattern",
            "BoltCircle",
            Axis="z",
            Angle=360.0,
            Occurrences=12,
            Originals=[hole],
        )
        copies, _ = copies_by_feature([hole, circle], FakeResolver())
        positions = [t.apply_point((25.0, 0.0, 8.0)) for t in copies["TappedHole"]]
        found = native_declarations(
            FakeTarget(out_list=[hole]),
            frames=lambda _t: [(hole, positions, _BORE_AXIS)],
        )
        self.assertFalse(found[0].covers(5.0, (0.0, 0.0, 8.0), _BORE_AXIS))
        self.assertFalse(found[0].covers(5.0, (12.0, 0.0, 8.0), _BORE_AXIS))


class TestApplyingAFact(unittest.TestCase):
    """What a stated thread writes onto the feature."""

    def _feature(self, **parameters):
        params = {"diameter_mm": 5.0, "depth_mm": 12.0}
        params.update(parameters)
        return FeatureInstance("h_0", FeatureType.BLIND_HOLE, [1], params)

    def test_the_bore_becomes_a_tapped_hole(self):
        feature = self._feature()
        _m6_declaration().as_fact().apply_to(feature, hole_depth_mm=12.0)
        self.assertEqual(feature.type, FeatureType.THREADED_HOLE)
        self.assertEqual(feature.param("thread_designation"), "M6x1.0")
        self.assertEqual(feature.param("thread_evidence"), NATIVE_DECLARATION)

    def test_an_unknown_pitch_is_left_unstated_rather_than_zeroed(self):
        # A pitch of nothing is not the same as an unknown pitch, and the
        # run-out rule reads the difference.
        feature = self._feature()
        ThreadFact("1/4 UNF", 6.35, pitch_mm=None).apply_to(feature)
        self.assertFalse(feature.has("thread_pitch_mm"))

    def test_a_stated_tapped_length_is_recorded(self):
        feature = self._feature()
        _m6_declaration(depth_mm=8.0).as_fact().apply_to(feature, hole_depth_mm=12.0)
        self.assertAlmostEqual(feature.number("thread_depth_mm"), 8.0)

    def test_a_tapped_length_past_the_bottom_of_the_bore_is_ignored(self):
        # A stale property left behind by an edit. Believing it would have
        # the thread running out into solid metal.
        feature = self._feature()
        _m6_declaration(depth_mm=40.0).as_fact().apply_to(feature, hole_depth_mm=12.0)
        self.assertFalse(feature.has("thread_depth_mm"))

    def test_a_left_hand_thread_is_called_out(self):
        feature = self._feature()
        _m6_declaration(hand="left").as_fact().apply_to(feature)
        self.assertEqual(feature.param("thread_hand"), "left")
        plain = self._feature()
        _m6_declaration().as_fact().apply_to(plain)
        self.assertFalse(plain.has("thread_hand"))


class TestTheHolePassReadsTheEvidence(unittest.TestCase):
    """The recognizer's side: a stated thread promotes a real bore."""

    def test_a_plain_bore_stays_plain_with_no_evidence(self):
        # The whole reason this work exists. 5 mm is the M6 tap drill and
        # most 5 mm bores are not tapped.
        found = _holes_with(blind_tap_drill_hole(), ThreadEvidence())
        self.assertEqual(
            [feature.type for feature in found], [FeatureType.BLIND_HOLE]
        )

    def test_a_declared_bore_is_promoted(self):
        evidence = ThreadEvidence(
            object_name="Part", declarations=(_m6_declaration(),)
        )
        found = _holes_with(blind_tap_drill_hole(), evidence)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].type, FeatureType.THREADED_HOLE)
        self.assertEqual(found[0].param("thread_designation"), "M6x1.0")
        self.assertEqual(found[0].param("thread_evidence"), NATIVE_DECLARATION)
        self.assertEqual(found[0].param("thread_declared_by"), "Hole")

    def test_a_declaration_about_another_hole_promotes_nothing(self):
        evidence = ThreadEvidence(
            object_name="Part",
            declarations=(_m6_declaration(positions=((5.0, 5.0, 8.0),)),),
        )
        found = _holes_with(blind_tap_drill_hole(), evidence)
        self.assertEqual(found[0].type, FeatureType.BLIND_HOLE)

    def test_a_modelled_helix_still_speaks_for_itself(self):
        # The oldest source has to keep working on a part with no document
        # behind it at all.
        from tests.test_machining_threads import tapped_blind_hole

        found = _holes_with(tapped_blind_hole(), ThreadEvidence())
        threaded = [f for f in found if f.type == FeatureType.THREADED_HOLE]
        self.assertEqual(len(threaded), 1)
        self.assertEqual(threaded[0].param("thread_evidence"), MODELLED_HELIX)


class TestOfferingCandidates(unittest.TestCase):
    """Which bores are worth putting to the user, and which are settled."""

    def _candidates(self, shape, evidence=None):
        context = _context(shape)
        return candidates_for(
            context.recognition.features, context.graph, evidence or ThreadEvidence()
        )

    def test_a_tap_drill_sized_bore_is_offered(self):
        offered = self._candidates(blind_tap_drill_hole())
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0].designation, "M6x1.0")
        self.assertAlmostEqual(offered[0].diameter_mm, 5.0, places=3)

    def test_a_bore_at_no_standard_size_is_not_offered(self):
        # 7.5 mm sits between the M8 and M10 tap drills. There is no question
        # to ask about it.
        self.assertEqual(self._candidates(off_size_hole()), [])

    def test_a_bore_already_answered_is_not_asked_about_again(self):
        key = bore_key("Part", 5.0, _BORE_ORIGIN, _BORE_AXIS)
        for verdict in (True, False):
            evidence = ThreadEvidence(
                object_name="Part",
                confirmations=ConfirmationStore([Confirmation(key, verdict)]),
            )
            self.assertEqual(
                self._candidates(blind_tap_drill_hole(), evidence),
                [],
                f"a bore answered {verdict} was offered again",
            )

    def test_a_declared_bore_is_not_put_to_a_vote(self):
        evidence = ThreadEvidence(
            object_name="Part", declarations=(_m6_declaration(),)
        )
        self.assertEqual(self._candidates(blind_tap_drill_hole(), evidence), [])

    def test_a_candidate_reads_as_a_machinist_would_say_it(self):
        offered = self._candidates(blind_tap_drill_hole())[0]
        self.assertIn("5.00 mm blind bore", offered.describe())
        self.assertIn("12.0 mm deep", offered.describe())

    def test_the_answers_are_filed_and_counted(self):
        offered = self._candidates(blind_tap_drill_hole())
        store = ConfirmationStore()
        confirmed = record_answers(
            store, offered, {offered[0].key.encode(): True}
        )
        self.assertEqual(confirmed, 1)
        self.assertTrue(store.verdict_for(offered[0].key).accepted)

    def test_an_unanswered_row_files_nothing(self):
        # A dialog somebody skimmed is not a shop decision.
        offered = self._candidates(blind_tap_drill_hole())
        store = ConfirmationStore()
        self.assertEqual(record_answers(store, offered, {}), 0)
        self.assertEqual(len(store), 0)

    def test_a_rejection_is_filed_but_not_counted(self):
        # Nothing to re-analyse for: the workbench was never going to assert
        # that thread anyway.
        offered = self._candidates(blind_tap_drill_hole())
        store = ConfirmationStore()
        self.assertEqual(
            record_answers(store, offered, {offered[0].key.encode(): False}), 0
        )
        self.assertEqual(len(store), 1)


class TestTheWholeWayThrough(unittest.TestCase):
    """The analyzer, a document that remembers, and the rules downstream."""

    def test_a_confirmed_bore_comes_back_tapped_on_the_next_run(self):
        shape = blind_tap_drill_hole()
        context = _context(shape)
        # The evidence the question was asked with carries the object's name,
        # and so does the key the answer is filed under: an answer given about
        # one part's hole is not an answer about another part's.
        offered = candidates_for(
            context.recognition.features,
            context.graph,
            ThreadEvidence(object_name="Part"),
        )
        self.assertEqual(len(offered), 1)

        # What the dialog does: file the answer and write it to the document.
        store = ConfirmationStore()
        record_answers(store, offered, {offered[0].key.encode(): True})
        document = FakeDocument([FakeStore(store.encode())])

        again = _context(shape, target=FakeTarget("Part", document))
        threaded = again.recognition.of_type(FeatureType.THREADED_HOLE)
        self.assertEqual(len(threaded), 1)
        self.assertEqual(threaded[0].param("thread_designation"), "M6x1.0")
        self.assertEqual(threaded[0].param("thread_evidence"), USER_CONFIRMED)

    def test_the_analyzer_asks_for_nothing_it_was_not_given(self):
        # Every headless caller passes a shape and no object. That has to go
        # on working, with the helix search as the only source.
        context = _context(blind_tap_drill_hole())
        self.assertEqual(context.recognition.of_type(FeatureType.THREADED_HOLE), [])


if __name__ == "__main__":
    unittest.main()
