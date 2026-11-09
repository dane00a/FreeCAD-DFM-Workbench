# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Real letterforms, cut and raised, and four things that only look like them.

Every other marked part in the corpus is lettered with the seven-segment
stick font in `slice2.py`: axis-aligned boxes, no curves, no counters, no
serifs, no kerning. That kept the parts free of any font dependency, but it
also meant the marking recognizer had only ever been shown the letterforms it
was designed around. Its stroke-width, cap-height, baseline, spacing and fill
measurements had never met an 'O' with a hole in it.

The parts here use real glyph outlines from TrueType files bundled in
`tests/fixtures/fonts/`. Fonts are opened by file path and never by family
name: a fixture whose geometry depends on which fonts the host has installed
would differ from one machine to the next, and a fixture that does that is
worse than none. There is deliberately no system-font fallback.

The text parts span the axes the recognizer has to cope with -- size (a
1.5 mm cap height up to 22 mm), stroke modulation (sans against serif),
depth (0.12 mm up to 2 mm), polarity (engraved, raised, and relief where the
field is milled away around the letters), and a curved host. The
`text_neg_*` parts sit at glyph scale, glyph pitch and marking depth and must
NOT be read as text; each is aimed at a different discriminator, so a false
positive names the test that let it through.

On OpenCascade 7.8 the font-to-B-rep converter is `StdPrs_BRepFont`; it was
renamed `Font_BRepFont` later, with the same methods.
"""

from __future__ import annotations

import math
import os

from OCP.Bnd import Bnd_Box
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
)
from OCP.GC import GC_MakeArcOfCircle
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
try:  # OCCT 7.x
    from OCP.NCollection import NCollection_Utf8String
except ImportError:  # OCCT 8.x names the same string class NCollection_String
    from OCP.NCollection import NCollection_String as NCollection_Utf8String
from OCP.StdPrs import StdPrs_BRepFont
from OCP.TopoDS import TopoDS_Shape, TopoDS_Wire
from freecad.DFM.core.utils.occt import TopTools_ListOfShape

from . import fixture
from .shapes import rounded_rect_prism, seam_ax2


#: The bundled fonts, found relative to this package and nowhere else.
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fonts")


# -- private vocabulary -------------------------------------------------------


def _plate(length: float, width: float, thickness: float) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(length, width, thickness)).Shape()


def _cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    return BRepAlgoAPI_Cut(a, b).Shape()


def _fuse(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    return BRepAlgoAPI_Fuse(a, b).Shape()


def _transformed(shape: TopoDS_Shape, transform: gp_Trsf) -> TopoDS_Shape:
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def _fuse_all(parts: list) -> TopoDS_Shape:
    """Fuse a list of solids in one multi-argument boolean.

    Glyphs are usually disjoint, but a tightly tracked pair overlaps, and an
    overlapping compound handed to a cut as its tool leaves internal faces
    behind. One fuse with everything as arguments and tools resolves every
    overlap in a single pass.
    """
    if not parts:
        return TopoDS_Shape()
    if len(parts) == 1:
        return parts[0]
    arguments = TopTools_ListOfShape()
    tools = TopTools_ListOfShape()
    arguments.Append(parts[0])
    for part in parts[1:]:
        tools.Append(part)
    operation = BRepAlgoAPI_Fuse()
    operation.SetArguments(arguments)
    operation.SetTools(tools)
    operation.Build()
    if not operation.IsDone():
        raise RuntimeError("glyph fuse failed")
    return operation.Shape()


def _bounds(shape: TopoDS_Shape) -> tuple:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return box.Get()


class _Font:
    """A bundled font opened so that its CAP HEIGHT is exactly `cap_h` mm.

    The em size is found by measurement: render 'H' at a nominal 100, take
    the height of its bounding box, and rescale. 'H' is all straight lines,
    so that box is exact. Matching cap height rather than em size is what
    lets a serif, a monospace and a heavy sans be compared like for like;
    their cap-to-em ratios differ by about ten percent.
    """

    def __init__(self, file: str, cap_h: float) -> None:
        path = os.path.normpath(os.path.join(_FONT_DIR, file))
        if not os.path.isfile(path):
            raise RuntimeError(
                f"bundled font not found: {path}. Fixtures never fall back to a system font."
            )
        self.cap_h = cap_h
        probe = self._open(path, 100.0)
        h = probe.RenderGlyph("H")
        if h.IsNull():
            raise RuntimeError(f"no 'H' glyph in {file}")
        _, y0, _, _, y1, _ = _bounds(h)
        cap_at_100 = y1 - y0
        if not cap_at_100 > 0.0:
            raise RuntimeError(f"degenerate 'H' in {file}")
        self._font = self._open(path, cap_h * 100.0 / cap_at_100)

    @staticmethod
    def _open(path: str, em: float) -> StdPrs_BRepFont:
        font = StdPrs_BRepFont()
        if not font.Init(NCollection_Utf8String(path), em, 0):
            raise RuntimeError(f"font initialisation failed for {path}")
        return font

    @staticmethod
    def _next(text: str, i: int) -> str:
        return text[i + 1] if i + 1 < len(text) else "\0"

    def width(self, text: str, tracking: float = 0.0) -> float:
        """Pen advance of `text`, kerning included.

        `tracking` is extra advance per pair as a fraction of cap height, so
        a negative value tightens the setting and enough of it makes
        neighbouring glyphs touch and merge.
        """
        total = 0.0
        for i, c in enumerate(text):
            total += self._font.AdvanceX(c, self._next(text, i)) + tracking * self.cap_h
        return total - tracking * self.cap_h

    def text(
        self,
        text: str,
        x: float,
        y: float,
        z0: float,
        z1: float,
        tracking: float = 0.0,
    ) -> TopoDS_Shape:
        """`text` prismed from z0 to z1, pen origin at (x, y) on the baseline."""
        solids = []
        pen = 0.0
        for i, c in enumerate(text):
            glyph = self._font.RenderGlyph(c)
            if not glyph.IsNull():
                shift = gp_Trsf()
                shift.SetTranslation(gp_Vec(x + pen, y, z0))
                # A copy, because the font hands back its cached glyph and
                # reuses it for every repeat of the letter.
                placed = _transformed(glyph, shift)
                solids.append(
                    BRepPrimAPI_MakePrism(placed, gp_Vec(0.0, 0.0, z1 - z0)).Shape()
                )
            pen += self._font.AdvanceX(c, self._next(text, i)) + tracking * self.cap_h
        return _fuse_all(solids)

    def glyph(self, c: str, z0: float, z1: float) -> TopoDS_Shape:
        """One glyph centred on x = 0, baseline on y = 0, prismed z0..z1."""
        glyph = self._font.RenderGlyph(c)
        if glyph.IsNull():
            return TopoDS_Shape()
        x0, _, _, x1, _, _ = _bounds(glyph)
        shift = gp_Trsf()
        shift.SetTranslation(gp_Vec(-0.5 * (x0 + x1), 0.0, z0))
        placed = _transformed(glyph, shift)
        return BRepPrimAPI_MakePrism(placed, gp_Vec(0.0, 0.0, z1 - z0)).Shape()


def _rounded_rect_wire(
    x0: float, y0: float, x1: float, y1: float, r: float, z: float
) -> TopoDS_Wire:
    """A rounded rectangle at height z, always built in the same edge order.

    A side the corner radii have used up entirely is left out rather than
    added as a zero-length edge, so an obround and a circle come out of the
    same code. Every station of a drawn pad insets box and radius together,
    so a side that vanishes at one station vanishes at all of them and the
    loft sees the same edge count throughout.
    """
    wire = BRepBuilderAPI_MakeWire()

    def line(ax, ay, bx, by):
        if math.hypot(bx - ax, by - ay) < 1.0e-9:
            return
        wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(ax, ay, z), gp_Pnt(bx, by, z)).Edge())

    if r <= 1.0e-9:
        line(x0, y0, x1, y0)
        line(x1, y0, x1, y1)
        line(x1, y1, x0, y1)
        line(x0, y1, x0, y0)
        return wire.Wire()

    k = r - r / math.sqrt(2.0)

    def arc(ax, ay, mx, my, bx, by):
        curve = GC_MakeArcOfCircle(
            gp_Pnt(ax, ay, z), gp_Pnt(mx, my, z), gp_Pnt(bx, by, z)
        ).Value()
        wire.Add(BRepBuilderAPI_MakeEdge(curve).Edge())

    line(x0 + r, y0, x1 - r, y0)
    arc(x1 - r, y0, x1 - k, y0 + k, x1, y0 + r)
    line(x1, y0 + r, x1, y1 - r)
    arc(x1, y1 - r, x1 - k, y1 - k, x1 - r, y1)
    line(x1 - r, y1, x0 + r, y1)
    arc(x0 + r, y1, x0 + k, y1 - k, x0, y1 - r)
    line(x0, y1 - r, x0, y0 + r)
    arc(x0, y0 + r, x0 + k, y0 + k, x0 + r, y0)
    return wire.Wire()


def _drawn_pad(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    r_plan: float,
    t: float,
    a: float,
    r_root: float,
    r_crest: float,
    theta_deg: float,
    sink: float,
) -> tuple:
    """A pressed, closed emboss: the pad to fuse on and the cavity to cut under.

    (x0, y0)-(x1, y1) is the footprint at the deck, where the root radius
    starts. The wall profile is a root arc, a straight ramp and a crest arc,
    lofted through a stack of rounded-rectangle stations; the outer and inner
    skins share arc centres, so the gauge is exactly `t` everywhere and the
    crest and cavity ceiling are flat and one gauge apart. If the requested
    ramp angle would over-rise the pad, the angle is reduced until the two
    arcs meet tangentially; the radii are never shortened. `sink` drops the
    run-out below the deck so the fuse is never tangential.
    """
    if r_crest <= t:
        raise RuntimeError("drawn pad: crest radius must exceed the gauge")
    zs = t - sink
    rise = (t + a) - zs
    rr = r_root + r_crest
    th = math.radians(theta_deg)
    if rise < rr * (1.0 - math.cos(th)):
        th = math.acos(1.0 - rise / rr)
    ct, st = math.cos(th), math.sin(th)
    run = max(0.0, (rise - rr * (1.0 - ct)) / st)
    plan_run = rr * st + run * ct

    half = min(0.5 * (x1 - x0), 0.5 * (y1 - y0))
    if plan_run >= half:
        raise RuntimeError(
            f"drawn pad: wall needs {plan_run} mm of plan per side, footprint has {half}"
        )
    if r_plan <= plan_run:
        raise RuntimeError(
            f"drawn pad: plan radius {r_plan} is inside the wall run {plan_run}"
        )

    n_root, n_ramp, n_crest = 6, 2, 6
    stations = []  # (inset_outer, z_outer, inset_inner, z_inner)
    for i in range(n_root + 1):
        phi = th * i / n_root
        stations.append(
            (
                r_root * math.sin(phi),
                zs + r_root * (1.0 - math.cos(phi)),
                (r_root + t) * math.sin(phi),
                zs + r_root - (r_root + t) * math.cos(phi),
            )
        )
    i1o, z1o = r_root * st, zs + r_root * (1.0 - ct)
    i1i, z1i = (r_root + t) * st, zs + r_root - (r_root + t) * ct
    for i in range(1, n_ramp + 1):
        u = run * i / n_ramp
        stations.append((i1o + u * ct, z1o + u * st, i1i + u * ct, z1i + u * st))
    czo = t + a - r_crest
    for i in range(1, n_crest + 1):
        psi = th * (n_crest - i) / n_crest
        stations.append(
            (
                plan_run - r_crest * math.sin(psi),
                czo + r_crest * math.cos(psi),
                plan_run - (r_crest - t) * math.sin(psi),
                czo + (r_crest - t) * math.cos(psi),
            )
        )

    def loft(outer: bool) -> TopoDS_Shape:
        lofter = BRepOffsetAPI_ThruSections(True, False, 1.0e-6)
        for inset_o, z_o, inset_i, z_i in stations:
            d = inset_o if outer else inset_i
            lofter.AddWire(
                _rounded_rect_wire(
                    x0 + d, y0 + d, x1 - d, y1 - d, r_plan - d, z_o if outer else z_i
                )
            )
        lofter.Build()
        return lofter.Shape()

    hood = loft(True)
    air = loft(False)

    # A failed loft does not fail loudly, so check that each skin landed
    # roughly where it should. The slack covers OpenCascade boxing a lofted
    # face by its poles, which stand off the surface.
    def envelope(shape, what, zlo, zhi):
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        if box.IsVoid():
            raise RuntimeError(f"drawn pad: {what} is empty")
        ax, ay, az, bx, by, bz = box.Get()
        slack = 1.5
        if (
            az < zlo - slack
            or bz > zhi + slack
            or ax < x0 - slack
            or bx > x1 + slack
            or ay < y0 - slack
            or by > y1 + slack
        ):
            raise RuntimeError(f"drawn pad: {what} escaped its envelope")

    envelope(hood, "pad", zs, t + a)
    envelope(air, "cavity", zs - t, a)
    return hood, air


# -- real-font engraved and raised legends ------------------------------------


@fixture("text_dejavu_serial_engraved")
def text_dejavu_serial_engraved() -> TopoDS_Shape:
    """A serial number engraved in DejaVu Sans: the baseline real-font case.

    Cap 4 mm, 0.35 mm deep in a 110 x 45 x 8 nameplate. The first line,
    "SN-2026-0147", is the first time the marking recognizer sees a curve and
    a counter -- the closed holes of 0, 6 and 4 come through as islands in
    the cut floor. The second line, "AVAT WAVE", is set so tight that pairs
    overlap and merge into single ink patches, which breaks the assumption
    that one connected component is one glyph. It shares a cluster with the
    first line so the cap-height and baseline evidence stays strong.
    """
    L, W, T, D = 110.0, 45.0, 8.0, 0.35
    plate = _plate(L, W, T)
    font = _Font("DejaVuSans.ttf", 4.0)
    line = "SN-2026-0147"
    plate = _cut(plate, font.text(line, 0.5 * (L - font.width(line)), 26.0, T - D, T + 0.4))
    line, tracking = "AVAT WAVE", -0.26
    plate = _cut(
        plate,
        font.text(
            line, 0.5 * (L - font.width(line, tracking)), 12.0, T - D, T + 0.4, tracking
        ),
    )
    return plate


@fixture("text_serif_mixedcase_engraved")
def text_serif_mixedcase_engraved() -> TopoDS_Shape:
    """Mixed-case serif text: the part most likely to break stroke measurement.

    Liberation Serif at cap 6 mm, 0.5 mm deep in 110 x 45 x 10. A serif face
    carries roughly a 3:1 ratio between stem and hairline inside one glyph,
    and a stroke width taken as twice area over perimeter gives one number
    for a shape that does not have one. "Ogee-Bag 38." brings a descender (so
    cap height is not every glyph's height), the counters of O/e/g/B/a/8, and
    a hyphen and full stop small enough to fall outside the stroke-width
    pairing window the clusterer uses.
    """
    L, W, T, D = 110.0, 45.0, 10.0, 0.5
    plate = _plate(L, W, T)
    font = _Font("LiberationSerif-Regular.ttf", 6.0)
    line = "Ogee-Bag 38."
    return _cut(plate, font.text(line, 0.5 * (L - font.width(line)), 19.0, T - D, T + 0.5))


@fixture("text_mono_micro_engraved")
def text_mono_micro_engraved() -> TopoDS_Shape:
    """The small end: a 1.5 mm cap monospace tag cut only 0.12 mm deep.

    Liberation Mono in a 34 x 16 x 4 tag, stroke about 0.2 mm. The depth is
    only a little over twice the noise floor, so this is the shallowest mark
    the pipeline is expected to see at all. A monospace puts every glyph on
    the same advance, so the spacing evidence is perfect while the absolute
    sizes are marginal -- isolating size as the variable that decides recall.
    """
    L, W, T, D = 34.0, 16.0, 4.0, 0.12
    plate = _plate(L, W, T)
    font = _Font("LiberationMono-Regular.ttf", 1.5)
    line = "0123-AB"
    return _cut(plate, font.text(line, 0.5 * (L - font.width(line)), 7.0, T - D, T + 0.2))


@fixture("text_serif_hairline_engraved")
def text_serif_hairline_engraved() -> TopoDS_Shape:
    """Serif text so small its hairlines are about 0.11 mm wide.

    Liberation Serif at cap 2.5 mm, 0.15 mm deep in 60 x 24 x 6. The
    stroke-to-cap ratio is around a third of the sans parts', and depth over
    stroke runs from about 1.4 on the hairlines to 0.55 on the stems, so one
    short legend, "Rev. B-3", spans most of the accepted depth/stroke band by
    itself.
    """
    L, W, T, D = 60.0, 24.0, 6.0, 0.15
    plate = _plate(L, W, T)
    font = _Font("LiberationSerif-Regular.ttf", 2.5)
    line = "Rev. B-3"
    return _cut(plate, font.text(line, 0.5 * (L - font.width(line)), 10.0, T - D, T + 0.2))


@fixture("text_sans_large_engraved")
def text_sans_large_engraved() -> TopoDS_Shape:
    """The large end, still under the stroke-width ceiling.

    Liberation Sans at cap 18 mm (stroke about 2.2 mm), 1.2 mm deep in
    240 x 90 x 18. At this size a glyph starts to look like a pocket: the
    counters of P, A and R are holes several millimetres across in the floor.
    Its ratios match the 4 mm serial plate's, so it tests the claim that the
    marking criteria are scale-free.
    """
    L, W, T, D = 240.0, 90.0, 18.0, 1.2
    plate = _plate(L, W, T)
    font = _Font("LiberationSans-Regular.ttf", 18.0)
    line = "PART 9"
    return _cut(plate, font.text(line, 0.5 * (L - font.width(line)), 36.0, T - D, T + 1.0))


@fixture("text_bold_heavy_engraved")
def text_bold_heavy_engraved() -> TopoDS_Shape:
    """Heavy bold lettering whose stroke is wider than the marking ceiling.

    Liberation Sans Bold at cap 22 mm has a stem of about 4.2 mm, past the
    3.5 mm maximum stroke width, so the hard reject for "wider than a marking
    tool cuts" should fire and this should not be read as marking. Whether
    that is right for a cast or struck legend is an open question; the
    baseline records whatever the pipeline answers, and the ceiling should
    move only on measurement, never to make this part pass.
    """
    L, W, T, D = 260.0, 95.0, 22.0, 1.6
    plate = _plate(L, W, T)
    font = _Font("LiberationSans-Bold.ttf", 22.0)
    line = "HEAVY8"
    return _cut(plate, font.text(line, 0.5 * (L - font.width(line)), 38.0, T - D, T + 1.2))


@fixture("text_sans_deep_engraved")
def text_sans_deep_engraved() -> TopoDS_Shape:
    """Deep engraving that approaches both depth limits at once.

    Liberation Sans at cap 10 mm (stroke about 1.2 mm), 2 mm deep in
    120 x 50 x 12: depth over stroke is about 1.65, just under the 2.0 band
    edge, and depth over thickness is 0.17 against a 0.5 limit. Having both
    in one part shows which bound bites first.
    """
    L, W, T, D = 120.0, 50.0, 12.0, 2.0
    plate = _plate(L, W, T)
    font = _Font("LiberationSans-Regular.ttf", 10.0)
    line = "DEEP-08"
    return _cut(plate, font.text(line, 0.5 * (L - font.width(line)), 20.0, T - D, T + 0.8))


@fixture("text_sans_raised_cast")
def text_sans_raised_cast() -> TopoDS_Shape:
    """Raised lettering on a flat-backed plate: the polarity test.

    Liberation Sans at cap 8 mm standing 0.8 mm proud of 120 x 50 x 6. From
    the front a proud letterform and a field of bosses look alike; only the
    flat back, where the wall under the letters is thicker by the letter
    height, says the material was added. The glyph solids start 0.1 mm inside
    the plate so the fuse never meets a face exactly tangentially.
    """
    L, W, T, H = 120.0, 50.0, 6.0, 0.8
    plate = _plate(L, W, T)
    font = _Font("LiberationSans-Regular.ttf", 8.0)
    line = "CAST-07"
    return _fuse(plate, font.text(line, 0.5 * (L - font.width(line)), 21.0, T - 0.1, T + H))


@fixture("text_serif_relief_plaque")
def text_serif_relief_plaque() -> TopoDS_Shape:
    """A relief plaque: the field milled down and the letters left standing.

    Liberation Serif at cap 12 mm on 140 x 70 x 10. A 100 x 34 field with
    R3 plan corners is lowered 1.5 mm around "FORGE", so the letters are at
    the original top level, as islands inside a much larger coplanar face.
    That is the case the host grouping has to hand back to the member pool or
    the plaque loses the letters it is made of. Serif flares and stems share
    each island, so every glyph's own stroke estimate averages a 3:1 range.
    """
    L, W, T, D = 140.0, 70.0, 10.0, 1.5
    plate = _plate(L, W, T)
    font = _Font("LiberationSerif-Regular.ttf", 12.0)
    line = "FORGE"
    # The relief tool is the field minus the glyphs, so the letters survive
    # at full plate height.
    field = rounded_rect_prism(20.0, 18.0, 120.0, 52.0, 3.0, T - D, D + 0.5)
    glyphs = font.text(line, 0.5 * (L - font.width(line)), 28.0, T - D - 0.1, T + 0.6)
    field = _cut(field, glyphs)
    return _cut(plate, field)


@fixture("text_bold_midweight_engraved")
def text_bold_midweight_engraved() -> TopoDS_Shape:
    """Bold lettering just under the stroke-width ceiling.

    Liberation Sans Bold at cap 14 mm, 1.2 mm deep in 130 x 50 x 10. Its ink
    widths sit between the widest mark accepted elsewhere in the corpus and
    the heavy bold part that is rejected, so the two together bracket the
    ceiling with real letterforms on both sides of it.

    The walls are vertical. A drafted version was attempted upstream and
    abandoned because offsetting the glyph contours failed on most of them;
    no real-font fixture covers drafted or V-bottom strokes.
    """
    L, W, T, D = 130.0, 50.0, 10.0, 1.2
    plate = _plate(L, W, T)
    font = _Font("LiberationSans-Bold.ttf", 14.0)
    line = "V-CUT 1"
    return _cut(plate, font.text(line, 0.5 * (L - font.width(line)), 20.0, T - D, T + 0.8))


@fixture("text_cyl_rotary_engraved")
def text_cyl_rotary_engraved() -> TopoDS_Shape:
    """Rotary engraving around the outside of a collar: text on a curved host.

    A 60 diameter x 45 collar with a 25 mm through bore -- one ordinary
    feature the marking claim must not swallow -- and "ROT-2026" in DejaVu
    Sans at cap 5 mm, 0.4 mm deep. Each glyph is cut on its own tangent plane
    at its own angular station, as a rotary engraver does, rather than
    projected from one plane across the arc; the stroke floors are chord
    planes and the depth varies by about a quarter across a glyph. A cylinder
    unrolls without distortion, so this is the part that exercises marking
    recognition on a cylindrical host face.
    """
    R, height, D = 30.0, 45.0, 0.4
    part = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), R, height).Shape()
    bore = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, -1.0), gp_Dir(0, 0, 1)), 12.5, height + 2.0
    ).Shape()
    part = _cut(part, bore)

    font = _Font("DejaVuSans.ttf", 5.0)
    line = "ROT-2026"
    d_theta = 7.0 / R  # about 7 mm of arc per glyph
    tools = []
    for i, c in enumerate(line):
        glyph = font.glyph(c, 0.0, 0.7)
        if glyph.IsNull():
            continue
        # Glyph in XY, up +Y, cutting along +Z. A quarter turn about X sends
        # up to +Z and the cut to -Y, radially inward once parked at y = R.
        rot_x = gp_Trsf()
        rot_x.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), math.pi / 2.0)
        place = gp_Trsf()
        place.SetTranslation(gp_Vec(0.0, R + (0.7 - D), 20.0))
        spin = gp_Trsf()
        spin.SetRotation(
            gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)),
            (float(i) - 0.5 * float(len(line) - 1)) * d_theta,
        )
        tools.append(_transformed(glyph, spin.Multiplied(place).Multiplied(rot_x)))
    return _cut(part, _fuse_all(tools))


# -- near-miss negatives ------------------------------------------------------


@fixture("text_neg_pocket_row")
def text_neg_pocket_row() -> TopoDS_Shape:
    """A row of small rounded pockets with every typographic signal of text.

    Ten 2.6 x 4.0 x 0.4 pockets with R0.5 corners on a 7 mm pitch: a shared
    baseline, a common height, even spacing, one depth. The only thing that
    says "not text" is component fill -- each pocket fills about all of its
    bounding box, where a glyph fills roughly 0.4 to 0.6 -- so this is the
    test of the fill discriminator.
    """
    L, W, T, D = 100.0, 40.0, 8.0, 0.4
    plate = _plate(L, W, T)
    tools = []
    for i in range(10):
        x = 16.0 + i * 7.0
        tools.append(rounded_rect_prism(x, 18.0, x + 2.6, 22.0, 0.5, T - D, D + 0.4))
    return _cut(plate, _fuse_all(tools))


@fixture("text_neg_knurl_patch")
def text_neg_knurl_patch() -> TopoDS_Shape:
    """A diamond knurl patch: a texture at marking depth and stroke width.

    A 40 x 16 patch of 0.35 mm grooves, 0.25 mm deep, on a 2.5 mm pitch at
    plus and minus 45 degrees. Depth and width both sit inside the marking
    band; what separates it from text is slenderness -- each groove runs tens
    of millimetres against a 0.35 mm width -- and the whole patch being one
    connected component. The grooves are clipped to the patch so they do not
    run off the plate edge, which would be an easier negative.
    """
    L, W, T, D = 70.0, 40.0, 8.0, 0.25
    plate = _plate(L, W, T)
    cx, cy = 35.0, 20.0
    grooves = []
    for sign in (-1, 1):
        for k in range(-12, 13):
            bar = BRepPrimAPI_MakeBox(
                gp_Pnt(cx - 45.0, cy + k * 2.5 - 0.175, T - D),
                gp_Pnt(cx + 45.0, cy + k * 2.5 + 0.175, T + 0.3),
            ).Shape()
            rot = gp_Trsf()
            rot.SetRotation(gp_Ax1(gp_Pnt(cx, cy, 0), gp_Dir(0, 0, 1)), sign * math.pi / 4.0)
            grooves.append(_transformed(bar, rot))
    patch = BRepPrimAPI_MakeBox(
        gp_Pnt(15.0, 12.0, T - D - 0.1), gp_Pnt(55.0, 28.0, T + 0.4)
    ).Shape()
    tool = BRepAlgoAPI_Common(_fuse_all(grooves), patch).Shape()
    return _cut(plate, tool)


@fixture("text_neg_decor_groove")
def text_neg_decor_groove(seam_deg: float = 0.0) -> TopoDS_Shape:
    """A shallow milled rosette: decoration, not lettering.

    Two concentric 1.2 mm wide rings at R18 and R22 and three radial bars at
    120 degrees, all 0.3 mm deep in 80 x 60 x 8. It is one connected
    component with no baseline, so any claim on it would have to come from
    the no-baseline gate -- the branch that exists to admit merged logotypes
    and so the one most at risk of admitting this. ``seam_deg`` turns the
    ring cylinders' seams without moving any material.
    """
    L, W, T, D = 80.0, 60.0, 8.0, 0.3
    plate = _plate(L, W, T)
    cx, cy = 40.0, 30.0
    tools = []
    for r in (18.0, 22.0):
        ax = seam_ax2(gp_Pnt(cx, cy, T - D), gp_Dir(0, 0, 1), seam_deg)
        tools.append(
            _cut(
                BRepPrimAPI_MakeCylinder(ax, r + 0.6, D + 0.3).Shape(),
                BRepPrimAPI_MakeCylinder(ax, r - 0.6, D + 0.3).Shape(),
            )
        )
    for k in range(3):
        bar = BRepPrimAPI_MakeBox(
            gp_Pnt(cx + 6.0, cy - 0.6, T - D), gp_Pnt(cx + 18.0, cy + 0.6, T + 0.3)
        ).Shape()
        rot = gp_Trsf()
        rot.SetRotation(gp_Ax1(gp_Pnt(cx, cy, 0), gp_Dir(0, 0, 1)), k * 2.0 * math.pi / 3.0)
        tools.append(_transformed(bar, rot))
    return _cut(plate, _fuse_all(tools))


@fixture("text_neg_louver_row")
def text_neg_louver_row() -> TopoDS_Shape:
    """Pressed pads at text size and pitch: formed sheet, not a raised legend.

    Six 8 x 6 obround pads drawn 0.7 mm proud of a 1.2 mm sheet on a 14 mm
    pitch. From the front they match a raised legend in size, pitch, shared
    baseline and height. The back gives them away: the wall is one gauge
    thick under the pad and beside it, so material was displaced, not added,
    and a formed candidate never enters the marking path.

    The pads are as wide as they are because a pressed pad needs room for
    its root and crest radii on every side; nothing at stroke width can be
    drawn. The 14 mm pitch keeps the visible web between pads clear of the
    formed-feature pitch rule. Each pad stays closed so the back carries a
    constant-gauge mirror of the front, which is the property under test.
    """
    L, W, t, h = 100.0, 34.0, 1.2, 0.7
    panel = _plate(L, W, t)
    for i in range(6):
        x = 10.0 + i * 14.0
        hood, air = _drawn_pad(x, 14.0, x + 8.0, 20.0, 3.0, t, h, t, 1.5 * t, 80.0, 0.4 * t)
        panel = _fuse(panel, hood)
        panel = _cut(panel, air)
    return panel
