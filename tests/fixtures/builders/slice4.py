# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The tail of the corpus: thresholds, torture blocks and formed sheet.

Four kinds of part live here, and they are here together because they are
what the alphabet left over rather than because they resemble each other.

The `threshold_*` pairs are the smallest parts in the set and the ones that
earn their keep most often. Each is one half of a pair sitting a hair either
side of a published limit -- 9.9 against 10.1 diameters of hole depth, 5.9
against 6.1 of pocket depth, a 0.9 mm web against a 0.7 mm one, 64.8%
stock removal against 71.3%. On their own they say nothing; as a pair they
pin a rule to a number. Reword the rule so it fires a shade earlier or later
and one half of every pair changes verdict, which is exactly the change that
is otherwise invisible. Their dimensions are therefore not adjustable.

The `torture_*` blocks are the opposite temperament: one part carrying every
bait a recognizer family can be fed, so a regression anywhere in the family
shows up on a single fixture. The production parts -- housings, disks,
flanges, a handpiece body -- are the realism check, cast and machined
geometry with the awkward details left in. And the `sm_*` parts are formed
sheet: constant-gauge folds with deliberate relief, notch, tab and louver
baits punched into them.
"""

from __future__ import annotations

import math

from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeRevol,
    BRepPrimAPI_MakeTorus,
)
from OCP.Convert import Convert_ParameterisationType
from OCP.GC import GC_MakeArcOfCircle
from OCP.GeomAPI import GeomAPI_PointsToBSpline
from OCP.GeomConvert import GeomConvert
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopoDS import TopoDS_Shape

from . import fixture
from .shapes import (
    box,
    box_between,
    common,
    cone,
    cut,
    cylinder,
    fillet_edges,
    fuse,
    moved,
    polygon_prism,
    revolved_profile,
    rotated,
    rounded_rect_prism,
    sphere,
)


# -- private vocabulary -------------------------------------------------------
#
# Everything below is local to this module. The shared vocabulary in
# `shapes.py` stays as it is; these are the few extra moves this group of
# parts happens to need.


#: Half the 118 degree included angle a twist drill leaves, as the tangent
#: used to turn a drill radius into the height of its point cone.
_DRILL_POINT = math.tan(math.radians(59.0))


def _drilled_blind(
    x: float,
    y: float,
    z: float,
    direction,
    radius: float,
    depth: float,
) -> TopoDS_Shape:
    """A blind drill: the bore plus the 118 degree point cone below it.

    A cylinder alone leaves a flat floor, which is what the flat-bottom rule
    is looking for. Real tapped and balance holes are twist-drilled and keep
    the point, so the fixtures that must not fire that rule cut this instead.
    """
    bore = cylinder(x, y, z, radius, depth, direction)
    tip = cone(
        x + direction[0] * depth,
        y + direction[1] * depth,
        z + direction[2] * depth,
        radius,
        0.0,
        radius / _DRILL_POINT,
        direction,
    )
    return fuse(bore, tip)


def _torus_segment(
    x: float,
    y: float,
    z: float,
    major: float,
    minor: float,
    angle1: float,
    angle2: float,
) -> TopoDS_Shape:
    """A torus limited to a range of angles, which `shapes.torus` will not do."""
    axis = gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1))
    return BRepPrimAPI_MakeTorus(axis, major, minor, angle1, angle2).Shape()


def _bspline_through(points) -> TopoDS_Shape:
    """An edge on the B-spline fitted through the given XZ points."""
    array = TColgp_Array1OfPnt(1, len(points))
    for index, (radius, height) in enumerate(points, start=1):
        array.SetValue(index, gp_Pnt(radius, 0.0, height))
    curve = GeomAPI_PointsToBSpline(array).Curve()
    return BRepBuilderAPI_MakeEdge(curve).Edge()


def _arc_edge(a, m, b) -> TopoDS_Shape:
    """An edge on the circular arc through three XZ points."""
    curve = GC_MakeArcOfCircle(
        gp_Pnt(a[0], 0.0, a[1]), gp_Pnt(m[0], 0.0, m[1]), gp_Pnt(b[0], 0.0, b[1])
    ).Value()
    return BRepBuilderAPI_MakeEdge(curve).Edge()


def _arc_as_bspline(a, m, b) -> TopoDS_Shape:
    """The same arc, but converted to a B-spline first.

    A turned valley modelled as a plain arc is read as a fillet; converted to
    a spline it stays a freeform profile, which is the geometry the turned
    profile-radius rule was written against.
    """
    arc = GC_MakeArcOfCircle(
        gp_Pnt(a[0], 0.0, a[1]), gp_Pnt(m[0], 0.0, m[1]), gp_Pnt(b[0], 0.0, b[1])
    ).Value()
    spline = GeomConvert.CurveToBSplineCurve_s(
        arc, Convert_ParameterisationType.Convert_RationalC1
    )
    return BRepBuilderAPI_MakeEdge(spline).Edge()


def _line_edge(a, b) -> TopoDS_Shape:
    """A straight edge between two XZ points."""
    return BRepBuilderAPI_MakeEdge(
        gp_Pnt(a[0], 0.0, a[1]), gp_Pnt(b[0], 0.0, b[1])
    ).Edge()


def _line_edge_3d(a, b) -> TopoDS_Shape:
    """A straight edge between two points anywhere in space."""
    return BRepBuilderAPI_MakeEdge(gp_Pnt(*a), gp_Pnt(*b)).Edge()


def _arc_edge_3d(a, m, b) -> TopoDS_Shape:
    """An arc through three points anywhere in space."""
    curve = GC_MakeArcOfCircle(gp_Pnt(*a), gp_Pnt(*m), gp_Pnt(*b)).Value()
    return BRepBuilderAPI_MakeEdge(curve).Edge()


def _wire_of(edges):
    """One wire out of a chain of edges."""
    wire = BRepBuilderAPI_MakeWire()
    for edge in edges:
        wire.Add(edge)
    return wire.Wire()


def _polygon_wire(points):
    """A closed wire through arbitrary points in space."""
    polygon = BRepBuilderAPI_MakePolygon()
    for point in points:
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    return polygon.Wire()


def _rect_wire(cx: float, cy: float, dx: float, dy: float, z: float):
    """A rectangle centred in plan, for lofting between."""
    polygon = BRepBuilderAPI_MakePolygon()
    polygon.Add(gp_Pnt(cx - dx / 2, cy - dy / 2, z))
    polygon.Add(gp_Pnt(cx + dx / 2, cy - dy / 2, z))
    polygon.Add(gp_Pnt(cx + dx / 2, cy + dy / 2, z))
    polygon.Add(gp_Pnt(cx - dx / 2, cy + dy / 2, z))
    polygon.Close()
    return polygon.Wire()


def _loft(wires, ruled: bool = True) -> TopoDS_Shape:
    """A solid lofted through a list of wires -- how draft angle is modelled."""
    lofter = BRepOffsetAPI_ThruSections(True, ruled)
    for wire in wires:
        lofter.AddWire(wire)
    lofter.Build()
    return lofter.Shape()


def _prism_wire(wire, dx: float, dy: float, dz: float) -> TopoDS_Shape:
    """A closed wire swept along a vector."""
    face = BRepBuilderAPI_MakeFace(wire).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(dx, dy, dz)).Shape()


def _revolve_wire(edges) -> TopoDS_Shape:
    """A solid of revolution about Z from a closed chain of XZ edges.

    `shapes.revolved_profile` takes a polygon; these profiles carry spline
    and arc edges, so they are assembled edge by edge instead.
    """
    face = BRepBuilderAPI_MakeFace(_wire_of(edges)).Face()
    return BRepPrimAPI_MakeRevol(
        face, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    ).Shape()


# -- sheet-metal helpers ------------------------------------------------------


def _bracket_profile(leg_x: float, leg_z: float, depth_y: float) -> TopoDS_Shape:
    """The shared L-bracket blank the sheet fixtures punch their baits into.

    Constant 2 mm gauge, inner bend radius 3, outer 5, swept along +Y. The
    arc midpoints are the exact 45 degree points: an approximate midpoint
    makes OpenCascade fit a slightly non-circular arc, and the gauge
    detection then reads a thickness that varies around the bend.
    """
    wire = BRepBuilderAPI_MakeWire()
    wire.Add(_line_edge((leg_x, 0), (5, 0)))
    wire.Add(_arc_edge((5, 0), (1.4645, 1.4645), (0, 5)))
    wire.Add(_line_edge((0, 5), (0, leg_z)))
    wire.Add(_line_edge((0, leg_z), (2, leg_z)))
    wire.Add(_line_edge((2, leg_z), (2, 5)))
    wire.Add(_arc_edge((2, 5), (2.8787, 2.8787), (5, 2)))
    wire.Add(_line_edge((5, 2), (leg_x, 2)))
    wire.Add(_line_edge((leg_x, 2), (leg_x, 0)))
    return _prism_wire(wire.Wire(), 0.0, depth_y, 0.0)


def _cut_under_fold(
    part: TopoDS_Shape,
    lo,
    hi,
    axis_point,
    axis_direction,
    axis_length: float,
    outer_radius: float = 5.0,
) -> TopoDS_Shape:
    """Clear the flat plate that would otherwise run on under a fold.

    Fusing a full-size base plate with a flange band leaves the bent and the
    unbent part coexisting: material continues flat underneath the fold as
    well as turning up into it. Removing the under-fold box minus the outer
    bend cylinder leaves the bend arcs and the fold root untouched, and
    leaves the flat material beyond the band's ends -- which is precisely
    what the relief rules probe -- alone.
    """
    tool = cut(
        box_between(lo, hi),
        cylinder(
            axis_point[0],
            axis_point[1],
            axis_point[2],
            outer_radius,
            axis_length,
            axis_direction,
        ),
    )
    return cut(part, tool)


# -- threshold pairs ----------------------------------------------------------


@fixture("threshold_hole_deep_9_9x")
def build_threshold_hole_deep_9_9x() -> TopoDS_Shape:
    """A 10 mm drill 99 mm deep: 9.9 diameters, a shade under the limit."""
    return cut(box(0, 0, 0, 80.0, 80.0, 99.0), cylinder(40, 40, -1, 5.0, 101.0))


@fixture("threshold_hole_deep_10_1x")
def build_threshold_hole_deep_10_1x() -> TopoDS_Shape:
    """The same drill 101 mm deep: 10.1 diameters, a shade over it."""
    return cut(box(0, 0, 0, 80.0, 80.0, 101.0), cylinder(40, 40, -1, 5.0, 103.0))


@fixture("threshold_pocket_5_9x")
def build_threshold_pocket_5_9x() -> TopoDS_Shape:
    """A 10 mm pocket 59 mm deep -- 5.9 widths, under the 6x limit."""
    return cut(box(0, 0, 0, 50.0, 50.0, 70.0), box(20, 20, 11, 10.0, 10.0, 60.0))


@fixture("threshold_pocket_6_1x")
def build_threshold_pocket_6_1x() -> TopoDS_Shape:
    """The same pocket 61 mm deep -- 6.1 widths, over it."""
    return cut(box(0, 0, 0, 50.0, 50.0, 72.0), box(20, 20, 11, 10.0, 10.0, 62.0))


@fixture("threshold_wall_0_9mm")
def build_threshold_wall_0_9mm() -> TopoDS_Shape:
    """Two 10 mm bores 10.9 mm apart, leaving a 0.9 mm web -- above 0.8."""
    plate = cut(box(0, 0, 0, 60.0, 50.0, 25.0), cylinder(20, 25, -1, 5.0, 27.0))
    return cut(plate, cylinder(30.9, 25, -1, 5.0, 27.0))


@fixture("threshold_wall_0_7mm")
def build_threshold_wall_0_7mm() -> TopoDS_Shape:
    """The same pair 10.7 mm apart -- a 0.7 mm web, below 0.8."""
    plate = cut(box(0, 0, 0, 60.0, 50.0, 25.0), cylinder(20, 25, -1, 5.0, 27.0))
    return cut(plate, cylinder(30.7, 25, -1, 5.0, 27.0))


@fixture("threshold_removal_69pct")
def build_threshold_removal_69pct() -> TopoDS_Shape:
    """A 90 x 90 x 8 pocket out of a 100 x 100 x 10 plate: 64.8% removed."""
    return cut(box(0, 0, 0, 100.0, 100.0, 10.0), box(5, 5, 2, 90.0, 90.0, 9.0))


@fixture("threshold_removal_71pct")
def build_threshold_removal_71pct() -> TopoDS_Shape:
    """The same pocket 0.8 mm deeper: 71.3% removed, over the 70% mark."""
    return cut(box(0, 0, 0, 100.0, 100.0, 10.0), box(5, 5, 1.2, 90.0, 90.0, 9.0))


# -- threads, near walls and near shoulders -----------------------------------


@fixture("thread_m6_approx")
def build_thread_m6_approx() -> TopoDS_Shape:
    """A blind M6 tap hole as its tap-drill: STEP carries no helix geometry.

    Everything that reasons about threads in the corpus reasons about the
    5 mm hole a tap goes into, so that is what the fixture carries.
    """
    return cut(box(0, 0, 0, 30.0, 30.0, 25.0), cylinder(15, 15, 7, 2.5, 20.0))


@fixture("thread_shoulder_proximity")
def build_thread_shoulder_proximity() -> TopoDS_Shape:
    """A tap 1 mm from the edge of a shoulder -- too close for the tap to clear."""
    stepped = cut(box(0, 0, 0, 60.0, 60.0, 25.0), box(15, 15, 17, 30.0, 30.0, 10.0))
    return cut(stepped, cylinder(18.5, 30, 5, 2.5, 14.0))


@fixture("thread_wall_thickness")
def build_thread_wall_thickness() -> TopoDS_Shape:
    """A tap leaving 1 mm of material to the outside face, which will blow out."""
    return cut(box(0, 0, 0, 30.0, 30.0, 25.0), cylinder(3.5, 15, 5, 2.5, 22.0))


# -- reach, access and setups -------------------------------------------------


@fixture("undercut_part")
def build_undercut_part() -> TopoDS_Shape:
    """A narrow slot over a wider one: the lower slot cannot be reached from +Z."""
    narrow = box(17.5, 10, 20, 15.0, 30.0, 15.0)
    wide = box(10, 10, 10, 30.0, 30.0, 10.0)
    return cut(box(0, 0, 0, 50.0, 50.0, 30.0), narrow, wide)


@fixture("tool_access_blocked")
def build_tool_access_blocked() -> TopoDS_Shape:
    """A sealed internal cavity, reachable from no direction at all.

    Built as two halves so the pocket is cut on an open face and then
    capped: cutting a fully interior void from one solid is a boolean
    no-op, because the tool never meets a boundary.
    """
    bottom = cut(box(0, 0, 0, 60.0, 60.0, 20.0), box(15, 15, 5, 30.0, 30.0, 17.0))
    return fuse(bottom, box(0, 0, 20, 60.0, 60.0, 20.0))


@fixture("tool_access_special_setup")
def build_tool_access_special_setup() -> TopoDS_Shape:
    """One hole 30 degrees off +Z: a cardinal setup will not reach it."""
    drill = cylinder(30, 20, -10, 4.0, 50.0)
    return cut(
        box(0, 0, 0, 60.0, 40.0, 30.0),
        rotated(drill, (30, 20, 15), (0, 1, 0), 30.0),
    )


@fixture("tilted_hole_block")
def build_tilted_hole_block() -> TopoDS_Shape:
    """Three cardinal features plus one tilted hole -- a 3+1 job, not a 5-axis one.

    It carried the name five_axis_candidate for a while and did not deserve
    it: one tilted feature alongside cardinal ones indexes off a rotary at
    worst.
    """
    block = box(0, 0, 0, 60.0, 60.0, 40.0)
    tilted = rotated(cylinder(15, 30, -10, 5.0, 60.0), (15, 30, 20), (0, 1, 0), 30.0)
    block = cut(block, tilted)
    block = cut(block, cylinder(-1, 45, 30, 4.0, 20.0, (1, 0, 0)))
    block = cut(block, cylinder(45, 61, 30, 4.0, 20.0, (0, -1, 0)))
    return cut(block, box(35, 5, 32, 20.0, 20.0, 10.0))


@fixture("valve_body_mini")
def build_valve_body_mini() -> TopoDS_Shape:
    """Three orthogonal cross-bores that all meet, plus four corner taps."""
    cube = box(0, 0, 0, 50.0, 50.0, 40.0)
    cube = cut(cube, cylinder(-1, 25, 20, 10.0, 52.0, (1, 0, 0)))
    cube = cut(cube, cylinder(25, -1, 20, 6.0, 52.0, (0, 1, 0)))
    cube = cut(cube, cylinder(25, 25, 20, 4.0, 21.0))
    for x, y in ((8.0, 8.0), (42.0, 8.0), (8.0, 42.0), (42.0, 42.0)):
        cube = cut(cube, cylinder(x, y, 28, 2.5, 13.0))
    return cube


# -- plates, panels and shafts ------------------------------------------------


@fixture("structural_rib_panel")
def build_structural_rib_panel() -> TopoDS_Shape:
    """A thin plate stiffened by two crossing 2 mm ribs."""
    plate = box(0, 0, 0, 100.0, 80.0, 5.0)
    plate = fuse(plate, box(0, 39, 5, 100.0, 2.0, 12.0))
    return fuse(plate, box(49, 0, 5, 2.0, 80.0, 12.0))


@fixture("spline_shaft_blank")
def build_spline_shaft_blank() -> TopoDS_Shape:
    """A turned shaft with a diameter step, a keyway and an end tap."""
    shaft = cylinder(0, 0, 0, 15.0, 100.0)
    # The step is taken off as a ring rather than turned, so the shoulder
    # lands exactly at z=31 with no sliver left on the larger diameter.
    step_ring = cut(cylinder(0, 0, -1, 15.5, 32.0), cylinder(0, 0, -1, 10.0, 32.0))
    shaft = cut(shaft, step_ring)
    shaft = cut(shaft, box(11, -3, 40, 6.0, 6.0, 40.0))
    return cut(shaft, cylinder(0, 0, 85, 3.4, 16.0))


@fixture("tslot_carrier_plate")
def build_tslot_carrier_plate() -> TopoDS_Shape:
    """A through window and two open edge notches, offset so they cannot pair.

    The window's depth must read as the plate thickness, and the two notches
    must stay two notches rather than merging into one cavity spanning the
    outer silhouette.
    """
    plate = box_between((0, 0, 0), (100, 70, 10))
    plate = cut(plate, rounded_rect_prism(30.0, 25.0, 70.0, 45.0, 6.0, -0.1, 10.2))
    plate = cut(plate, box_between((10, -0.1, -0.1), (30, 12, 10.1)))
    return cut(plate, box_between((76, 58, -0.1), (96, 70.1, 10.1)))


@fixture("tap_drill_index_plate")
def build_tap_drill_index_plate() -> TopoDS_Shape:
    """Five ISO-coarse tap-drill sizes and one that is genuinely off-catalog.

    3.3, 4.2, 5.0, 6.8 and 10.2 are all stock tap drills and must stay
    silent. The 7.27 is the positive control: 7.3 would be an ordinary
    0.1 mm-increment drill, 7.27 is a reamer or a special.
    """
    plate = box_between((0, 0, 0), (90, 40, 15))
    for x, diameter in (
        (10.0, 3.3),
        (22.0, 4.2),
        (34.0, 5.0),
        (47.0, 6.8),
        (62.0, 10.2),
        (78.0, 7.27),
    ):
        radius = diameter * 0.5
        tip = radius / _DRILL_POINT
        tool = fuse(
            cylinder(x, 20, 5, radius, 11.0),
            cone(x, 20, 5 - tip, 0.0, radius, tip),
        )
        plate = cut(plate, tool)
    return plate


@fixture("wr90_waveguide_flange")
def build_wr90_waveguide_flange() -> TopoDS_Shape:
    """A WR-90 waveguide flange, at the real standard's uncomfortable margins.

    The socket counterbore's corner radius is the aperture radius plus the
    tube wall, so the two arcs are concentric and the solder shelf stays a
    constant width right through the corners. The bolt holes sit 1.83 mm
    from the edge; that is what the standard asks for, and the edge-distance
    warnings it draws are honest rather than a modelling slip.
    """
    flange = box_between((-20.7, -20.7, 0.0), (20.7, 20.7, 6.35))
    flange = cut(
        flange, rounded_rect_prism(-11.43, -5.08, 11.43, 5.08, 0.8, -0.1, 6.55)
    )
    flange = cut(
        flange, rounded_rect_prism(-12.7, -6.35, 12.7, 6.35, 2.07, -0.1, 3.1)
    )
    for sx in (-1, 1):
        for sy in (-1, 1):
            flange = cut(flange, cylinder(sx * 16.67, sy * 16.67, -0.1, 2.2, 6.55))
    for sy in (-1, 1):
        flange = cut(flange, cylinder(0.0, sy * 14.5, -0.1, 1.195, 6.55))
    return flange


# -- turned and revolved bodies -----------------------------------------------


@fixture("turned_valley_tight_radius")
def build_turned_valley_tight_radius() -> TopoDS_Shape:
    """A turned profile with a 0.2 mm concave valley.

    Below the nose radius of any turning insert that would be run on this
    part, which is the whole point: it is the one fixture that puts the
    turned profile-radius rule on real revolved geometry rather than a
    milled stand-in.
    """
    valley_z, valley_r = 22.0, 10.0
    lower = _bspline_through(
        [(11.0, 0.0), (12.0, 8.0), (12.5, 15.0), (valley_r, valley_z - 0.15)]
    )
    valley = _arc_as_bspline(
        (valley_r, valley_z - 0.15),
        (valley_r - 0.2, valley_z),
        (valley_r, valley_z + 0.15),
    )
    upper = _bspline_through(
        [(valley_r, valley_z + 0.15), (11.5, 30.0), (10.0, 38.0), (9.0, 44.0)]
    )
    return _revolve_wire(
        [
            _line_edge((0, 0), (11.0, 0)),
            lower,
            valley,
            upper,
            _line_edge((9.0, 44.0), (0, 44.0)),
            _line_edge((0, 44.0), (0, 0)),
        ]
    )


@fixture("turned_freeform_handle")
def build_turned_freeform_handle() -> TopoDS_Shape:
    """A control knob whose ergonomic waist is a revolved B-spline.

    Most of the part's area is a surface of revolution, so an honest
    classifier calls it turned. The baseline deliberately locks in the wrong
    label until the process classifier learns to read revolved surfaces, at
    which point this fixture is the one that flips.
    """
    waist = _bspline_through([(24.0, 8.0), (19.0, 18.0), (22.0, 34.0), (14.0, 52.0)])
    handle = _revolve_wire(
        [
            _line_edge((0, 0), (24, 0)),
            _line_edge((24, 0), (24, 8)),
            waist,
            _line_edge((14, 52), (0, 52)),
            _line_edge((0, 52), (0, 0)),
        ]
    )
    handle = cut(handle, cylinder(0, 0, -0.1, 5.0, 20.1))
    # 6.6 across is M8 clearance and not a tap-drill size, so the thread
    # heuristic has nothing to say about the cross hole.
    return cut(handle, cylinder(0, -30.0, 30.0, 3.3, 60.0, (0, 1, 0)))


@fixture("surgical_handpiece_body")
def build_surgical_handpiece_body() -> TopoDS_Shape:
    """A slender mill-turn handpiece: stepped body, taper seat, live-tool slot.

    The knurled grip is modelled as plain cylinder -- surface texture is not
    something a B-rep model of this kind carries, and no rule reads it.
    """
    body = fuse(
        cylinder(0, 0, 0, 7.0, 50.0),
        cylinder(0, 0, 50, 6.5, 50.0),
        cylinder(0, 0, 100, 5.5, 10.0),
    )
    body = cut(body, cylinder(0, 0, -1, 1.75, 112.0))
    body = cut(body, cylinder(0, 0, 80, 2.5, 15.0))
    body = cut(body, cone(0, 0, 95, 2.75, 3.0, 15.0))
    return cut(body, box(4.5, -0.75, 50.0, 2.5, 1.5, 6.0))


@fixture("turbine_compressor_disk")
def build_turbine_compressor_disk() -> TopoDS_Shape:
    """A compressor disk with 48 dovetail blade roots and a thinned web.

    The body is a revolved I-beam section rather than a cylinder with an
    annular pocket cut into both faces. Modelled the second way, the
    ring-pocket's outer wall reads as one enormous blind bore, and every
    balance hole inside it then draws a false intersecting-hole finding.
    Turned from a forging is also what actually happens.

    The blade roots are single-lobe dovetails: 9 mm at the floor narrowing
    to 5 mm at the opening, so the walls overhang and no rotating cutter can
    reach them from outside. That is broaching or wire EDM, and saying so is
    the fixture's job. Extra fir-tree lobes would add faces, not meaning.
    """
    disk = revolved_profile(
        [
            (50.0, 0.0),
            (50.0, 45.0),
            (70.0, 45.0),
            (70.0, 25.5),
            (87.5, 25.5),
            (87.5, 45.0),
            (110.0, 45.0),
            (110.0, 0.0),
            (87.5, 0.0),
            (87.5, 19.5),
            (70.0, 19.5),
            (70.0, 0.0),
        ]
    )
    for index in range(48):
        angle = index * (2.0 * math.pi / 48.0)
        slot = polygon_prism(
            [(104.0, -4.5), (104.0, 4.5), (110.5, 2.5), (110.5, -2.5)], -0.1, 45.4
        )
        disk = cut(disk, rotated(slot, (0, 0, 0), (0, 0, 1), math.degrees(angle)))
    # Balance holes are twist-drilled and keep their point cone: a flat
    # bottom would need a second operation that balance calibration never
    # asks for.
    for index in range(12):
        angle = index * (2.0 * math.pi / 12.0)
        x = 60.0 * math.cos(angle)
        y = 60.0 * math.sin(angle)
        disk = cut(disk, _drilled_blind(x, y, -1.0, (0, 0, 1), 4.0, 9.0))
    groove = cut(
        cylinder(0, 0, 45.0 - 2.001, 70.0, 2.002),
        cylinder(0, 0, 45.0 - 2.001, 65.0, 2.003),
    )
    return cut(disk, groove)


@fixture("turbo_compressor_housing")
def build_turbo_compressor_housing() -> TopoDS_Shape:
    """A cast-and-machined turbocharger compressor housing.

    The volute is six torus wedges rather than one: bigger segments walk
    into OpenCascade's numerical trouble during the cut, and adjacent
    wedges sharing an exact cap face disintegrate the housing, so each
    overlaps its neighbours by two degrees and the caps merge cleanly.

    The mounting foot is deliberately off-axis. A foot directly under the
    bore would put its bolts through the impeller, which is not a thing any
    turbocharger does.
    """
    body = cylinder(0, 0, 0, 80.0, 100.0)

    minor_by_wedge = (10.0, 12.0, 14.0, 15.0, 16.0, 18.0)
    overlap = math.radians(2.0)
    for index in range(6):
        start = index * (math.pi / 3.0) - overlap
        end = (index + 1) * (math.pi / 3.0) + overlap
        try:
            wedge = _torus_segment(0, 0, 50, 55.0, minor_by_wedge[index], start, end)
            body = cut(body, wedge)
        except Exception:
            continue

    body = cut(body, cylinder(0, 0, -1, 28.0, 46.0))
    body = cut(body, cylinder(0, 0, 40, 22.5, 65.0))

    # Discharge outlet on +Y, with four M8 taps around it. The outlet is
    # kept short so it does not graze the volute's outer wall: grazing
    # leaves sub-millimetre slivers that read as thin walls and are an
    # artefact of the boolean rather than anything a founder would cast.
    body = cut(body, cylinder(0, 76, 50, 20.0, 15.0, (0, -1, 0)))
    for index in range(4):
        angle = (index + 0.5) * (math.pi / 2.0)
        x = 37.5 * math.cos(angle)
        z = 50 + 37.5 * math.sin(angle)
        body = cut(body, _drilled_blind(x, 81.0, z, (0, -1, 0), 3.35, 16.0))

    foot = box(-40, -60, -8, 80.0, 45.0, 8.001)
    body = fuse(body, foot)
    for x, y in ((-30, -30), (30, -30), (-30, -50), (30, -50)):
        body = cut(body, _drilled_blind(x, y, -9.0, (0, 0, 1), 3.35, 12.0))

    # Wastegate seat at a compound angle, starting outside the housing and
    # drilling in until it breaks into the volute. That intersection is by
    # design: a wastegate exists to bypass gas into the passage.
    direction = (
        0.0,
        -math.cos(math.radians(30.0)),
        -math.sin(math.radians(30.0)),
    )
    start = (
        0.0,
        80.0 + 30.0 * math.cos(math.radians(30.0)),
        70.0 + 30.0 * math.sin(math.radians(30.0)),
    )
    return cut(body, cylinder(start[0], start[1], start[2], 9.0, 60.0, direction))


@fixture("worm_gearbox_housing")
def build_worm_gearbox_housing() -> TopoDS_Shape:
    """A right-angle worm reducer housing: gear chamber, two bearing bores, feet.

    The chamber floor sits above the worm tunnel crown on purpose. A floor
    that meets the crown tangentially fragments into two strips, and the
    pocket recognizer then seeds a phantom pocket a hundred millimetres deep
    off a chamber wall. The worm-to-wheel mesh opening is instead a milled
    window, which keeps the floor one connected face with an inner wire.

    The oil drain is drilled up from the bottom rather than in from the
    side: a side drain sitting near the floor half-buries its end disc below
    floor level and invents a flat-bottom finding.
    """
    block = box(0, 0, 0, 120.0, 100.0, 90.0)
    block = cut(block, box(20, 20, 45, 80.0, 60.0, 45.001))
    block = cut(block, box(45, 44, 38, 30.0, 12.0, 7.5))

    block = cut(block, cylinder(-1.0, 50, 25, 15.0, 122.0, (1, 0, 0)))
    block = cut(block, cylinder(-0.1, 50, 25, 20.0, 12.1, (1, 0, 0)))
    block = cut(block, cylinder(120.1, 50, 25, 20.0, 12.1, (-1, 0, 0)))

    block = cut(block, cylinder(60, -1.0, 62, 12.5, 102.0, (0, 1, 0)))
    block = cut(block, cylinder(60, -0.1, 62, 23.5, 12.1, (0, 1, 0)))
    block = cut(block, cylinder(60, 100.1, 62, 23.5, 12.1, (0, -1, 0)))

    for x, y in (
        (10, 10),
        (110, 10),
        (10, 90),
        (110, 90),
        (60, 10),
        (60, 90),
        (10, 50),
        (110, 50),
    ):
        block = cut(block, _drilled_blind(x, y, 90.1, (0, 0, -1), 2.5, 12.1))

    block = cut(block, cylinder(25.0, -0.1, 75.0, 7.0, 22.0, (0, 1, 0)))
    block = cut(block, cylinder(95.0, -0.1, 55.0, 4.25, 22.0, (0, 1, 0)))
    block = cut(block, cylinder(30.0, 28.0, -1.0, 4.25, 47.0))

    # 14 mm feet, not 10: a 10 mm slab 120 long sits exactly on the 12:1
    # thin-wall aspect gate and fires it.
    block = fuse(block, box(0.0, -15.0, 0.0, 120.0, 15.001, 14.0))
    block = fuse(block, box(0.0, 99.999, 0.0, 120.0, 15.001, 14.0))
    for x, y in ((20.0, -7.5), (100.0, -7.5), (20.0, 107.5), (100.0, 107.5)):
        block = cut(block, cylinder(x, y, -1.0, 4.5, 16.0))
    return block


@fixture("transaxle_housing_cover")
def build_transaxle_housing_cover() -> TopoDS_Shape:
    """A die-cast transaxle cover, drafted 2 degrees and then finish-machined.

    The frustum is lofted between two rectangles so every vertical wall
    carries real draft, and the fillets are restricted to the four corner
    edges: filleting the top and bottom perimeters as well walks into
    OpenCascade's three-way-corner failure.

    The pocket and the O-ring gland are swept rounded-rectangle profiles.
    Cut as sharp boxes they draw square-corner findings on features the
    drawing calls out as radiused, and the gland's corners have to be
    concentric inner and outer arcs or the channel narrows through the
    turns. The gland also sits inboard of the bolt line: run out to the
    drafted extent it passes straight through the bolt centrelines, which
    on a real cover is a leak path.

    The thread relief goes at the runout, where DIN 76 puts it. Parked at
    the top instead, its ceiling lands exactly on the mating face and the
    resulting boolean sliver reads as an undercut.
    """
    part = _loft(
        [
            _rect_wire(90.0, 70.0, 183.14, 143.14, 0.0),
            _rect_wire(90.0, 70.0, 180.0, 140.0, 45.0),
        ]
    )

    def corner_edge(edge, start, end) -> bool:
        low = min(start.Z(), end.Z())
        high = max(start.Z(), end.Z())
        return abs(low) < 0.1 and abs(high - 45.0) < 0.1

    part = fillet_edges(part, 3.0, corner_edge)

    part = cut(part, cylinder(90.0, 70.0, -0.1, 22.5, 45.2))
    part = cut(part, cone(90.0, 70.0, 45.0, 23.0, 22.5, 0.5, (0, 0, -1)))
    part = cut(part, cone(90.0, 70.0, 0.0, 23.0, 22.5, 0.5))

    part = cut(part, cylinder(90.0, 70.0, 45.1, 23.9, 11.2, (0, 0, -1)))
    part = cut(part, cone(90.0, 70.0, 45.1, 25.0, 23.9, 1.1, (0, 0, -1)))
    relief = cut(
        cylinder(90.0, 70.0, 34.9, 25.4, 3.0),
        cylinder(90.0, 70.0, 34.8, 23.9, 3.2),
    )
    part = cut(part, relief)

    part = cut(part, rounded_rect_prism(40.0, 40.0, 140.0, 100.0, 6.0, 37.0, 8.1))

    gland = cut(
        rounded_rect_prism(22.0, 22.0, 158.0, 118.0, 6.5, 42.2, 2.9),
        rounded_rect_prism(26.5, 26.5, 153.5, 113.5, 2.0, 42.1, 3.1),
    )
    part = cut(part, gland)

    for x, y in (
        (15.0, 15.0),
        (90.0, 15.0),
        (165.0, 15.0),
        (165.0, 70.0),
        (165.0, 125.0),
        (90.0, 125.0),
        (15.0, 125.0),
        (15.0, 70.0),
    ):
        part = cut(part, cylinder(x, y, -0.1, 4.5, 45.3))
        part = cut(part, cone(x, y, 45.0, 5.0, 4.5, 0.5, (0, 0, -1)))
    return part


# -- torture blocks -----------------------------------------------------------
#
# One part per recognizer family, carrying every bait that family can be
# fed. A bait that fails to fire here is a gap in the engine, not a
# judgement call about where a threshold sits. Several of the baits in this
# group have been rewritten once already because the first attempt was
# wrong and the rule was right -- a "nonstandard" diameter that turned out
# to be a fractional drill, a "too small" feature that sat above the limit,
# a "nonstandard" countersink at the standard aerospace angle.


@fixture("torture_hole_labyrinth")
def build_torture_hole_labyrinth() -> TopoDS_Shape:
    """Every hole and slot bait on one 150 x 100 x 40 block.

    Deep single-pass and peck-territory bores, two off-catalog diameters, a
    0.4 mm bore and a 0.35 mm slot below the minimum feature size, a bore
    one millimetre from an edge, crossing bores, a side bore terminating on
    the curved wall of a spherical bowl, a 70 degree countersink and a
    counterbore stack.
    """
    part = box(0, 0, 0, 150.0, 100.0, 40.0)
    part = cut(part, cylinder(15, 15, 4.0, 2.0, 37.0))
    part = cut(part, cylinder(15, 40, -1.0, 2.5, 42.0))
    part = cut(part, cylinder(30, 60, -1.0, 6.115, 42.0))
    part = cut(part, cylinder(50, 60, 20.0, 5.88, 21.0))
    part = cut(part, cylinder(70, 15, -1.0, 0.2, 42.0))
    part = cut(part, box_between((80.0, 24.825, 32.0), (100.0, 25.175, 41.0)))
    part = cut(part, cylinder(60, 5, -1.0, 4.0, 42.0))
    part = cut(part, cylinder(110, 30, 9.0, 5.0, 32.0))
    part = cut(part, cylinder(110, -1.0, 20.0, 5.0, 36.0, (0, 1, 0)))
    part = cut(part, box_between((110.0, 60.0, 25.0), (140.0, 80.0, 41.0)))
    # The bowl is what makes the side bore interesting: its far rim lands on
    # curved wall with no planar exit cap, which is the only geometry that
    # reads as terminating in a cavity. Two earlier baits exited through
    # flat pocket walls and were correctly called through-web holes.
    part = cut(part, sphere(135.0, 15.0, 32.0, 12.0))
    part = cut(part, cylinder(151.0, 15.0, 30.0, 3.0, 20.0, (-1, 0, 0)))
    depth = 4.0 / math.tan(math.radians(35.0))
    countersink = fuse(
        cone(95.0, 80.0, 40.0 - depth, 3.0, 7.0, depth),
        cylinder(95.0, 80.0, -1.0, 3.0, 42.0),
    )
    part = cut(part, countersink)
    part = cut(part, cylinder(30, 85, 34.0, 10.0, 7.0))
    part = cut(part, cylinder(30, 85, 24.0, 6.0, 11.0))
    return cut(part, cylinder(30, 85, -1.0, 3.0, 42.0))


@fixture("torture_casting_ribfield")
def build_torture_casting_ribfield() -> TopoDS_Shape:
    """A drafted plinth carrying a field of ribs and bosses.

    Thin undrafted ribs against one drafted one, a boss undercut by a ring
    groove at its root, four bosses past the height-to-diameter limit, and a
    square-cornered ring groove in the plate.

    The ribs sit on the base plate rather than the plinth roof, where an
    earlier version put them and where the rib recognizer never classified
    them at all; the boss field sits on clear plate for the same reason,
    since a boss touching the plinth wall merges into it topologically and
    stops being a boss.
    """
    part = box(0, 0, 0, 160.0, 110.0, 12.0)
    part = fuse(
        part,
        _loft(
            [
                _rect_wire(55.0, 55.0, 100.0, 70.0, 12.0),
                _rect_wire(55.0, 55.0, 98.0, 68.0, 37.0),
            ]
        ),
    )
    for index in range(3):
        y0 = 3.0 + index * 5.8
        part = fuse(part, box_between((30.0, y0, 12.0), (80.0, y0 + 1.2, 34.0)))
    part = fuse(
        part,
        _loft(
            [
                _rect_wire(55.0, 75.0, 50.0, 3.5, 37.0),
                _rect_wire(55.0, 75.0, 50.0, 1.5, 57.0),
            ]
        ),
    )
    part = fuse(part, cylinder(130.0, 30.0, 12.0, 8.0, 18.0))
    ring = cut(
        cylinder(130.0, 30.0, 15.0, 9.5, 2.5),
        cylinder(130.0, 30.0, 15.0, 6.5, 2.5),
    )
    part = cut(part, ring)
    for index in range(4):
        part = fuse(part, cylinder(20.0 + index * 14.0, 100.0, 12.0, 2.5, 26.0))
    groove = cut(
        box_between((118.0, 62.0, 6.0), (152.0, 96.0, 12.5)),
        box_between((124.0, 68.0, 5.0), (146.0, 90.0, 13.0)),
    )
    return cut(part, groove)


@fixture("torture_machinists_maze")
def build_torture_machinists_maze() -> TopoDS_Shape:
    """Every milling-access bait on one 160 x 120 x 50 block.

    A deep narrow slot, a chamber whose only mouth is a 6 mm slot, a fully
    enclosed void, a spherical pocket sunk past its equator, a T-slot, a
    broached square through-slot, a 2.5 mm clamping lip, a multi-level
    pocket with a floor island, and a sheared corner with a bore normal to
    the new face.

    The sealed void is carved across the joint of two Z-halves that are then
    fused. Cutting a fully interior sphere from one solid removes nothing at
    all -- there is no boundary for the tool to intersect.
    """
    void = sphere(140.0, 25.0, 25.0, 7.0)
    bottom = cut(box_between((0, 0, 0), (160.0, 120.0, 25.0)), void)
    top = cut(box_between((0, 0, 25.0), (160.0, 120.0, 50.0)), void)
    part = fuse(bottom, top)

    part = cut(part, box_between((10.0, 10.0, 30.0), (70.0, 50.0, 51.0)))
    part = cut(part, box_between((20.0, 18.0, 18.0), (60.0, 42.0, 30.0)))
    part = fuse(part, box_between((34.0, 26.0, 18.0), (46.0, 34.0, 36.0)))

    part = cut(part, box_between((88.0, 20.0, 22.0), (112.0, 44.0, 38.0)))
    part = cut(part, box_between((97.0, 29.0, 38.0), (103.0, 35.0, 51.0)))

    part = cut(part, box_between((10.0, 70.0, 5.0), (80.0, 74.0, 51.0)))
    part = cut(part, box_between((-1.0, 90.0, 20.0), (161.0, 98.0, 28.0)))

    part = cut(part, sphere(95.0, 108.0, 50.0, 10.0))
    part = cut(part, sphere(125.0, 108.0, 46.0, 8.0))

    part = cut(part, box_between((100.0, 60.0, 42.0), (160.5, 68.0, 51.0)))
    part = cut(part, box_between((100.0, 55.0, 34.0), (160.5, 73.0, 42.0)))

    part = fuse(part, box_between((-18.0, 0.0, 0.0), (0.0, 120.0, 2.5)))

    wedge_face = _polygon_wire(
        [(130.0, 100.0, 50.0), (160.0, 100.0, 50.0), (160.0, 100.0, 38.0)]
    )
    part = cut(part, _prism_wire(wedge_face, 0.0, 21.0, 0.0))
    normal = (-0.3714, 0.0, -0.9285)
    entry = (147.0, 110.0, 44.0)
    return cut(
        part,
        cylinder(
            entry[0] - 2.0 * normal[0],
            entry[1],
            entry[2] - 2.0 * normal[2],
            3.0,
            25.0,
            normal,
        ),
    )


@fixture("torture_freeform_leviathan")
def build_torture_freeform_leviathan() -> TopoDS_Shape:
    """A revolved B-spline hub with every freeform bait hung off it.

    An exact 0.28 mm concave turning valley, a tight-concave lofted
    scallop milled into the flank, a 1.2 mm freeform web, a 37 degree
    chamfer around the flange rim and four 100 degree countersinks.
    """
    valley_z, valley_r = 45.0, 22.0
    lower = _bspline_through(
        [(32.0, 0.0), (30.0, 15.0), (26.0, 32.0), (valley_r, valley_z - 0.17)]
    )
    valley = _arc_as_bspline(
        (valley_r, valley_z - 0.17),
        (valley_r - 0.22, valley_z),
        (valley_r, valley_z + 0.17),
    )
    upper = _bspline_through(
        [(valley_r, valley_z + 0.17), (25.0, 60.0), (28.0, 75.0), (20.0, 90.0)]
    )
    part = _revolve_wire(
        [
            _line_edge((0, 0), (32.0, 0)),
            lower,
            valley,
            upper,
            _line_edge((20.0, 90.0), (0, 90.0)),
            _line_edge((0, 90.0), (0, 0)),
        ]
    )

    def arc_wire(cx, cz, sag, half, y):
        return _wire_of(
            [
                _arc_edge_3d(
                    (cx - half, y, cz), (cx, y, cz - sag), (cx + half, y, cz)
                ),
                _line_edge_3d((cx + half, y, cz), (cx - half, y, cz)),
            ]
        )

    scallop = _loft(
        [arc_wire(0.0, 22.0, 5.0, 11.0, 24.0), arc_wire(0.0, 20.0, 8.5, 13.0, 34.5)],
        ruled=False,
    )
    part = cut(part, scallop)

    part = fuse(part, box_between((18.0, -0.6, 62.0), (38.0, 0.6, 84.0)))
    part = fuse(part, cylinder(0, 0, -10.0, 40.0, 10.0))
    cone_ring = cut(
        cylinder(0, 0, -10.0, 41.0, 4.0), cone(0, 0, -10.0, 34.97, 40.0, 4.0)
    )
    part = cut(part, cone_ring)
    for index in range(4):
        angle = index * (math.pi / 2.0) + (math.pi / 4.0)
        x = 33.0 * math.cos(angle)
        y = 33.0 * math.sin(angle)
        part = cut(part, cylinder(x, y, -11.0, 3.0, 12.0))
        part = cut(part, cone(x, y, 0.0, 6.5, 3.0, 2.94, (0, 0, -1)))
    return part


# -- formed sheet -------------------------------------------------------------
#
# Everything here is constant gauge: the crest of a formed feature and its
# walls are one thickness, and the void behind it is cut right through the
# plate. Doubled material anywhere -- a flat plate continuing under a fold,
# a pad sitting on top of an unbroken skin -- is a part that could not be
# pressed out of one blank, and it confuses the gauge detection as well.


@fixture("sm_punch_grid")
def build_sm_punch_grid() -> TopoDS_Shape:
    """Punched holes with a tight pitch, a close edge, and one below gauge.

    The layout keeps every hole that is not a bait at least 6 mm of web from
    its neighbour and 9 mm clear of the bend tangent, so each bait draws
    exactly its own finding and nothing else.
    """
    part = _bracket_profile(40.0, 30.0, 30.0)

    def punch(x, y, radius):
        return cylinder(x, y, -1, radius, 4.0)

    part = cut(part, punch(16.0, 8.0, 2.0))
    part = cut(part, punch(16.0, 15.0, 2.0))
    part = cut(part, punch(26.0, 8.0, 2.0))
    part = cut(part, punch(26.0, 20.0, 2.0))
    part = cut(part, punch(36.0, 26.0, 2.0))
    return cut(part, punch(34.0, 12.0, 0.75))


@fixture("sm_notch_baits")
def build_sm_notch_baits() -> TopoDS_Shape:
    """Two outline notches: one too narrow, one too deep for its width.

    The 60 mm leg exists for the second one. At 2 mm gauge a depth-only bait
    needs a width of at least 2 and a depth over ten times that, which the
    40 mm legs the other sheet fixtures use cannot reach.
    """
    part = _bracket_profile(60.0, 30.0, 40.0)
    part = cut(part, box_between((50.0, 6.0, -1.0), (61.0, 7.5, 3.0)))
    return cut(part, box_between((24.0, 20.0, -1.0), (61.0, 23.0, 3.0)))


@fixture("sm_tab_notch_comb")
def build_sm_tab_notch_comb() -> TopoDS_Shape:
    """Tab and notch baits along a free edge, spaced far enough apart to read.

    Isolation is the point: crowded together, the greedy strip pairing has a
    choice about which cut belongs to which finger, and the fixture stops
    proving anything.
    """
    part = _bracket_profile(40.0, 30.0, 40.0)
    for x0, y0, x1, y1 in (
        (32.0, 6.0, 41.0, 8.0),
        (20.0, 14.0, 41.0, 17.5),
        (28.0, 24.0, 41.0, 30.0),
        (28.0, 31.5, 41.0, 37.5),
    ):
        part = cut(part, box_between((x0, y0, -1.0), (x1, y1, 3.0)))
    return part


@fixture("sm_tall_lance")
def build_sm_tall_lance() -> TopoDS_Shape:
    """A bridge lance formed 9 mm proud -- four and a half times the gauge."""
    part = _bracket_profile(40.0, 30.0, 40.0)
    part = fuse(part, box_between((16.0, 14.0, 2.0), (36.0, 26.0, 11.0)))
    part = cut(part, box_between((18.0, 16.0, -1.0), (34.0, 24.0, 9.0)))
    part = cut(part, box_between((15.0, 14.0, -1.0), (37.0, 16.0, 9.0)))
    return cut(part, box_between((15.0, 24.0, -1.0), (37.0, 26.0, 9.0)))


@fixture("sm_sharp_fold_shell")
def build_sm_sharp_fold_shell() -> TopoDS_Shape:
    """A constant-gauge L modelled with sharp corners instead of bend radii.

    The shortcut some engineers take when drawing a sheet part. No bend
    cylinders exist, so this must not classify as sheet metal: a sharp L is
    geometrically an angle plate and nothing distinguishes the two. It takes
    the machining voice plus an advisory to model the radii if folding was
    the intent, and it pins the classifier boundary from the third side.
    """
    part = box_between((0.0, 0.0, 0.0), (40.0, 40.0, 2.0))
    return fuse(part, box_between((0.0, 0.0, 2.0), (2.0, 40.0, 30.0)))


@fixture("sm_relief_bait")
def build_sm_relief_bait() -> TopoDS_Shape:
    """A bend that stops mid-edge at both ends, relieved at one of them.

    One end gets a proper notch, cut from the part edge right through the
    bend zone; the other is bare. Exactly one missing-relief warning is the
    right answer, and cutting the notch from the edge rather than inboard
    avoids leaving a sliver finger between the relief and the outline.
    """
    base = box_between((0.0, 0.0, 0.0), (40.0, 40.0, 2.0))
    flange = moved(_bracket_profile(40.0, 30.0, 24.0), 0.0, 8.0, 0.0)
    part = fuse(base, flange)
    part = _cut_under_fold(
        part, (-1.0, 8.0, -1.0), (5.0, 32.0, 2.0), (5.0, 7.0, 5.0), (0, 1, 0), 26.0
    )
    return cut(part, box_between((-1.0, 32.0, -1.0), (7.2, 35.2, 3.0)))


@fixture("sm_relief_shapes")
def build_sm_relief_shapes() -> TopoDS_Shape:
    """Six relief shapes on three bends, five of which must stay silent.

    A V-notch, a round scallop, a keyhole, a diagonal cutback and an open
    corner strip are all valid ways to relieve a bend end, and the witness
    has to accept every one of them; the bare end on the first band is the
    only finding.

    The base is 100 deep so each relief owns its territory, because
    overlapping relief cuts merge into one opening. Each is cut flush to the
    end line and clear through the fold strip: a shape merely tangent to the
    end leaves cusp slivers the strip probe rightly flags, and a shape
    leaving a margin is an incomplete relief. None of them slices a bend
    arc, since a cut through an arc reads as a machined bore.
    """
    part = box_between((0.0, 0.0, 0.0), (40.0, 110.0, 2.0))
    for y0, length in ((8.0, 20.0), (40.0, 20.0), (84.0, 16.0)):
        part = fuse(part, moved(_bracket_profile(40.0, 30.0, length), 0.0, y0, 0.0))
    for y0, y1 in ((8.0, 28.0), (40.0, 60.0), (84.0, 100.0)):
        part = _cut_under_fold(
            part,
            (-1.0, y0, -1.0),
            (5.0, y1, 2.0),
            (5.0, y0 - 1.0, 5.0),
            (0, 1, 0),
            y1 - y0 + 2.0,
        )

    def tri_cut(a, b, c):
        return polygon_prism([a, b, c], -1.0, 7.0)

    part = cut(part, tri_cut((-1.0, 28.0), (7.5, 28.0), (3.0, 34.0)))
    # Between the V's sloping edge, the under-fold void and the scallop, the
    # plate corner would otherwise survive as a floating triangular island.
    part = cut(part, box_between((-1.0, 28.0, -1.0), (1.8, 33.1, 7.0)))
    part = cut(part, cylinder(2.5, 36.0, -1.0, 4.0, 9.0))
    part = cut(part, box_between((-1.0, 39.0, -1.0), (6.8, 40.0, 3.0)))
    part = cut(part, box_between((-1.0, 60.0, -1.0), (6.5, 63.5, 6.0)))
    part = cut(part, cylinder(4.75, 64.5, -1.0, 2.2, 7.0))
    part = cut(part, tri_cut((-1.0, 84.5), (8.5, 84.5), (-1.0, 76.5)))
    return cut(part, box_between((-1.0, 99.5, -1.0), (9.0, 111.0, 7.0)))


@fixture("sm_torture_combo")
def build_sm_torture_combo() -> TopoDS_Shape:
    """Several sheet families on one bracket, each with a single bait.

    An emboss 1 mm from the bend tangent against one far away, a hole below
    the punchable minimum, a hole 3 mm from the tangent, and a 2 mm notch on
    the free edge. Everything else on the part is deliberately clean, so any
    finding beyond those four is a false positive with an obvious address.
    """
    part = _bracket_profile(40.0, 30.0, 50.0)

    def emboss(x0, y0, x1, y1, height):
        formed = fuse(part, box_between((x0, y0, 2.0), (x1, y1, 2.0 + height)))
        return cut(
            formed,
            box_between((x0 + 2.0, y0 + 2.0, -1.0), (x1 - 2.0, y1 - 2.0, height)),
        )

    part = emboss(6.0, 30.0, 14.0, 38.0, 4.0)
    part = emboss(26.0, 30.0, 34.0, 38.0, 4.0)
    part = cut(part, cylinder(20.0, 8.0, -1.0, 0.75, 5.0))
    part = cut(part, cylinder(11.0, 16.0, -1.0, 3.0, 5.0))
    return cut(part, box_between((30.0, 6.0, -1.0), (41.0, 8.0, 3.0)))


@fixture("sm_torture_curved_outline")
def build_sm_torture_curved_outline() -> TopoDS_Shape:
    """Laser-cut curves on the outline, which the planar strip logic once missed.

    A round-ended tab whose U is closed by a half-cylinder rather than a
    flat, and a bend end relieved by a round-bottomed notch: both have to be
    read through a cylindrical connector rather than a straight one. The
    other bend end is bare and is the fixture's one intended finding.

    The tab is made by clearing a corner window open through both edges --
    no leftover rim strips -- and then putting a finger back. Its arc apex
    stops half a millimetre short of the old plate edge, because tangency
    there would seam-split the end band.
    """
    base = box_between((0.0, 0.0, 0.0), (40.0, 50.0, 2.0))
    flange = moved(_bracket_profile(40.0, 30.0, 30.0), 0.0, 10.0, 0.0)
    part = fuse(base, flange)
    part = _cut_under_fold(
        part, (-1.0, 10.0, -1.0), (5.0, 40.0, 2.0), (5.0, 9.0, 5.0), (0, 1, 0), 32.0
    )
    part = cut(part, box_between((-1.0, 40.0, -1.0), (7.0, 44.0, 3.0)))
    part = cut(part, box_between((14.0, 42.0, -1.0), (18.4, 47.0, 3.0)))
    part = cut(part, cylinder(16.2, 47.0, -1.0, 2.2, 5.0))
    part = cut(part, box_between((32.0, 41.0, -1.0), (41.0, 51.0, 3.0)))
    part = fuse(part, box_between((32.0, 44.5, 0.0), (38.0, 47.5, 2.0)))
    return fuse(part, cylinder(38.0, 46.0, 0.0, 1.5, 2.0))


@fixture("sm_torture_mixed_folds")
def build_sm_torture_mixed_folds() -> TopoDS_Shape:
    """One real bend and one sharp zero-radius fold on the same part.

    The real bend pair keeps the sheet classification, and the sharp fold
    then has to draw its own finding -- the in-family twin of the advisory
    that a wholly sharp part gets from the machining side. The sharp wall is
    one gauge thick so the rib exemption keeps the classification intact.
    """
    part = _bracket_profile(40.0, 30.0, 50.0)
    return fuse(part, box_between((8.0, 46.0, 2.0), (40.0, 48.0, 26.0)))


@fixture("sm_vent_pan")
def build_sm_vent_pan() -> TopoDS_Shape:
    """A four-wall pan, every corner relieved, with a full field of formed baits.

    The systematic inverse of the corner-box fixture: relieve all four
    corners properly and the corner rule and every bend end must go quiet,
    and a relieved pan's panel graph is a star rather than a cycle, so the
    closed-flange-loop rule stays quiet too. Against that silence sit the
    baits -- a countersink sunk past 0.6 of the gauge, a formed pair with a
    3 mm web, a deep dome, a dome close to a fold, a tall wall louver and a
    hole close to the hand-hole wall.

    The countersinks are cut first, into the bare plate. Cone booleans
    against a complex accumulated shape have been seen to silently drop the
    cone face in long-running processes, and these cuts commute with
    everything built afterwards, so doing them first costs nothing.

    Each wall band is built explicitly for its own edge rather than mirrored
    or rotated from one master. A transformed copy lands inverted, with its
    outer face toward the pan interior, and then no bend pairs at all.
    """
    t, outer_r = 2.0, 4.0
    part = box_between((0, 0, 0), (180, 120, t))

    def countersunk(x, y, depth):
        tool = fuse(
            cylinder(x, y, -1, 2.0, t + 2),
            cone(x, y, t, 2.0 + depth, 2.0, depth, (0, 0, -1)),
        )
        return cut(part, tool)

    part = countersunk(25, 25, 1.0)
    part = countersunk(155, 25, 1.0)
    part = countersunk(25, 95, 1.0)
    part = countersunk(90, 20, 1.3)

    mid_outer = 4.0 - 4.0 / math.sqrt(2.0)
    mid_inner = 4.0 - 2.0 / math.sqrt(2.0)

    def band_profile(place):
        return _wire_of(
            [
                _arc_edge_3d(place(4, 0), place(mid_outer, mid_outer), place(0, 4)),
                _line_edge_3d(place(0, 4), place(0, 30)),
                _line_edge_3d(place(0, 30), place(2, 30)),
                _line_edge_3d(place(2, 30), place(2, 4)),
                _arc_edge_3d(place(2, 4), place(mid_inner, mid_inner), place(4, 2)),
                _line_edge_3d(place(4, 2), place(4, 0)),
            ]
        )

    def band_along_x(x0, x1, edge=0.0, sign=1.0):
        wire = band_profile(lambda d, z: (x0, edge + sign * d, z))
        return _prism_wire(wire, x1 - x0, 0, 0)

    def band_along_y(ya, yb, edge, sign):
        wire = band_profile(lambda d, z: (edge + sign * d, ya, z))
        return _prism_wire(wire, 0, yb - ya, 0)

    part = fuse(part, band_along_x(12, 168))
    part = fuse(part, band_along_x(12, 168, 120, -1))
    part = fuse(part, band_along_y(12, 108, 0, 1))
    part = fuse(part, band_along_y(12, 108, 180, -1))

    part = _cut_under_fold(
        part, (11, -1, -1), (169, 4, t), (11, 4, 4), (1, 0, 0), 158, outer_r
    )
    part = _cut_under_fold(
        part, (11, 116, -1), (169, 121, t), (11, 116, 4), (1, 0, 0), 158, outer_r
    )
    part = _cut_under_fold(
        part, (-1, 11, -1), (4, 109, t), (4, 11, 4), (0, 1, 0), 98, outer_r
    )
    part = _cut_under_fold(
        part, (176, 11, -1), (181, 109, t), (176, 11, 4), (0, 1, 0), 98, outer_r
    )

    def corner_relief(cx, cy, sx, sy):
        cleared = part
        for arm_x, arm_y in ((12, 5), (5, 12)):
            x0, x1 = cx - sx, cx + sx * arm_x
            y0, y1 = cy - sy, cy + sy * arm_y
            cleared = cut(
                cleared,
                box_between(
                    (min(x0, x1), min(y0, y1), -1.0),
                    (max(x0, x1), max(y0, y1), t + 1),
                ),
            )
        return cleared

    part = corner_relief(0, 0, 1, 1)
    part = corner_relief(180, 0, -1, 1)
    part = corner_relief(0, 120, 1, -1)
    part = corner_relief(180, 120, -1, -1)

    def emboss_box(shape, x0, y0, x1, y1, height):
        formed = fuse(shape, box_between((x0, y0, t), (x1, y1, t + height)))
        return cut(
            formed, box_between((x0 + t, y0 + t, -1), (x1 - t, y1 - t, height))
        )

    part = fuse(
        part,
        cut(cylinder(90, 60, t, 22.0, 4.0), cylinder(90, 60, 1, 16.0, 6.0)),
    )
    part = cut(
        part,
        cut(cylinder(90, 60, -1, 20.0, 5.0), cylinder(90, 60, -2, 18.0, 7.0)),
    )

    for ya in (52, 65):
        part = emboss_box(part, 35, ya, 49, ya + 10, 4.0)
        for cx in (35, 49):
            part = fuse(part, cylinder(cx, ya + 5, t, 5.0, 4.0))
            part = cut(part, cylinder(cx, ya + 5, -1, 3.0, 5.0))

    part = fuse(
        part,
        common(
            sphere(130, 40, t, 7.0), box_between((122, 32, t), (138, 48, 12))
        ),
    )
    part = cut(part, sphere(130, 40, t, 5.0))
    part = fuse(
        part,
        common(sphere(60, 12, t, 4.0), box_between((55, 7, t), (65, 17, 8))),
    )
    part = cut(part, sphere(60, 12, t, 2.0))

    for x0, x1 in ((50, 68), (80, 98), (110, 128)):
        height, z0, z1 = 5.0, 14, 21
        part = fuse(part, box_between((x0, -height, z0), (x1, 0, z1)))
        part = cut(
            part, box_between((x0 + t, -height + t, z0 + t), (x1 - t, 3, z1 - t))
        )
        part = cut(part, box_between((x0, -height + t, z0), (x1, 0, z0 + t)))

    for column in range(5):
        for row in range(5):
            part = cut(
                part,
                cylinder(120 + 10 * column, 60 + 10 * row, -1, 2.5, t + 2),
            )

    hand_hole = box_between((62, 88, -1), (78, 96, t + 1))
    hand_hole = fuse(hand_hole, box_between((60, 90, -1), (80, 94, t + 1)))
    for cx in (62.0, 78.0):
        for cy in (90.0, 94.0):
            hand_hole = fuse(hand_hole, cylinder(cx, cy, -1, 2.0, t + 2))
    part = cut(part, hand_hole)
    return cut(part, cylinder(70, 82.5, -1, 2.5, t + 2))
