# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the sheet-metal recognizers: bends, outline features and forming.

Every fixture here is a folded bracket, because nothing else classifies as
sheet metal. The classifier wants a developable uniform-gauge shell carrying
at least one concentric cylinder pair one gauge apart between two flats at an
angle, and a bracket is the smallest honest shape that has one. Each test
asserts its own fixture still classifies SHEET_METAL before asking anything
else, since all three recognizers stand down on any other verdict and a
silently mis-classified fixture would pass by returning nothing.

The brackets are built as a prism of a folded profile rather than by a
Boolean, so the bend arrives as a genuine pair of coaxial cylinders instead of
a fillet the kernel happened to produce.
"""

import math
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeSphere,
)
from OCP.GC import GC_MakeArcOfCircle
from OCP.Geom import Geom_BezierCurve, Geom_OffsetCurve
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.analyzers.machining_analyzer import CONTEXT_KEY, MachiningAnalyzer
from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.config import RuleThresholds
from freecad.DFM.core.machining.process_classifier import (
    PartProcessType,
    classify_part_process,
)
from freecad.DFM.core.machining.recognizers.bend_recognizer import (
    BEND,
    BendRecognizer,
)
from freecad.DFM.core.machining.recognizers.sheet_formed_recognizer import (
    SHEET_FORMED,
    SheetFormedRecognizer,
)
from freecad.DFM.core.machining.recognizers.sheet_outline_recognizer import (
    NOTCH,
    TAB,
    SheetOutlineRecognizer,
)
from freecad.DFM.core.utils.geometry import EdgeIndex, FaceIndex


# =============================================================================
# Fixtures
# =============================================================================

GAUGE = 2.0
INNER_RADIUS = 3.0


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    return op.Shape()


def _common(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Common(a, b)
    op.Build()
    return op.Shape()


def _box(p0, p1) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def _profile_point(x: float, z: float) -> gp_Pnt:
    """A point on the folded section, drawn in the XZ plane."""
    return gp_Pnt(x, 0.0, z)


def _folded_prism(segments, width: float) -> TopoDS_Shape:
    """Prism the folded section along Y.

    Building the brackets this way rather than by a Boolean is what makes the
    bend a genuine pair of coaxial cylinders instead of a fillet the kernel
    happened to produce. Segments are ("line", start, end) or
    ("arc", start, middle, end).
    """
    wire = BRepBuilderAPI_MakeWire()
    for segment in segments:
        if segment[0] == "line":
            wire.Add(BRepBuilderAPI_MakeEdge(segment[1], segment[2]).Edge())
        else:
            curve = GC_MakeArcOfCircle(segment[1], segment[2], segment[3]).Value()
            wire.Add(BRepBuilderAPI_MakeEdge(curve).Edge())
    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, width, 0.0)).Shape()


def _fold_midpoint(centre_x: float, centre_z: float, radius: float) -> gp_Pnt:
    """The point on a quarter fold arc halfway round, for the arc builder."""
    step = math.sqrt(0.5) * radius
    return _profile_point(centre_x - step, centre_z - step)


def bracket(
    gauge: float = GAUGE,
    inner_radius: float = INNER_RADIUS,
    leg_x: float = 60.0,
    leg_z: float = 40.0,
    width: float = 50.0,
) -> TopoDS_Shape:
    """A right-angle folded bracket, gauge thick, one bend.

    The smallest honest sheet part: two flats and the concentric cylinder pair
    one gauge apart that joins them, which is exactly the signature the
    classifier and the bend recognizer both key on.
    """
    outer_radius = inner_radius + gauge
    tangent = _profile_point(outer_radius, 0.0)
    segments = [
        ("line", tangent, _profile_point(leg_x, 0.0)),
        ("line", _profile_point(leg_x, 0.0), _profile_point(leg_x, gauge)),
        ("line", _profile_point(leg_x, gauge), _profile_point(outer_radius, gauge)),
        (
            "arc",
            _profile_point(outer_radius, gauge),
            _fold_midpoint(outer_radius, outer_radius, inner_radius),
            _profile_point(gauge, outer_radius),
        ),
        ("line", _profile_point(gauge, outer_radius), _profile_point(gauge, leg_z)),
        ("line", _profile_point(gauge, leg_z), _profile_point(0.0, leg_z)),
        ("line", _profile_point(0.0, leg_z), _profile_point(0.0, outer_radius)),
        (
            "arc",
            _profile_point(0.0, outer_radius),
            _fold_midpoint(outer_radius, outer_radius, outer_radius),
            tangent,
        ),
    ]
    return _folded_prism(segments, width)


def hemmed_bracket(
    gauge: float = GAUGE,
    inner_radius: float = INNER_RADIUS,
    hem_radius: float = 1.0,
    leg_x: float = 60.0,
    leg_z: float = 40.0,
    return_z: float = 20.0,
    width: float = 50.0,
) -> TopoDS_Shape:
    """The same bracket with the top of its upright folded back on itself.

    Two bends on one part, and they are different animals: the corner opens to
    ninety degrees, while the hem closes right back to a hundred and eighty
    with its return leg lying against the upright. Both are the same coaxial
    pair one gauge apart, so nothing but the panel angle tells them apart.
    """
    outer_radius = inner_radius + gauge
    hem_outer = hem_radius + gauge
    centre_x = gauge + hem_radius
    tangent = _profile_point(outer_radius, 0.0)
    segments = [
        ("line", tangent, _profile_point(leg_x, 0.0)),
        ("line", _profile_point(leg_x, 0.0), _profile_point(leg_x, gauge)),
        ("line", _profile_point(leg_x, gauge), _profile_point(outer_radius, gauge)),
        (
            "arc",
            _profile_point(outer_radius, gauge),
            _fold_midpoint(outer_radius, outer_radius, inner_radius),
            _profile_point(gauge, outer_radius),
        ),
        ("line", _profile_point(gauge, outer_radius), _profile_point(gauge, leg_z)),
        (
            "arc",
            _profile_point(gauge, leg_z),
            _profile_point(centre_x, leg_z + hem_radius),
            _profile_point(centre_x + hem_radius, leg_z),
        ),
        (
            "line",
            _profile_point(centre_x + hem_radius, leg_z),
            _profile_point(centre_x + hem_radius, return_z),
        ),
        (
            "line",
            _profile_point(centre_x + hem_radius, return_z),
            _profile_point(centre_x + hem_outer, return_z),
        ),
        (
            "line",
            _profile_point(centre_x + hem_outer, return_z),
            _profile_point(centre_x + hem_outer, leg_z),
        ),
        (
            "arc",
            _profile_point(centre_x + hem_outer, leg_z),
            _profile_point(centre_x, leg_z + hem_outer),
            _profile_point(0.0, leg_z),
        ),
        ("line", _profile_point(0.0, leg_z), _profile_point(0.0, outer_radius)),
        (
            "arc",
            _profile_point(0.0, outer_radius),
            _fold_midpoint(outer_radius, outer_radius, outer_radius),
            tangent,
        ),
    ]
    return _folded_prism(segments, width)


def notched_bracket() -> TopoDS_Shape:
    """A bracket with one rectangular bite out of the free edge of its base.

    Twelve deep and eight across, so the bottom of the bite is shorter than
    either of its sides -- which is what a notch bottom always is, and what
    the connector gate demands.
    """
    return _cut(bracket(), _box((48, 20, -1), (61, 28, 3)))


def tabbed_bracket() -> TopoDS_Shape:
    """A bracket whose base ends in a narrow tab between two wide cuts.

    A comb, deliberately: the tab is six across and the cuts either side are
    twelve, so the tab's two walls belong to three valid pairings at once. The
    tightest one is the true one.
    """
    shape = _cut(bracket(), _box((44, 10, -1), (61, 22, 3)))
    return _cut(shape, _box((44, 28, -1), (61, 40, 3)))


def embossed_bracket() -> TopoDS_Shape:
    """A bracket with a square emboss drawn out of the back of its base.

    Built the way the press makes it: the plateau is pushed clear of the panel
    and the metal behind it follows, so the crest carries a back-side skin
    exactly one gauge behind and the recess is open to the inside of the sheet.
    """
    raised = _fuse(bracket(), _box((25, 17, -6), (41, 33, 0)))
    return _cut(raised, _box((27, 19, -4), (39, 31, 3)))


def dimpled_bracket(radius: float = 8.0) -> TopoDS_Shape:
    """A bracket with a domed dimple drawn out of the back of its base.

    The same forming, spherical: an outer cap of radius r and an inner cap of
    radius r minus the gauge, sharing a centre that sits on the panel.
    """
    centre = gp_Pnt(30.0, 25.0, 0.0)
    axis = gp_Ax2(centre, gp_Dir(0, 0, 1))
    low = (30 - radius - 1, 25 - radius - 1, -radius - 1)
    high = (30 + radius + 1, 25 + radius + 1)
    dome = _common(
        BRepPrimAPI_MakeSphere(axis, radius).Shape(),
        _box(low, high + (0.0,)),
    )
    recess = _common(
        BRepPrimAPI_MakeSphere(axis, radius - GAUGE).Shape(),
        _box(low, high + (GAUGE,)),
    )
    return _cut(_fuse(bracket(), dome), recess)


def lanced_bracket() -> TopoDS_Shape:
    """A bracket with a formed hood sheared open at both ends: a bridge lance.

    The punch shears the metal across two lines and forms the strip between
    them into a hood, so the hood's two ends are gauge-thin cut faces standing
    perpendicular to the crest. That is what makes it a lance rather than a
    closed emboss, and it is the open-edge count that says so.
    """
    raised = _fuse(bracket(), _box((10, 17, 2), (50, 33, 6)))
    return _cut(raised, _box((12, 15, -1), (48, 35, 4)))


def louvered_bracket() -> TopoDS_Shape:
    """A bracket with a formed hood sheared open along one side only.

    The far side of the hood stays attached to the panel and carries the air
    over it; the near side is cut free. One sheared edge is what makes it a
    louver rather than a closed emboss or a bridged lance.
    """
    raised = _fuse(bracket(), _box((10, 17, 2), (50, 33, 6)))
    return _cut(raised, _box((12, 15, -1), (48, 31, 4)))


def swaged_bracket(width: float = 16.0) -> TopoDS_Shape:
    """A bracket with a swage bridged over an opening in its base.

    The hood has no analytic surface at all: its crest is a spline swept along
    the run and its back skin is that spline's exact offset, so neither
    carries a radius, an axis or a centre for the analytic passes to compare.
    Only the sampled point-offset test can pair them, which is what this
    fixture is here to exercise.
    """
    poles = ((10.0, 2.0), (18.0, 2.0), (30.0, 14.0), (42.0, 2.0), (50.0, 2.0))
    array = TColgp_Array1OfPnt(1, len(poles))
    for index, (x, z) in enumerate(poles, start=1):
        array.SetValue(index, gp_Pnt(x, 17.0, z))
    crest = Geom_BezierCurve(array)
    # The back skin is the crest's true offset, not the crest moved down: a
    # constant-gauge shell is what drawn metal makes, and a vertical shift
    # would put the two skins further apart wherever the hood is steep.
    skin = Geom_OffsetCurve(crest, -GAUGE, gp_Dir(0, 1, 0))

    wire = BRepBuilderAPI_MakeWire()
    wire.Add(BRepBuilderAPI_MakeEdge(crest).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(crest.Value(1.0), skin.Value(1.0)).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(skin).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(skin.Value(0.0), crest.Value(0.0)).Edge())
    section = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    hood = BRepPrimAPI_MakePrism(section, gp_Vec(0.0, width, 0.0)).Shape()

    opened = _cut(bracket(), _box((10, 15, -1), (50, 35, 3)))
    return _fuse(opened, hood)


def milled_block() -> TopoDS_Shape:
    """A plain block. Nothing here is sheet, so nothing here is a bend."""
    return BRepPrimAPI_MakeBox(80.0, 50.0, 30.0).Shape()


# =============================================================================
# Harness
# =============================================================================


def recognize(shape: TopoDS_Shape):
    """Run the three sheet passes in pipeline order, as the analyzer would."""
    graph = AagBuilder(shape, FaceIndex(shape)).build()
    process = classify_part_process(graph, RuleThresholds(), shape)

    found = []
    for recognizer_class in (
        BendRecognizer,
        SheetOutlineRecognizer,
        SheetFormedRecognizer,
    ):
        recognizer = recognizer_class()
        recognizer.part_process = process
        found.extend(recognizer.recognize(graph, shape, set(), list(found)))
    return process, graph, found


def of_type(features, wanted):
    return [feature for feature in features if feature.type == wanted]


# =============================================================================


class SheetFixtureTests(unittest.TestCase):
    """The fixtures have to be sheet before anything else means anything."""

    def test_bracket_classifies_as_sheet_metal(self):
        shape = bracket()
        data = MachiningAnalyzer().execute(
            shape, FaceIndex(shape), EdgeIndex(shape), prefs={}
        )
        context = data[CONTEXT_KEY]
        self.assertIs(context.part_process.type, PartProcessType.SHEET_METAL)
        self.assertAlmostEqual(context.part_process.sheet_thickness_mm, GAUGE, places=3)

    def test_every_fixture_is_sheet_metal(self):
        for name, shape in (
            ("bracket", bracket()),
            ("notched", notched_bracket()),
            ("tabbed", tabbed_bracket()),
            ("embossed", embossed_bracket()),
            ("dimpled", dimpled_bracket()),
            ("hemmed", hemmed_bracket()),
            ("louvered", louvered_bracket()),
            ("lanced", lanced_bracket()),
            ("swaged", swaged_bracket()),
        ):
            with self.subTest(fixture=name):
                process, _, _ = recognize(shape)
                self.assertIs(process.type, PartProcessType.SHEET_METAL)
                self.assertAlmostEqual(process.sheet_thickness_mm, GAUGE, places=3)


class BendTests(unittest.TestCase):
    def test_bracket_has_one_right_angle_bend(self):
        _, _, features = recognize(bracket())
        bends = of_type(features, BEND)
        self.assertEqual(len(bends), 1)

        bend = bends[0]
        self.assertAlmostEqual(bend.number("inner_radius_mm"), INNER_RADIUS, places=3)
        self.assertAlmostEqual(
            bend.number("outer_radius_mm"), INNER_RADIUS + GAUGE, places=3
        )
        self.assertAlmostEqual(bend.number("thickness_mm"), GAUGE, places=3)
        self.assertAlmostEqual(bend.number("angle_deg"), 90.0, places=3)
        self.assertAlmostEqual(bend.number("length_mm"), 50.0, places=3)
        self.assertFalse(bend.param("is_hem"))

    def test_bend_owns_both_skins_of_the_fold(self):
        _, graph, features = recognize(bracket())
        bend = of_type(features, BEND)[0]
        self.assertEqual(len(bend.faces), 2)
        radii = sorted(graph.node(face_id).cyl_radius for face_id in bend.faces)
        self.assertAlmostEqual(radii[0], INNER_RADIUS, places=3)
        self.assertAlmostEqual(radii[1], INNER_RADIUS + GAUGE, places=3)

    def test_bend_names_the_two_panels_it_joins(self):
        _, graph, features = recognize(bracket())
        bend = of_type(features, BEND)[0]
        panels = (bend.param("panel_a"), bend.param("panel_b"))
        for face_id in panels:
            self.assertTrue(graph.has_node(face_id))
        first = graph.node(panels[0]).outward_normal
        second = graph.node(panels[1]).outward_normal
        self.assertAlmostEqual(abs(first.Dot(second)), 0.0, places=6)

    def test_a_hem_reads_as_a_bend_folded_right_back(self):
        """Two bends, and the angle is the only thing that separates them.

        Both are the same coaxial pair one gauge apart. The corner opens to
        ninety degrees; the hem closes to a hundred and eighty, which is what
        the brake operator sets a different tool up for.
        """
        _, _, features = recognize(hemmed_bracket())
        bends = sorted(of_type(features, BEND), key=lambda f: f.number("angle_deg"))
        self.assertEqual(len(bends), 2)

        corner, hem = bends
        self.assertAlmostEqual(corner.number("angle_deg"), 90.0, places=3)
        self.assertFalse(corner.param("is_hem"))
        self.assertAlmostEqual(hem.number("angle_deg"), 180.0, places=3)
        self.assertTrue(hem.param("is_hem"))
        self.assertAlmostEqual(hem.number("inner_radius_mm"), 1.0, places=3)
        self.assertAlmostEqual(hem.number("thickness_mm"), GAUGE, places=3)

    def test_the_two_bends_do_not_share_a_face(self):
        """Each fold claims its own pair, even where they share a panel."""
        _, _, features = recognize(hemmed_bracket())
        bends = of_type(features, BEND)
        self.assertEqual(len(bends), 2)
        self.assertEqual(set(bends[0].faces) & set(bends[1].faces), set())

    def test_a_hem_is_not_read_as_a_formed_hood(self):
        """The return lip passes every plateau gate; the fold beside it does not.

        A hem's return leg has a skin one gauge behind it, stands proud of the
        upright and reaches back down to it -- everything a formed hood has.
        What says it is a fold is the bend cylinders in its own neighbourhood.
        """
        _, _, features = recognize(hemmed_bracket())
        self.assertEqual(of_type(features, SHEET_FORMED), [])
        self.assertEqual(of_type(features, TAB), [])
        self.assertEqual(of_type(features, NOTCH), [])

    def test_a_milled_block_has_no_bends(self):
        _, _, features = recognize(milled_block())
        self.assertEqual(of_type(features, BEND), [])

    def test_the_recognizer_stands_down_without_a_classification(self):
        shape = bracket()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        self.assertEqual(BendRecognizer().recognize(graph, shape), [])


class OutlineTests(unittest.TestCase):
    def test_a_plain_bracket_has_no_tabs_or_notches(self):
        _, _, features = recognize(bracket())
        self.assertEqual(of_type(features, TAB), [])
        self.assertEqual(of_type(features, NOTCH), [])

    def test_a_bite_out_of_the_outline_is_a_notch(self):
        _, _, features = recognize(notched_bracket())
        self.assertEqual(of_type(features, TAB), [])
        notches = of_type(features, NOTCH)
        self.assertEqual(len(notches), 1)

        notch = notches[0]
        self.assertAlmostEqual(notch.number("width_mm"), 8.0, places=3)
        self.assertAlmostEqual(notch.number("length_mm"), 12.0, places=3)
        self.assertAlmostEqual(notch.number("aspect"), 1.5, places=3)
        self.assertEqual(len(notch.faces), 3)  # two sides and the bottom

    def test_a_peninsula_is_a_tab_and_beats_the_cuts_beside_it(self):
        """The comb case: the tab is the tightest pairing, so it wins.

        Its two walls are also each a wall of the wide cut beside them. If the
        cuts claimed them first the part would report two notches and no tab,
        which is the wrong reading of a bent-up mounting ear.
        """
        _, _, features = recognize(tabbed_bracket())
        tabs = of_type(features, TAB)
        self.assertEqual(len(tabs), 1)
        self.assertEqual(of_type(features, NOTCH), [])

        tab = tabs[0]
        self.assertAlmostEqual(tab.number("width_mm"), 6.0, places=3)
        self.assertAlmostEqual(tab.number("length_mm"), 16.0, places=3)

    def test_the_recognizer_stands_down_on_a_milled_part(self):
        _, _, features = recognize(milled_block())
        self.assertEqual(of_type(features, TAB), [])
        self.assertEqual(of_type(features, NOTCH), [])


class FormedTests(unittest.TestCase):
    def test_a_drawn_plateau_is_an_emboss(self):
        _, _, features = recognize(embossed_bracket())
        formed = of_type(features, SHEET_FORMED)
        self.assertEqual(len(formed), 1)

        emboss = formed[0]
        self.assertEqual(emboss.param("subtype"), "emboss")
        self.assertEqual(emboss.param("open_edges"), 0)
        self.assertAlmostEqual(emboss.number("height_mm"), 6.0, places=3)
        self.assertAlmostEqual(emboss.number("width_mm"), 16.0, places=3)
        self.assertAlmostEqual(emboss.number("length_mm"), 16.0, places=3)

    def test_the_emboss_owns_its_back_side_skin(self):
        """The recess behind a formed feature is part of it, not a pocket."""
        _, graph, features = recognize(embossed_bracket())
        emboss = of_type(features, SHEET_FORMED)[0]
        crest = graph.node(emboss.faces[0])
        skin = graph.node(emboss.faces[1])
        offset = gp_Vec(crest.centroid, skin.centroid).Dot(
            gp_Vec(crest.outward_normal)
        )
        self.assertAlmostEqual(abs(offset), GAUGE, places=3)

    def test_a_domed_dimple_is_recognized_from_its_spherical_crest(self):
        _, _, features = recognize(dimpled_bracket())
        formed = of_type(features, SHEET_FORMED)
        self.assertEqual(len(formed), 1)
        self.assertEqual(formed[0].param("subtype"), "emboss")
        self.assertAlmostEqual(formed[0].number("height_mm"), 8.0, places=3)

    def test_a_hood_sheared_down_one_side_is_a_louver(self):
        _, _, features = recognize(louvered_bracket())
        formed = of_type(features, SHEET_FORMED)
        self.assertEqual(len(formed), 1)
        self.assertEqual(formed[0].param("subtype"), "louver")
        self.assertEqual(formed[0].param("open_edges"), 1)
        self.assertAlmostEqual(formed[0].number("height_mm"), 4.0, places=3)

    def test_a_hood_sheared_at_both_ends_is_a_lance(self):
        _, _, features = recognize(lanced_bracket())
        formed = of_type(features, SHEET_FORMED)
        self.assertEqual(len(formed), 1)
        self.assertEqual(formed[0].param("subtype"), "lance")
        self.assertEqual(formed[0].param("open_edges"), 2)

    def test_a_spline_hood_is_paired_by_the_sampled_offset_test(self):
        """The freeform pass: no radius, no axis, no centre to compare.

        The crest is a spline and its back skin is that spline's offset, so
        the pair can only be established by marching a gauge inward from
        probes spread over the crest and asking what the landing point is on.
        """
        _, graph, features = recognize(swaged_bracket())
        formed = of_type(features, SHEET_FORMED)
        self.assertEqual(len(formed), 1)

        hood = formed[0]
        self.assertEqual(hood.param("subtype"), "lance")
        self.assertAlmostEqual(hood.number("height_mm"), 4.5, places=3)
        crest = graph.node(hood.faces[0])
        skin = graph.node(hood.faces[1])
        self.assertTrue(crest.surface_type.is_freeform)
        self.assertTrue(skin.surface_type.is_freeform)

    def test_the_spline_hood_seeds_from_its_outer_skin_only(self):
        """The sampled test is symmetric; the crest facing the sky is not.

        March a gauge in from either skin and you land on the other, so
        without the facing test the same hood would report twice -- once
        outward and once into its own air path.
        """
        _, _, features = recognize(swaged_bracket())
        self.assertEqual(len(of_type(features, SHEET_FORMED)), 1)

    def test_the_complementary_recess_does_not_seed_a_second_feature(self):
        """Each formed feature protrudes on exactly one side of the sheet.

        Seen from behind, the emboss is a recess whose ceiling has the same
        gauge-thick skin partner. Only the outward-offset plateau seeds, so
        the feature is reported once rather than once per side.
        """
        _, _, features = recognize(embossed_bracket())
        self.assertEqual(len(of_type(features, SHEET_FORMED)), 1)

    def test_a_plain_bracket_has_nothing_formed(self):
        _, _, features = recognize(bracket())
        self.assertEqual(of_type(features, SHEET_FORMED), [])

    def test_the_recognizer_stands_down_on_a_milled_part(self):
        _, _, features = recognize(milled_block())
        self.assertEqual(of_type(features, SHEET_FORMED), [])


if __name__ == "__main__":
    unittest.main()
