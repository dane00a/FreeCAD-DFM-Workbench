# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Hole interactions, pocket variants, and the production parts h through r.

Three kinds of part live here. The first is the set that isolates one
awkward *interaction* -- a bore that breaks out into a cavity rather than
onto a floor, a bore that crosses a void wide enough to need drilling from
both ends, a pocket whose corners are tighter than any end mill. Those are
one-idea parts in the spirit of `basic`, just aimed at rules that need two
features to touch before they have anything to say.

The second is the production set: manifolds, housings, flanges, a bicycle
stem. They carry thirty or forty features apiece and no single rule owns
them; what they are for is the interference between rules, which is where
a recognizer that passes every isolated case still gets a real part wrong.

The third is the marking set, and it needs a word of explanation. Engraved
text is a swarm of features far below any sane minimum feature size, so a
part with a serial number on it will bury the report in findings unless the
text is recognised as text first. The lettering is drawn with a seven
segment stick font rather than a real typeface: system fonts differ between
machines and emit spline outlines, and neither is something a fixture can
afford.
"""

from __future__ import annotations

import math

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.Convert import Convert_ParameterisationType
from OCP.GC import GC_MakeArcOfCircle
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.GeomAPI import GeomAPI_PointsToBSpline, GeomAPI_PointsToBSplineSurface
from OCP.GeomConvert import GeomConvert
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Vec
from OCP.TColgp import TColgp_Array1OfPnt, TColgp_Array2OfPnt
from OCP.TopoDS import TopoDS_Shape

from . import fixture
from .shapes import (
    box,
    box_between,
    chamfer_edges,
    cone,
    cut,
    cylinder,
    edge_endpoints,
    edges_of,
    fillet_edges,
    fuse,
    moved,
    rotated,
    rounded_rect_prism,
    sphere,
)


# -- private vocabulary -------------------------------------------------------

#: Half the included angle of a 118 degree twist drill.
_DRILL_HALF_ANGLE = math.radians(59.0)


def _unit(direction) -> tuple:
    dx, dy, dz = direction
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    return dx / length, dy / length, dz / length


def _drilled_blind(x, y, z, direction, radius, depth) -> TopoDS_Shape:
    """A blind hole with the cone a twist drill actually leaves behind.

    A plain cylinder cut into a block bottoms out flat, and a flat floor is
    something no drill produces -- so a fixture built that way trips the
    flat-bottom rule for a reason that exists only in the model. The cone
    sits apex-forward past the nominal depth, base flush with it.
    """
    dx, dy, dz = _unit(direction)
    body = cylinder(x, y, z, radius, depth, (dx, dy, dz))
    point = cone(
        x + dx * depth,
        y + dy * depth,
        z + dz * depth,
        radius,
        0.0,
        radius / math.tan(_DRILL_HALF_ANGLE),
        (dx, dy, dz),
    )
    return fuse(body, point)


#: Which of the seven segments each character lights. Order is A top,
#: B top-right, C bottom-right, D bottom, E bottom-left, F top-left,
#: G middle. Letters follow display convention: S reads as 5, O as 0, and
#: the ones with no uppercase seven-segment form take their lowercase shape.
_SEVEN_SEG = {
    "0": (0, 1, 2, 3, 4, 5),
    "1": (1, 2),
    "2": (0, 1, 6, 4, 3),
    "3": (0, 1, 6, 2, 3),
    "4": (5, 6, 1, 2),
    "5": (0, 5, 6, 2, 3),
    "6": (0, 5, 6, 4, 3, 2),
    "7": (0, 1, 2),
    "8": (0, 1, 2, 3, 4, 5, 6),
    "9": (0, 1, 2, 3, 5, 6),
    "-": (6,),
    "B": (2, 3, 4, 5, 6),
    "C": (0, 3, 4, 5),
    "H": (1, 2, 4, 5, 6),
    "N": (2, 4, 6),
    "O": (0, 1, 2, 3, 4, 5),
    "R": (4, 6),
    "S": (0, 2, 3, 5, 6),
    "T": (3, 4, 5, 6),
    "U": (1, 2, 3, 4, 5),
}


def _seven_seg_text(text, origin_x, origin_y, char_h, stroke, z0, z1) -> TopoDS_Shape:
    """Lettering as one solid, drawn from segment strokes.

    The segments of a character overlap at its corners on purpose, and they
    are fused here rather than handed out as a compound: a compound of
    overlapping boxes used as a single boolean tool leaves internal shell
    faces at every overlap, which read back as sealed voids.
    """
    height = char_h
    width = 0.55 * char_h
    rects = (
        (0.0, width, height - stroke, height),
        (width - stroke, width, height / 2.0, height),
        (width - stroke, width, 0.0, height / 2.0),
        (0.0, width, 0.0, stroke),
        (0.0, stroke, 0.0, height / 2.0),
        (0.0, stroke, height / 2.0, height),
        (0.0, width, (height - stroke) / 2.0, (height + stroke) / 2.0),
    )
    advance = width + char_h * 0.25

    text_shape = None
    pen_x = origin_x
    for character in text:
        segments = _SEVEN_SEG.get(character) or _SEVEN_SEG.get(character.upper(), ())
        for index in segments:
            x0, x1, y0, y1 = rects[index]
            bar = box_between(
                (pen_x + x0, origin_y + y0, z0), (pen_x + x1, origin_y + y1, z1)
            )
            text_shape = bar if text_shape is None else fuse(text_shape, bar)
        pen_x += advance
    return text_shape


def _circle_of(edge):
    """The circle an edge lies on, or None if it is not circular."""
    curve = BRepAdaptor_Curve(edge)
    if curve.GetType() != GeomAbs_CurveType.GeomAbs_Circle:
        return None
    return curve.Circle()


def _once(select):
    """Wrap a selector so only the first edge it accepts is taken."""
    taken = []

    def matches(edge, start, end) -> bool:
        if taken or not select(edge, start, end):
            return False
        taken.append(edge)
        return True

    return matches


def _rim(centre_x, centre_y, z, radius, slack=0.05):
    """A selector for one bore rim, scoped by its centre and its radius."""

    def matches(edge, start, end) -> bool:
        circle = _circle_of(edge)
        if circle is None:
            return False
        if abs(circle.Radius() - radius) > slack:
            return False
        location = circle.Location()
        return (
            abs(location.Z() - z) <= slack
            and abs(location.X() - centre_x) <= slack
            and abs(location.Y() - centre_y) <= slack
        )

    return matches


def _on_outer(point, size, slack=0.1) -> bool:
    """Whether a point sits on the perimeter of a square plan footprint."""
    return (
        abs(point.X()) < slack
        or abs(point.X() - size) < slack
        or abs(point.Y()) < slack
        or abs(point.Y() - size) < slack
    )


# -- holes that run into something else ---------------------------------------


def _tilted_top_block() -> TopoDS_Shape:
    """A 60 mm cube with its top clipped off at 45 degrees."""
    clip = rotated(box(-10, -10, 25, 80.0, 80.0, 50.0), (30, 30, 25), (0, 1, 0), 45.0)
    return cut(box(0, 0, 0, 60.0, 60.0, 30.0), clip)


@fixture("hole_partial_entry")
def build_hole_partial_entry() -> TopoDS_Shape:
    """A vertical drill entering a sloped face, so it bites on one side first.

    The through hole leaves a square face at the bottom to drill from, which
    is the escape route the rule is supposed to notice.
    """
    return cut(_tilted_top_block(), cylinder(30, 30, -1, 3.0, 32.0))


@fixture("hole_partial_entry_blind")
def build_hole_partial_entry_blind() -> TopoDS_Shape:
    """The same sloped entry with nowhere else to drill from.

    Blind at 20 mm, so the angled top is the only surface the drill can
    start on and the rule has no perpendicular alternative to fall back to.
    """
    return cut(_tilted_top_block(), cylinder(30, 30, 10, 3.0, 22.0))


@fixture("hole_intersecting")
def build_hole_intersecting() -> TopoDS_Shape:
    """Two cross-bores meeting at the centre of the block."""
    block = box(0, 0, 0, 60.0, 60.0, 30.0)
    along_x = cylinder(-1, 30, 15, 4.0, 62.0, (1, 0, 0))
    along_y = cylinder(30, -1, 15, 4.0, 62.0, (0, 1, 0))
    return cut(block, along_x, along_y)


@fixture("hole_countersink_angle")
def build_hole_countersink_angle() -> TopoDS_Shape:
    """A 75 degree countersink: not 60, 82, 90, 100 or 120, so not a stock tool."""
    depth = 6.0 / math.tan(math.radians(37.5))
    sink = cone(25, 25, 25.0 - depth, 2.0, 8.0, depth)
    bore = cylinder(25, 25, -1, 2.0, 27.0)
    return cut(box(0, 0, 0, 50.0, 50.0, 25.0), fuse(sink, bore))


@fixture("hole_breakout_into_pocket")
def build_hole_breakout_into_pocket() -> TopoDS_Shape:
    """A side bore that ends on the curved wall of a bowl, not on a floor.

    The far end of the bore lands in the sphere's void with no cap face and
    no drill cone, and the cavity it opens into is a pocket rather than
    another bore -- the one arrangement that leaves the drill unsupported
    for a reason no hole-pair pairing can explain away.
    """
    block = box(0, 0, 0, 60.0, 50.0, 40.0)
    bowled = cut(block, sphere(30, 25, 32, 12.0))
    return cut(bowled, cylinder(-1, 25, 30, 3.0, 25.0, (1, 0, 0)))


@fixture("hole_crosses_wide_cavity")
def build_hole_crosses_wide_cavity() -> TopoDS_Shape:
    """A bore crossing a void wider than three diameters, so it needs two passes.

    The 20 mm chamber is wide enough that the drill loses radial guidance
    across it and must come in from both ends, but narrow enough that the
    two material segments still merge into one interrupted bore rather than
    reading as two unrelated holes.
    """
    block = box(0, 0, 0, 100.0, 40.0, 40.0)
    chambered = cut(block, box(40, -1, 10, 20.0, 42.0, 20.0))
    return cut(chambered, cylinder(-1, 20, 20, 3.0, 102.0, (1, 0, 0)))


# -- pockets, one variation each ----------------------------------------------


@fixture("pocket_island")
def build_pocket_island() -> TopoDS_Shape:
    """A pocket with a boss standing in the middle of its floor."""
    hollowed = cut(box(0, 0, 0, 60.0, 60.0, 20.0), box(10, 10, 8, 40.0, 40.0, 15.0))
    return fuse(hollowed, box(25, 25, 8, 10.0, 10.0, 10.0))


@fixture("pocket_multiple_floor_levels")
def build_pocket_multiple_floor_levels() -> TopoDS_Shape:
    """One opening, two floors: the halves overlap so the cavity stays single."""
    block = box(0, 0, 0, 70.0, 50.0, 25.0)
    shallow = box(10, 10, 10, 25.0, 30.0, 20.0)
    deep = box(35, 10, 5, 25.0, 30.0, 25.0)
    return cut(block, shallow, deep)


@fixture("pocket_narrow_opening")
def build_pocket_narrow_opening() -> TopoDS_Shape:
    """0.9 mm wide: narrower than the smallest cutter in the default library."""
    return cut(box(0, 0, 0, 40.0, 40.0, 20.0), box(10, 10, 12, 0.9, 20.0, 10.0))


@fixture("pocket_aspect_ratio")
def build_pocket_aspect_ratio() -> TopoDS_Shape:
    """55 mm deep on a 6 mm square: past any flute length worth quoting."""
    return cut(box(0, 0, 0, 30.0, 30.0, 60.0), box(12, 12, 5, 6.0, 6.0, 60.0))


@fixture("pocket_edm_inner_corner")
def build_pocket_edm_inner_corner() -> TopoDS_Shape:
    """R0.3 inner corners: no rotating cutter is that small, so this needs EDM.

    A genuine manufacturability failure rather than a cost signal, which is
    what separates it from the merely awkward radius parts.
    """
    pocket = rounded_rect_prism(15.0, 10.0, 45.0, 30.0, 0.3, 10.0, 11.0)
    return cut(box(0, 0, 0, 60.0, 40.0, 20.0), pocket)


def _square_pocket_plate() -> TopoDS_Shape:
    """The 50 mm plate with a 30 mm square pocket the corner parts share."""
    return cut(box(0, 0, 0, 50.0, 50.0, 25.0), box(10, 10, 5, 30.0, 30.0, 25.0))


@fixture("pocket_filleted_corners")
def build_pocket_filleted_corners() -> TopoDS_Shape:
    """R5 on the four vertical inner corners: the square-corner rule must stay quiet."""

    def inner_corner(edge, start, end) -> bool:
        if abs(start.X() - end.X()) > 0.1 or abs(start.Y() - end.Y()) > 0.1:
            return False
        if abs(start.Z() - end.Z()) < 0.1:
            return False
        on_x = abs(start.X() - 10.0) < 0.5 or abs(start.X() - 40.0) < 0.5
        on_y = abs(start.Y() - 10.0) < 0.5 or abs(start.Y() - 40.0) < 0.5
        return on_x and on_y

    return fillet_edges(_square_pocket_plate(), 5.0, inner_corner)


@fixture("pocket_chamfered_top")
def build_pocket_chamfered_top() -> TopoDS_Shape:
    """A chamfer round the opening with the inner corners left sharp.

    The pair to `pocket_filleted_corners`: breaking the top edge must not
    read as softening the corners, so the square-corner rule should still
    fire here.
    """

    def opening_edge(edge, start, end) -> bool:
        mid_x = (start.X() + end.X()) / 2.0
        mid_y = (start.Y() + end.Y()) / 2.0
        mid_z = (start.Z() + end.Z()) / 2.0
        if abs(mid_z - 25.0) > 0.1:
            return False
        on_x = abs(mid_x - 10.0) < 0.1 or abs(mid_x - 40.0) < 0.1
        on_y = abs(mid_y - 10.0) < 0.1 or abs(mid_y - 40.0) < 0.1
        return on_x or on_y

    return chamfer_edges(_square_pocket_plate(), 2.0, opening_edge)


@fixture("multi_pocket_stepped")
def build_multi_pocket_stepped() -> TopoDS_Shape:
    """Two pockets side by side at different depths."""
    block = box(0, 0, 0, 80.0, 50.0, 30.0)
    return cut(block, box(5, 12.5, 15, 25.0, 25.0, 20.0), box(40, 12.5, 10, 25.0, 25.0, 25.0))


@fixture("minimum_feature_size")
def build_minimum_feature_size() -> TopoDS_Shape:
    """A 0.3 mm slot: under the half-millimetre floor, and meant to be."""
    return cut(box(0, 0, 0, 30.0, 30.0, 10.0), box(14.85, 14.0, 5, 0.3, 2.0, 6.0))


# -- parts that carry a bit of everything --------------------------------------


@fixture("kitchen_sink")
def build_kitchen_sink() -> TopoDS_Shape:
    """One of each feature type on a single plate, as a smoke test."""
    plate = box(0, 0, 0, 100.0, 80.0, 30.0)
    plate = cut(plate, cylinder(25, 40, -1, 5.0, 32.0))
    plate = cut(plate, cylinder(75, 40, 15, 4.0, 20.0))
    plate = cut(plate, box(25, 15, 20, 20.0, 20.0, 15.0))
    plate = cut(plate, box(60, 56, 18, 45.0, 8.0, 15.0))
    plate = cut(plate, box(50, 0, 20, 55.0, 80.0, 15.0))

    def corner_edge(edge, start, end) -> bool:
        return (
            abs(start.X()) < 0.5
            and abs(start.Y()) < 0.5
            and abs(end.X()) < 0.5
            and abs(end.Y()) < 0.5
            and abs(end.Z() - start.Z()) > 10.0
        )

    plate = fillet_edges(plate, 2.0, _once(corner_edge))

    def left_top_edge(edge, start, end) -> bool:
        mid_x = (start.X() + end.X()) / 2.0
        mid_y = (start.Y() + end.Y()) / 2.0
        mid_z = (start.Z() + end.Z()) / 2.0
        return abs(mid_z - 30.0) < 0.5 and mid_x < 25.0 and mid_y < 1.0

    return chamfer_edges(plate, 1.5, _once(left_top_edge))


@fixture("inspection_test_block")
def build_inspection_test_block() -> TopoDS_Shape:
    """The clean part. Nothing above INFO may fire on it.

    A false-positive guard rather than a feature test: every edge is broken,
    the bore is generous, and any rule that speaks up here is wrong.
    """
    cube = box(0, 0, 0, 60.0, 60.0, 60.0)
    with_bore = cut(cube, cylinder(30, 30, -1, 10.0, 62.0))

    def outer_at(z):
        def matches(edge, start, end) -> bool:
            if abs(start.Z() - z) >= 0.1 or abs(end.Z() - z) >= 0.1:
                return False
            return _on_outer(start, 60.0) and _on_outer(end, 60.0)

        return matches

    rounded = fillet_edges(with_bore, 2.0, outer_at(60.0))
    return chamfer_edges(rounded, 1.0, outer_at(0.0))


@fixture("hydraulic_manifold_block")
def build_hydraulic_manifold_block() -> TopoDS_Shape:
    """Three orthogonal bores crossing at one point, plus a pair of port taps.

    The two taps are deliberately unalike: the first bottoms flat, which is
    a real concern worth reporting, and the second carries a proper drill
    point, which must not be reported. One part, both sides of the rule.
    """
    block = box(0, 0, 0, 60.0, 40.0, 40.0)
    block = cut(block, cylinder(-1, 20, 20, 4.0, 62.0, (1, 0, 0)))
    block = cut(block, cylinder(30, -1, 20, 4.0, 42.0, (0, 1, 0)))
    block = cut(block, cylinder(30, 20, -1, 3.0, 42.0))
    block = cut(block, cylinder(8, 8, 32, 2.0, 10.0))
    point = 2.0 / math.tan(_DRILL_HALF_ANGLE)
    drilled = fuse(
        cylinder(52, 32, 32, 2.0, 10.0), cone(52, 32, 32.0 - point, 0.0, 2.0, point)
    )
    return cut(block, drilled)


@fixture("linear_rail_block")
def build_linear_rail_block() -> TopoDS_Shape:
    """A rail carrier: top channel, a row of counterbored fixings, two dowels."""
    block = cut(box(0, 0, 0, 100.0, 40.0, 25.0), box(0, 10, 13, 100.0, 20.0, 15.0))
    for index in range(6):
        x = 8.0 + index * 17.0
        block = cut(block, cylinder(x, 20, 8, 3.5, 6.0))
        block = cut(block, cylinder(x, 20, -1, 2.0, 27.0))
    for x, y in ((5.0, 5.0), (95.0, 5.0)):
        block = cut(block, cylinder(x, y, -1, 4.0, 27.0))
    return block


@fixture("injection_mold_cavity_pattern")
def build_injection_mold_cavity_pattern() -> TopoDS_Shape:
    """A cavity with R0.6 corners: awkward enough to want a special cutter, not impossible."""
    block = cut(box(0, 0, 0, 80.0, 60.0, 30.0), box(20, 15, 10, 40.0, 30.0, 22.0))

    def inner_corner(edge, start, end) -> bool:
        if abs(end.Z() - start.Z()) <= 15.0:
            return False
        on_x = abs(start.X() - 20.0) < 0.1 or abs(start.X() - 60.0) < 0.1
        on_y = abs(start.Y() - 15.0) < 0.1 or abs(start.Y() - 45.0) < 0.1
        return on_x and on_y

    block = fillet_edges(block, 0.6, inner_corner)
    for x, y in ((30.0, 25.0), (50.0, 25.0), (30.0, 35.0), (50.0, 35.0)):
        block = cut(block, cylinder(x, y, -1, 3.0, 12.0))
    return block


@fixture("off_cardinal_viz_block")
def build_off_cardinal_viz_block() -> TopoDS_Shape:
    """One feature per off-axis direction, for the analytic renderers.

    Every primitive class -- cylinder, cone, sphere, torus -- on an axis
    that lines up with nothing, so a regression in the bounding-box or
    tessellation shortcuts shows up the moment anyone looks at it. The DFM
    findings this part provokes are expected and not the point.
    """
    cube = box(0, 0, 0, 100.0, 100.0, 100.0)

    # A diagonal through-bore entering the +X face and leaving through -Y.
    diagonal = _unit((-0.7071067811865, -0.7071067811865, 0.0))
    cube = cut(cube, cylinder(102.0, 52.0, 50.0, 4.0, 160.0, diagonal))

    # A compound-angle blind hole with its drill point, the direct repro
    # for the cone-rendering fault.
    into = _unit((0.5, -0.5, -0.7071067811865))
    entry = (30.0 - into[0], 70.0 - into[1], 100.0 - into[2])
    cube = cut(cube, _drilled_blind(entry[0], entry[1], entry[2], into, 3.0, 13.0))

    # A counterbore tilted 30 degrees in the XZ plane.
    into = _unit((-0.8660254037844, 0.0, -0.5))
    entry = (100.0 - into[0], 25.0 - into[1], 85.0 - into[2])
    outer = cylinder(entry[0], entry[1], entry[2], 6.0, 9.0, into)
    inner = cylinder(
        entry[0] + 8.0 * into[0],
        entry[1] + 8.0 * into[1],
        entry[2] + 8.0 * into[2],
        3.0,
        15.0,
        into,
    )
    cube = cut(cube, fuse(outer, inner))

    # A 90 degree countersink tilted 60 degrees in the XY plane.
    into = _unit((-0.5, 0.8660254037844, 0.0))
    entry = (100.0 - into[0], 50.0 - into[1], 30.0 - into[2])
    sink = cone(entry[0], entry[1], entry[2], 6.0, 3.0, 3.0, into)
    bore = cylinder(
        entry[0] + 3.0 * into[0],
        entry[1] + 3.0 * into[1],
        entry[2] + 3.0 * into[2],
        3.0,
        14.0,
        into,
    )
    cube = cut(cube, fuse(sink, bore))

    # A spherical pocket opening through the +Y face off-axis, and a second
    # one buried deep enough to leave a genuine overhang ring.
    cube = cut(
        cube,
        sphere(50.0, 100.0 + (-0.8660254037844) * 3.0, 30.0 + (-0.5) * 3.0, 8.0),
    )
    cube = cut(cube, sphere(20.0, 94.0, 60.0, 8.0))

    # A boss leaning out of the top face on a compound axis.
    cube = fuse(cube, cylinder(75.0, 75.0, 99.0, 5.0, 21.0, _unit((0.5, 0.5, 0.7071067811865))))

    # One R3 fillet, for a torus face. The edge is cardinal because walking
    # to an off-cardinal one after all those booleans is not something a
    # fixture should be doing.
    def top_x_edge(edge, start, end) -> bool:
        return (
            abs(start.X() - 100.0) < 0.01
            and abs(end.X() - 100.0) < 0.01
            and abs(start.Z() - 100.0) < 0.01
            and abs(end.Z() - 100.0) < 0.01
        )

    return fillet_edges(cube, 3.0, _once(top_x_edge))


@fixture("heat_sink_finned_block")
def build_heat_sink_finned_block() -> TopoDS_Shape:
    """Five 1.5 mm fins on a base plate: thin walls in a field, not in isolation."""
    plate = box(0, 0, 0, 60.0, 40.0, 8.0)
    for index in range(5):
        plate = fuse(plate, box(0, 7.25 + index * 7.0, 8, 60.0, 1.5, 12.0))
    return plate


@fixture("instrument_chassis")
def build_instrument_chassis() -> TopoDS_Shape:
    """A vented enclosure: features on five faces, so the setup count climbs."""
    block = cut(box(0, 0, 0, 100.0, 60.0, 30.0), box(10, 10, 10, 80.0, 40.0, 25.0))
    for i in range(4):
        for j in range(2):
            block = cut(block, cylinder(20.0 + i * 20.0, 20.0 + j * 20.0, 5, 1.5, 6.0))
    for i in range(4):
        block = cut(block, cylinder(15.0 + i * 22.0, -1, 20, 1.5, 12.0, (0, 1, 0)))
    for i in range(4):
        block = cut(block, cylinder(15.0 + i * 22.0, 61, 20, 1.5, 12.0, (0, -1, 0)))
    for x, y in ((5.0, 5.0), (95.0, 5.0), (5.0, 55.0), (95.0, 55.0)):
        block = cut(block, cylinder(x, y, -1, 1.25, 7.0))
    return block


@fixture("radial_bolt_pattern_hub")
def build_radial_bolt_pattern_hub() -> TopoDS_Shape:
    """A turned hub with eight taps on a bolt circle."""
    hub = cylinder(0, 0, 0, 30.0, 25.0)
    hub = cut(hub, cylinder(0, 0, -1, 10.0, 27.0))
    for index in range(8):
        angle = index * math.pi / 4.0
        hub = cut(
            hub,
            cylinder(22.5 * math.cos(angle), 22.5 * math.sin(angle), 10, 3.0, 16.0),
        )

    def outer_top_rim(edge, start, end) -> bool:
        mid_x = (start.X() + end.X()) / 2.0
        mid_y = (start.Y() + end.Y()) / 2.0
        mid_z = (start.Z() + end.Z()) / 2.0
        radius = math.sqrt(mid_x * mid_x + mid_y * mid_y)
        return abs(mid_z - 25.0) < 0.1 and abs(radius - 30.0) < 0.5

    return chamfer_edges(hub, 1.0, _once(outer_top_rim))


@fixture("precision_optical_mount")
def build_precision_optical_mount() -> TopoDS_Shape:
    """A mount plate: central bore, three adjusters at 120 degrees, rounded outline."""
    plate = box(0, 0, 0, 60.0, 60.0, 20.0)
    plate = cut(plate, cylinder(30, 30, 2, 15.0, 19.0))
    for index in range(3):
        angle = index * 2.0 * math.pi / 3.0
        plate = cut(
            plate,
            cylinder(
                30.0 + 22.5 * math.cos(angle), 30.0 + 22.5 * math.sin(angle), 12, 1.5, 9.0
            ),
        )
    for x, y in ((5.0, 5.0), (55.0, 5.0), (5.0, 55.0), (55.0, 55.0)):
        plate = cut(plate, cylinder(x, y, -1, 1.25, 22.0))

    def top_outer(edge, start, end) -> bool:
        if abs(start.Z() - 20.0) >= 0.1 or abs(end.Z() - 20.0) >= 0.1:
            return False
        return _on_outer(start, 60.0) and _on_outer(end, 60.0)

    return fillet_edges(plate, 3.0, top_outer)


@fixture("lightweight_grid_panel")
def build_lightweight_grid_panel() -> TopoDS_Shape:
    """A pocketed panel sitting on the material-removal warning boundary."""
    plate = box(0, 0, 0, 120.0, 80.0, 10.0)
    for i in range(4):
        for j in range(3):
            plate = cut(plate, box(5.0 + i * 30.0, 5.0 + j * 25.0, 1, 25.0, 25.0, 10.0))
    return plate


@fixture("micro_fluidic_channels")
def build_micro_fluidic_channels() -> TopoDS_Shape:
    """Millimetre channels with ports: small features in quantity."""
    plate = box(0, 0, 0, 80.0, 40.0, 10.0)
    for index in range(4):
        y0 = 8.0 + index * 8.0
        plate = cut(plate, box(5, y0, 7.5, 70.0, 1.0, 3.5))
        for port in range(3):
            plate = cut(plate, cylinder(15.0 + port * 25.0, y0 + 0.5, -1, 0.5, 12.0))
    return plate


# -- production parts ----------------------------------------------------------


@fixture("hydraulic_actuator_end_cap")
def build_hydraulic_actuator_end_cap() -> TopoDS_Shape:
    """A turned end cap with the full seal-groove vocabulary on one bore.

    Wiper seat, O-ring gland, rod bore, an internal thread bore with its
    relief undercut, and a bolt circle drilled from the front so it does
    not pierce the thread.
    """
    flange = cylinder(0, 0, 0, 42.5, 12.0)
    body = cylinder(0, 0, 12, 40.0, 38.0)
    cap = fuse(flange, body)

    cap = cut(cap, cylinder(0, 0, -0.001, 16.0, 6.001))
    cap = cut(cap, cylinder(0, 0, 6, 14.0, 44.1))

    gland = cut(cylinder(0, 0, 1.65, 17.83, 2.7), cylinder(0, 0, 1.55, 16.0, 2.9))
    cap = cut(cap, gland)

    cap = cut(cap, cylinder(0, 0, 32, 35.0, 18.1))
    relief = cut(cylinder(0, 0, 31.0, 35.75, 3.0), cylinder(0, 0, 30.9, 35.0, 3.2))
    cap = cut(cap, relief)

    for index in range(4):
        angle = index * math.pi / 2.0
        x = 36.0 * math.cos(angle)
        y = 36.0 * math.sin(angle)
        tap = fuse(cylinder(x, y, -0.1, 2.5, 15.1), cone(x, y, 15.0, 2.5, 0.0, 1.5))
        cap = cut(cap, tap)
    return cap


@fixture("hydraulic_manifold_block_v2")
def build_hydraulic_manifold_block_v2() -> TopoDS_Shape:
    """A production manifold: four valve cavities over a cross-drilled network.

    The valve pads are flat, not recessed. An earlier version milled a
    shallow seat for each valve and collected sixteen sharp-corner findings
    that the rules were right about and the part would never have had.
    """
    block = box(0, 0, 0, 140.0, 90.0, 60.0)
    block = cut(block, cylinder(-1, 45, 20, 4.0, 142.0, (1, 0, 0)))
    block = cut(block, cylinder(-1, 45, 32, 4.0, 142.0, (1, 0, 0)))

    block = cut(block, _drilled_blind(141, 25, 20, (-1, 0, 0), 4.0, 80.0))
    block = cut(block, _drilled_blind(141, 65, 20, (-1, 0, 0), 4.0, 80.0))
    block = cut(block, _drilled_blind(35, 45, -1, (0, 0, 1), 4.0, 41.0))
    block = cut(block, _drilled_blind(105, 45, -1, (0, 0, 1), 4.0, 41.0))

    for cx, cy in ((35.0, 25.0), (35.0, 65.0), (105.0, 25.0), (105.0, 65.0)):
        for ox, oy in ((-20.25, -6.35), (20.25, -6.35), (-20.25, 6.35), (20.25, 6.35)):
            tap = fuse(
                cylinder(cx + ox, cy + oy, 60.1, 2.1, 14.0, (0, 0, -1)),
                cone(cx + ox, cy + oy, 60.1 - 14.0, 2.1, 0.0, 1.26, (0, 0, -1)),
            )
            block = cut(block, tap)
        for ox, oy in ((-15.0, -10.0), (15.0, -10.0), (-15.0, 10.0), (15.0, 10.0)):
            block = cut(
                block, _drilled_blind(cx + ox, cy + oy, 60.1, (0, 0, -1), 4.0, 42.0)
            )

    for x, y in ((10.0, 10.0), (130.0, 10.0), (10.0, 80.0), (130.0, 80.0)):
        block = cut(block, cylinder(x, y, -1, 4.25, 62.0))
        block = cut(block, cylinder(x, y, -1, 7.5, 8.0))
    for x, y in ((30.0, 25.0), (110.0, 65.0)):
        block = cut(block, _drilled_blind(x, y, -1, (0, 0, 1), 3.0, 13.0))

    block = cut(block, _drilled_blind(-1, 45, 20, (1, 0, 0), 8.5, 22.0))
    block = cut(block, _drilled_blind(-1, 45, 32, (1, 0, 0), 8.5, 22.0))
    block = cut(block, _drilled_blind(125, 25, 60.1, (0, 0, -1), 6.75, 19.0))
    block = cut(block, _drilled_blind(125, 65, 60.1, (0, 0, -1), 6.75, 19.0))
    return block


@fixture("inertial_sensor_housing")
def build_inertial_sensor_housing() -> TopoDS_Shape:
    """A sealed sensor housing: face gland, pin holes, two of them on a compound angle."""
    block = box(0, 0, 0, 75.0, 75.0, 45.0)
    block = cut(block, box(7.5, 7.5, 13, 60.0, 60.0, 32.001))

    gland = cut(
        cylinder(37.5, 37.5, 43, 35.0, 2.1), cylinder(37.5, 37.5, 43, 32.5, 2.2)
    )
    block = cut(block, gland)

    for x, y in ((30.0, 30.0), (45.0, 30.0), (30.0, 45.0), (45.0, 45.0)):
        block = cut(block, _drilled_blind(x, y, 13.1, (0, 0, -1), 1.25, 8.0))

    polar = math.radians(30.0)
    azimuth = math.radians(45.0)
    tilted = (
        math.sin(polar) * math.cos(azimuth),
        math.sin(polar) * math.sin(azimuth),
        -math.cos(polar),
    )
    for x, y in ((37.5, 37.5), (32.5, 32.5)):
        block = cut(block, _drilled_blind(x, y, 13.2, tilted, 1.25, 10.0))

    for x, y in ((5.0, 5.0), (70.0, 5.0), (5.0, 70.0), (70.0, 70.0)):
        block = cut(block, cylinder(x, y, -1, 2.25, 47.0))
        block = cut(block, cylinder(x, y, -1, 4.0, 4.0))

    return cut(block, box(28.5, -0.1, 28.5, 18.0, 7.6, 9.0))


@fixture("optical_periscope_housing")
def build_optical_periscope_housing() -> TopoDS_Shape:
    """A long-span optical chassis with 45 degree mirror seats at each end.

    The lightening pockets are placed clear of the main slot on purpose: an
    earlier layout punched them through the slot floor, and the fragments
    left behind stopped the slot reading as one cavity at all.
    """
    body = box(0, 0, 0, 260.0, 80.0, 50.0)
    body = cut(body, box(30, 22.5, 20, 200.0, 35.0, 30.001))

    seat_1 = rotated(box(35, 30, 18, 30.0, 20.0, 4.0), (50, 40, 20), (0, 1, 0), 45.0)
    body = cut(body, seat_1)
    seat_2 = rotated(box(195, 30, 18, 30.0, 20.0, 4.0), (210, 40, 20), (0, 1, 0), -45.0)
    body = cut(body, seat_2)

    body = cut(body, cylinder(-0.1, 40, 25, 25.0, 30.1, (1, 0, 0)))
    body = cut(body, cylinder(260.1, 40, 25, 22.0, 25.1, (-1, 0, 0)))

    for index in range(4):
        angle = (index + 0.5) * math.pi / 2.0
        body = cut(
            body,
            _drilled_blind(
                -0.1, 40 + 23 * math.sin(angle), 25 + 23 * math.cos(angle),
                (1, 0, 0), 1.7, 8.1,
            ),
        )
    for y, z in ((20.0, 5.0), (60.0, 5.0), (20.0, 45.0), (60.0, 45.0)):
        body = cut(body, _drilled_blind(260.1, y, z, (-1, 0, 0), 1.25, 8.1))
    for x, y in ((15.0, 5.0), (245.0, 5.0), (15.0, 75.0), (245.0, 75.0)):
        body = cut(body, cylinder(x, y, -0.1, 2.75, 50.2))

    for x, y in ((60.0, 2.0), (140.0, 2.0), (60.0, 70.0), (140.0, 70.0)):
        body = cut(body, box(x, y, 30.0, 60.0, 8.0, 20.1))
    for x, y in ((70.0, 2.0), (140.0, 2.0), (70.0, 63.0), (140.0, 63.0)):
        body = cut(body, box(x, y, -0.1, 50.0, 15.0, 25.0))
    return body


@fixture("injection_mold_cavity_plate")
def build_injection_mold_cavity_plate() -> TopoDS_Shape:
    """A mold plate: four cavities, a sprue, and the cooling network under them."""
    block = box(0, 0, 0, 220.0, 160.0, 65.0)
    cavities = ((60.0, 40.0), (160.0, 40.0), (60.0, 120.0), (160.0, 120.0))
    for cx, cy in cavities:
        block = cut(block, box(cx - 30, cy - 20, 53, 60.0, 40.0, 12.1))
    block = cut(block, cone(110, 80, 55, 2.0, 3.0, 10.0))

    for y, z in ((40.0, 35.0), (120.0, 35.0), (40.0, 20.0), (120.0, 20.0)):
        block = cut(block, cylinder(-1, y, z, 4.0, 222.0, (1, 0, 0)))
    for x in (30.0, 190.0):
        block = cut(block, cylinder(x, -1, 27.5, 4.0, 162.0, (0, 1, 0)))

    for cx, cy in cavities:
        for dx in (-15.0, 15.0):
            block = cut(block, cylinder(cx + dx, cy, -1, 1.5, 56.0))
    for x, y in ((10.0, 10.0), (210.0, 10.0), (10.0, 150.0), (210.0, 150.0)):
        block = cut(block, cylinder(x, y, -1, 5.0, 67.0))
    for x, y in ((60.0, 80.0), (160.0, 80.0)):
        block = cut(block, cylinder(x, y, -1, 6.0, 26.0))
    return block


@fixture("photonic_fiber_v_groove_array")
def build_photonic_fiber_v_groove_array() -> TopoDS_Shape:
    """Eight fibre V-grooves at 250 micron pitch on a bonding substrate.

    The groove flank angle is the silicon crystal angle, 70.53 degrees
    included; the whole array is about the size of a fingernail, which is
    what makes it a useful floor for the small-feature rules.
    """
    block = box(0, 0, 0, 25.0, 12.0, 5.0)

    block = cut(block, box(0, 0, 4.95, 25.0, 12.0, 0.06))
    block = fuse(block, box(3.25, 5.0, 4.95, 18.5, 4.0, 0.05))

    half_top = 0.107 * math.tan(math.radians(35.265))
    for index in range(8):
        y_centre = 5.125 + index * 0.25
        polygon = BRepBuilderAPI_MakePolygon()
        polygon.Add(gp_Pnt(3.5, y_centre - half_top, 5.0))
        polygon.Add(gp_Pnt(3.5, y_centre + half_top, 5.0))
        polygon.Add(gp_Pnt(3.5, y_centre, 5.0 - 0.107))
        polygon.Close()
        face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
        groove = BRepPrimAPI_MakePrism(face, gp_Vec(18.0, 0, 0)).Shape()
        block = cut(block, groove)

    for x in (3.5, 21.5):
        dowel = fuse(
            cylinder(x, 6, -0.001, 0.5, 2.501), cone(x, 6, 2.500, 0.5, 0.0, 0.30)
        )
        block = cut(block, dowel)
    return block


@fixture("optics_spider_mount")
def build_optics_spider_mount() -> TopoDS_Shape:
    """A kinematic optics spider, built up from a hub rather than cut from a plate.

    Subtracting the diagonal gaps from a full square plate was the obvious
    construction and the wrong one: the corner cuts ate the hub and left
    four leg tips floating round an empty bore.
    """
    part = cylinder(0, 0, 0, 45.0, 15.0)
    for index in range(4):
        angle = index * (math.pi / 2.0)
        leg = box(44.0, -1.0, 0.0, 48.5, 2.0, 15.0)
        part = fuse(part, rotated(leg, (0, 0, 0), (0, 0, 1), math.degrees(angle)))
    for index in range(4):
        angle = index * (math.pi / 2.0)
        pad = box(85.0, -7.5, 0.0, 15.0, 15.0, 15.0)
        part = fuse(part, rotated(pad, (0, 0, 0), (0, 0, 1), math.degrees(angle)))

    part = cut(part, cylinder(0, 0, -0.001, 37.5, 15.002))
    part = cut(part, cylinder(0, 0, -0.001, 42.5, 5.001))

    for index in range(4):
        angle = index * (math.pi / 2.0)
        x = 92.5 * math.cos(angle)
        y = 92.5 * math.sin(angle)
        part = cut(part, cylinder(x, y, -0.001, 2.25, 15.002))
        part = cut(part, cylinder(x, y, 12.0 - 0.001, 4.0, 3.002))

    # Two of the three Kelvin seats are cut straight into the hub face; the
    # third is the raw face itself, which is all a Kelvin flat ever is.
    angle = math.radians(30.0)
    v_groove = box(-1.0, -2.0, 14.0, 2.0, 4.0, 1.001)
    part = cut(part, moved(v_groove, 41.0 * math.cos(angle), 41.0 * math.sin(angle), 0.0))
    angle = math.radians(150.0)
    slot = box(-1.0, -2.5, 14.0, 2.0, 5.0, 1.001)
    part = cut(part, moved(slot, 41.0 * math.cos(angle), 41.0 * math.sin(angle), 0.0))

    for index in range(4):
        angle = (index + 0.5) * (math.pi / 2.0)
        slit = box(38.0, -0.25, -0.001, 6.0, 0.5, 15.002)
        part = cut(part, rotated(slit, (0, 0, 0), (0, 0, 1), math.degrees(angle)))
    return part


@fixture("progressive_die_punch")
def build_progressive_die_punch() -> TopoDS_Shape:
    """A wire-EDM punch insert: a T profile with a land and a relief taper.

    The internal corners of the T are square, which no end mill can cut --
    the part exists to be an honest example of geometry that has to be
    wire-cut and not a merely expensive one.
    """
    block = box(0, 0, 0, 60.0, 50.0, 30.0)
    block = cut(block, cylinder(30, 20, -1, 0.75, 32.0))

    land = fuse(
        box(20.0, 22.0, 24.999, 20.0, 4.0, 5.002),
        box(27.0, 12.0, 24.999, 6.0, 10.0, 5.002),
    )
    block = cut(block, land)

    taper = 0.25
    relief = fuse(
        box(20.0 - taper, 22.0 - taper, -0.001, 20.0 + 2 * taper, 4.0 + 2 * taper, 25.002),
        box(27.0 - taper, 12.0 - taper, -0.001, 6.0 + 2 * taper, 10.0 + 2 * taper, 25.002),
    )
    block = cut(block, relief)

    for x, y in ((5.0, 5.0), (55.0, 5.0), (5.0, 45.0), (55.0, 45.0)):
        block = cut(block, cylinder(x, y, -1, 3.0, 32.0))
    block = cut(block, cylinder(10, 25, 31, 2.5, 13.0, (0, 0, -1)))
    return cut(block, cylinder(50, 25, 31, 2.5, 13.0, (0, 0, -1)))


@fixture("mtb_handlebar_stem")
def build_mtb_handlebar_stem() -> TopoDS_Shape:
    """A machined bicycle stem: two clamp bosses joined by a relieved extension.

    The pinch slit has to break through the steerer bore wall along its
    whole width or the clamp cannot close, so its inner edge sits a
    millimetre inside the bore.
    """
    stem = cylinder(0, 0, 0, 22.0, 40.0)
    stem = fuse(stem, box(0.0, -19.0, 8.0, 90.0, 38.0, 24.0))
    stem = fuse(stem, cylinder(90.0, -19.0, 25.0, 25.0, 38.0, (0, 1, 0)))
    stem = fuse(stem, box(-25.0, -16.0, 0.0, 13.0, 32.0, 40.0))

    stem = cut(stem, cylinder(0, 0, -0.1, 14.3, 40.2))
    stem = cut(stem, box(-25.5, -1.25, -0.1, 12.5, 2.5, 40.2))
    for z in (10.0, 30.0):
        stem = cut(stem, cylinder(-20.0, -17.0, z, 2.75, 34.0, (0, 1, 0)))

    stem = cut(stem, cylinder(90.0, -19.1, 25.0, 15.9, 38.2, (0, 1, 0)))
    stem = cut(stem, box(90.0, -19.1, -1.0, 30.0, 38.2, 52.0))

    radius = 2.1
    depth = 12.0
    point = radius / math.tan(_DRILL_HALF_ANGLE)
    for y in (-11.9, 11.9):
        for z in (4.5, 45.5):
            tap = fuse(
                cylinder(90.1, y, z, radius, depth, (-1, 0, 0)),
                cone(90.1 - depth, y, z, radius, 0.0, point, (-1, 0, 0)),
            )
            stem = cut(stem, tap)

    return cut(stem, box(25.0, -10.0, 7.9, 37.0, 20.0, 8.1))


@fixture("precision_spindle_end_cap")
def build_precision_spindle_end_cap() -> TopoDS_Shape:
    """A mill-turn end cap carrying three different groove families on one bore.

    A retaining ring groove inside the pilot, a thread relief undercut, and
    two face glands, plus an off-axis grease pool. The blends go on the
    clean stepped body before any of it is bored: filleting afterwards
    catches edges that the cuts created and were never meant to be blended.
    """
    part = fuse(cylinder(0, 0, 0, 42.5, 40.0), cylinder(0, 0, 35.0, 56.0, 5.0))

    def rim(radius, z):
        def matches(edge, start, end) -> bool:
            circle = _circle_of(edge)
            if circle is None:
                return False
            return (
                abs(circle.Radius() - radius) < 0.05
                and abs(circle.Location().Z() - z) < 0.05
            )

        return matches

    part = fillet_edges(part, 3.0, rim(42.5, 0.0))
    part = fillet_edges(part, 2.0, rim(42.5, 35.0))

    def flange_rim(edge, start, end) -> bool:
        circle = _circle_of(edge)
        return circle is not None and abs(circle.Radius() - 56.0) < 0.05

    part = chamfer_edges(part, 0.5, flange_rim)

    part = cut(part, cylinder(0, 0, 0, 22.5, 40.2))
    part = cut(part, cylinder(0, 0, -0.1, 27.5, 10.2))

    ring = cut(cylinder(0, 0, 3.15, 28.6, 1.7), cylinder(0, 0, 3.05, 27.4, 1.9))
    part = cut(part, ring)

    part = cut(part, cylinder(0, 0, 27.0, 24.2, 13.2))
    relief = cut(cylinder(0, 0, 24.5, 25.7, 2.5), cylinder(0, 0, 24.4, 22.5, 2.7))
    part = cut(part, relief)

    for inner_radius, outer_radius in ((32.5, 36.0), (37.5, 41.0)):
        gland = cut(
            cylinder(0, 0, -0.1, outer_radius, 2.9),
            cylinder(0, 0, -0.2, inner_radius, 3.1),
        )
        part = cut(part, gland)

    part = cut(part, sphere(34.0, 0.0, 49.0, 12.0))

    for index in range(4):
        angle = index * math.pi / 2.0
        x = 49.0 * math.cos(angle)
        y = 49.0 * math.sin(angle)
        part = cut(part, cylinder(x, y, 34.9, 3.25, 5.3))
        part = cut(part, cone(x, y, 40.1, 5.75, 3.25, 2.5, (0, 0, -1)))
    return part


@fixture("iso_k_vacuum_flange")
def build_iso_k_vacuum_flange() -> TopoDS_Shape:
    """A vacuum flange with an elastomer gland and a knife-edge witness chamfer.

    The rim break is R2 rather than R3: a larger fillet eats far enough into
    the face disc that the remaining material edge lands a millimetre and a
    half from the bolt rims, and the edge-distance rule then fires on a wall
    that is really four and a half millimetres thick.
    """
    part = cylinder(0, 0, 0, 82.5, 20.0)

    def any_circle(edge, start, end) -> bool:
        return _circle_of(edge) is not None

    part = fillet_edges(part, 2.0, any_circle)

    part = cut(part, cylinder(0, 0, 0, 51.0, 20.2))
    part = cut(part, cone(0, 0, 20.0, 52.0, 51.0, 1.0, (0, 0, -1)))
    part = cut(part, cone(0, 0, 0.0, 52.0, 51.0, 1.0))

    gland = cut(cylinder(0, 0, 16.8, 56.75, 3.3), cylinder(0, 0, 16.7, 54.0, 3.5))
    part = cut(part, gland)
    part = cut(part, cone(0, 0, 19.5, 53.71, 54.0, 0.51))

    for index in range(8):
        angle = index * math.pi / 4.0
        x = 72.5 * math.cos(angle)
        y = 72.5 * math.sin(angle)
        part = cut(part, cylinder(x, y, -0.1, 5.5, 20.2))
        part = cut(part, cone(x, y, 0.0, 6.0, 5.5, 0.5))
        part = cut(part, cone(x, y, 20.0, 6.0, 5.5, 0.5, (0, 0, -1)))

    for index in range(4):
        angle = math.pi / 8.0 + index * math.pi / 2.0
        x = 61.5 * math.cos(angle)
        y = 61.5 * math.sin(angle)
        part = cut(part, cylinder(x, y, -0.1, 2.5, 11.2))
        part = cut(part, cone(x, y, 11.1, 2.5, 0.0, 2.5 / math.tan(_DRILL_HALF_ANGLE)))
        port_relief = cut(
            cylinder(x, y, 6.5, 3.25, 1.5), cylinder(x, y, 6.4, 2.5, 1.7)
        )
        part = cut(part, port_relief)
        part = cut(part, cone(x, y, 0.0, 3.0, 2.5, 0.5))
    return part


@fixture("hygienic_manifold_plate")
def build_hygienic_manifold_plate() -> TopoDS_Shape:
    """The canonical filleted-rim part: hygienic radii on every hole class.

    Food and pharma standards want R3 on any rim product touches, and a
    filleted rim costs the bore its planar cap adjacency -- a torus gets in
    between. The two transfer ports are rounded on both faces and are the
    holes that vanish from the recognizer entirely; the four bolt holes are
    plain and are the control.
    """
    part = box(0, 0, 0, 110.0, 80.0, 20.0)

    # R5 on the vertical corners and R2 round both faces, in one pass so
    # the corner patches blend into each other.
    part = _blend_plate_edges(part)

    gland = cut(
        cylinder(55.0, 40.0, -0.1, 32.5, 2.9), cylinder(55.0, 40.0, -0.2, 29.0, 3.1)
    )
    part = cut(part, gland)

    for cx in (37.0, 73.0):
        part = cut(part, cylinder(cx, 40.0, -1.0, 6.0, 22.0))
        part = fillet_edges(part, 3.0, _rim(cx, 40.0, 20.0, 6.0))
        part = fillet_edges(part, 3.0, _rim(cx, 40.0, 0.0, 6.0))

    part = cut(part, cylinder(55.0, 52.0, 10.0, 8.0, 11.0))
    part = fillet_edges(part, 3.0, _rim(55.0, 52.0, 20.0, 8.0))

    part = cut(part, cylinder(55.0, 26.0, 12.0, 5.0, 9.0))
    part = fillet_edges(part, 2.0, _rim(55.0, 26.0, 12.0, 5.0))

    for cx, cy in ((12.0, 12.0), (98.0, 12.0), (12.0, 68.0), (98.0, 68.0)):
        part = cut(part, cylinder(cx, cy, -1.0, 3.3, 22.0))
        part = cut(part, cone(cx, cy, 20.0, 3.8, 3.3, 0.5, (0, 0, -1)))
    return part


def _blend_plate_edges(part: TopoDS_Shape) -> TopoDS_Shape:
    """R5 on the uprights and R2 round the faces, added in one fillet pass."""
    maker = BRepFilletAPI_MakeFillet(part)
    for edge in edges_of(part):
        start, end = edge_endpoints(edge)
        vertical = abs(start.Z() - end.Z()) > 1.0
        maker.Add(5.0 if vertical else 2.0, edge)
    maker.Build()
    return maker.Shape() if maker.IsDone() else part


# -- parts that carry lettering ------------------------------------------------


@fixture("pump_cover_part_marking")
def build_pump_cover_part_marking() -> TopoDS_Shape:
    """A pump cover with a raised casting number and an engraved serial.

    The engraving is 0.4 mm wide and 0.3 mm deep, under both the minimum
    feature size and the smallest end mill, which is exactly what real
    marking is. Without text recognition the report drowns in it, and that
    is the behaviour this part pins down.
    """
    plate = box_between((-50.0, -35.0, 0.0), (50.0, 35.0, 10.0))

    # Fused onto pristine topology, and starting 0.1 mm inside the plate so
    # the fuse never has to resolve a face sitting exactly on a face.
    plate = fuse(plate, _seven_seg_text("0426", -11.8, 15.0, 8.0, 2.0, 9.9, 11.0))

    plate = cut(plate, rounded_rect_prism(-13.0, -25.0, 13.0, -18.0, 2.0, 9.5, 0.6))
    plate = cut(plate, _seven_seg_text("26-0147", -10.7, -23.5, 4.0, 0.4, 9.2, 9.6))

    plate = cut(plate, cylinder(0.0, 0.0, -0.1, 10.0, 10.2))
    plate = cut(plate, cone(0.0, 0.0, -0.001, 10.5, 10.0, 0.501))
    plate = cut(plate, cone(0.0, 0.0, 10.001, 10.5, 10.0, 0.501, (0, 0, -1)))

    for sign_x in (-1, 1):
        for sign_y in (-1, 1):
            cx = sign_x * 42.0
            cy = sign_y * 26.0
            plate = cut(plate, cylinder(cx, cy, -0.1, 3.3, 10.2))
            plate = cut(plate, cone(cx, cy, -0.001, 3.8, 3.3, 0.501))
            plate = cut(plate, cone(cx, cy, 10.001, 3.8, 3.3, 0.501, (0, 0, -1)))
    return plate


@fixture("instrument_front_panel")
def build_instrument_front_panel() -> TopoDS_Shape:
    """A panel engraved on both faces: three front legends and a rear serial.

    The three front legends run together into one eleven-glyph cluster,
    which is the case that separates recognising a marking from recognising
    a word. The serial on the back proves a second host face is handled.
    """
    panel = box_between((-60.0, -25.0, 0.0), (60.0, 25.0, 4.0))

    for text, left in (("CH-1", -45.16), ("CH-2", -23.16), ("OUT", 16.24)):
        panel = cut(panel, _seven_seg_text(text, left, 8.0, 3.5, 0.5, 3.75, 4.1))
    panel = cut(panel, _seven_seg_text("SN-2608", -10.7, 13.0, 4.0, 0.4, -0.1, 0.3))

    panel = cut(panel, rounded_rect_prism(10.4, -11.5, 29.6, -0.5, 1.5, -0.1, 4.2))

    for cx, diameter in ((-40.0, 9.5), (-18.0, 9.5), (42.0, 3.2)):
        panel = cut(panel, cylinder(cx, -6.0, -0.1, diameter / 2.0, 4.2))

    for sign_x in (-1, 1):
        for sign_y in (-1, 1):
            cx = sign_x * 54.0
            cy = sign_y * 19.0
            panel = cut(panel, cylinder(cx, cy, -0.1, 1.75, 4.2))
            panel = cut(panel, cone(cx, cy, 4.001, 3.26, 1.75, 1.511, (0, 0, -1)))
    return panel


@fixture("relieved_logo_plaque")
def build_relieved_logo_plaque() -> TopoDS_Shape:
    """Raised lettering made by milling the background away round it.

    The letters are what is left standing, so today this reads as a pocket
    with islands. Locked as it is, so a future raised-logotype recognizer
    has a before and an after to compare.
    """
    plate = box_between((0, 0, 0), (70, 40, 8))
    relief = rounded_rect_prism(15.0, 12.0, 55.0, 28.0, 3.0, 6.5, 1.7)
    relief = cut(relief, _seven_seg_text("CNC", 24.2, 15.0, 10.0, 2.0, 6.4, 8.3))
    return cut(plate, relief)


@fixture("machine_base_angle_plate")
def build_machine_base_angle_plate() -> TopoDS_Shape:
    """A base with a stout upright and one deliberately spindly gusset.

    The 20 mm upright is fifteen times as tall as it is thick and must stay
    quiet under the absolute-thickness cap; the 5 mm web beside it is
    eighteen times and must not.
    """
    part = box_between((0, 0, 0), (300, 200, 25))
    part = fuse(part, box_between((0, 0, 25), (300, 20, 85)))
    return fuse(part, box_between((145, 20, 25), (150, 110, 75)))


# -- freeform surfaces ---------------------------------------------------------


@fixture("impeller_blade_hub")
def build_impeller_blade_hub() -> TopoDS_Shape:
    """A three-blade impeller whose channels are lofted, not the blades.

    Cutting a twisted void out of a turned blank keeps the blank's analytic
    faces analytic. Fusing the blades on instead -- the first attempt --
    shattered every cylinder into B-spline patches and left nothing for the
    turning recognizers to hold on to.
    """
    part = cylinder(0, 0, 0, 30.0, 12.0)
    part = fuse(part, cylinder(0, 0, 12, 22.0, 28.0))
    part = cut(part, cylinder(0, 0, -0.1, 4.0, 40.2))

    radii = (12.0, 12.0, 12.0, 16.5, 21.0, 21.0, 21.0, 16.5, 12.0)
    angles = (10.0, 37.0, 65.0, 68.0, 65.0, 37.0, 10.0, 7.0, 10.0)

    def kidney_wire(z, spin_degrees):
        points = TColgp_Array1OfPnt(1, 9)
        for index in range(9):
            angle = math.radians(angles[index] + spin_degrees)
            points.SetValue(
                index + 1,
                gp_Pnt(
                    radii[index] * math.cos(angle),
                    radii[index] * math.sin(angle),
                    z,
                ),
            )
        curve = GeomAPI_PointsToBSpline(points).Curve()
        return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(curve).Edge()).Wire()

    loft = BRepOffsetAPI_ThruSections(True, False)
    loft.AddWire(kidney_wire(13.0, 0.0))
    loft.AddWire(kidney_wire(40.2, 25.0))
    loft.Build()
    channel = loft.Shape()

    for index in range(3):
        part = cut(part, rotated(channel, (0, 0, 0), (0, 0, 1), index * 120.0))
    return part


@fixture("molded_style_cover")
def build_molded_style_cover() -> TopoDS_Shape:
    """A styled cover: a drafted side wall that bulges back, over a sculpted floor.

    Both freeform surfaces arrive as cutters rather than as built-up faces,
    which keeps everything else on the part analytic. The bulge is a genuine
    reverse draft against a +Z pull; the floor carries one valley tighter
    than the cutter that would have to finish it.
    """
    part = box_between((0, 0, 0), (70, 50, 22))

    wall_ys = (-5.0, 15.0, 35.0, 55.0)
    wall_zs = (-5.0, 8.0, 16.0, 27.0)
    wall_xs = (
        (70.1, 69.8, 69.6, 69.3),
        (70.1, 69.9, 70.2, 69.4),
        (70.1, 69.9, 70.8, 69.4),
        (70.1, 69.8, 69.6, 69.3),
    )
    grid = TColgp_Array2OfPnt(1, 4, 1, 4)
    for i in range(4):
        for j in range(4):
            grid.SetValue(i + 1, j + 1, gp_Pnt(wall_xs[i][j], wall_ys[i], wall_zs[j]))
    sheet = GeomAPI_PointsToBSplineSurface(grid).Surface()
    face = BRepBuilderAPI_MakeFace(sheet, 1e-6).Face()
    part = cut(part, BRepPrimAPI_MakePrism(face, gp_Vec(12, 0, 0)).Shape())

    part = cut(part, box_between((10, 10, 10), (60, 40, 22.1)))

    floor_xs = (8.0, 25.0, 42.0, 62.0)
    floor_ys = (8.0, 20.0, 32.0, 42.0)
    # The border sits below the flat floor it replaces. At 10.0 the two
    # surfaces were near enough coincident that the sheet's own wiggle left
    # tenth-millimetre hairline steps, and the engine was quite right to
    # call them sub-tool-scale corners.
    floor_zs = (
        (9.7, 9.7, 9.7, 9.7),
        (9.6, 7.9, 9.4, 9.7),
        (9.7, 9.3, 9.7, 9.7),
        (9.7, 9.7, 9.7, 9.7),
    )
    grid = TColgp_Array2OfPnt(1, 4, 1, 4)
    for i in range(4):
        for j in range(4):
            grid.SetValue(i + 1, j + 1, gp_Pnt(floor_xs[i], floor_ys[j], floor_zs[i][j]))
    sheet = GeomAPI_PointsToBSplineSurface(grid).Surface()
    face = BRepBuilderAPI_MakeFace(sheet, 1e-6).Face()
    part = cut(part, BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, 15)).Shape())

    for x in (5.0, 65.0):
        part = cut(part, cylinder(x, 5.0, -0.1, 2.25, 24.0))
    return part


@fixture("profiled_spindle_tight_valley")
def build_profiled_spindle_tight_valley() -> TopoDS_Shape:
    """A turned profile with one R0.3 concave valley.

    The valley is an arc converted to B-spline basis rather than
    interpolated through points: interpolation overshoots, and the whole
    point is a radius that is exactly 0.3 -- below the smallest insert nose
    with any safety factor at all.
    """
    valley_z = 30.0
    valley_r = 13.0

    arc = GC_MakeArcOfCircle(
        gp_Pnt(valley_r, 0, valley_z - 0.18),
        gp_Pnt(valley_r - 0.24, 0, valley_z),
        gp_Pnt(valley_r, 0, valley_z + 0.18),
    ).Value()
    valley = GeomConvert.CurveToBSplineCurve_s(
        arc, Convert_ParameterisationType.Convert_RationalC1
    )

    lower_points = TColgp_Array1OfPnt(1, 4)
    for index, (r, z) in enumerate(
        ((14.0, 0.0), (15.5, 10.0), (16.0, 20.0), (valley_r, valley_z - 0.18))
    ):
        lower_points.SetValue(index + 1, gp_Pnt(r, 0, z))
    lower = GeomAPI_PointsToBSpline(lower_points).Curve()

    upper_points = TColgp_Array1OfPnt(1, 4)
    for index, (r, z) in enumerate(
        ((valley_r, valley_z + 0.18), (15.0, 40.0), (13.5, 50.0), (12.0, 56.0))
    ):
        upper_points.SetValue(index + 1, gp_Pnt(r, 0, z))
    upper = GeomAPI_PointsToBSpline(upper_points).Curve()

    wire = BRepBuilderAPI_MakeWire()
    wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(14.0, 0, 0)).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(lower).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(valley).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(upper).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(12.0, 0, 56.0), gp_Pnt(0, 0, 56.0)).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 56.0), gp_Pnt(0, 0, 0)).Edge())

    profile = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    axis = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    return BRepPrimAPI_MakeRevol(profile, axis).Shape()
