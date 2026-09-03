# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for part-marking recognition.

Two failures matter here and they pull in opposite directions. Miss the text
and the face-level rules bury the report in findings about character strokes.
Claim too much and a real pocket disappears from the analysis, which is worse.

So every positive case here is paired with the functional geometry it most
resembles: engraved strokes against a shallow pocket, a relieved plaque
against the same plaque carrying a structural boss.
"""

import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Shape

from freecad.DFM.core.machining import AagBuilder
from freecad.DFM.core.machining.features import FeatureType
from freecad.DFM.core.machining.recognizers import MarkingRecognizer
from freecad.DFM.core.utils.geometry import FaceIndex


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
    """The 100 x 80 x 40 block everything is marked on. Top face is z = 40."""
    return BRepPrimAPI_MakeBox(100.0, 80.0, 40.0).Shape()


def markings_in(shape: TopoDS_Shape, claimed=None):
    graph = AagBuilder(shape, FaceIndex(shape)).build()
    return MarkingRecognizer().recognize(graph, shape, claimed=claimed)


# -- pass 1: separate characters ---------------------------------------------


def make_engraved_strokes(count: int = 3, width: float = 1.5, depth: float = 0.5):
    """`count` separate strokes cut into the top face, 8 mm long.

    The simplest thing that reads as text: several small disconnected recesses
    at stroke scale, all the same depth, in a line.
    """
    shape = block()
    for index in range(count):
        x = 10.0 + index * 10.0
        shape = _cut(shape, _box((x, 40.0, 40.0 - depth), (x + width, 48.0, 41.0)))
    return shape


def make_raised_strokes():
    """The same three strokes, standing proud of the face instead of cut in."""
    shape = block()
    for index in range(3):
        x = 10.0 + index * 10.0
        shape = _fuse(shape, _box((x, 40.0, 40.0), (x + 1.5, 48.0, 40.5)))
    return shape


def make_scattered_strokes():
    """Three strokes flung to the far corners of the face.

    Each one qualifies as a glyph; together they are not a text block, because
    a legend is a compact strip and this is not.
    """
    shape = block()
    for x, y in ((5.0, 5.0), (90.0, 5.0), (90.0, 70.0)):
        shape = _cut(shape, _box((x, y, 39.5), (x + 1.5, y + 8.0, 41.0)))
    return shape


# -- pass 2: connected logotypes ---------------------------------------------


def make_logotype():
    """One connected graphic: an H of 2 mm bars with a round dot over each leg.

    Cut 1 mm deep, so it is inside the marking slab, but it arrives as a single
    component far too wide to be one character -- which is what forces it down
    the logotype path.
    """
    tool = _box((30, 30, 39), (32, 44, 41))
    tool = _fuse(tool, _box((40, 30, 39), (42, 44, 41)))
    tool = _fuse(tool, _box((30, 36, 39), (42, 38, 41)))
    for x in (31.0, 41.0):
        tool = _fuse(
            tool,
            BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(x, 45, 39), gp_Dir(0, 0, 1)), 1.5, 2.0
            ).Shape(),
        )
    return _cut(block(), tool)


def make_shallow_round_pocket():
    """A 20 x 15 pocket 1 mm deep with R3 corners.

    Deliberately built to clear every logotype gate but one: it has the face
    count, it has the curvature, and it sits in the slab. Its floor fills its
    own bounding box, and that is what says pocket rather than lettering.
    """
    tool = _box((33, 30, 39), (47, 45, 41))
    tool = _fuse(tool, _box((30, 33, 39), (50, 42, 41)))
    for x, y in ((33, 33), (47, 33), (33, 42), (47, 42)):
        tool = _fuse(
            tool,
            BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(x, y, 39), gp_Dir(0, 0, 1)), 3.0, 2.0
            ).Shape(),
        )
    return _cut(block(), tool)


# -- pass 3: background relief -----------------------------------------------


def _ring(x: float, y: float) -> TopoDS_Shape:
    """An O of 8 mm outside diameter and 2 mm stroke, standing 1 mm tall."""
    return _cut(
        BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x, y, 39), gp_Dir(0, 0, 1)), 4.0, 1.0
        ).Shape(),
        BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x, y, 39), gp_Dir(0, 0, 1)), 2.0, 1.0
        ).Shape(),
    )


def make_background_relief():
    """A plaque milled 1 mm down, leaving three O letters at the top surface.

    Each letter is 8 mm across its bounding box and 2 mm across its stroke,
    which is exactly the case a bounding box cannot measure and area over half
    perimeter can.
    """
    tool = _box((30, 30, 39), (66, 48, 41))
    for x in (37.0, 48.0, 59.0):
        tool = _cut(tool, _ring(x, 39.0))
    return _cut(block(), tool)


def make_plaque_with_boss():
    """The same plaque, with one 12 x 12 pedestal instead of the letters.

    A boss machined flush with the surface around it, which is the one shape
    that could fool the relief pass. Its width is what gives it away.
    """
    tool = _cut(
        _box((30, 30, 39), (66, 48, 41)), _box((40, 33, 39), (52, 45, 40.0))
    )
    return _cut(block(), tool)


# -- pass 4: floorless engraving ---------------------------------------------


def _capsule_groove(x0: float, y0: float, x1: float, y1: float) -> TopoDS_Shape:
    """A 0.6 mm wide stroke with a round bottom and round ends, 1.3 mm deep.

    What a ball-nose cutter leaves: vertical side walls down to a cylindrical
    bottom, with no flat floor anywhere in it.
    """
    radius = 0.3
    bottom, top = 39.0, 41.0
    if x0 == x1:
        tool = _box((x0 - radius, y0, bottom), (x0 + radius, y1, top))
        axis, length = gp_Dir(0, 1, 0), y1 - y0
    else:
        tool = _box((x0, y0 - radius, bottom), (x1, y0 + radius, top))
        axis, length = gp_Dir(1, 0, 0), x1 - x0

    for x, y in ((x0, y0), (x1, y1)):
        tool = _fuse(
            tool,
            BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(x, y, bottom), gp_Dir(0, 0, 1)), radius, top - bottom
            ).Shape(),
        )
        tool = _fuse(
            tool, BRepPrimAPI_MakeSphere(gp_Pnt(x, y, bottom), radius).Shape()
        )
    tool = _fuse(
        tool,
        BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x0, y0, bottom), axis), radius, length
        ).Shape(),
    )
    return tool


def make_floorless_mark():
    """Two crossing ball-nose strokes -- a connected mark with no flat floor."""
    tool = _fuse(
        _capsule_groove(30, 50, 42, 50), _capsule_groove(36, 44, 36, 56)
    )
    return _cut(block(), tool)


def make_through_window():
    """A 12 x 12 window cut clean through the plate.

    Floorless in the same sense the engraving is, and made of the same kind of
    walls. Its opposing walls are 12 mm apart rather than half a millimetre,
    which is the whole of the difference.
    """
    return _cut(block(), _box((30, 40, -1), (42, 52, 41)))


# =============================================================================


class EngravedTextTests(unittest.TestCase):
    """Separate characters cut into a face."""

    def test_three_strokes_are_one_marking(self):
        found = markings_in(make_engraved_strokes())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].type, FeatureType.MARKING_TEXT)

    def test_marking_is_measured(self):
        marking = markings_in(make_engraved_strokes())[0]
        self.assertEqual(marking.param("marking_type"), "engraved")
        self.assertEqual(marking.param("glyph_count"), 3)
        self.assertAlmostEqual(marking.number("depth_mm"), 0.5, places=3)
        self.assertAlmostEqual(marking.number("stroke_width_mm"), 1.5, places=3)

    def test_marking_claims_every_stroke_face(self):
        """Five faces per stroke -- floor and four walls -- or the rules that
        skip marking members will still see the strokes."""
        marking = markings_in(make_engraved_strokes())[0]
        self.assertEqual(len(marking.faces), 15)

    def test_host_face_is_reported(self):
        marking = markings_in(make_engraved_strokes())[0]
        host = marking.param("host_face")
        self.assertIsInstance(host, int)
        self.assertNotIn(host, marking.faces)

    def test_faces_are_usable_as_geometry_keys(self):
        marking = markings_in(make_engraved_strokes())[0]
        for face_id in marking.faces:
            self.assertIsInstance(face_id, int)
            self.assertGreaterEqual(face_id, 1)

    def test_two_strokes_are_not_text(self):
        self.assertEqual(markings_in(make_engraved_strokes(count=2)), [])

    def test_wide_strokes_are_not_text(self):
        """A 5 mm floor is a milled slot, whatever it is arranged into."""
        self.assertEqual(markings_in(make_engraved_strokes(width=5.0)), [])

    def test_deep_strokes_are_not_text(self):
        """Past 1.6 mm nobody is engraving; that is a machined recess."""
        self.assertEqual(markings_in(make_engraved_strokes(depth=2.0)), [])

    def test_scattered_glyphs_are_not_a_text_block(self):
        self.assertEqual(markings_in(make_scattered_strokes()), [])

    def test_prior_claims_do_not_suppress_marking(self):
        """Every stroke has already been claimed as a slot by the time this
        recognizer runs. Overruling that is the point of running it late."""
        shape = make_engraved_strokes()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        everything = {node.face_id for node in graph.nodes}
        found = MarkingRecognizer().recognize(graph, shape, claimed=everything)
        self.assertEqual(len(found), 1)

    def test_recognition_is_repeatable(self):
        shape = make_engraved_strokes()
        first = markings_in(shape)
        second = markings_in(shape)
        self.assertEqual(
            [(f.instance_id, f.faces) for f in first],
            [(f.instance_id, f.faces) for f in second],
        )

    def test_plain_block_has_no_marking(self):
        self.assertEqual(markings_in(block()), [])


class RaisedTextTests(unittest.TestCase):
    """Characters standing proud, as cast or moulded text does."""

    def test_proud_strokes_are_marking(self):
        found = markings_in(make_raised_strokes())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].param("marking_type"), "raised")

    def test_proud_height_is_reported(self):
        marking = markings_in(make_raised_strokes())[0]
        self.assertAlmostEqual(marking.number("depth_mm"), 0.5, places=3)
        self.assertEqual(marking.param("glyph_count"), 3)


class LogotypeTests(unittest.TestCase):
    """One connected graphic, qualified on shape statistics."""

    def test_connected_graphic_is_marking(self):
        found = markings_in(make_logotype())
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].param("logotype"))
        self.assertEqual(found[0].param("glyph_count"), 1)

    def test_logotype_depth_is_reported(self):
        marking = markings_in(make_logotype())[0]
        self.assertEqual(marking.param("marking_type"), "engraved")
        self.assertAlmostEqual(marking.number("depth_mm"), 1.0, places=3)

    def test_logotype_claims_the_whole_graphic(self):
        shape = make_logotype()
        graph = AagBuilder(shape, FaceIndex(shape)).build()
        marking = MarkingRecognizer().recognize(graph, shape)[0]
        # The block itself contributes six faces; everything else belongs to
        # the graphic and must be claimed, or its strokes fire rules.
        self.assertEqual(len(marking.faces), graph.face_count - 6)

    def test_shallow_round_pocket_is_not_a_logotype(self):
        """The floor fills its own box, so it is a pocket."""
        self.assertEqual(markings_in(make_shallow_round_pocket()), [])


class BackgroundReliefTests(unittest.TestCase):
    """Letters left standing where the background was milled away."""

    def test_relieved_plaque_is_marking(self):
        found = markings_in(make_background_relief())
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].param("background_relief"))
        self.assertEqual(found[0].param("marking_type"), "raised")

    def test_islands_are_counted_and_measured(self):
        marking = markings_in(make_background_relief())[0]
        self.assertEqual(marking.param("glyph_count"), 3)
        self.assertAlmostEqual(marking.number("depth_mm"), 1.0, places=3)
        # Area over half perimeter, which for an annulus is its wall thickness.
        self.assertAlmostEqual(marking.number("stroke_width_mm"), 2.0, places=2)

    def test_flush_boss_is_not_a_letter(self):
        """Wider than any stroke, so the whole plaque is left alone."""
        self.assertEqual(markings_in(make_plaque_with_boss()), [])


class FloorlessMarkTests(unittest.TestCase):
    """V-cut and ball-nose engraving, which has no flat floor to key on."""

    def test_ball_nose_strokes_are_marking(self):
        found = markings_in(make_floorless_mark())
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].param("floorless"))
        self.assertEqual(found[0].param("marking_type"), "engraved")

    def test_stroke_width_comes_from_opposing_walls(self):
        marking = markings_in(make_floorless_mark())[0]
        self.assertAlmostEqual(marking.number("stroke_width_mm"), 0.6, places=3)
        self.assertAlmostEqual(marking.number("depth_mm"), 1.3, places=3)

    def test_through_window_is_not_a_stroke(self):
        self.assertEqual(markings_in(make_through_window()), [])


if __name__ == "__main__":
    unittest.main()
