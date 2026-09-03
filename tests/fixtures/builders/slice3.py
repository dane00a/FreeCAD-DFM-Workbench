# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Sheet-metal parts, and the machined parts that bracket them.

Most of this module is folded sheet. A folded part is not a machined one
with rounded corners, and the recognizers tell them apart by looking for a
bend: two concentric cylinders exactly one gauge apart, bridging two flats.
So a fold here is never a fillet applied to a solid. It is a closed profile
wire -- straight runs and circular arcs, drawn in the plane the bend axis is
normal to -- swept into a prism. That gives the inner and outer bend arcs as
two separate faces of the right radii, the constant gauge that makes the
part sheet at all, and no seams anywhere; a fillet would give one blended
surface over a solid interior, which is a machined angle plate.

Two consequences are worth knowing before editing anything here. Arc
midpoints are the exact 45 degree points, `centre - radius / sqrt(2)`, never
rounded approximations: a midpoint off by a micron makes the three-point
circle slightly non-circular, and the gauge then reads as varying. And a
flange fused onto a full-size base plate leaves the plate running flat
underneath the fold as well as bent up into it -- the folded and the unfolded
shape coexisting -- so `_cut_under_fold` removes the doubled material,
taking care to leave the bend cylinder itself alone.

The formed features -- embosses, louvers, lances -- follow one convention:
the hood is fused on above the panel and the matching void cut from below,
walls inset by exactly one gauge, so every crest and ceiling sits a gauge
apart. That skin pair is what says the metal was drawn rather than milled,
and the cavity runs clear through the plate bottom because that is what a
real draw does.

