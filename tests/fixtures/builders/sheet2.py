# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Formed sheet that a press could actually make, and the four fold families.

The first half of this module is the formed-feature set redrawn as real
tooling would draw it. The earlier versions of these parts were boxes fused
on and boxes cut out: vertical walls, flat roofs, square plan corners and a
zero-radius fold at every hinge. None of that can be pressed. Here an emboss
is a closed draw with a root radius, a ramp, a crest radius and rounded plan
corners; a lance is an arch sheared on two sides and anchored at both ends;
a louver is a lofted scoop sheared on one side; and a bead is a half-round
rolled into the deck with nothing sheared at all. Each keeps the bait its
box ancestor carried -- the same heights against the same thresholds -- so
the rules see the same verdicts on honest geometry.

The second half is the fold census: one witness for each of the bend
recognizer's four fold families -- press-brake cylindrical, conical, rolled
and past-180 -- plus the controlled pair for fold direction and two parts
that put a press-brake fold beside a non-press-brake one. Every fold is a
closed cross-section swept along its axis, or for a cone a section revolved
about it, so each skin is exactly the analytic surface it claims to be.
"""

from __future__ import annotations

import math

from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.GC import GC_MakeArcOfCircle
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.TopoDS import TopoDS_Shape

from . import fixture
from .shapes import box_between, common, cut, cylinder, fuse
from .slice3 import (
    _Profile,
    _bracket_profile,
    _cut_under_fold,
    _in_xz,
    _lofted_hood,
    _place_lofted_louver,
    _plateau_stations,
    _polygon_prism,
)


# -- drawn pads ---------------------------------------------------------------


def _rounded_rect_wire(x0, y0, x1, y1, r, z):
    """A rounded rectangle at height z: four lines and four quarter arcs.

    Always drawn in the same order, so a stack of them lofts cleanly. A side
    the corner radii have used up entirely is left out rather than added at
    zero length; the pad stack insets the box and the radius together, so a
    side that vanishes at one station has vanished at all of them and every
    section keeps the same edge count.
    """
    wire = BRepBuilderAPI_MakeWire()

    def place(x, y):
        return gp_Pnt(x, y, z)

    def line(ax, ay, bx, by):
        if math.hypot(bx - ax, by - ay) < 1.0e-9:
            return
        wire.Add(BRepBuilderAPI_MakeEdge(place(ax, ay), place(bx, by)).Edge())

    if r <= 1.0e-9:
        line(x0, y0, x1, y0)
        line(x1, y0, x1, y1)
        line(x1, y1, x0, y1)
        line(x0, y1, x0, y0)
        return wire.Wire()

    k = r - r / math.sqrt(2.0)

    def arc(ax, ay, mx, my, bx, by):
        curve = GC_MakeArcOfCircle(place(ax, ay), place(mx, my), place(bx, by)).Value()
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


def _drawn_pad(x0, y0, x1, y1, r_plan, t, a, r_root, r_crest, theta_deg, sink):
    """A closed draw: the pad and the void it leaves, as two lofted solids.

    The wall is root arc, straight ramp, crest arc, taken round a rounded
    plan outline, so the pad is closed on every side and has no open edge.
    Both radii are honoured whatever the depth: if the requested ramp angle
    would over-rise the apex, the angle is solved down until the two arcs
    meet tangentially rather than a radius being shortened. The inner skin
    shares every arc centre with the outer, one gauge out at the root and
    one gauge in at the crest, so the gauge cannot drift.

    The run-out sits `sink` below the deck's top face, so the pad crosses the
    deck transversally rather than grazing it, and the void opens through
    the underside of the blank as a drawn pad's does. The wall lofts to a
    B-spline, which keeps it out of the bend recognizer's way; the crest and
    cavity ceiling stay planar and exactly one gauge apart, and that pair is
    what the formed recognizer seeds on.

    (x0, y0)-(x1, y1) is the footprint at the deck, where the root radius
    begins.
    """
    if r_crest <= t:
        raise ValueError("drawn pad: crest radius must exceed the gauge")
    zs = t - sink
    rise = (t + a) - zs
    rr = r_root + r_crest
    th = math.radians(theta_deg)
    if rise < rr * (1.0 - math.cos(th)):
        th = math.acos(1.0 - rise / rr)
    ct, st = math.cos(th), math.sin(th)
    ramp = max(0.0, (rise - rr * (1.0 - ct)) / st)
    run = rr * st + ramp * ct

    half = min(0.5 * (x1 - x0), 0.5 * (y1 - y0))
    if run >= half:
        raise ValueError("drawn pad: the wall needs more plan than the footprint has")
    if r_plan <= run:
        raise ValueError("drawn pad: plan corner radius is inside the wall run")

    n_root, n_ramp, n_crest = 6, 2, 6
    stations = []
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
        u = ramp * i / n_ramp
        stations.append((i1o + u * ct, z1o + u * st, i1i + u * ct, z1i + u * st))
    czo = t + a - r_crest
    for i in range(1, n_crest + 1):
        psi = th * (n_crest - i) / n_crest
        stations.append(
            (
                run - r_crest * math.sin(psi),
                czo + r_crest * math.cos(psi),
                run - (r_crest - t) * math.sin(psi),
                czo + (r_crest - t) * math.cos(psi),
            )
        )

    def loft(outer: bool) -> TopoDS_Shape:
        sections = BRepOffsetAPI_ThruSections(True, False, 1.0e-6)
        for inset_o, z_o, inset_i, z_i in stations:
            d = inset_o if outer else inset_i
            sections.AddWire(
                _rounded_rect_wire(
                    x0 + d, y0 + d, x1 - d, y1 - d, r_plan - d, z_o if outer else z_i
                )
            )
        sections.Build()
        return sections.Shape()

    return loft(True), loft(False)


# -- lanced bridges -----------------------------------------------------------


def _lance_bridge_arch(x0, x1, y0, y1, t, a, r_root, r_crown, theta_deg, sink):
    """A bridge lance: a constant-gauge arch prismed across the strip.

    The section is root arc, straight ramp at theta, crown arc, plateau, and
    the same mirrored at the far end; the inner skin is its exact offset,
    built about the same arc centres. Prismed along the shear lines, every
    face comes out a plane or a cylinder, so the gauge is exact by
    construction and nothing is lofted.

    A taller lance needs a longer run, not a steeper ramp, and the check is
    made before any boolean runs. As with the drawn pad the run-outs sink
    below the deck's top face so the fuse is transversal; the hood is then
    clipped at the deck's back face so nothing hangs below the blank.

    Returns the hood and the air path under it.
    """
    th = math.radians(theta_deg)
    ct, st = math.cos(th), math.sin(th)
    zs = t - sink
    rise = (t + a) - zs
    ramp = (rise - (r_root + r_crown) * (1.0 - ct)) / st
    if r_crown <= t:
        raise ValueError("lance arch: crown radius must exceed the gauge")
    if ramp < 0.0:
        raise ValueError("lance arch: the radii alone over-rise the apex")
    run = (r_root + r_crown) * st + ramp * ct
    if x1 - x0 < 2.0 * run + 1.0:
        raise ValueError("lance arch: the run is shorter than its two ramps need")

    def half(xe, s):
        hf = 0.5 * th
        rcx, rcz = xe, zs + r_root

        def on_root(rad, phi):
            return (rcx + s * rad * math.sin(phi), rcz - rad * math.cos(phi))

        h = {}
        h["P0"] = on_root(r_root, 0.0)
        h["Pm_root"] = on_root(r_root, hf)
        h["P1"] = on_root(r_root, th)
        h["Q0"] = on_root(r_root + t, 0.0)
        h["Qm_root"] = on_root(r_root + t, hf)
        h["Q1"] = on_root(r_root + t, th)
        h["P2"] = (h["P1"][0] + s * ramp * ct, h["P1"][1] + ramp * st)
        h["Q2"] = (h["Q1"][0] + s * ramp * ct, h["Q1"][1] + ramp * st)
        ccx = h["P2"][0] + s * r_crown * st
        ccz = h["P2"][1] - r_crown * ct

        def on_crown(rad, psi):
            return (ccx - s * rad * math.sin(psi), ccz + rad * math.cos(psi))

        h["Pm_crown"] = on_crown(r_crown, hf)
        h["P3"] = on_crown(r_crown, 0.0)
        h["Qm_crown"] = on_crown(r_crown - t, hf)
        h["Q3"] = on_crown(r_crown - t, 0.0)
        return h

    left = half(x0, 1.0)
    right = half(x1, -1.0)

    def at(p):
        return gp_Pnt(p[0], y0, p[1])

    def seg(wire, p, q):
        wire.Add(BRepBuilderAPI_MakeEdge(at(p), at(q)).Edge())

    def arc(wire, p, m, q):
        wire.Add(
            BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(at(p), at(m), at(q)).Value()).Edge()
        )

    L, R = left, right
    hw = BRepBuilderAPI_MakeWire()
    arc(hw, L["P0"], L["Pm_root"], L["P1"])
    seg(hw, L["P1"], L["P2"])
    arc(hw, L["P2"], L["Pm_crown"], L["P3"])
    seg(hw, L["P3"], R["P3"])
    arc(hw, R["P3"], R["Pm_crown"], R["P2"])
    seg(hw, R["P2"], R["P1"])
    arc(hw, R["P1"], R["Pm_root"], R["P0"])
    seg(hw, R["P0"], R["Q0"])
    arc(hw, R["Q0"], R["Qm_root"], R["Q1"])
    seg(hw, R["Q1"], R["Q2"])
    arc(hw, R["Q2"], R["Qm_crown"], R["Q3"])
    seg(hw, R["Q3"], L["Q3"])
    arc(hw, L["Q3"], L["Qm_crown"], L["Q2"])
    seg(hw, L["Q2"], L["Q1"])
    arc(hw, L["Q1"], L["Qm_root"], L["Q0"])
    seg(hw, L["Q0"], L["P0"])

    zbot = -(t + 1.0)
    aw = BRepBuilderAPI_MakeWire()
    arc(aw, L["Q0"], L["Qm_root"], L["Q1"])
    seg(aw, L["Q1"], L["Q2"])
    arc(aw, L["Q2"], L["Qm_crown"], L["Q3"])
    seg(aw, L["Q3"], R["Q3"])
    arc(aw, R["Q3"], R["Qm_crown"], R["Q2"])
    seg(aw, R["Q2"], R["Q1"])
    arc(aw, R["Q1"], R["Qm_root"], R["Q0"])
    seg(aw, R["Q0"], (x1, zbot))
    seg(aw, (x1, zbot), (x0, zbot))
    seg(aw, (x0, zbot), L["Q0"])

    span = gp_Vec(0.0, y1 - y0, 0.0)
    hood = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(hw.Wire()).Face(), span).Shape()
    air = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(aw.Wire()).Face(), span).Shape()
    hood = common(
        hood, box_between((x0 - 1.0, y0 - 1.0, 0.0), (x1 + 1.0, y1 + 1.0, t + a + 1.0))
    )
    return hood, air


# -- folds --------------------------------------------------------------------


class _FoldProfile:
    """A closed cross-section in the XZ plane, prismed along +Y.

    Arcs are given by their two end points and their centre, and the
    mid-point is computed from the centre rather than typed in: a hand-typed
    sag is how a fold ends up a fraction of a degree off tangency while still
    looking tangent to the face graph. The end points are passed explicitly
    so consecutive edges share the same point and the wire closes exactly.

    `major` takes the long way round the circle, which is how a sweep past
    180 degrees comes out as a single cylindrical face rather than two.
    """

    def __init__(self):
        self._wire = BRepBuilderAPI_MakeWire()

    def seg(self, x0, z0, x1, z1) -> "_FoldProfile":
        self._wire.Add(
            BRepBuilderAPI_MakeEdge(gp_Pnt(x0, 0.0, z0), gp_Pnt(x1, 0.0, z1)).Edge()
        )
        return self

    def arc_to(self, x0, z0, cx, cz, x1, z1, major: bool = False) -> "_FoldProfile":
        a0 = math.atan2(z0 - cz, x0 - cx)
        a1 = math.atan2(z1 - cz, x1 - cx)
        r = math.hypot(x0 - cx, z0 - cz)
        d = a1 - a0
        while d <= -math.pi:
            d += 2.0 * math.pi
        while d > math.pi:
            d -= 2.0 * math.pi
        if major:
            d += -2.0 * math.pi if d > 0.0 else 2.0 * math.pi
        am = a0 + 0.5 * d
        curve = GC_MakeArcOfCircle(
            gp_Pnt(x0, 0.0, z0),
            gp_Pnt(cx + r * math.cos(am), 0.0, cz + r * math.sin(am)),
            gp_Pnt(x1, 0.0, z1),
        ).Value()
        self._wire.Add(BRepBuilderAPI_MakeEdge(curve).Edge())
        return self

    def prism(self, depth_y: float) -> TopoDS_Shape:
        face = BRepBuilderAPI_MakeFace(self._wire.Wire()).Face()
        return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, depth_y, 0.0)).Shape()


def _quad(points):
    """A planar face bounded by a closed four-point polygon."""
    polygon = BRepBuilderAPI_MakePolygon(*(gp_Pnt(*p) for p in points), True)
    return BRepBuilderAPI_MakeFace(polygon.Wire()).Face()


def _transformed(shape, transform: gp_Trsf, copy: bool = True) -> TopoDS_Shape:
    return BRepBuilderAPI_Transform(shape, transform, copy).Shape()


# -- formed features ----------------------------------------------------------


@fixture("sm_emboss_field")
def build_sm_emboss_field() -> TopoDS_Shape:
    """Four drawn embosses: a control, one too deep, and a pair too close.

    Each pad is a real closed draw with root and crest radii of one gauge
    inside and rounded plan corners, which is why the footprints are 20 mm:
    the radii alone take nearly 6 mm of plan on every side. The second pad,
    7 mm deep, is past a third of its plan width and is the
    `sheet_emboss_deep` bait. The last two sit 0.6 mm apart at the deck,
    and because each root radius starts below the top face the visible web
    between them is under two gauges, so `sheet_formed_pitch` fires on that
    pair alone. Every pad stays 10 mm off the bend, keeping the near-bend
    rule quiet.

    The blank is square on purpose: the bend spans the whole of it, so the
    bend length and the part's largest extent are equal, which is the
    zero-margin case for `sheet_bend_longer_than_body`.
    """
    t = 2.0
    part = _bracket_profile(68.0, 30.0, 68.0)

    def emboss(x0, y0, x1, y1, height, r_plan):
        hood, air = _drawn_pad(x0, y0, x1, y1, r_plan, t, height, t, 2.0 * t, 80.0, 0.4 * t)
        return cut(fuse(part, hood), air)

    part = emboss(15.0, 8.0, 35.0, 28.0, 4.0, 8.0)
    part = emboss(43.0, 8.0, 63.0, 28.0, 7.0, 8.5)
    part = emboss(15.0, 36.0, 35.0, 56.0, 4.0, 8.0)
    return emboss(35.6, 36.0, 55.6, 56.0, 4.0, 8.0)


@fixture("sm_louver_bank")
def build_sm_louver_bank() -> TopoDS_Shape:
    """Two lofted louvers, one under the height ceiling and one over it.

    Both are the symmetric plateau scoop scaled as a whole -- run, plateau,
    run-out width and apex together -- because the proportions are what
    keep the offset surface from folding. The 6 mm hood is the silent
    near-threshold control for `sheet_louver_tall`; the 9 mm one fires it.
    Both start 11 mm from the bend axis and sit far enough apart that the
    near-bend and pitch rules both stay quiet.
    """
    t = 2.0
    part = _bracket_profile(60.0, 30.0, 55.0)

    def louver(ys, apex):
        k = apex / 7.0
        x0 = 16.0
        stations = _plateau_stations(
            x0, x0 + 28.0 * k, apex, 11.0 * k, 4.0 * k, 1.0 / 3.0, 30
        )
        return _place_lofted_louver(part, stations, ys, t)

    part = louver(8.0, 6.0)
    return louver(30.0, 9.0)


@fixture("sm_lance_bridge")
def build_sm_lance_bridge() -> TopoDS_Shape:
    """A bridge lance: a strip sheared on both sides and arched 5 mm up.

    Root and crown radii are both one gauge on the inside, the ramps run at
    55 degrees, and the strip is 12 mm wide over a 30 mm run. At two and a
    half gauges it is under every height threshold. Its ramps are planes
    tangent to cylinder pairs one gauge apart, which is exactly a bend to
    the bend recognizer, so this part censuses as bends rather than as a
    formed lance; that is a property of the engine, not of the part.

    The blank is 60 mm square so the planar and cylindrical share of the
    area stays clear of the sheet classifier's developability gate.
    """
    t = 2.0
    part = _bracket_profile(60.0, 30.0, 60.0)
    hood, air = _lance_bridge_arch(16.0, 46.0, 14.0, 26.0, t, 5.0, t, 2.0 * t, 55.0, 0.4 * t)
    return fuse(cut(part, air), hood)


@fixture("sm_tall_lance")
def build_sm_tall_lance() -> TopoDS_Shape:
    """The same bridge lance raised to 9 mm, four and a half gauges.

    Identical to `sm_lance_bridge` in blank, run, strip and radii, so any
    difference between the two comes from the height alone. The ramps grow
    to reach the extra rise rather than steepening; the 30 mm run still
    leaves a plateau.
    """
    t = 2.0
    part = _bracket_profile(60.0, 30.0, 60.0)
    hood, air = _lance_bridge_arch(16.0, 46.0, 14.0, 26.0, t, 9.0, t, 2.0 * t, 55.0, 0.4 * t)
    return fuse(cut(part, air), hood)


@fixture("sm_curved_bead")
def build_sm_curved_bead() -> TopoDS_Shape:
    """Two half-round stiffening beads, 5 and 8 mm tall, rolled into the deck.

    The only positive coverage of the formed recognizer's cylindrical-crest
    pass. A bead is a closed draw: nothing is sheared, so it has no open
    strip and censuses as an emboss. The crest is the outer cylinder and the
    channel ceiling the coaxial inner one, a gauge apart; the channel is
    open underneath, so there is no capped bore to read as a blind hole.
    The bore stops one gauge short of each end so the bead runs out into
    deck material.

    It is not claimed as a bend because the arch meets the deck square: the
    inner cylinder has no tangent planar neighbour, so the bend seed never
    forms.
    """
    t = 2.0
    part = _bracket_profile(50.0, 30.0, 50.0)

    def bead(x0, x1, yc, outer_r):
        inner_r = outer_r - t

        def cyl(xa, length, radius):
            return cylinder(xa, yc, t, radius, length, (1, 0, 0))

        shell = cut(cyl(x0, x1 - x0, outer_r), cyl(x0 - 1.0, (x1 - x0) + 2.0, inner_r))
        shell = common(
            shell,
            box_between(
                (x0 - 1.0, yc - outer_r - 1.0, 0.0),
                (x1 + 1.0, yc + outer_r + 1.0, t + outer_r + 1.0),
            ),
        )
        air = cyl(x0 + t, (x1 - x0) - 2.0 * t, inner_r)
        return fuse(cut(part, air), shell)

    part = bead(20.0, 40.0, 14.0, 5.0)
    return bead(20.0, 40.0, 34.0, 8.0)


@fixture("sm_e_chassis")
def build_sm_e_chassis() -> TopoDS_Shape:
    """A 1.5 mm electronics chassis: U section, return flanges, front hem.

    One bare bend end, one undersized hole, one hole near a bend, one tight
    hole pitch and one tall louver, each with clean controls beside it. The
    wall bands are built explicitly per edge rather than mirrored, since a
    mirror flips face orientation and breaks the inner-to-outer bend pairing.

    The keyhole circles overlap their slots far enough to cross both slot
    walls, so each keyhole is one clean opening with no sliver faces where
    slot and circle meet. The wall louvers are lofted scoops carried onto
    the vertical wall by a proper rotation, sheared along their low edge;
    the last is the `sheet_louver_tall` bait, its run lengthened with its
    apex so the lip stays open.
    """
    t = 1.5
    outer_r = 3.0
    part = box_between((0, 0, 0), (160, 100, t))

    def wall_band(edge: float, sign: float) -> TopoDS_Shape:
        def place(u, v):
            return gp_Pnt(5, edge + sign * u, v)

        outer_mid = 3.0 - 3.0 / math.sqrt(2.0)
        inner_mid = 3.0 - 1.5 / math.sqrt(2.0)
        return (
            _Profile(place)
            .arc(3, 0, outer_mid, outer_mid, 0, 3)
            .line(0, 3, 0, 37)
            .arc(0, 37, outer_mid, 40.0 - outer_mid, 3, 40)
            .line(3, 40, 15, 40)
            .line(15, 40, 15, 38.5)
            .line(15, 38.5, 3, 38.5)
            .arc(3, 38.5, inner_mid, 40.0 - inner_mid, 1.5, 37)
            .line(1.5, 37, 1.5, 3)
            .arc(1.5, 3, inner_mid, inner_mid, 3, 1.5)
            .line(3, 1.5, 3, 0)
            .prism(150, 0, 0)
        )

    part = fuse(part, wall_band(0, 1))
    part = fuse(part, wall_band(100, -1))
    part = _cut_under_fold(part, (4, -1, -1), (156, 3, t), (4, 3, 3), (1, 0, 0), 152, outer_r)
    part = _cut_under_fold(
        part, (4, 97, -1), (156, 101, t), (4, 97, 3), (1, 0, 0), 152, outer_r
    )

    hem = (
        _Profile(_in_xz(10.0))
        .arc(3, 0, 0, 3, 3, 6)
        .line(3, 6, 11, 6)
        .line(11, 6, 11, 4.5)
        .line(11, 4.5, 3, 4.5)
        .arc(3, 4.5, 1.5, 3, 3, 1.5)
        .line(3, 1.5, 3, 0)
        .prism(0, 80, 0)
    )
    part = fuse(part, hem)
    part = _cut_under_fold(part, (-1, 9, -1), (3, 91, t), (3, 9, 3), (0, 1, 0), 82, outer_r)

    part = cut(part, box_between((-1, 6.5, -1), (4.6, 10, 7)))
    part = cut(part, box_between((-1, 90, -1), (4.6, 95.5, 7)))
    part = cut(part, box_between((155, -1, -1), (161, 4.6, t + 1)))
    part = cut(part, box_between((155, 95.4, -1), (161, 101, t + 1)))
    part = cut(part, box_between((-1, 95.4, -1), (5, 101, t + 1)))

    def hole(x: float, y: float, radius: float) -> TopoDS_Shape:
        return cut(part, cylinder(x, y, -1, radius, t + 2))

    for x, y in ((20, 20), (140, 20), (20, 80), (140, 80)):
        part = hole(x, y, 2.0)
    for x in (40.0, 80.0, 120.0):
        for y in (30.0, 70.0):
            part = hole(x, y, 1.6)
    part = cut(part, box_between((48, 45, -1), (52, 55, t + 1)))
    part = hole(50, 57, 3.5)
    part = cut(part, box_between((108, 45, -1), (112, 55, t + 1)))
    part = hole(110, 43, 3.5)
    part = hole(80, 50, 0.6)
    part = hole(70, 93, 2.0)
    part = hole(95, 25, 2.0)
    part = hole(101.5, 25, 2.0)

    window = box_between((62, -1, 14), (88, 3, 26))
    window = fuse(window, box_between((60, -1, 16), (90, 3, 24)))
    for cx in (62.0, 88.0):
        for cz in (16.0, 24.0):
            window = fuse(window, cylinder(cx, -1, cz, 2.0, 4.0, (0, 1, 0)))
    part = cut(part, window)
    part = cut(
        part,
        _polygon_prism([(110, -1, 16), (130, -1, 16), (127, -1, 24), (113, -1, 24)], 0, 4, 0),
    )

    # The loft helper builds a hood rising +Z from a shear line at y = 0. A
    # half turn about (0, 1, 1) then a shift maps local (x, y, z) onto world
    # (x1 - x, 98.5 + z, z0 + y): out of the deck becomes out of the wall,
    # and along the run toward the hinge becomes up the wall.
    def wall_louver(x1, z0, length, run, apex):
        count = 30
        w_min = max(run * (4.0 / 11.0), 2.0 * t)
        stations = []
        for index in range(count + 1):
            u = index / count
            if u < 1.0 / 3.0:
                s = u / (1.0 / 3.0)
            elif u > 1.0 - 1.0 / 3.0:
                s = (1.0 - u) / (1.0 / 3.0)
            else:
                s = 1.0
            f = math.sin(0.5 * math.pi * min(s, 1.0))
            stations.append((u * length, w_min + (run - w_min) * f, apex * f))
        hood, air = _lofted_hood(stations, 0.0, t, 1.0)

        rot = gp_Trsf()
        rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 1)), math.pi)
        mov = gp_Trsf()
        mov.SetTranslation(gp_Vec(x1, 100.0 - t, z0))
        placement = mov.Multiplied(rot)
        hood = _transformed(hood, placement, False)
        air = _transformed(air, placement, False)
        return fuse(cut(part, air), hood)

    for x in (20.0, 50.0, 80.0, 110.0):
        part = wall_louver(x + 20, 12, 20, 8, 4.0)
    return wall_louver(154, 12, 20, 10, 6.5)


# -- the fold census ----------------------------------------------------------


@fixture("sm_fold_conical")
def build_sm_fold_conical() -> TopoDS_Shape:
    """A tapered flange: the corpus's pure conical fold.

    Inner radius 3 mm at one end of the fold line and 6 mm at the other, on
    2 mm gauge, a 6 degree cone. It is not a variable fillet, which may come
    back as a B-spline: one trapezoid between the inner and outer tangent
    lines is revolved a quarter turn for the band -- a line skew to the axis
    revolves to an exact cone -- and prismed for each panel, so the panels
    are exactly tangent and join the band face on face, with no cone cut
    anywhere. The panels' normals are 89.37 degrees apart, not 90; the
    shortfall is the taper. This is the witness for the bend recognizer's
    conical family.
    """
    t = 2.0
    alpha = math.radians(6.0)
    ta, ca = math.tan(alpha), math.cos(alpha)
    s_lo = 3.0 / ta
    s_hi = 6.0 / ta
    width = 25.0

    quad = _quad(
        (
            (s_lo, s_lo * ta, 0.0),
            (s_hi, s_hi * ta, 0.0),
            (s_hi, s_hi * ta + t / ca, 0.0),
            (s_lo, s_lo * ta + t / ca, 0.0),
        )
    )
    axis = gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(1.0, 0.0, 0.0))
    band = BRepPrimAPI_MakeRevol(quad, axis, math.pi / 2.0).Shape()
    panel_a = BRepPrimAPI_MakePrism(quad, gp_Vec(0.0, 0.0, -width)).Shape()
    r90 = gp_Trsf()
    r90.SetRotation(axis, math.pi / 2.0)
    panel_b = BRepPrimAPI_MakePrism(
        _transformed(quad, r90), gp_Vec(0.0, -width, 0.0)
    ).Shape()
    return fuse(fuse(band, panel_a), panel_b)


@fixture("sm_fold_rolled")
def build_sm_fold_rolled() -> TopoDS_Shape:
    """A rolled scroll lip off the edge of a flat deck.

    Three arcs of alternating sense, each tangent to the next, keep the
    gauge at 2 mm: r12 off the deck, r3 the other way, then r8 out to a
    radial free edge. The middle arc has two cylindrical neighbours and no
    planar one -- the rolled family's defining case -- the first is the mixed
    case with the deck on one side, and the last is a run-out whose only
    other neighbour is the blank's cut edge. All three are rolled folds and
    none can be unfolded as a flat-panel bend. The deck is there because a
    part with no planar skin pair would never classify as sheet at all.
    """
    p = _FoldProfile()
    p.seg(0.0, 0.0, 50.0, 0.0)
    p.arc_to(50.0, 0.0, 50.0, 12.0, 62.0, 12.0)
    p.arc_to(62.0, 12.0, 65.0, 12.0, 65.0, 15.0)
    p.arc_to(65.0, 15.0, 65.0, 23.0, 73.0, 23.0)
    p.seg(73.0, 23.0, 71.0, 23.0)
    p.arc_to(71.0, 23.0, 65.0, 23.0, 65.0, 17.0)
    p.arc_to(65.0, 17.0, 65.0, 12.0, 60.0, 12.0)
    p.arc_to(60.0, 12.0, 50.0, 12.0, 50.0, 2.0)
    p.seg(50.0, 2.0, 0.0, 2.0)
    p.seg(0.0, 2.0, 0.0, 0.0)
    return p.prism(40.0)


@fixture("sm_fold_curl_225")
def build_sm_fold_curl_225() -> TopoDS_Shape:
    """A 225 degree return curl, inner r5 on 2 mm gauge.

    Past 180 degrees the angle between the two panels' normals folds back
    on itself, so it reads 135 while the band itself sweeps 225. That
    disagreement is what `BEND_ANGLE_INCONSISTENT` exists to catch, and no
    fold under 180 degrees can produce it honestly. The band is one
    cylindrical face per skin, drawn the long way round. The base leg is
    held to 10 mm because the return leg passes underneath it.
    """
    t, r_in, r_out = 2.0, 5.0, 7.0
    base_leg, return_leg, depth = 10.0, 25.0, 30.0
    u = math.radians(225.0)
    cu, su = math.cos(u), math.sin(u)
    bx, bz = r_in * cu, r_in * su
    tx, tz = -su, cu

    p = _FoldProfile()
    p.seg(r_in, -base_leg, r_in, 0.0)
    p.arc_to(r_in, 0.0, 0.0, 0.0, bx, bz, True)
    p.seg(bx, bz, bx + return_leg * tx, bz + return_leg * tz)
    p.seg(
        bx + return_leg * tx,
        bz + return_leg * tz,
        bx + return_leg * tx + t * cu,
        bz + return_leg * tz + t * su,
    )
    p.seg(
        bx + return_leg * tx + t * cu,
        bz + return_leg * tz + t * su,
        r_out * cu,
        r_out * su,
    )
    p.arc_to(r_out * cu, r_out * su, 0.0, 0.0, r_out, 0.0, True)
    p.seg(r_out, 0.0, r_out, -base_leg)
    p.seg(r_out, -base_leg, r_in, -base_leg)
    return p.prism(depth)


@fixture("sm_fold_u_channel")
def build_sm_fold_u_channel() -> TopoDS_Shape:
    """A U channel: two 90 degree folds, both turned the same way.

    One half of the fold-direction pair. Gauge 2, radii 3 inside and 5
    outside, a 50 mm base between tangents and 17 mm of straight flange past
    each bend. Its twin `sm_fold_z_bracket` differs only in the sense of the
    left fold, which is what shows that a fold's signed angle is relative to
    its own frame and only the fold-toward direction compares across folds.
    """
    p = _FoldProfile()
    p.seg(0.0, 22.0, 0.0, 5.0)
    p.arc_to(0.0, 5.0, 5.0, 5.0, 5.0, 0.0)
    p.seg(5.0, 0.0, 55.0, 0.0)
    p.arc_to(55.0, 0.0, 55.0, 5.0, 60.0, 5.0)
    p.seg(60.0, 5.0, 60.0, 22.0)
    p.seg(60.0, 22.0, 58.0, 22.0)
    p.seg(58.0, 22.0, 58.0, 5.0)
    p.arc_to(58.0, 5.0, 55.0, 5.0, 55.0, 2.0)
    p.seg(55.0, 2.0, 5.0, 2.0)
    p.arc_to(5.0, 2.0, 5.0, 5.0, 2.0, 5.0)
    p.seg(2.0, 5.0, 2.0, 22.0)
    p.seg(2.0, 22.0, 0.0, 22.0)
    return p.prism(40.0)


@fixture("sm_fold_z_bracket")
def build_sm_fold_z_bracket() -> TopoDS_Shape:
    """The U channel with its left fold turned down instead of up.

    Everything right of the left bend is the same as `sm_fold_u_channel`.
    The left fold's concave-side panel becomes the deck's bottom skin
    instead of its top, which flips the reference frame its signed angle is
    measured in.
    """
    p = _FoldProfile()
    p.seg(0.0, -20.0, 0.0, -3.0)
    p.arc_to(0.0, -3.0, 5.0, -3.0, 5.0, 2.0)
    p.seg(5.0, 2.0, 55.0, 2.0)
    p.arc_to(55.0, 2.0, 55.0, 5.0, 58.0, 5.0)
    p.seg(58.0, 5.0, 58.0, 22.0)
    p.seg(58.0, 22.0, 60.0, 22.0)
    p.seg(60.0, 22.0, 60.0, 5.0)
    p.arc_to(60.0, 5.0, 55.0, 5.0, 55.0, 0.0)
    p.seg(55.0, 0.0, 5.0, 0.0)
    p.arc_to(5.0, 0.0, 5.0, -3.0, 2.0, -3.0)
    p.seg(2.0, -3.0, 2.0, -20.0)
    p.seg(2.0, -20.0, 0.0, -20.0)
    return p.prism(40.0)


@fixture("sm_fold_conical_and_brake")
def build_sm_fold_conical_and_brake() -> TopoDS_Shape:
    """A channel with one flange tapered and the other a press-brake fold.

    Two 90 degree folds, and `sheet_bend_radius_small` must fire on exactly
    one: the 1.5 mm press-brake radius. The conical fold has no single
    inner radius, so the rule declines it by family before any number is
    read.

    The cone is placed so that one of its tangent planes is exactly z = 0,
    so the deck is an ordinary slab. Its seed section is a true rectangle
    one gauge thick -- its short sides run along the surface normal rather
    than square to the axis -- which makes the band's end caps shallow cones
    instead of planes but gives the deck an exact face to join. The deck and
    the straight flange are one swept profile whose end face is that seed
    rectangle exactly, so every join is face on face and no cone is ever
    cut. A deck overrunning the band would leave a sliver of seam face that
    reads as a machined step and vetoes the sheet classification.
    """
    t = 2.0
    alpha = math.radians(6.0)
    sa, ca = math.sin(alpha), math.cos(alpha)
    lam_lo = 3.0 / sa
    lam_hi = 6.0 / sa
    fold_len = lam_hi - lam_lo
    deck_w = 25.0
    taper_len = 25.0
    ri = 1.5
    ro = ri + t
    bu = -(deck_w - ro)
    tip = 18.0

    quad = _quad(
        (
            (lam_lo, 0.0, 0.0),
            (lam_hi, 0.0, 0.0),
            (lam_hi, 0.0, -t),
            (lam_lo, 0.0, -t),
        )
    )
    axis = gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(ca, 0.0, sa))
    band = BRepPrimAPI_MakeRevol(quad, axis, math.pi / 2.0).Shape()

    p = _FoldProfile()
    p.seg(0.0, 0.0, bu, 0.0)
    p.arc_to(bu, 0.0, bu, ri, bu - ri, ri)
    p.seg(bu - ri, ri, bu - ri, tip)
    p.seg(bu - ri, tip, bu - ro, tip)
    p.seg(bu - ro, tip, bu - ro, ri)
    p.arc_to(bu - ro, ri, bu, ri, bu, -t)
    p.seg(bu, -t, 0.0, -t)
    p.seg(0.0, -t, 0.0, 0.0)
    spin = gp_Trsf()
    spin.SetRotation(gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), math.pi / 2.0)
    shift = gp_Trsf()
    shift.SetTranslation(gp_Vec(lam_hi, 0.0, 0.0))
    deck = _transformed(_transformed(p.prism(fold_len), spin), shift)

    r90 = gp_Trsf()
    r90.SetRotation(axis, math.pi / 2.0)
    taper_flange = BRepPrimAPI_MakePrism(
        _transformed(quad, r90), gp_Vec(-sa * taper_len, 0.0, ca * taper_len)
    ).Shape()

    return fuse(fuse(deck, band), taper_flange)


@fixture("sm_fold_rolled_and_brake")
def build_sm_fold_rolled_and_brake() -> TopoDS_Shape:
    """A scroll lip on one edge of a deck and a press-brake flange on the other.

    The scroll is `sm_fold_rolled`'s with its middle arc re-centred to a
    1.5 mm inner radius, the same as the press-brake fold's, so the two
    folds share radius and gauge and `sheet_bend_radius_small` must still
    fire only on the press-brake one: a V-die floor says nothing about a
    section formed on rolls. Moving that centre moves the next one with it,
    so tangency and the 2 mm gauge are kept exactly. The press-brake fold is
    the only fold on the part with two planar panels, so the panel graph has
    one real link beside three rolled folds it cannot unfold.
    """
    ri = 1.5
    ro = ri + 2.0
    tip = 20.0

    p = _FoldProfile()
    p.seg(0.0, tip, 0.0, ro)
    p.arc_to(0.0, ro, ro, ro, ro, 0.0)
    p.seg(ro, 0.0, 50.0, 0.0)
    p.arc_to(50.0, 0.0, 50.0, 12.0, 62.0, 12.0)
    p.arc_to(62.0, 12.0, 63.5, 12.0, 63.5, 13.5)
    p.arc_to(63.5, 13.5, 63.5, 21.5, 71.5, 21.5)
    p.seg(71.5, 21.5, 69.5, 21.5)
    p.arc_to(69.5, 21.5, 63.5, 21.5, 63.5, 15.5)
    p.arc_to(63.5, 15.5, 63.5, 12.0, 60.0, 12.0)
    p.arc_to(60.0, 12.0, 50.0, 12.0, 50.0, 2.0)
    p.seg(50.0, 2.0, ro, 2.0)
    p.arc_to(ro, 2.0, ro, ro, 2.0, ro)
    p.seg(2.0, ro, 2.0, tip)
    p.seg(2.0, tip, 0.0, tip)
    return p.prism(40.0)