The rest is ordinary milled and turned work, kept here because it shares the
neighbourhood: a dome with a single datum face, a cube drilled from four
sides, a sealed internal void.
"""

from __future__ import annotations

import math

from OCP.BRep import BRep_Tool
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.GC import GC_MakeArcOfCircle
from OCP.Geom import Geom_OffsetSurface
from OCP.GeomAPI import GeomAPI_Interpolate, GeomAPI_PointsToBSplineSurface
from OCP.gp import gp_Ax3, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.Precision import Precision
from OCP.TColgp import TColgp_Array2OfPnt, TColgp_HArray1OfPnt
from OCP.TopoDS import TopoDS_Shape

from . import fixture
from .shapes import (
    box,
    box_between,
    chamfer_edges,
    common,
    cone,
    cut,
    cylinder,
    faces_of,
    fillet_edges,
    fuse,
    moved,
    rotated,
    sphere,
)


# -- folded profiles ----------------------------------------------------------


def _in_xz(y: float = 0.0):
    """Draw in the XZ plane at a given y -- a bend running along +Y."""
    return lambda u, v: gp_Pnt(u, y, v)


def _in_yz(x: float = 0.0):
    """Draw in the YZ plane at a given x -- a bend running along +X."""
    return lambda u, v: gp_Pnt(x, u, v)


class _Profile:
    """A closed section of straight runs and circular arcs, ready to sweep.

    The whole sheet-metal vocabulary is here: `line` walks the outline and
    `arc` puts in a bend, named by its start, its exact 45 degree midpoint
    and its end. `prism` sweeps the closed result along the bend axis.
    """

    def __init__(self, place):
        self._place = place
        self._wire = BRepBuilderAPI_MakeWire()

    def line(self, u0, v0, u1, v1) -> "_Profile":
        self._wire.Add(
            BRepBuilderAPI_MakeEdge(self._place(u0, v0), self._place(u1, v1)).Edge()
        )
        return self

    def arc(self, u0, v0, um, vm, u1, v1) -> "_Profile":
        curve = GC_MakeArcOfCircle(
            self._place(u0, v0), self._place(um, vm), self._place(u1, v1)
        ).Value()
        self._wire.Add(BRepBuilderAPI_MakeEdge(curve).Edge())
        return self

    def face(self):
        return BRepBuilderAPI_MakeFace(self._wire.Wire()).Face()

    def prism(self, dx: float, dy: float, dz: float) -> TopoDS_Shape:
        return BRepPrimAPI_MakePrism(self.face(), gp_Vec(dx, dy, dz)).Shape()


def _polygon_prism(points, dx: float, dy: float, dz: float) -> TopoDS_Shape:
    """A prism swept from a closed polygon given as points in space."""
    polygon = BRepBuilderAPI_MakePolygon()
    for point in points:
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(dx, dy, dz)).Shape()


def _bracket_profile(leg_x: float, leg_z: float, depth_y: float) -> TopoDS_Shape:
    """The house L-bracket section: t=2, inner bend r3, outer r5.

    Most of the sheet fixtures are baits punched or formed into this, so
    each differs from the others in one respect and nothing else.
    """
    return (
        _Profile(_in_xz())
        .line(leg_x, 0, 5, 0)
        .arc(5, 0, 1.4645, 1.4645, 0, 5)
        .line(0, 5, 0, leg_z)
        .line(0, leg_z, 2, leg_z)
        .line(2, leg_z, 2, 5)
        .arc(2, 5, 2.8787, 2.8787, 5, 2)
        .line(5, 2, leg_x, 2)
        .line(leg_x, 2, leg_x, 0)
        .prism(0, depth_y, 0)
    )


def _l_section(t: float, leg_x: float, leg_z: float, depth_y: float) -> TopoDS_Shape:
    """The same L at an arbitrary gauge, inner r = t and outer r = 2t.

    A bend exactly one gauge in radius meets the press-brake floor without
    tripping it, so a part proving something about the gauge itself is not
    also arguing about its own bend radius.
    """
    r_in = t
    r_out = 2.0 * t
    outer_mid = r_out - r_out / math.sqrt(2.0)
    inner_mid = r_out - r_in / math.sqrt(2.0)
    return (
        _Profile(_in_xz())
        .line(leg_x, 0, r_out, 0)
        .arc(r_out, 0, outer_mid, outer_mid, 0, r_out)
        .line(0, r_out, 0, leg_z)
        .line(0, leg_z, t, leg_z)
        .line(t, leg_z, t, r_out)
        .arc(t, r_out, inner_mid, inner_mid, r_out, t)
        .line(r_out, t, leg_x, t)
        .line(leg_x, t, leg_x, 0)
        .prism(0, depth_y, 0)
    )


def _cut_under_fold(
    part: TopoDS_Shape,
    box_lo,
    box_hi,
    axis_origin,
    axis_direction,
    axis_length: float,
    outer_r: float = 5.0,
) -> TopoDS_Shape:
    """Remove the flat plate that survives underneath a fused-on fold.

    Folded material goes up into the bend; it does not also continue flat
    out to the part edge. The tool is the under-fold box less the outer bend
    cylinder, so the arcs and the fold root stay untouched, and flat material
    beyond the band's ends -- which is what the relief rules probe for --
    survives, because the box spans only the band.
    """
    tool = cut(
        box_between(box_lo, box_hi),
        cylinder(*axis_origin, outer_r, axis_length, axis_direction),
    )
    return cut(part, tool)


# -- milled and turned parts --------------------------------------------------


@fixture("single_datum_dome")
def build_single_datum_dome() -> TopoDS_Shape:
    """A hemisphere: one planar face, and nothing to hold it parallel to.

    The flat base is large, so the missing-datum rule stays quiet and the
    no-parallel-pair rule is left on its own.
    """
    return cut(sphere(0.0, 0.0, 0.0, 30.0), box(-40.0, -40.0, -40.0, 80.0, 80.0, 40.0))


@fixture("sealed_void")
def build_sealed_void() -> TopoDS_Shape:
    """A cube with a fully enclosed spherical cavity -- no way in, so no way to cut it.

    Built as two halves each carved by the same sphere and fused back
    together, which is the only way to get a void with no opening.
    """
    cavity = sphere(25.0, 25.0, 25.0, 12.0)
    lower = cut(box(0.0, 0.0, 0.0, 50.0, 50.0, 25.0), cavity)
    upper = cut(box(0.0, 0.0, 25.0, 50.0, 50.0, 25.0), cavity)
    return fuse(lower, upper)


@fixture("setup_count_high")
def build_setup_count_high() -> TopoDS_Shape:
    """One blind hole into each of four faces: four setups on one cube."""
    part = box(0.0, 0.0, 0.0, 60.0, 60.0, 60.0)
    part = cut(part, cylinder(30.0, 30.0, 41.0, 3.0, 20.0))
    part = cut(part, cylinder(30.0, 30.0, -1.0, 3.0, 20.0))
    part = cut(part, cylinder(41.0, 30.0, 30.0, 3.0, 20.0, (1, 0, 0)))
    return cut(part, cylinder(30.0, 41.0, 30.0, 3.0, 20.0, (0, 1, 0)))


@fixture("slot_nonstandard_width")
def build_slot_nonstandard_width() -> TopoDS_Shape:
    """4.7 mm wide: no cutter in the default library is that size."""
    return cut(
        box(0.0, 0.0, 0.0, 60.0, 30.0, 20.0),
        box(5.0, 12.65, 12.0, 50.0, 4.7, 10.0),
    )


@fixture("slot_filleted_corner")
def build_slot_filleted_corner() -> TopoDS_Shape:
    """A closed-end slot with R3 at its four interior corners.

    The slot is 8 mm wide rather than 5 so two R3 fillets at the same end
    still leave straight wall between them. An L-shaped slot would show the
    corner radius more plainly but splits into two pockets during
    recognition, which would test the wrong thing.
    """
    slot = cut(
        box(0.0, 0.0, 0.0, 50.0, 50.0, 25.0),
        box(10.0, 21.0, 13.0, 30.0, 8.0, 15.0),
    )

    def interior_corner(edge, start, end) -> bool:
        if abs(start.X() - end.X()) > 0.1 or abs(start.Y() - end.Y()) > 0.1:
            return False
        if abs(start.Z() - end.Z()) < 0.1:
            return False
        at_x = abs(start.X() - 10.0) < 0.5 or abs(start.X() - 40.0) < 0.5
        at_y = abs(start.Y() - 21.0) < 0.5 or abs(start.Y() - 29.0) < 0.5
        return at_x and at_y

    return fillet_edges(slot, 3.0, interior_corner)


@fixture("sensor_housing")
def build_sensor_housing() -> TopoDS_Shape:
    """A shallow instrument box: pocket, corner mounts, two wire passes."""
    part = cut(
        box(0.0, 0.0, 0.0, 50.0, 50.0, 15.0),
        box(7.5, 7.5, 5.0, 35.0, 35.0, 12.0),
    )
    for x, y in ((5.0, 5.0), (45.0, 5.0), (5.0, 45.0), (45.0, 45.0)):
        part = cut(part, cylinder(x, y, -1.0, 1.5, 17.0))
    for x, z in ((15.0, 7.0), (35.0, 7.0)):
        part = cut(part, cylinder(x, -1.0, z, 0.8, 52.0, (0, 1, 0)))
    return part


@fixture("shaft_endcap")
def build_shaft_endcap() -> TopoDS_Shape:
    """Turned and then milled: a bored cap with a bolt circle and a side tap.

    The side hole is the interesting part -- it needs a second setup on a
    part that is otherwise all one turning operation.
    """
    part = cut(
        cylinder(0.0, 0.0, 0.0, 25.0, 30.0), cylinder(0.0, 0.0, 15.0, 10.0, 16.0)
    )
    for index in range(4):
        angle = index * math.pi / 2.0
        part = cut(
            part,
            cylinder(18.0 * math.cos(angle), 18.0 * math.sin(angle), -1.0, 3.0, 32.0),
        )
    # M8 tap drill entering from outside the part, along +Y.
    part = cut(part, cylinder(0.0, -26.0, 15.0, 3.4, 14.0, (0, 1, 0)))

    def outer_top_rim(edge, start, end) -> bool:
        radius = math.hypot(start.X(), start.Y())
        return abs(start.Z() - 30.0) < 0.1 and abs(radius - 25.0) < 0.5

    return chamfer_edges(part, 2.0, outer_top_rim)


@fixture("servo_coupling_clamp")
def build_servo_coupling_clamp() -> TopoDS_Shape:
    """A dual-bore clamp coupling: two bores, two slits, two clamp screws.

    Each slit reaches 1 mm past its bore wall. It has to: a clamp needs the
    slit to split the bore so it can flex closed, and a slit stopping level
    with the wall leaves slivers of material at the slit's edges that the
    recognizer reads as a closed pocket instead.
    """
    part = cylinder(0.0, 0.0, 0.0, 16.0, 38.0)
    part = cut(part, cylinder(0.0, 0.0, -0.1, 4.0, 18.1))
    part = cut(part, cylinder(0.0, 0.0, 20.0, 5.0, 18.1))
    part = cut(part, box(3.0, -0.8, 0.0, 13.5, 1.6, 18.0))
    part = cut(part, cylinder(0.0, -17.0, 10.0, 1.65, 34.0, (0, 1, 0)))
    part = cut(part, cylinder(0.0, 12.0, 10.0, 3.5, 4.1, (0, 1, 0)))
    part = cut(part, box(-16.5, -0.8, 20.0, 12.5, 1.6, 18.0))
    part = cut(part, cylinder(0.0, -17.0, 28.0, 1.65, 34.0, (0, 1, 0)))
    return cut(part, cylinder(0.0, 12.0, 28.0, 3.5, 4.1, (0, 1, 0)))


@fixture("robotic_gripper_finger")
def build_robotic_gripper_finger() -> TopoDS_Shape:
    """A contoured finger: tapered outline, relief pockets either side of a 2 mm web.

    The tip holes sit 14 and 8 mm from the tip centreline so they clear the
    taper. Further out they would enter through the tapered surface rather
    than into material, leaving a sliver the recognizer reads as a shallow
    blind hole.
    """
    part = _polygon_prism(
        [
            (0, 0, 0),
            (90, 0, 0),
            (90, 0, 10),
            (75, 0, 30),
            (75, 0, 85),
            (65, 0, 110),
            (25, 0, 110),
            (15, 0, 85),
            (15, 0, 30),
            (0, 0, 10),
        ],
        0,
        22,
        0,
    )
    part = cut(part, box(20.0, 12.0, 35.0, 50.0, 10.1, 50.0))
    part = cut(part, box(20.0, -0.1, 35.0, 50.0, 10.1, 50.0))
    for x in (15.0, 75.0):
        part = cut(part, cylinder(x, -1.0, 50.0, 2.75, 24.0, (0, 1, 0)))
        part = cut(part, cylinder(x, 18.0, 50.0, 4.25, 4.1, (0, 1, 0)))
        part = cut(part, cylinder(x, -0.1, 50.0, 4.25, 4.1, (0, 1, 0)))
    for x in (31.0, 59.0):
        part = cut(part, cylinder(x, 11.0, 110.1, 2.75, 15.0, (0, 0, -1)))
    for x in (37.0, 53.0):
        part = cut(part, cylinder(x, 11.0, 110.1, 2.0, 8.0, (0, 0, -1)))
    for x in (10.0, 80.0):
        for y in (2.0, 20.0):
            part = cut(part, cylinder(x, y, -0.1, 2.85, 14.1))
    return cut(part, cylinder(45.0, 11.0, -0.1, 3.0, 10.1))


@fixture("sculpted_lid_thin_web")
def build_sculpted_lid_thin_web() -> TopoDS_Shape:
    """A freeform top over two pockets, one of which leaves a 1.2 mm web.

    Pocket A's floor sits under the sheet's dip and must fire the freeform
    thin-wall pass; pocket B's floor sits under the high ground and must
    stay silent. The pair is what stops the rule being reworded into
    something that fires on any pocket at all.
    """
    part = box_between((0, 0, 0), (90, 60, 18))

    # Interpolated 4x4 spline sheet, oversized so the trimmed face spans the
    # whole blank. Gentle curvature only.
    xs = (-6.0, 25.0, 55.0, 96.0)
    ys = (-6.0, 20.0, 40.0, 66.0)
    zs = (
        (14.5, 14.0, 14.0, 14.5),
        (13.8, 13.6, 13.7, 14.2),
        (16.0, 16.3, 16.2, 16.0),
        (17.0, 17.0, 16.8, 16.8),
    )
    grid = TColgp_Array2OfPnt(1, 4, 1, 4)
    for i in range(4):
        for j in range(4):
            grid.SetValue(i + 1, j + 1, gp_Pnt(xs[i], ys[j], zs[i][j]))
    sheet = GeomAPI_PointsToBSplineSurface(grid).Surface()
    cutter = BRepPrimAPI_MakePrism(
        BRepBuilderAPI_MakeFace(sheet, 1e-6).Face(), gp_Vec(0, 0, 10.0)
    ).Shape()
    part = cut(part, cutter)

    part = cut(part, box_between((12, 14, -0.1), (38, 46, 12.4)))
    return cut(part, box_between((52, 14, -0.1), (80, 46, 9.0)))


# -- the proven folds ---------------------------------------------------------


@fixture("sheet_metal_bracket")
def build_sheet_metal_bracket() -> TopoDS_Shape:
    """The reference fold: one 90 degree bend, inner r3 and outer r5 at t=2.

    Two concentric cylinders one gauge apart, bridging two flats, is the
    signature the process detector keys on. The holes are punched clean
    through the gauge -- nothing is cut into solid, so the feature veto
    leaves the part classified sheet.
    """
    part = _bracket_profile(40.0, 30.0, 30.0)
    for y in (8.0, 22.0):
        part = cut(part, cylinder(25.0, y, -1.0, 2.0, 4.0))
    for y in (8.0, 22.0):
        part = cut(part, cylinder(-1.0, y, 20.0, 2.0, 4.0, (1, 0, 0)))
    return part


@fixture("sheet_metal_channel")
def build_sheet_metal_channel() -> TopoDS_Shape:
    """The same gauge and radii with two bends instead of one."""
    part = (
        _Profile(_in_xz())
        .line(0, 25, 0, 5)
        .arc(0, 5, 1.4645, 1.4645, 5, 0)
        .line(5, 0, 45, 0)
        .arc(45, 0, 48.5355, 1.4645, 50, 5)
        .line(50, 5, 50, 25)
        .line(50, 25, 48, 25)
        .line(48, 25, 48, 5)
        .arc(48, 5, 47.1213, 2.8787, 45, 2)
        .line(45, 2, 5, 2)
        .arc(5, 2, 2.8787, 2.8787, 2, 5)
        .line(2, 5, 2, 25)
        .line(2, 25, 0, 25)
        .prism(0, 60, 0)
    )
    for y in (15.0, 30.0, 45.0):
        part = cut(part, cylinder(25.0, y, -1.0, 2.5, 4.0))
    return part


@fixture("sm_bend_radius_pair")
def build_sm_bend_radius_pair() -> TopoDS_Shape:
    """One channel, two bend radii: 0.4 gauges left, 0.9 gauges right.

    Straddling the press-brake floor from both sides in a single part means
    the rule cannot be satisfied by a blanket answer. The walls are tall
    enough that the flange rule has nothing to say.
    """
    return (
        _Profile(_in_xz())
        .line(0, 25, 0, 2.8)
        .arc(0, 2.8, 0.820, 0.820, 2.8, 0)
        .line(2.8, 0, 46.2, 0)
        .arc(46.2, 0, 48.887, 1.113, 50, 3.8)
        .line(50, 3.8, 50, 25)
        .line(50, 25, 48, 25)
        .line(48, 25, 48, 3.8)
        .arc(48, 3.8, 47.473, 2.527, 46.2, 2)
        .line(46.2, 2, 2.8, 2)
        .arc(2.8, 2, 2.234, 2.234, 2, 2.8)
        .line(2, 2.8, 2, 25)
        .line(2, 25, 0, 25)
        .prism(0, 30, 0)
    )


@fixture("sm_flange_ladder")
def build_sm_flange_ladder() -> TopoDS_Shape:
    """Same bend both sides, different flange lengths: 6 mm against 16 mm.

    A press brake needs about 4t + r of material to grip. The short wall has
    6 mm and must warn; the tall one has 16 and must not.
    """
    return (
        _Profile(_in_xz())
        .line(0, 11, 0, 5)
        .arc(0, 5, 1.4645, 1.4645, 5, 0)
        .line(5, 0, 45, 0)
        .arc(45, 0, 48.5355, 1.4645, 50, 5)
        .line(50, 5, 50, 21)
        .line(50, 21, 48, 21)
        .line(48, 21, 48, 5)
        .arc(48, 5, 47.1213, 2.8787, 45, 2)
        .line(45, 2, 5, 2)
        .arc(5, 2, 2.8787, 2.8787, 2, 5)
        .line(2, 5, 2, 11)
        .line(2, 11, 0, 11)
        .prism(0, 30, 0)
    )


@fixture("sm_bend_angle_135")
def build_sm_bend_angle_135() -> TopoDS_Shape:
    """A 135 degree over-bend at t=2: past what a standard die will reach.

    The radius and both flanges are comfortable, so the fold angle is the
    only thing left to speak.
    """
    return (
        _Profile(_in_xz())
        .line(0, 0, 30, 0)
        .arc(30, 0, 34.619, 3.087, 33.536, 8.536)
        .line(33.536, 8.536, 25.051, 17.021)
        .line(25.051, 17.021, 23.636, 15.606)
        .line(23.636, 15.606, 32.121, 7.121)
        .arc(32.121, 7.121, 32.772, 3.852, 30, 2)
        .line(30, 2, 0, 2)
        .line(0, 2, 0, 0)
        .prism(0, 25, 0)
    )


@fixture("sm_bend_angle_heavy")
def build_sm_bend_angle_heavy() -> TopoDS_Shape:
    """The same 135 degree fold in 3 mm stock, where the ceiling is lower.

    The angle a die can reach falls with the gauge, so an over-bend that is
    marginal at 2 mm is plainly past it at 3. The flange grows to 18 mm to
    match: at this gauge the brake needs 15, and the 12 mm that was
    comfortable before would drag the flange rule in alongside.
    """
    return (
        _Profile(_in_xz())
        .line(0, 0, 30, 0)
        .arc(30, 0, 35.5433, 3.7039, 34.2426, 10.2426)
        .line(34.2426, 10.2426, 21.5147, 22.9706)
        .line(21.5147, 22.9706, 19.3934, 20.8492)
        .line(19.3934, 20.8492, 32.1213, 8.1213)
        .arc(32.1213, 8.1213, 32.7716, 4.8519, 30, 3)
        .line(30, 3, 0, 3)
        .line(0, 3, 0, 0)
        .prism(0, 30, 0)
    )


@fixture("sm_gauge_range")
def build_sm_gauge_range() -> TopoDS_Shape:
    """7 mm plate folded into an L: over any shop's sheet stock ceiling.

    The bend is one gauge in radius, so the radius rule stays quiet and the
    thickness is the only thing under test.
    """
    return (
        _Profile(_in_xz())
        .line(14, 0, 80, 0)
        .line(80, 0, 80, 7)
        .line(80, 7, 14, 7)
        .arc(14, 7, 9.0503, 9.0503, 7, 14)
        .line(7, 14, 7, 70)
        .line(7, 70, 0, 70)
        .line(0, 70, 0, 14)
        .arc(0, 14, 4.1005, 4.1005, 14, 0)
        .prism(0, 60, 0)
    )


@fixture("sm_gauge_below_floor")
def build_sm_gauge_below_floor() -> TopoDS_Shape:
    """0.25 mm: under the thinnest gauge anyone stocks, and into shim territory.

    The floor is a question about geometry rather than about alloy, so this
    part declares no material. Deliberately roomy in plan, so nothing else
    has an opinion about it.
    """
    return _l_section(0.25, 50.0, 20.0, 30.0)


@fixture("sm_gauge_steel_ceiling")
def build_sm_gauge_steel_ceiling() -> TopoDS_Shape:
    """4.5 mm, which is over the steel ceiling and under the aluminium one.

    The material is not in the geometry and cannot be -- there is no B-rep
    signal for alloy, so it rides the request. That is exactly why this part
    exists: the same solid must be silent declared aluminium and must speak
    declared steel. The legs are long because the brake's grip minimum
    scales with the gauge.
    """
    return _l_section(4.5, 70.0, 45.0, 40.0)


@fixture("sm_hem_pair")
def build_sm_hem_pair() -> TopoDS_Shape:
    """Two hems on one part: a 4 mm return that is too short, a 20 mm control.

    A hem needs roughly four gauges of return to fold without the end
    wandering. The hem radii sit above the bend-radius warning band, so only
    the return length is under test.
    """
    return (
        _Profile(_in_xz())
        .line(5, 0, 70, 0)
        .arc(70, 0, 74.5, 4.5, 70, 9)
        .line(70, 9, 66, 9)
        .line(66, 9, 66, 7)
        .line(66, 7, 70, 7)
        .arc(70, 7, 72.5, 4.5, 70, 2)
        .line(70, 2, 5, 2)
        .arc(5, 2, 2.8787, 2.8787, 2, 5)
        .line(2, 5, 2, 30)
        .arc(2, 30, 4.5, 32.5, 7, 30)
        .line(7, 30, 7, 10)
        .line(7, 10, 9, 10)
        .line(9, 10, 9, 30)
        .arc(9, 30, 4.5, 34.5, 0, 30)
        .line(0, 30, 0, 5)
        .arc(0, 5, 1.4645, 1.4645, 5, 0)
        .prism(0, 30, 0)
    )


@fixture("sm_hem_teardrop")
def build_sm_hem_teardrop() -> TopoDS_Shape:
    """Closed hem against teardrop hem on 3 mm stock.

    Above 2 mm a closed hem cracks on the outside of the fold, and the fix
    is to leave it open. The base end is closed at r1 and must warn; the
    wall top is an open teardrop at r2.5 and must not. Hems are exempt from
    the V-die radius rule by design, so nothing else speaks.
    """
    return (
        _Profile(_in_xz())
        .line(6, 0, 70, 0)
        .arc(70, 0, 74, 4, 70, 8)
        .line(70, 8, 55, 8)
        .line(55, 8, 55, 5)
        .line(55, 5, 70, 5)
        .arc(70, 5, 71, 4, 70, 3)
        .line(70, 3, 6, 3)
        .arc(6, 3, 3.8787, 3.8787, 3, 6)
        .line(3, 6, 3, 30)
        .arc(3, 30, 5.5, 32.5, 8, 30)
        .line(8, 30, 8, 15)
        .line(8, 15, 11, 15)
        .line(11, 15, 11, 30)
        .arc(11, 30, 5.5, 35.5, 0, 30)
        .line(0, 30, 0, 6)
        .arc(0, 6, 1.7574, 1.7574, 6, 0)
        .prism(0, 30, 0)
    )


@fixture("sm_hem_thin_gauge")
def build_sm_hem_thin_gauge() -> TopoDS_Shape:
    """A 5.2 mm hem return at 1 mm gauge, where the minimum stops scaling.

    At 14 gauge and thinner the shortest hem a brake can fold is a flat
    quarter inch rather than four gauges, so 5.2 mm speaks on the new floor
    having been comfortably silent under the old one. That branch is what is
    under test; the 15 mm return on the wall is the control.
    """
    return (
        _Profile(_in_xz())
        .line(4, 0, 70, 0)
        .arc(70, 0, 72.5, 2.5, 70, 5)
        .line(70, 5, 64.8, 5)
        .line(64.8, 5, 64.8, 4)
        .line(64.8, 4, 70, 4)
        .arc(70, 4, 71.5, 2.5, 70, 1)
        .line(70, 1, 4, 1)
        .arc(4, 1, 1.8787, 1.8787, 1, 4)
        .line(1, 4, 1, 30)
        .arc(1, 30, 2.5, 31.5, 4, 30)
        .line(4, 30, 4, 15)
        .line(4, 15, 5, 15)
        .line(5, 15, 5, 30)
        .arc(5, 30, 2.5, 32.5, 0, 30)
        .line(0, 30, 0, 4)
        .arc(0, 4, 1.1716, 1.1716, 4, 0)
        .prism(0, 30, 0)
    )


@fixture("sm_closed_channel")
def build_sm_closed_channel() -> TopoDS_Shape:
    """A closed square tube: four bends whose panel graph is a cycle.

    A brake cannot fold the last bend of a loop -- the tool has nowhere to
    go -- so the closed graph is the finding. Built as one ring section
    minus a second inset by a gauge, the inner one overshooting axially so
    the cut opens both tube ends.
    """

    def ring(inset: float, radius: float) -> "_Profile":
        lo, hi = inset, 40.0 - inset
        corner = 5.0
        sag = radius * 0.70710678
        return (
            _Profile(_in_xz())
            .line(corner, lo, 40.0 - corner, lo)
            .arc(40.0 - corner, lo, 35.0 + sag, 5.0 - sag, hi, corner)
            .line(hi, corner, hi, 40.0 - corner)
            .arc(hi, 40.0 - corner, 35.0 + sag, 35.0 + sag, 40.0 - corner, hi)
            .line(40.0 - corner, hi, corner, hi)
            .arc(corner, hi, 5.0 - sag, 35.0 + sag, lo, 40.0 - corner)
            .line(lo, 40.0 - corner, lo, corner)
            .arc(lo, corner, 5.0 - sag, 5.0 - sag, corner, lo)
        )

    outer = ring(0.0, 5.0).prism(0, 30, 0)
    inner = moved(ring(2.0, 3.0).prism(0, 32, 0), 0, -1, 0)
    return cut(outer, inner)


@fixture("sm_diagonal_bend")
def build_sm_diagonal_bend() -> TopoDS_Shape:
    """A 40 mm square folded along its own diagonal, so the bend outruns the body.

    This is the one sheet fixture that is not a prism of a 2D profile. Every
    other bend here is axis-aligned and spans the whole blank, so its length
    equals the plan extent exactly; a diagonal fold is 50.6 mm of bend
    across a 37.9 mm body, and the margin is deliberately enormous so the
    bait tests the rule rather than its tolerance.

    A diagonal fold has to be swept along a rotated axis, so it is built as
    an ordinary L-section, swung 45 degrees onto the diagonal, and then
    intersected with the blank outline extruded vertically. The vertical
    trim is what makes the construction work at all: one vertical plane
    meets the flat base and the standing wall both at right angles, so a
    single intersection gives both panels honest square edges instead of the
    45 degree slivers the raw square corners would leave.
    """
    t = 3.0
    side = 40.0
    tip = 3.0
    diagonal = side * math.sqrt(2.0)

    # Both legs overshoot the blank so the outline, not the profile, decides
    # every edge: the half-diagonal reach is 28.3 mm and the base leg is 45.
    fold = _l_section(t, 45.0, 25.0, diagonal + 24.0)
    fold = rotated(moved(fold, 0.0, -12.0, 0.0), (0, 0, 0), (0, 0, 1), -45.0)

    # The blank: the square with its two diagonal corner tips cut back. The
    # other two corners are ordinary sheet corners a long way from the fold.
    corner = tip * math.sqrt(2.0)
    edge = side - corner
    blank = _polygon_prism(
        [
            (corner, 0.0, -5.0),
            (side, 0.0, -5.0),
            (side, edge, -5.0),
            (edge, side, -5.0),
            (0.0, side, -5.0),
            (0.0, corner, -5.0),
        ],
        0,
        0,
        40.0,
    )
    return common(fold, blank)


# -- punched and countersunk --------------------------------------------------


@fixture("sm_hole_bend_field")
def build_sm_hole_bend_field() -> TopoDS_Shape:
    """A hole 4 mm from the bend tangent, and a control at 14 mm.

    A hole too near a bend distorts into an oval as the metal stretches
    round it. The clearance wanted here is 2.5t + r, which is 8 mm.
    """
    part = _bracket_profile(40.0, 30.0, 30.0)
    for x, y in ((11.0, 10.0), (21.0, 20.0)):
        part = cut(part, cylinder(x, y, -1.0, 2.0, 4.0))
    return part


@fixture("sm_countersunk_panel")
def build_sm_countersunk_panel() -> TopoDS_Shape:
    """Two 90 degree countersinks: one 0.8 gauges deep, one 0.5.

    Sink much past 0.6 of the gauge and the land left under the screw head
    goes knife-edge.
    """
    part = _bracket_profile(40.0, 30.0, 30.0)

    def countersunk(x: float, y: float, rim_radius: float, depth: float):
        pierced = cut(part, cylinder(x, y, -1.0, 2.0, 4.0))
        return cut(pierced, cone(x, y, 2.0, rim_radius, 2.0, depth, (0, 0, -1)))

    part = countersunk(16.0, 10.0, 3.6, 1.6)
    return countersunk(28.0, 20.0, 3.0, 1.0)


@fixture("sm_csk_knife_edge")
def build_sm_csk_knife_edge() -> TopoDS_Shape:
    """Countersinks in 0.5 mm sheet, where a ratio guard alone is not enough.

    At this gauge 0.6t is 0.30 mm, so a countersink that passes on ratio can
    still leave a land of a quarter millimetre. The middle of the three is
    exactly that case and speaks on the absolute floor alone; the first
    passes both guards and the last trips both.

    The countersinks are cut first, against the pristine plate, and the wall
    band fused on afterwards -- the part needs one bend to classify as sheet
    at all, but its geometry must not disturb them.
    """
    t = 0.5
    outer_r = 1.0
    part = box_between((0, 0, 0), (60, 40, t))

    def countersunk(x: float, y: float, depth: float):
        tool = fuse(
            cylinder(x, y, -1.0, 0.8, 3.0),
            cone(x, y, t, 0.8 + depth, 0.8, depth, (0, 0, -1)),
        )
        return cut(part, tool)

    part = countersunk(15.0, 20.0, 0.15)
    part = countersunk(30.0, 20.0, 0.25)
    part = countersunk(45.0, 20.0, 0.40)

    outer_mid = 1.0 - 1.0 / math.sqrt(2.0)
    inner_mid = 1.0 - 0.5 / math.sqrt(2.0)
    band = (
        _Profile(_in_yz(5.0))
        .arc(1, 0, outer_mid, outer_mid, 0, 1)
        .line(0, 1, 0, 8)
        .line(0, 8, 0.5, 8)
        .line(0.5, 8, 0.5, 1)
        .arc(0.5, 1, inner_mid, inner_mid, 1, 0.5)
        .line(1, 0.5, 1, 0)
        .prism(50, 0, 0)
    )
    part = fuse(part, band)
    part = _cut_under_fold(
        part, (4, -1, -1), (56, 1, t), (4, 1, 1), (1, 0, 0), 52, outer_r
    )
    # Open cutbacks past both bend ends: no bare end, and no sliver finger.
    part = cut(part, box_between((-1, -1, -1), (5, 2, 1.5)))
    return cut(part, box_between((55, -1, -1), (61, 2, 1.5)))


# -- reliefs at bend ends and corners -----------------------------------------


@fixture("sm_corner_box")
def build_sm_corner_box() -> TopoDS_Shape:
    """Two flanges on perpendicular edges whose bends collide at the shared corner.

    Nothing is relieved there, so the folded material has nowhere to go and
    tears. Each stub's prism stops short of the other's footprint, so the
    only overlap is flat material coincident with the base plate -- fusing
    two bend arcs across each other is what makes this construction fail.
    The inboard bend ends sit mid-edge with no relief either.
    """
    part = box_between((0, 0, 0), (40, 40, 2))

    def stub() -> TopoDS_Shape:
        return (
            _Profile(_in_xz())
            .line(30, 0, 35, 0)
            .arc(35, 0, 38.5355, 1.4645, 40, 5)
            .line(40, 5, 40, 25)
            .line(40, 25, 38, 25)
            .line(38, 25, 38, 5)
            .arc(38, 5, 37.1213, 2.8787, 35, 2)
            .line(35, 2, 30, 2)
            .line(30, 2, 30, 0)
            .prism(0, 35, 0)
        )

    part = fuse(part, stub())
    # The same stub turned a quarter turn and shifted across: (x,y) -> (40-y,x).
    part = fuse(
        part, moved(rotated(stub(), (0, 0, 0), (0, 0, 1), 90.0), 40.0, 0.0, 0.0)
    )
    part = _cut_under_fold(
        part, (35.0, -1.0, -1.0), (41.0, 35.0, 2.0), (35.0, -1.0, 5.0), (0, 1, 0), 37.0
    )
    return _cut_under_fold(
        part, (5.0, 35.0, -1.0), (41.0, 41.0, 2.0), (4.0, 35.0, 5.0), (1, 0, 0), 37.0
    )


@fixture("sm_corner_relief_round")
def build_sm_corner_relief_round() -> TopoDS_Shape:
    """The corner box with a drilled corner relief instead of a square one.

    A relief is tested by probing the geometry, so its shape does not matter
    -- only whether material is still there. The hole covers the corner
    point but stays shy of both walls, so it kills the corner finding and
    leaves both bend ends only partly relieved. That split is what this part
    pins.
    """
    return cut(build_sm_corner_box(), cylinder(33.0, 32.5, -1.0, 4.0, 4.0))


# -- formed features ----------------------------------------------------------


@fixture("sm_emboss_field")
def build_sm_emboss_field() -> TopoDS_Shape:
    """Four closed embosses: one control, one too deep, and a pair too close.

    Draw much past three gauges and the metal thins at the corners; put two
    draws closer than two gauges apart and the web between them tears. Every
    footprint stays 10 mm clear of the bend, so the near-bend rule is quiet.
    """
    part = _bracket_profile(40.0, 30.0, 40.0)

    def emboss(x0: float, y0: float, x1: float, y1: float, height: float):
        raised = fuse(part, box_between((x0, y0, 2.0), (x1, y1, 2.0 + height)))
        return cut(
            raised,
            box_between((x0 + 2.0, y0 + 2.0, -1.0), (x1 - 2.0, y1 - 2.0, height)),
        )

    part = emboss(15.0, 4.0, 25.0, 14.0, 4.0)
    part = emboss(30.0, 4.0, 39.0, 14.0, 7.0)
    part = emboss(15.0, 22.0, 23.0, 30.0, 4.0)
    return emboss(25.0, 22.0, 33.0, 30.0, 4.0)


@fixture("sm_emboss_gallery")
def build_sm_emboss_gallery() -> TopoDS_Shape:
    """Embosses that are not rectangles: a stadium, an L, and a ring.

    A recognizer keyed on the outline rather than on the skin pair would
    find none of these. The ring is the sharpest of the three -- its
    back-side void is a circular channel the machining recognizers read as a
    groove, which the skin-pair exemption has to absorb.
    """
    part = _bracket_profile(40.0, 30.0, 60.0)
    # Stadium: a slab capped by half-cylinders, cavity inset one gauge.
    part = fuse(part, box_between((20.0, 3.0, 2.0), (32.0, 11.0, 6.0)))
    part = fuse(part, cylinder(20.0, 7.0, 2.0, 4.0, 4.0))
    part = fuse(part, cylinder(32.0, 7.0, 2.0, 4.0, 4.0))
    part = cut(part, box_between((20.0, 5.0, -1.0), (32.0, 9.0, 4.0)))
    part = cut(part, cylinder(20.0, 7.0, -1.0, 2.0, 5.0))
    part = cut(part, cylinder(32.0, 7.0, -1.0, 2.0, 5.0))
    # L-shaped: two overlapping arms, whose crest fragments flood into one.
    part = fuse(part, box_between((16.0, 17.0, 2.0), (24.0, 31.0, 6.0)))
    part = fuse(part, box_between((16.0, 17.0, 2.0), (34.0, 25.0, 6.0)))
    part = cut(part, box_between((18.0, 19.0, -1.0), (22.0, 29.0, 4.0)))
    part = cut(part, box_between((18.0, 19.0, -1.0), (32.0, 23.0, 4.0)))
    # Ring: an annular pad over an annular void through the plate.
    part = fuse(
        part,
        cut(
            cylinder(27.0, 47.0, 2.0, 11.0, 7.0),
            cylinder(27.0, 47.0, 1.0, 5.0, 9.0),
        ),
    )
    return cut(
        part,
        cut(
            cylinder(27.0, 47.0, -1.0, 9.0, 8.0),
            cylinder(27.0, 47.0, -2.0, 7.0, 10.0),
        ),
    )


@fixture("sm_emboss_freeform")
def build_sm_emboss_freeform() -> TopoDS_Shape:
    """Embosses with freeform outlines: closed periodic splines, not polygons.

    The crest stays planar so the skin pair still carries recognition, but
    the wall thickness varies a little -- the cavity outline is the same
    blob scaled toward its centre -- and tolerating that is the point.
    """
    part = _bracket_profile(40.0, 30.0, 50.0)

    def blob(cx: float, cy: float, scale: float, height: float):
        def ring(factor: float, z0: float, z1: float) -> TopoDS_Shape:
            # Eight lobes, radius wandering between 0.75 and 1.05 of scale.
            radii = (1.05, 0.80, 0.95, 0.75, 1.00, 0.85, 0.90, 0.80)
            points = TColgp_HArray1OfPnt(1, 8)
            for i in range(8):
                angle = i * math.pi / 4.0
                radius = radii[i] * scale * factor
                points.SetValue(
                    i + 1,
                    gp_Pnt(
                        cx + radius * math.cos(angle),
                        cy + radius * math.sin(angle),
                        z0,
                    ),
                )
            interpolate = GeomAPI_Interpolate(points, True, 1e-6)
            interpolate.Perform()
            edge = BRepBuilderAPI_MakeEdge(interpolate.Curve()).Edge()
            wire = BRepBuilderAPI_MakeWire(edge).Wire()
            face = BRepBuilderAPI_MakeFace(wire).Face()
            return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, z1 - z0)).Shape()

        raised = fuse(part, ring(1.0, 2.0, 2.0 + height))
        return cut(raised, ring(0.72, -1.0, height))

    part = blob(25.0, 12.0, 8.5, 4.0)
    return blob(25.0, 35.0, 8.5, 7.0)


@fixture("sm_formed_shapes")
def build_sm_formed_shapes() -> TopoDS_Shape:
    """A round emboss, a cross, and a spherical dome.

    The dome is the one that matters: its crest is freeform rather than
    flat, and it must still census as an emboss and answer the depth rule.
    Its cavity opens through the plate bottom, as a real drawn dome does.
    """
    part = _bracket_profile(40.0, 30.0, 40.0)
    # Round: a cylindrical pad over a shell void through the plate.
    part = fuse(part, cylinder(13.0, 8.0, 2.0, 5.0, 4.0))
    part = cut(part, cylinder(13.0, 8.0, -1.0, 3.0, 5.0))
    # Cross: two crossing arms, cavity arms inset one gauge.
    part = fuse(part, box_between((24.0, 6.0, 2.0), (38.0, 14.0, 6.0)))
    part = fuse(part, box_between((28.0, 2.0, 2.0), (34.0, 18.0, 6.0)))
    part = cut(part, box_between((26.0, 8.0, -1.0), (36.0, 12.0, 4.0)))
    part = cut(part, box_between((30.0, 4.0, -1.0), (32.0, 16.0, 4.0)))
    # Dome: an R7 cap centred on the panel plane over an R5 shell.
    cap = common(
        sphere(14.0, 29.0, 2.0, 7.0),
        box_between((6.0, 21.0, 2.0), (22.0, 37.0, 10.0)),
    )
    part = fuse(part, cap)
    return cut(part, sphere(14.0, 29.0, 2.0, 5.0))


@fixture("sm_lance_bridge")
def build_sm_lance_bridge() -> TopoDS_Shape:
    """A bridge lance: a hood sheared open at both ends rather than one.

    Two open edges instead of one is what separates a lance from a louver,
    and at two and a half gauges tall it is under every threshold -- so it
    is also the all-silent control for the formed recognizer's subtype
    logic.
    """
    part = _bracket_profile(40.0, 30.0, 40.0)
    part = fuse(part, box_between((16.0, 14.0, 2.0), (36.0, 26.0, 7.0)))
    part = cut(part, box_between((18.0, 16.0, -1.0), (34.0, 24.0, 5.0)))
    part = cut(part, box_between((15.0, 14.0, -1.0), (37.0, 16.0, 5.0)))
    return cut(part, box_between((15.0, 24.0, -1.0), (37.0, 26.0, 5.0)))


@fixture("sm_louver_bank")
def build_sm_louver_bank() -> TopoDS_Shape:
    """Two boxy louvers, one three gauges tall and one four and a half.

    The front wall is sheared away entirely below the crest, side posts
    included, leaving a gauge-thin band hanging from the plateau. That band
    is the recognizer's witness that the hood was sheared open rather than
    drawn closed.
    """
    part = _bracket_profile(40.0, 30.0, 50.0)

    def louver(x0: float, y0: float, x1: float, y1: float, height: float):
        raised = fuse(part, box_between((x0, y0, 2.0), (x1, y1, 2.0 + height)))
        drawn = cut(
            raised,
            box_between((x0 + 2.0, y0 + 2.0, -1.0), (x1 - 2.0, y1 - 2.0, height)),
        )
        return cut(
            drawn, box_between((x0 - 1.0, y0, -1.0), (x1 + 1.0, y0 + 2.0, height))
        )

    part = louver(15.0, 8.0, 35.0, 18.0, 6.0)
    return louver(15.0, 30.0, 35.0, 40.0, 9.0)


@fixture("sm_curved_louver")
def build_sm_curved_louver() -> TopoDS_Shape:
    """Louvers that are half-pipes rather than boxes -- the cylindrical crest.

    Each hood's axis lies in the deck's top plane, so it arches over the
    panel instead of standing on it, and it is sheared open 60 degrees round
    the arc. That angle is the point: the lip it leaves is a radial band
    tilted 60 degrees off the deck normal, so a test comparing the lip
    against the host panel's normal would not count it and the hood would
    census as an emboss. Against the crest's own outward direction the
    answer is exactly perpendicular, because a sheared edge is by definition
    a cut through the thickness.

    This is not read as a bend, and not by exception: the arch meets the
    deck at right angles, so the inner cylinder has no tangent planar
    neighbour and the bend seed never forms. A hood blending tangentially
    into its panel would be a genuine bend by every measure the engine has,
    which is why a real louver is lanced first.
    """
    t = 2.0
    part = _bracket_profile(50.0, 30.0, 50.0)

    def louver(x0: float, x1: float, yc: float, outer_r: float, shear_deg: float):
        inner_r = outer_r - t
        # The bore runs a millimetre past each end of the outer wall so the
        # tube is open at both. Capped, it would read as a cylindrical pocket
        # with two flat floors -- a blind hole, which is solid-stock evidence
        # and vetoes the sheet classification outright.
        hood = cut(
            cylinder(x0, yc, t, outer_r, x1 - x0, (1, 0, 0)),
            cylinder(x0 - 1.0, yc, t, inner_r, (x1 - x0) + 2.0, (1, 0, 0)),
        )
        hood = common(
            hood,
            box_between(
                (x0 - 1.0, yc - outer_r - 1.0, t),
                (x1 + 1.0, yc + outer_r + 1.0, t + outer_r + 1.0),
            ),
        )
        # Shear with a radial half-space -- a plane through the arc axis --
        # so the lip is one gauge thick whatever angle it is cut at. Applied
        # to the hood alone: the same half-space would halve the deck.
        blade = rotated(
            box_between((x0 - 2.0, yc, t - 60.0), (x1 + 2.0, yc + 60.0, t + 60.0)),
            (0.0, yc, t),
            (1, 0, 0),
            -shear_deg,
        )
        raised = fuse(part, cut(hood, blade))
        # The lanced opening, cut with a box so no plane is ever tangent to
        # the bore: a cut at y = yc - r would graze it exactly where it meets
        # the deck, and boolean tangency is how sliver faces grow.
        return cut(
            raised,
            box_between((x0, yc - inner_r + 1.5, -1.0), (x1, yc + outer_r, t)),
        )

    part = louver(20.0, 40.0, 13.0, 5.0, 60.0)
    return louver(20.0, 40.0, 33.0, 8.0, 60.0)


# -- the standard louver forms ------------------------------------------------
#
# A punch shears the sheet along one straight line and draws the material on
# one side of it up into a raised hood. All three forms below share what that
# act implies: exactly one long side is open, the hood runs out flush into
# the deck at its ends because nothing was cut there, and the gauge is
# uniform throughout because it is formed rather than milled. They differ
# only in the shape the die draws between those constraints -- faceted,
# tapered scoop, or symmetric lens.


def _half_space(normal, point, big: float = 400.0) -> TopoDS_Shape:
    """A big box filling the half-space behind a plane.

    Intersecting a handful of these is how the faceted hood is built, and it
    is what makes its gauge exact: the inner shell is the same set of planes
    each pushed one gauge along its own normal, so the miter at every crease
    falls out of the intersection rather than having to be constructed.
    Offsetting a polygon section by section inside its own cross-section
    plane would not be exact, because it ignores the facet's tilt along the
    run.
    """
    axes = gp_Ax3(gp_Pnt(*point), gp_Dir(*normal))
    placement = gp_Trsf()
    placement.SetTransformation(axes)
    return BRepBuilderAPI_Transform(
        box_between((-big, -big, -2.0 * big), (big, big, 0.0)),
        placement.Inverted(),
        True,
    ).Shape()


def _faceted_louver(part: TopoDS_Shape, t: float, ys: float, facets) -> TopoDS_Shape:
    """Fuse a faceted hood onto a deck and open the air path beneath it.

    The floor of both shells is the deck's own back face, z=0, and it has to
    be. The facet planes are tilted, so the wedge they bound flares as it
    goes down; floored anywhere below the sheet the hood comes out as a
    shell running far below the plate and past both its ends, and the fuse
    then welds that on. Floored at the back face, everything the flare adds
    is at most one gauge tall and lies inside the deck slab, where the fuse
    swallows it.
    """
    bounds = common(
        _half_space((0, -1, 0), (0.0, ys, 0.0)),
        _half_space((0, 0, -1), (0.0, 0.0, 0.0)),
    )
    outer = bounds
    inner = bounds
    for normal, point in facets:
        length = math.sqrt(sum(component * component for component in normal))
        unit = tuple(component / length for component in normal)
        pushed = tuple(point[i] - unit[i] * t for i in range(3))
        outer = common(outer, _half_space(normal, point))
        inner = common(inner, _half_space(normal, pushed))
    part = fuse(part, cut(outer, inner))
    return cut(part, inner)


@fixture("sm_louver_angular")
def build_sm_louver_angular() -> TopoDS_Shape:
    """The faceted louver: every hood face a plane, so the analytic path suffices.

    The top ridge is horizontal, and that is a constraint rather than a
    styling choice -- the crest and its host panel must share an outward
    normal, so a ridge that ramped along the run would find no host and the
    hood would never seed. A real angular louver ramps at the tail and then
    runs flat, which is what this builds. At five millimetres the hood is
    under the height ceiling, so it is the family's silent control.
    """
    t = 2.0
    ridge = 5.0
    ys = 18.0
    width = 10.0
    flank_run = 4.0
    x0, x1 = 16.0, 44.0
    tail_run = 8.0
    nose_run = 4.0

    facets = (
        ((0.0, 0.0, 1.0), (0.0, 0.0, t + ridge)),
        ((-ridge, 0.0, tail_run), (x0, 0.0, t)),
        ((ridge, 0.0, nose_run), (x1, 0.0, t)),
        ((0.0, ridge, flank_run), (0.0, ys + width, t)),
    )
    return _faceted_louver(_bracket_profile(50.0, 30.0, 50.0), t, ys, facets)


@fixture("sm_louver_angular_sym")
def build_sm_louver_angular_sym() -> TopoDS_Shape:
    """The faceted louver done symmetrically: the same ramp at both ends.

    A die is symmetric about its own centreline along the shear, so the
    metal is lifted the same way approaching either end. The width stays
    asymmetric on purpose -- exactly one long side is open, and that is the
    whole difference between a louver and a bead.

    The only change from the asymmetric twin is that the two ramps share a
    run, which makes the mirror exact by construction.
    """
    t = 2.0
    ridge = 5.0
    ys = 18.0
    width = 10.0
    flank_run = 4.0
    x0, x1 = 16.0, 44.0
    ramp = 6.0

    facets = (
        ((0.0, 0.0, 1.0), (0.0, 0.0, t + ridge)),
        ((-ridge, 0.0, ramp), (x0, 0.0, t)),
        ((ridge, 0.0, ramp), (x1, 0.0, t)),
        ((0.0, ridge, flank_run), (0.0, ys + width, t)),
    )
    return _faceted_louver(_bracket_profile(50.0, 30.0, 50.0), t, ys, facets)


def _louver_arc_section(x: float, ys: float, t: float, w: float, a: float):
    """One cross-section of a lofted hood, from the hinge to the sheared lip.

    Three things about this arc are load-bearing. The hinge sits below the
    deck's top plane, at t*cos(beta), because that is the one depth at which
    the hood's inner skin lands exactly on the deck's bottom plane -- any
    higher and the cut that opens the louver stops short, leaving a sliver
    face running the whole length of the hinge; any lower and the hood pokes
    out through the back of the sheet. It also makes every hood-to-deck
    crossing transversal, and a tangential boolean is where degenerate edges
    grow.

    The arc turns 1.85 times its chord angle off the hinge, so it leaves the
    deck steeply and arrives at the lip nearly flat. That is the shape of a
    real scoop, and it is what keeps the sheared lip near perpendicular to
    the crest; flatten the turn and the hood censuses as an emboss instead.

    And a zero rise degenerates the section to a straight line lying in the
    deck plane, which is what both ends of every lofted hood use: the hood
    there spans exactly the deck's own slab, so the fuse swallows it and the
    sheared lip is left as the only free edge.
    """
    # Fixed point on (hinge depth, beta): beta depends on the rise, the rise
    # depends on the hinge depth. Converges in three or four passes; at a
    # zero rise it converges to the flat section the ends want.
    hinge_z = t
    beta = 0.0
    rise = a
    for _ in range(8):
        rise = a + t - hinge_z
        chord = math.atan2(rise, w)
        beta = min(1.85 * chord, math.radians(70.0))
        hinge_z = t * math.cos(beta)

    hinge = gp_Pnt(x, ys + w, hinge_z)
    lip = gp_Pnt(x, ys, t + a)
    if rise < 1.0e-4:
        return BRepBuilderAPI_MakeWire(
            BRepBuilderAPI_MakeEdge(hinge, lip).Edge()
        ).Wire()

    # The radius comes out negative for a hood convex upward, which every
    # real louver section is. The algebra is sign-agnostic: the centre simply
    # falls on the other side.
    radius = (w * w + rise * rise) / (
        2.0 * (rise * math.cos(beta) - w * math.sin(beta))
    )
    centre = gp_Pnt(
        x, ys + w + radius * math.sin(beta), hinge_z + radius * math.cos(beta)
    )
    to_hinge = gp_Vec(centre, hinge)
    to_lip = gp_Vec(centre, lip)
    to_hinge.Normalize()
    to_lip.Normalize()
    bisector = to_hinge.Added(to_lip)
    bisector.Normalize()
    middle = centre.Translated(bisector.Multiplied(abs(radius)))
    return BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(hinge, middle, lip).Value()).Edge()
    ).Wire()


def _lofted_hood(stations, ys: float, t: float, overshoot: float):
    """Loft the sections, thicken to a uniform gauge, return the hood and its air.

    The gauge comes from an offset surface, and that is the point of these
    parts. A loft through offset curves is not the offset of a loft: at the
    nose, where the hood falls away at nearly 60 degrees along the run, an
    in-section offset would leave a wall of half the gauge. An explicit
    offset surface is exact everywhere, so the skin pair the recognizer has
    to find is a true one.

    Both skins are graphs over the deck, so the solid between them is just
    the difference of their two downward prisms -- no sewing, no shell
    repair -- and the inner prism is the air path. The two skins do not share
    a plan footprint, though: the inner is offset along a tilted normal, so
    at the hinge it stops short of the outer's boundary and at the lip it
    starts inboard, and the raw difference returns those strips as full-depth
    fins. The lip strip is disposed of by the shear trim, which is why the
    sections are lofted a millimetre past the shear line; the hinge strip is
    bounded below at the deck's back face instead, so what leaves is a wedge
    no taller than the gauge lying inside the deck slab, where the fuse
    swallows it -- the same accounting the hinge itself relies on.
    """
    loft = BRepOffsetAPI_ThruSections(False, False, 1.0e-6)
    for x, w, a in stations:
        loft.AddWire(_louver_arc_section(x, ys - overshoot, t, w + overshoot, a))
    loft.Build()

    skin = faces_of(loft.Shape())[0]
    u0, u1, v0, v1 = BRepTools.UVBounds_s(skin)
    outer_surface = BRep_Tool.Surface_s(skin)
    point = gp_Pnt()
    du = gp_Vec()
    dv = gp_Vec()
    outer_surface.D1(0.5 * (u0 + u1), 0.5 * (v0 + v1), point, du, dv)
    # The offset runs along the surface's own du x dv, which owes nothing to
    # the face's orientation, so the sign comes from that vector.
    offset = -t if du.Crossed(dv).Z() > 0.0 else t
    inner = BRepBuilderAPI_MakeFace(
        Geom_OffsetSurface(outer_surface, offset),
        u0,
        u1,
        v0,
        v1,
        Precision.Confusion_s(),
    ).Face()

    under = BRepPrimAPI_MakePrism(skin, gp_Vec(0.0, 0.0, -60.0)).Shape()
    core = BRepPrimAPI_MakePrism(inner, gp_Vec(0.0, 0.0, -60.0)).Shape()
    shear = _half_space((0.0, -1.0, 0.0), (0.0, ys, 0.0))
    below = box_between((-200.0, -200.0, -200.0), (400.0, 400.0, 0.0))

    hood = common(cut(under, fuse(core, below)), shear)
    return hood, common(core, shear)


def _place_lofted_louver(part: TopoDS_Shape, stations, ys: float, t: float):
    """Open the air path, then fuse the hood into the hole it left."""
    hood, air = _lofted_hood(stations, ys, t, 1.0)
    return fuse(cut(part, air), hood)


def _plateau_stations(x0, x1, a_max, w_max, w_min, ramp, count):
    """A symmetric ramp-plateau-ramp height profile, sampled along the run.

    The profile reaches zero with a finite slope, so the hood exits into the
    deck steeply enough to thicken cleanly. A raised cosine would be the
    obvious alternative and is the wrong one: it arrives horizontally, so the
    hood would run out tangentially into the deck and the fuse would be
    grazing over a wide band at each end -- the exact situation the
    hinge-depth rule exists to avoid. It reaches the plateau with zero slope,
    so it is smooth at both plateau corners, and the station count is chosen
    so stations land on those corners rather than straddling them.

    The width taper is linear in the same profile rather than raised to a
    fractional power: a fractional power has an unbounded derivative at zero,
    so the plan boundary leaves each run-out with a cusp, which resolves as a
    degenerate pole in the crest face. A symmetric hood has two run-outs and
    so would pay for it twice.
    """
    stations = []
    for index in range(count + 1):
        u = index / count
        if u < ramp:
            s = u / ramp
        elif u > 1.0 - ramp:
            s = (1.0 - u) / ramp
        else:
            s = 1.0
        f = math.sin(0.5 * math.pi * min(s, 1.0))
        stations.append((x0 + u * (x1 - x0), w_min + (w_max - w_min) * f, a_max * f))
    return stations


@fixture("sm_louver_standard")
def build_sm_louver_standard() -> TopoDS_Shape:
    """The tapered scoop: flush at the tail, rising to a rounded bullet nose.

    The height profile is an asymmetric half-sine reaching half height at 72
    percent of the run, which gives a long shallow ramp off the deck and a
    short domed nose, and is smooth throughout. A quarter-ellipse nose was
    tried first and could not be thickened: an ellipse reaches zero height
    with a vertical tangent, the last sections dropped four and a half
    millimetres over one and a half, and the offset self-intersected into
    degenerate edges. A half-sine arrives steeply but finitely.

    The hood is a spline with a spline offset partner, so no analytic gauge
    pair exists and the sampled skin test is the only way to see it. At seven
    millimetres it is over the height ceiling, which is the job: the rule has
    to reach a freeform hood at all.
    """
    t = 2.0
    ys, x0, x1 = 18.0, 16.0, 44.0
    a_max, w_max, w_min = 7.0, 11.0, 4.0
    u_nose = 0.72
    count = 24

    stations = []
    for index in range(count + 1):
        u = index / count
        if u <= u_nose:
            g = 0.5 * u / u_nose
        else:
            g = 0.5 + 0.5 * (u - u_nose) / (1.0 - u_nose)
        f = math.sin(math.pi * g)
        stations.append(
            (x0 + u * (x1 - x0), w_min + (w_max - w_min) * f**0.7, a_max * f)
        )
    return _place_lofted_louver(_bracket_profile(50.0, 30.0, 50.0), stations, ys, t)


@fixture("sm_louver_dome")
def build_sm_louver_dome() -> TopoDS_Shape:
    """A lens in plan, flush at both ends and peaking in the middle.

    Same loft machinery as the scoop, and deliberately so: the only
    difference is that its height and width profiles are symmetric. That
    makes it a double taper, which is the point of having it -- a skin test
    or a height measure that quietly assumed a monotonically growing hood
    would pass on the scoop and fail here.
    """
    t = 2.0
    ys, x0, x1 = 18.0, 16.0, 44.0
    a_max, w_max, w_min = 7.5, 11.0, 4.0
    count = 24

    stations = []
    for index in range(count + 1):
        u = index / count
        f = math.sin(math.pi * u)
        stations.append(
            (x0 + u * (x1 - x0), w_min + (w_max - w_min) * f**0.7, a_max * f)
        )
    return _place_lofted_louver(_bracket_profile(50.0, 30.0, 50.0), stations, ys, t)


@fixture("sm_louver_standard_sym")
def build_sm_louver_standard_sym() -> TopoDS_Shape:
    """The rounded louver done symmetrically: a plateau across the middle, no nose.

    A symmetric hood has no nose because a symmetric punch has none. Its two
    ends mirror each other, which is what a die actually draws.
    """
    t = 2.0
    ys, x0, x1 = 18.0, 16.0, 44.0
    stations = _plateau_stations(x0, x1, 7.0, 11.0, 4.0, 1.0 / 3.0, 30)
    return _place_lofted_louver(_bracket_profile(50.0, 30.0, 50.0), stations, ys, t)


@fixture("sm_louver_bank_curved")
def build_sm_louver_bank_curved() -> TopoDS_Shape:
    """Five lofted louvers on one deck: the pitch rule's only curved coverage.

    Every other lofted louver here carries a single hood, so the sampled
    skin test has only ever been measured with one freeform pair on the
    part. Five of them is where a search comparing every freeform face
    against every other would show itself.

    The station set is reproduced from the symmetric scoop rather than
    re-derived. It is not a free parameter: the same profile at a different
    station count silently produces a hood three times the volume whose
    bounding box runs far below the sheet, and it still builds, still
    writes, and still classifies as sheet.

    The hoods are cut and fused one at a time. They are disjoint in plan so
    the booleans do not interact, and doing them pairwise keeps each hood's
    own sanity check meaningful.
    """
    t = 2.0
    x0, x1 = 16.0, 44.0
    w_max = 11.0
    louvers = 5
    pitch = 13.0
    first_shear = 12.0

    stations = _plateau_stations(x0, x1, 7.0, w_max, 4.0, 1.0 / 3.0, 30)
    part = _bracket_profile(
        50.0, 30.0, first_shear + (louvers - 1) * pitch + w_max + 10.0
    )
    for index in range(louvers):
        part = _place_lofted_louver(part, stations, first_shear + index * pitch, t)
    return part


# -- the classifier boundary --------------------------------------------------


@fixture("sm_lookalike_billet")
def build_sm_lookalike_billet() -> TopoDS_Shape:
    """Sheet-like proportions on a solid billet: must not classify as sheet.

    There is no constant gauge and no bend pair, only a rounded edge and a
    milled pocket. It pins the boundary from the machining side.
    """
    part = box(0.0, 0.0, 0.0, 60.0, 40.0, 12.0)
    part = fuse(part, cylinder(30.0, 0.0, 12.0, 6.0, 40.0, (0, 1, 0)))
    return cut(part, box_between((8.0, 8.0, 6.0), (24.0, 32.0, 13.0)))


def _sharp_fold_shell() -> TopoDS_Shape:
    """A constant-gauge L drawn with square corners instead of bend radii.

    The shortcut some engineers take for sheet parts. No bend cylinders
    exist, so it is geometrically identical to a machined angle plate and
    must be read as one.
    """
    part = box_between((0.0, 0.0, 0.0), (40.0, 40.0, 2.0))
    return fuse(part, box_between((0.0, 0.0, 2.0), (2.0, 40.0, 30.0)))


@fixture("sm_hybrid_milled_pocket")
def build_sm_hybrid_milled_pocket() -> TopoDS_Shape:
    """A sharp-folded shell with a genuinely milled pocket in it.

    The pocket floor sits half a gauge above the bottom skin, so no
    constant-gauge pair exempts it and the solid-stock veto reverts the
    classification to milled. That makes this the only part in the corpus
    that can carry the "model the bend radii, move the machined features to
    secondary ops" advisory.
    """
    return cut(_sharp_fold_shell(), box_between((14.0, 12.0, 1.0), (30.0, 26.0, 3.0)))


# -- engineered parts ---------------------------------------------------------


@fixture("sm_e_chassis")
def build_sm_e_chassis() -> TopoDS_Shape:
    """A 1.5 mm electronics chassis: U section, return flanges, front hem.

    One bare bend end, one undersized hole, one hole near a bend, one tight
    hole pitch and one tall louver, each with clean controls beside it. The
    wall bands are built explicitly per edge rather than mirrored -- a mirror
    transform flips face orientation and breaks the inner-to-outer bend
    pairing that makes a fold recognizable at all.
    """
    t = 1.5
    outer_r = 3.0
    part = box_between((0, 0, 0), (160, 100, t))

    def wall_band(edge: float, sign: float) -> TopoDS_Shape:
        def place(u, v):
            return gp_Pnt(5, edge + sign * u, v)

        # Exact arc midpoints: a rounded value makes the three-point circle
        # about a micron off, and the under-fold cut then shaves a tangency
        # sliver that breaks bend recognition.
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
    part = _cut_under_fold(
        part, (4, -1, -1), (156, 3, t), (4, 3, 3), (1, 0, 0), 152, outer_r
    )
    part = _cut_under_fold(
        part, (4, 97, -1), (156, 101, t), (4, 97, 3), (1, 0, 0), 152, outer_r
    )

    # Front hem on the x=0 edge: open, r1.5, 8 mm return.
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
    part = _cut_under_fold(
        part, (-1, 9, -1), (3, 91, t), (3, 9, 3), (0, 1, 0), 82, outer_r
    )

    # Flush reliefs at both hem ends, kept clear of the wall-bend corners so
    # the corner rule stays out of reach.
    part = cut(part, box_between((-1, 6.5, -1), (4.6, 10, 7)))
    part = cut(part, box_between((-1, 90, -1), (4.6, 95.5, 7)))
    # Bend-end reliefs: both ends of the right bend and one end of the left.
    # The left bend's other end stays bare, which is the bait.
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
    # Keyhole mounts: a slot and a wider circle, one opening each.
    part = cut(part, box_between((48, 45, -1), (52, 55, t + 1)))
    part = hole(50, 58, 3.5)
    part = cut(part, box_between((108, 45, -1), (112, 55, t + 1)))
    part = hole(110, 42, 3.5)
    part = hole(80, 50, 0.6)
    part = hole(70, 93, 2.0)
    part = hole(95, 25, 2.0)
    part = hole(101.5, 25, 2.0)

    # Wall cutouts in the left wall: a rounded window and a trapezoid, the
    # latter a canary for the documented limit on non-rectangular openings.
    window = box_between((62, -1, 14), (88, 3, 26))
    window = fuse(window, box_between((60, -1, 16), (90, 3, 24)))
    for cx in (62.0, 88.0):
        for cz in (16.0, 24.0):
            window = fuse(window, cylinder(cx, -1, cz, 2.0, 4.0, (0, 1, 0)))
    part = cut(part, window)
    part = cut(
        part,
        _polygon_prism(
            [(110, -1, 16), (130, -1, 16), (127, -1, 24), (113, -1, 24)], 0, 4, 0
        ),
    )

    # Louvers on the right wall, hoods opening downward.
    def wall_louver(x0: float, x1: float, z0: float, z1: float, height: float):
        raised = fuse(part, box_between((x0, 100, z0), (x1, 100 + height, z1)))
        drawn = cut(
            raised,
            box_between((x0 + t, 97, z0 + t), (x1 - t, 100 + height - t, z1 - t)),
        )
        return cut(
            drawn, box_between((x0, 100, z0), (x1, 100 + height - t, z0 + t))
        )

    for x in (20.0, 50.0, 80.0, 110.0):
        part = wall_louver(x, x + 20, 12, 20, 4.0)
    return wall_louver(134, 154, 12, 20, 7.0)


@fixture("sm_hd_bracket")
def build_sm_hd_bracket() -> TopoDS_Shape:
    """A 3 mm Z-profile bracket with a gusset, hems, tabs and slots.

    The baits are a closed hem on heavy stock, a bare bend end on the
    gusset, a zero-radius stiffener, a narrow tab and an undersized hole.
    The main section is one constant-gauge wire from the teardrop hem
    through both Z bends to the closed hem, so no material is doubled
    anywhere along it.
    """
    t = 3.0
    part = (
        _Profile(_in_yz(0.0))
        .arc(7.5, 0, 0, 7.5, 7.5, 15)
        .line(7.5, 15, 20.5, 15)
        .line(20.5, 15, 20.5, 12)
        .line(20.5, 12, 7.5, 12)
        .arc(7.5, 12, 3, 7.5, 7.5, 3)
        .line(7.5, 3, 57, 3)
        .arc(57, 3, 59.121, 3.879, 60, 6)
        .line(60, 6, 60, 38)
        .arc(60, 38, 61.757, 42.243, 66, 44)
        .line(66, 44, 106, 44)
        .arc(106, 44, 110, 40, 106, 36)
        .line(106, 36, 91, 36)
        .line(91, 36, 91, 39)
        .line(91, 39, 106, 39)
        .arc(106, 39, 107, 40, 106, 41)
        .line(106, 41, 66, 41)
        .arc(66, 41, 63.879, 40.121, 63, 38)
        .line(63, 38, 63, 6)
        .arc(63, 6, 61.243, 1.757, 57, 0)
        .line(57, 0, 7.5, 0)
        .prism(90, 0, 0)
    )
    # Narrow the Z stack to x10..80. Beyond that the base continues flat, but
    # the fold strips there are cut away entirely, so they read relieved --
    # correctly, because nothing continues.
    part = cut(part, box_between((-1, 54, -1), (10, 115, 50)))
    part = cut(part, box_between((80, 54, -1), (91, 115, 50)))

    # A 45 degree gusset on the x=90 edge: bend r3 inner, r6 outer.
    diag = 0.70710678
    gusset = (
        _Profile(_in_xz(22.0))
        .arc(87, 0, 89.296, 0.457, 87 + 6 * diag, 6 - 6 * diag)
        .line(
            87 + 6 * diag,
            6 - 6 * diag,
            87 + 6 * diag + 25 * diag,
            6 - 6 * diag + 25 * diag,
        )
        .line(
            87 + 6 * diag + 25 * diag,
            6 - 6 * diag + 25 * diag,
            87 + 3 * diag + 25 * diag,
            6 - 3 * diag + 25 * diag,
        )
        .line(
            87 + 3 * diag + 25 * diag,
            6 - 3 * diag + 25 * diag,
            87 + 3 * diag,
            6 - 3 * diag,
        )
        .arc(87 + 3 * diag, 6 - 3 * diag, 88.148, 3.228, 87, 3)
        .line(87, 3, 87, 0)
        .prism(0, 24, 0)
    )
    part = fuse(part, gusset)
    part = _cut_under_fold(
        part, (87, 22, -1), (112, 46, t), (87, 22, 6), (0, 1, 0), 24, 6.0
    )
    # A flush V relief at the gusset's far end; the near end stays bare.
    part = cut(
        part, _polygon_prism([(85, 46, -1), (95, 46, -1), (90, 53, -1)], 0, 0, t + 2)
    )
    # A zero-radius stiffener: the sharp-fold bait.
    part = fuse(part, box_between((25, 30, t), (55, 33, 18)))
    # Assembly tabs off the shelf edge: three clean, one narrow.
    for y in (70.0, 84.0, 98.0):
        part = fuse(part, box_between((80, y, 41), (90, y + 8, 44)))
    part = fuse(part, box_between((-12, 84, 41), (10, 89, 44)))

    def hole(x: float, y: float, radius: float) -> TopoDS_Shape:
        return cut(part, cylinder(x, y, -1, radius, t + 2))

    for x in (25.0, 41.0, 57.0, 73.0):
        part = hole(x, 40, 3.3)
    part = hole(15, 44, 1.25)

    # Obround slots: a canary for the documented limit on slot recognition.
    def obround(x0: float, y0: float) -> TopoDS_Shape:
        slot = cut(part, box_between((x0 + 3, y0, -1), (x0 + 15, y0 + 6, t + 1)))
        for cx in (x0 + 3, x0 + 15):
            slot = cut(slot, cylinder(cx, y0 + 3, -1, 3.0, t + 2))
        return slot

    part = obround(28, 22)
    return obround(56, 22)
