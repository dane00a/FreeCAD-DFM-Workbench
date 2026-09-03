# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Real parts, and the controls that pin down the rules they exercise.

Where `basic.py` carries one feature each, most of these carry a job:
brackets, flanges, a servo valve body, a tube sheet with a hundred holes in
it. Between them they cover the awkward corners -- bosses whose walls are
part of the stock silhouette and so are not bosses at all, undercuts a
three-axis machine cannot reach, drill points that must not read as flat
bottoms, engraved text that must not read as a field of tiny pockets.

Several come in pairs. `cutter_radius_infeasible` and
`cutter_radius_suboptimal` are the same pocket with a different corner
radius, one below any cutter and one merely between two standard ones; the
pair is what stops the corner-radius rule being reworded into something
that fires on both or on neither.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GeomAPI import GeomAPI_PointsToBSpline, GeomAPI_PointsToBSplineSurface
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.TColgp import TColgp_Array1OfPnt, TColgp_Array2OfPnt
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

from . import fixture
from .shapes import (
    box,
    box_between,
    cone,
    cut,
    cylinder,
    fuse,
    polygon_prism,
    revolved_profile,
    rotated,
    rounded_rect_prism,
)


# -- local vocabulary ---------------------------------------------------------


def _explored_edges(shape: TopoDS_Shape) -> Iterable:
    """Edges in explorer order, repeats and all.

    An explorer visits an edge once per face that owns it, so a plain box
    yields twenty-four rather than twelve. The repetition is load-bearing
    here: `chamfer_edges` alternates its bottom angle on a running counter,
    so the order and the repeats are part of the part.
    """
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        yield TopoDS.Edge_s(explorer.Current())
        explorer.Next()


def _edge_points(edge):
    """Start, end and midpoint of an edge, or None if it carries no curve."""
    try:
        first, last = BRep_Tool.Range_s(edge)
        curve = BRep_Tool.Curve_s(edge, first, last)
    except Exception:
        return None
    if curve is None:
        return None
    return (
        curve.Value(first),
        curve.Value(last),
        curve.Value((first + last) / 2.0),
    )


def _drill_point_height(radius: float) -> float:
    """How far the tip of a 118 degree drill stands below the bore floor."""
    return radius / math.tan(math.radians(59.0))


def _bspline(points: Sequence[Sequence[float]]):
    """A B-spline through the given points, in the order given."""
    array = TColgp_Array1OfPnt(1, len(points))
    for index, (x, y, z) in enumerate(points, start=1):
        array.SetValue(index, gp_Pnt(x, y, z))
    return GeomAPI_PointsToBSpline(array).Curve()


def _wire_of(*edges) -> TopoDS_Shape:
    wire = BRepBuilderAPI_MakeWire()
    for edge in edges:
        wire.Add(edge)
    return wire.Wire()


# The stick font. Real fonts are avoided deliberately: they come off the
# host system, so a fixture built on Windows would not match one built on
# Linux, and their outlines are splines where a milled letterform is not.
# Segments run A top, B top-right, C bottom-right, D bottom, E bottom-left,
# F top-left, G middle, and are given as fractions of the character box.
_SEVEN_SEG_STROKES = {
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


def _seven_seg_text(
    text: str,
    origin_x: float,
    origin_y: float,
    char_h: float,
    stroke: float,
    z0: float,
    z1: float,
) -> TopoDS_Shape:
    """One solid spelling `text`, for fusing proud or cutting in.

    The strokes of a character overlap at its corners by construction, so
    they are fused here rather than handed over as a compound. A compound
    of overlapping boxes used as a single boolean tool leaves the internal
    faces of those overlaps behind, and the void recognizer reads every one
    of them as a sealed cavity.
    """
    width = 0.55 * char_h
    thickness = stroke
    segments = (
        (0.0, width, char_h - thickness, char_h),
        (width - thickness, width, char_h / 2.0, char_h),
        (width - thickness, width, 0.0, char_h / 2.0),
        (0.0, width, 0.0, thickness),
        (0.0, thickness, 0.0, char_h / 2.0),
        (0.0, thickness, char_h / 2.0, char_h),
        (0.0, width, (char_h - thickness) / 2.0, (char_h + thickness) / 2.0),
    )
    advance = width + char_h * 0.25

    solid = None
    cursor = origin_x
    for character in text:
        for index in _SEVEN_SEG_STROKES.get(character.upper(), ()):
            x0, x1, y0, y1 = segments[index]
            stroke_box = box_between(
                (cursor + x0, origin_y + y0, z0),
                (cursor + x1, origin_y + y1, z1),
            )
            solid = stroke_box if solid is None else fuse(solid, stroke_box)
        cursor += advance
    return solid


# -- edge treatments ----------------------------------------------------------


@fixture("fillet_edges")
def build_fillet_edges() -> TopoDS_Shape:
    """One block, five different fillet radii.

    The top four edges stay at a uniform R3, which is the classic case and
    what the older tests look for. The bottom four each get their own
    radius, so every corner down there blends two mismatched arcs: R0.5 is
    below any sensible cutter, R1 wants a 2 mm end mill, R2.5 is
    comfortable and R5 is easy with anything. Filleting all twelve in one
    pass fails on the corner topology, hence the two passes.
    """
    block = box(0, 0, 0, 50.0, 50.0, 25.0)

    top = BRepFilletAPI_MakeFillet(block)
    for edge in _explored_edges(block):
        points = _edge_points(edge)
        if points is None:
            continue
        if abs(points[2].Z() - 25.0) < 0.5:
            top.Add(3.0, edge)
    top.Build()
    if not top.IsDone():
        return block
    after_top = top.Shape()

    bottom = BRepFilletAPI_MakeFillet(after_top)
    for edge in _explored_edges(after_top):
        points = _edge_points(edge)
        if points is None:
            continue
        mid = points[2]
        if abs(mid.Z()) > 0.5:
            continue
        if abs(mid.Y()) < 0.5:
            radius = 0.5
        elif abs(mid.X() - 50.0) < 0.5:
            radius = 1.0
        elif abs(mid.Y() - 50.0) < 0.5:
            radius = 2.5
        elif abs(mid.X()) < 0.5:
            radius = 5.0
        else:
            continue
        bottom.Add(radius, edge)
    bottom.Build()
    return bottom.Shape() if bottom.IsDone() else after_top


@fixture("chamfer_edges")
def build_chamfer_edges() -> TopoDS_Shape:
    """Symmetric chamfers on top, asymmetric ones underneath.

    The top four are the ordinary 45 degree, 2 mm case. The bottom four
    alternate 30 and 60 degrees measured from the bottom face, so the blend
    recognizer has to work from the two distances rather than assume the
    easy angle.
    """
    block = box(0, 0, 0, 50.0, 50.0, 25.0)

    top = BRepFilletAPI_MakeChamfer(block)
    for edge in _explored_edges(block):
        points = _edge_points(edge)
        if points is None:
            continue
        if abs(points[2].Z() - 25.0) < 0.5:
            top.Add(2.0, edge)
    top.Build()
    if not top.IsDone():
        return block
    after_top = top.Shape()

    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(after_top, TopAbs_EDGE, TopAbs_FACE, edge_faces)

    bottom = BRepFilletAPI_MakeChamfer(after_top)
    index = 0
    for edge in _explored_edges(after_top):
        points = _edge_points(edge)
        if points is None:
            continue
        if abs(points[2].Z()) > 0.5:
            continue
        if not edge_faces.Contains(edge):
            continue
        # An asymmetric chamfer is quoted as two distances against one
        # named face, so the bottom face has to be found before it can be
        # asked for.
        reference = None
        for shape in edge_faces.FindFromKey(edge):
            face = TopoDS.Face_s(shape)
            surface = BRepAdaptor_Surface(face, True)
            if surface.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
                continue
            plane = surface.Plane()
            if (
                abs(plane.Location().Z()) < 0.1
                and abs(abs(plane.Axis().Direction().Z()) - 1.0) < 0.01
            ):
                reference = face
                break
        if reference is None:
            continue
        if index % 2 == 0:
            far = 2.0 * math.tan(math.pi / 6.0)
        else:
            far = 2.0 * math.tan(math.pi / 3.0)
        bottom.Add(2.0, far, edge, reference)
        index += 1
    bottom.Build()
    return bottom.Shape() if bottom.IsDone() else after_top


# -- drill points -------------------------------------------------------------
#
# A drill does not leave a flat floor, it leaves a 118 degree cone. These
# three are the negative controls for the flat-bottom rule: it has to stay
# quiet on every one of them.


@fixture("blind_hole_drill_point")
def build_blind_hole_drill_point() -> TopoDS_Shape:
    """A blind bore ending the way a drill would end it.

    Diameter is 9 rather than 8 on purpose. 8 is the tap drill for
    3/8-16 UNC, and the hole would come back tagged as a thread, which
    muddies the one thing this part is here to say.
    """
    bore = cylinder(25.0, 25.0, 10.0, 4.5, 16.0)
    tip = cone(25.0, 25.0, 7.3, 0.0, 4.5, 2.7)
    return cut(box(0, 0, 0, 50.0, 50.0, 25.0), fuse(bore, tip))


@fixture("counterbore_drill_point")
def build_counterbore_drill_point() -> TopoDS_Shape:
    """The same argument one step further in: a counterbore over a drilled hole."""
    outer = cylinder(25.0, 25.0, 17.0, 5.0, 9.0)
    inner = cylinder(25.0, 25.0, 5.0, 3.0, 12.0)
    tip = cone(25.0, 25.0, 3.2, 0.0, 3.0, 1.8)
    return cut(box(0, 0, 0, 50.0, 50.0, 25.0), fuse(outer, fuse(inner, tip)))


@fixture("countersink_drill_point")
def build_countersink_drill_point() -> TopoDS_Shape:
    """A cone at each end of one bore: countersunk entry, drilled floor.

    Mostly a rendering case. The recognizer takes whichever cone it meets
    first and calls the whole thing a blind hole with no flat bottom, which
    is the right answer for the rule even if it is not the whole story.
    """
    csk = cone(25.0, 25.0, 22.0, 3.0, 8.0, 3.0)
    bore = cylinder(25.0, 25.0, 10.0, 3.0, 12.0)
    tip = cone(25.0, 25.0, 8.2, 0.0, 3.0, 1.8)
    return cut(box(0, 0, 0, 50.0, 50.0, 25.0), fuse(fuse(csk, bore), tip))


# -- bosses -------------------------------------------------------------------


@fixture("boss_flush_edge")
def build_boss_flush_edge() -> TopoDS_Shape:
    """A pad whose back wall is the plate's own outer face.

    That wall was never cut, it is leftover stock, so the pad is a step at
    the edge and not a freestanding boss. The recognizer has to turn it
    down.
    """
    plate = box(0, 0, 0, 60.0, 60.0, 5.0)
    return fuse(plate, box(22.5, 0.0, 5.0, 15.0, 10.0, 8.0))


@fixture("boss_flush_corner")
def build_boss_flush_corner() -> TopoDS_Shape:
    """The same argument with two walls flush instead of one."""
    plate = box(0, 0, 0, 60.0, 60.0, 5.0)
    return fuse(plate, box(0.0, 0.0, 5.0, 15.0, 15.0, 8.0))


@fixture("boss_on_stepped_plate")
def build_boss_on_stepped_plate() -> TopoDS_Shape:
    """The positive control for the two above.

    A round boss on the raised half of a stepped plate: none of its wall
    belongs to the outer silhouette, so it is a real boss and has to be
    admitted even though what it stands on is not flat.
    """
    base = box(0, 0, 0, 80.0, 60.0, 8.0)
    stepped = cut(base, box(0.0, 0.0, 3.0, 40.0, 60.0, 6.0))
    return fuse(stepped, cylinder(60.0, 30.0, 8.0, 5.0, 12.0))


@fixture("boss_wall_thickness")
def build_boss_wall_thickness() -> TopoDS_Shape:
    """A 2.4 mm pin: under the 3 mm minimum, but only 3.3 diameters tall.

    Kept short deliberately, so it trips the thickness rule and nothing
    else.
    """
    return fuse(box(0, 0, 0, 50.0, 50.0, 5.0), cylinder(25.0, 25.0, 5.0, 1.2, 8.0))


@fixture("boss_field_tall_and_thin")
def build_boss_field_tall_and_thin() -> TopoDS_Shape:
    """Two problem bosses on one plate, each failing a different way.

    The tall one is 4.5 diameters high and will chatter; the pin is under
    3 mm across and will snap. Each is proportioned to trip exactly one
    rule -- the tall boss is fat enough to pass the thickness check, the
    pin short enough to pass the height check.
    """
    plate = box(0, 0, 0, 90.0, 60.0, 8.0)
    tall = cylinder(25.0, 30.0, 8.0, 5.0, 45.0)
    pin = cylinder(65.0, 30.0, 8.0, 1.25, 8.0)
    return fuse(fuse(plate, tall), pin)


@fixture("boss_undercut")
def build_boss_undercut() -> TopoDS_Shape:
    """A boss leaning 30 degrees off vertical, so its own flank shadows its root."""
    plate = box(0, 0, 0, 60.0, 60.0, 8.0)
    boss = cylinder(30.0, 30.0, 8.0, 6.0, 20.0)
    return fuse(plate, rotated(boss, (30.0, 30.0, 8.0), (1, 0, 0), 30.0))


# -- pockets and corner radii -------------------------------------------------


@fixture("cutter_radius_infeasible")
def build_cutter_radius_infeasible() -> TopoDS_Shape:
    """A pocket with R0.3 inside corners: smaller than any cutter that would survive."""
    pocket = cut(
        box(0, 0, 0, 50.0, 50.0, 20.0), box(10.0, 10.0, 8.0, 30.0, 30.0, 15.0)
    )
    return _fillet_pocket_corners(pocket, 0.3)


@fixture("cutter_radius_suboptimal")
def build_cutter_radius_suboptimal() -> TopoDS_Shape:
    """The same pocket at R1.2, which falls between two stock end mills.

    R1.0 and R1.25 both exist; 1.2 does not, so the corner is machinable
    but only with a tool somebody has to order. The pair with
    `cutter_radius_infeasible` is what keeps the two severities apart.
    """
    pocket = cut(
        box(0, 0, 0, 50.0, 50.0, 20.0), box(10.0, 10.0, 8.0, 30.0, 30.0, 15.0)
    )
    return _fillet_pocket_corners(pocket, 1.2)


def _fillet_pocket_corners(shape: TopoDS_Shape, radius: float) -> TopoDS_Shape:
    """Round the four upright corners of the 30 x 30 pocket the pair share."""
    fillet = BRepFilletAPI_MakeFillet(shape)
    for edge in _explored_edges(shape):
        points = _edge_points(edge)
        if points is None:
            continue
        start, end = points[0], points[1]
        if abs(end.Z() - start.Z()) <= 10.0:
            continue
        if abs(start.X() - 10.0) >= 0.1 and abs(start.X() - 40.0) >= 0.1:
            continue
        if abs(start.Y() - 10.0) >= 0.1 and abs(start.Y() - 40.0) >= 0.1:
            continue
        fillet.Add(radius, edge)
    fillet.Build()
    return fillet.Shape() if fillet.IsDone() else shape


@fixture("aerospace_pocket_pyramid")
def build_aerospace_pocket_pyramid() -> TopoDS_Shape:
    """Three pockets stepping down inside one another, as a lightening pattern is."""
    plate = box(0, 0, 0, 100.0, 80.0, 15.0)
    plate = cut(plate, box(10.0, 10.0, 11.0, 80.0, 60.0, 6.0))
    plate = cut(plate, box(20.0, 20.0, 7.0, 60.0, 40.0, 6.0))
    return cut(plate, box(40.0, 30.0, 3.0, 20.0, 20.0, 6.0))


@fixture("flexure_thin_blade")
def build_flexure_thin_blade() -> TopoDS_Shape:
    """Two 1 mm slots leaving a 4 mm blade between them.

    A flexure is thin on purpose, which is exactly the case the thin-wall
    rule has to be able to be told about rather than simply flagging.
    """
    block = box(0, 0, 0, 60.0, 40.0, 20.0)
    block = cut(block, box(5.0, 0.0, 8.0, 50.0, 40.0, 1.0))
    return cut(block, box(5.0, 0.0, 13.0, 50.0, 40.0, 1.0))


# -- undercuts ----------------------------------------------------------------


@fixture("bracket_with_undercut_pocket")
def build_bracket_with_undercut_pocket() -> TopoDS_Shape:
    """A T-slot: a 10 mm mouth over a 25 mm chamber, with a cross-bore through it.

    Nothing that fits through the opening can reach the corners of the
    chamber, which is what makes it an undercut rather than a deep pocket.
    """
    block = box(0, 0, 0, 60.0, 60.0, 30.0)
    block = cut(block, box(25.0, 25.0, 15.0, 10.0, 10.0, 16.0))
    block = cut(block, box(17.5, 17.5, 5.0, 25.0, 25.0, 10.0))
    return cut(block, cylinder(-1.0, 30.0, 10.0, 3.0, 62.0, (1, 0, 0)))


@fixture("complex_undercut_part_v2")
def build_complex_undercut_part_v2() -> TopoDS_Shape:
    """Two unrelated undercuts on one block: a dovetail on the side, a T-slot underneath.

    One undercut proves the recognizer fires. Two facing different ways
    prove it does not stop after the first.
    """
    block = box(0, 0, 0, 80.0, 60.0, 40.0)
    block = cut(block, box(70.0, 20.0, 35.0, 11.0, 20.0, 6.0))
    block = cut(block, box(70.0, 15.0, 20.0, 11.0, 30.0, 15.0))
    block = cut(block, box(25.0, 25.0, -1.0, 10.0, 10.0, 11.0))
    return cut(block, box(17.5, 17.5, 10.0, 25.0, 25.0, 10.0))


# -- setups and tool access ---------------------------------------------------


@fixture("four_sided_milled_block")
def build_four_sided_milled_block() -> TopoDS_Shape:
    """A pocket on each of the four upright faces plus a hole down the middle.

    Five approach directions, so five setups, which is where the setup
    count rule starts to have an opinion.
    """
    block = box(0, 0, 0, 60.0, 60.0, 40.0)
    block = cut(block, box(-1.0, 15.0, 10.0, 9.0, 30.0, 20.0))
    block = cut(block, box(52.0, 15.0, 10.0, 9.0, 30.0, 20.0))
    block = cut(block, box(15.0, -1.0, 10.0, 30.0, 9.0, 20.0))
    block = cut(block, box(15.0, 52.0, 10.0, 30.0, 9.0, 20.0))
    return cut(block, cylinder(30.0, 30.0, -1.0, 5.0, 42.0))


@fixture("four_axis_hex_block")
def build_four_axis_hex_block() -> TopoDS_Shape:
    """A hex prism drilled normal to each of its six flats.

    One rotary axis about Z can bring any flat vertical, so this is the
    honest four-axis case: six approach directions, 60 degrees apart, only
    two of which land on a cardinal direction. The bores end in drill-point
    cones so the flat-bottom rule stays quiet on radial holes too.
    """
    corner_radius = 25.0
    height = 60.0
    face_distance = corner_radius * math.cos(math.pi / 6.0)

    corners = [
        (
            corner_radius * math.cos(index * math.pi / 3.0),
            corner_radius * math.sin(index * math.pi / 3.0),
        )
        for index in range(6)
    ]
    prism = polygon_prism(corners, 0.0, height)

    radius = 3.0
    bore_length = 9.0
    cone_height = _drill_point_height(radius)
    for index in range(6):
        azimuth = math.pi / 6.0 + index * math.pi / 3.0
        outward_x = math.cos(azimuth)
        outward_y = math.sin(azimuth)
        inward = (-outward_x, -outward_y, 0.0)
        entry_x = (face_distance + 1.0) * outward_x
        entry_y = (face_distance + 1.0) * outward_y
        entry_z = height / 2.0
        bore = cylinder(entry_x, entry_y, entry_z, radius, bore_length, inward)
        tip = cone(
            entry_x + bore_length * inward[0],
            entry_y + bore_length * inward[1],
            entry_z,
            radius,
            0.0,
            cone_height,
            inward,
        )
        prism = cut(prism, fuse(bore, tip))
    return prism


@fixture("five_axis_compound_angles")
def build_five_axis_compound_angles() -> TopoDS_Shape:
    """Four holes, each at its own polar angle and its own azimuth.

    No single rotary axis brings all four upright, so this one genuinely
    needs two continuous axes. The entry points are chosen so each 31 mm
    drill stays inside the block.
    """
    block = box(0, 0, 0, 60.0, 60.0, 40.0)
    holes = (
        (30.0, 0.0, 25.0, 15.0),
        (45.0, 60.0, 15.0, 30.0),
        (60.0, 120.0, 30.0, 45.0),
        (75.0, 240.0, 30.0, 25.0),
    )
    for polar_deg, azimuth_deg, entry_x, entry_y in holes:
        polar = math.radians(polar_deg)
        azimuth = math.radians(azimuth_deg)
        approach = (
            math.sin(polar) * math.cos(azimuth),
            math.sin(polar) * math.sin(azimuth),
            math.cos(polar),
        )
        into_part = (-approach[0], -approach[1], -approach[2])
        drill = cylinder(
            entry_x + approach[0],
            entry_y + approach[1],
            40.0 + approach[2],
            4.0,
            31.0,
            into_part,
        )
        block = cut(block, drill)
    return block


@fixture("five_axis_candidate")
def build_five_axis_candidate() -> TopoDS_Shape:
    """One tilted hole plus three cardinal features -- the near miss.

    The name is the one the part shipped under, and it overstates the case:
    a single tilted feature alongside cardinal ones indexes fine on three
    axes plus a rotary. It sits next to `five_axis_compound_angles` so the
    setup rule has to tell a 3+1 job from one that really needs five.
    """
    block = box(0, 0, 0, 60.0, 60.0, 40.0)
    tilted = rotated(
        cylinder(15.0, 30.0, -10.0, 5.0, 60.0), (15.0, 30.0, 20.0), (0, 1, 0), 30.0
    )
    block = cut(block, tilted)
    block = cut(block, cylinder(-1.0, 45.0, 30.0, 4.0, 20.0, (1, 0, 0)))
    block = cut(block, cylinder(45.0, 61.0, 30.0, 4.0, 20.0, (0, -1, 0)))
    return cut(block, box(35.0, 5.0, 32.0, 20.0, 20.0, 10.0))


@fixture("datum_blocks_access")
def build_datum_blocks_access() -> TopoDS_Shape:
    """One blind hole in the largest planar face of a plain plate.

    The part behind a retired rule: the biggest flat face is the obvious
    datum, and this one has a feature in it. The rule went because a
    two-setup flip is ordinary CNC rather than a manufacturing problem, but
    the part stays in the corpus as a silence check.
    """
    return cut(box(0, 0, 0, 80.0, 60.0, 20.0), cylinder(40.0, 30.0, -1.0, 3.0, 11.0))


# -- brackets, plates and fixtures --------------------------------------------


@fixture("aerospace_bracket_l")
def build_aerospace_bracket_l() -> TopoDS_Shape:
    """An L bracket with mounting holes in both legs and a gusset in the corner.

    Four holes through the flat leg, two through the upright, and an R5
    fillet on the concave junction between them. Enough features to exercise
    the complexity count, and the outer holes sit close enough to the edges
    to be worth an opinion.
    """
    body = fuse(
        box(0, 0, 0, 80.0, 60.0, 8.0), box(0.0, 52.0, 8.0, 80.0, 8.0, 40.0)
    )
    for x, y in ((10.0, 6.0), (70.0, 6.0), (10.0, 46.0), (70.0, 46.0)):
        body = cut(body, cylinder(x, y, -1.0, 2.5, 10.0))
    for x, z in ((20.0, 24.0), (60.0, 24.0)):
        body = cut(body, cylinder(x, 51.0, z, 3.0, 10.0, (0, 1, 0)))

    fillet = BRepFilletAPI_MakeFillet(body)
    for edge in _explored_edges(body):
        points = _edge_points(edge)
        if points is None:
            continue
        mid = points[2]
        if (
            abs(mid.Y() - 52.0) < 0.1
            and abs(mid.Z() - 8.0) < 0.1
            and 0.5 < mid.X() < 79.5
        ):
            fillet.Add(5.0, edge)
            break
    fillet.Build()
    return fillet.Shape() if fillet.IsDone() else body


@fixture("bracket_with_thread_array")
def build_bracket_with_thread_array() -> TopoDS_Shape:
    """A plate carrying four M6 blind taps and two clearance holes.

    The taps are on a rectangular pattern, which is what the array
    recognizer is meant to collapse into one finding rather than four. The
    R5 fillet round the top perimeter is there so the outer edges are not
    a trivially planar case.
    """
    plate = box(0, 0, 0, 100.0, 60.0, 15.0)
    for x, y in ((20.0, 10.0), (80.0, 10.0), (20.0, 50.0), (80.0, 50.0)):
        plate = cut(plate, cylinder(x, y, 3.0, 2.5, 13.0))
    for x, y in ((40.0, 30.0), (60.0, 30.0)):
        plate = cut(plate, cylinder(x, y, -1.0, 4.0, 17.0))

    def on_outer(point) -> bool:
        return (
            abs(point.X()) < 0.1
            or abs(point.X() - 100.0) < 0.1
            or abs(point.Y()) < 0.1
            or abs(point.Y() - 60.0) < 0.1
        )

    fillet = BRepFilletAPI_MakeFillet(plate)
    for edge in _explored_edges(plate):
        points = _edge_points(edge)
        if points is None:
            continue
        start, end = points[0], points[1]
        if abs(start.Z() - 15.0) >= 0.1 or abs(end.Z() - 15.0) >= 0.1:
            continue
        if on_outer(start) and on_outer(end):
            fillet.Add(5.0, edge)
    fillet.Build()
    return fillet.Shape() if fillet.IsDone() else plate


@fixture("gearbox_flange")
def build_gearbox_flange() -> TopoDS_Shape:
    """A turned flange: input-shaft seat, six bolt holes on a PCD, chamfered rim."""
    flange = cylinder(0.0, 0.0, 0.0, 50.0, 15.0)
    flange = cut(flange, cylinder(0.0, 0.0, 5.0, 25.0, 11.0))
    for index in range(6):
        angle = index * math.pi / 3.0
        flange = cut(
            flange,
            cylinder(
                40.0 * math.cos(angle), 40.0 * math.sin(angle), -1.0, 3.5, 17.0
            ),
        )

    chamfer = BRepFilletAPI_MakeChamfer(flange)
    for edge in _explored_edges(flange):
        points = _edge_points(edge)
        if points is None:
            continue
        mid = points[2]
        radius = math.hypot(mid.X(), mid.Y())
        if abs(mid.Z() - 15.0) < 0.1 and abs(radius - 50.0) < 0.5:
            chamfer.Add(2.0, edge)
            break
    chamfer.Build()
    return chamfer.Shape() if chamfer.IsDone() else flange


@fixture("fixture_plate_dowels")
def build_fixture_plate_dowels() -> TopoDS_Shape:
    """A workholding plate: dowels, bolt holes and taps, sixteen in all.

    Three hole sizes on three different patterns, which is what makes it a
    useful test of the array grouping -- getting one pattern right is easy,
    keeping three apart is not.
    """
    plate = box(0, 0, 0, 150.0, 100.0, 20.0)
    for x, y in ((15.0, 15.0), (135.0, 15.0), (15.0, 85.0), (135.0, 85.0)):
        plate = cut(plate, cylinder(x, y, -1.0, 3.0, 22.0))
    bolts = (
        (25.0, 20.0),
        (75.0, 20.0),
        (125.0, 20.0),
        (25.0, 50.0),
        (125.0, 50.0),
        (25.0, 80.0),
        (75.0, 80.0),
        (125.0, 80.0),
    )
    for x, y in bolts:
        plate = cut(plate, cylinder(x, y, -1.0, 4.0, 22.0))
    for x, y in ((75.0, 15.0), (15.0, 50.0), (135.0, 50.0), (75.0, 85.0)):
        plate = cut(plate, cylinder(x, y, 6.0, 2.0, 16.0))
    return plate


@fixture("clamp_jaw_assembly_half")
def build_clamp_jaw_assembly_half() -> TopoDS_Shape:
    """Half a clamp jaw: raised pad, two dowel holes, one counterbored screw hole."""
    base = fuse(box(0, 0, 0, 60.0, 30.0, 25.0), box(0.0, 0.0, 25.0, 20.0, 30.0, 5.0))
    for x, y in ((40.0, 8.0), (50.0, 22.0)):
        base = cut(base, cylinder(x, y, -1.0, 4.0, 27.0))
    base = cut(base, cylinder(10.0, 15.0, 20.0, 5.0, 11.0))
    return cut(base, cylinder(10.0, 15.0, -1.0, 3.0, 27.0))


@fixture("cnc_router_test_fixture")
def build_cnc_router_test_fixture() -> TopoDS_Shape:
    """A router calibration piece: round pocket, square pocket, open slot, corner holes."""
    plate = box(0, 0, 0, 100.0, 100.0, 10.0)
    plate = cut(plate, cylinder(25.0, 25.0, 5.0, 15.0, 6.0))
    plate = cut(plate, box(60.0, 60.0, 5.0, 30.0, 30.0, 6.0))
    plate = cut(plate, box(5.0, 80.0, 5.0, 30.0, 5.0, 6.0))
    for x, y in ((8.0, 92.0), (92.0, 8.0), (92.0, 92.0), (8.0, 8.0)):
        plate = cut(plate, cylinder(x, y, -1.0, 3.0, 12.0))
    return plate


@fixture("cnc_test_part_dfm_violation")
def build_cnc_test_part_dfm_violation() -> TopoDS_Shape:
    """Five deliberate mistakes on one block, to see them all reported at once.

    A through hole at six diameters, a 4 mm pocket nearly through the
    plate, a pair of holes leaving a 0.7 mm web, a hole 1.2 mm from an
    edge, and a square-cornered pocket. The first is exactly on the
    deep-hole threshold and so does not fire -- the rule uses a strict
    comparison, and that is recorded rather than nudged.
    """
    block = box(0, 0, 0, 80.0, 60.0, 30.0)
    block = cut(block, cylinder(10.0, 30.0, -1.0, 2.5, 32.0))
    block = cut(block, box(20.0, 28.0, 3.0, 4.0, 4.0, 30.0))
    block = cut(block, cylinder(35.0, 20.0, -1.0, 4.0, 32.0))
    block = cut(block, cylinder(35.0, 28.7, -1.0, 4.0, 32.0))
    block = cut(block, cylinder(2.7, 50.0, -1.0, 1.5, 32.0))
    return cut(block, box(55.0, 15.0, 25.0, 20.0, 20.0, 10.0))


@fixture("compact_mountain_block")
def build_compact_mountain_block() -> TopoDS_Shape:
    """A stepped block with a cross-hole that pierces both levels.

    The cross-hole enters through the tall half and leaves through the
    short one, so it breaks into two bores that have to be recognised as
    one hole.
    """
    base = cut(box(0, 0, 0, 80.0, 40.0, 30.0), box(40.0, 0.0, 15.0, 41.0, 40.0, 16.0))
    base = cut(base, cylinder(20.0, 20.0, -1.0, 4.0, 32.0))
    return cut(base, cylinder(-1.0, 30.0, 10.0, 3.0, 82.0, (1, 0, 0)))


# -- casting and moulding -----------------------------------------------------


@fixture("die_cast_pattern_block")
def build_die_cast_pattern_block() -> TopoDS_Shape:
    """A raised pad on a base plate with ejector holes through the corners."""
    base = fuse(box(0, 0, 0, 80.0, 60.0, 20.0), box(15.0, 15.0, 20.0, 50.0, 30.0, 15.0))
    for x, y in ((20.0, 20.0), (60.0, 20.0), (20.0, 40.0), (60.0, 40.0)):
        base = cut(base, cylinder(x, y, -1.0, 2.0, 22.0))
    return base


@fixture("as_cast_no_draft")
def build_as_cast_no_draft() -> TopoDS_Shape:
    """A part declared as cast whose every wall is dead vertical.

    The draft rule fires on the disagreement between what the blank says
    and what the geometry shows, so the geometry is kept deliberately mild
    -- open plate, low boss, stout rib -- and nothing else has anything to
    say about it.
    """
    part = box(0, 0, 0, 90.0, 60.0, 10.0)
    part = fuse(part, box_between((10.0, 18.0, 10.0), (50.0, 46.0, 22.0)))
    part = fuse(part, cylinder(70.0, 42.0, 10.0, 7.0, 10.0))
    return fuse(part, box_between((60.0, 12.0, 10.0), (84.0, 17.0, 22.0)))


@fixture("drafted_fin_channel")
def build_drafted_fin_channel() -> TopoDS_Shape:
    """Two lofted fins with 1.5 degrees of draft, and the channel between them.

    Lofting rather than boxing them means the fin walls come out as ruled
    surfaces instead of planes, so the channel recognizer has to sample
    normals rather than read a plane off the face. The draft is kept small
    enough that the fin tip stays above the thin-wall threshold and the
    channel is the only story.
    """

    def rect_wire(x0, x1, y0, y1, z):
        polygon = BRepBuilderAPI_MakePolygon()
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            polygon.Add(gp_Pnt(x, y, z))
        polygon.Close()
        return polygon.Wire()

    part = box(0, 0, 0, 90.0, 50.0, 8.0)
    shrink = 0.524
    for y0 in (15.0, 22.0):
        loft = BRepOffsetAPI_ThruSections(True, True)
        loft.AddWire(rect_wire(15.0, 55.0, y0, y0 + 3.0, 8.0))
        loft.AddWire(
            rect_wire(
                15.0 + shrink, 55.0 - shrink, y0 + shrink, y0 + 3.0 - shrink, 28.0
            )
        )
        loft.Build()
        part = fuse(part, loft.Shape())
    return part


# -- marking ------------------------------------------------------------------


@fixture("floorless_engraved_logo")
def build_floorless_engraved_logo() -> TopoDS_Shape:
    """Engraving cut with a round-nose tool, which leaves no floor at all.

    Each stroke is a pair of vertical walls tangent to a half-round bottom,
    so there is nothing an offset-parallel floor test can grip. The strokes
    stop exactly on their crossings rather than overhanging, so no stroke
    ends in a free round cap and the gaps stay at stroke scale -- which is
    what the marking pass keys on. The result is one connected cluster of
    walls and curvature and no flat surface anywhere inside it.
    """
    top = 12.0
    block = box(0, 0, 0, 50.0, 34.0, top)

    width = 2.0
    radius = width / 2.0
    depth = 2.0
    wall = depth - radius

    def cut_slot(shape, along_x, start, finish, lateral):
        if along_x:
            trough = box(start, lateral - radius, top - wall, finish - start, width, wall)
            round_bottom = cylinder(
                start, lateral, top - wall, radius, finish - start, (1, 0, 0)
            )
        else:
            trough = box(lateral - radius, start, top - wall, width, finish - start, wall)
            round_bottom = cylinder(
                lateral, start, top - wall, radius, finish - start, (0, 1, 0)
            )
        return cut(shape, fuse(trough, round_bottom))

    for y in (11.0, 17.0, 23.0):
        block = cut_slot(block, True, 18.0, 32.0, y)
    for x in (18.0, 25.0, 32.0):
        block = cut_slot(block, False, 11.0, 23.0, x)
    return block


@fixture("dual_marking_control_plate")
def build_dual_marking_control_plate() -> TopoDS_Shape:
    """Engraved text on two faces at once, so the clustering has to split it in two.

    One block on the top face and one on the front, the second built flat
    and then stood up. If the suppression only works on the face it was
    written for, the second block comes back as a swarm of tiny pockets.
    """
    plate = box_between((0, 0, 0), (80, 50, 6))

    plate = cut(plate, _seven_seg_text("SN-108", 12.0, 38.0, 5.0, 0.5, 5.65, 6.1))

    flat = _seven_seg_text("T-42", 0.0, 0.0, 3.5, 0.4, -0.3, 0.1)
    stand_up = gp_Trsf()
    stand_up.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), math.pi / 2.0)
    shift = gp_Trsf()
    shift.SetTranslation(gp_Vec(30.0, 0.0, 1.2))
    placed = BRepBuilderAPI_Transform(flat, shift.Multiplied(stand_up), True).Shape()
    return cut(plate, placed)


@fixture("billet_reservoir_cap")
def build_billet_reservoir_cap() -> TopoDS_Shape:
    """A turned cap with its logo standing proud of the machined top face.

    Raised marking is the harder half of the marking problem: engraved text
    is a cluster of pockets, but this is a cluster of bosses, and the
    letterforms sit just inside the recognizer caps so nothing is being
    made easy. Everything else on the cap -- skirt bore, internal gland,
    vent, two spanner holes -- is there to give the marking something
    ordinary to be found among.
    """
    cap = cylinder(0.0, 0.0, 0.0, 27.5, 15.0)

    chamfer = BRepFilletAPI_MakeChamfer(cap)
    for edge in _explored_edges(cap):
        points = _edge_points(edge)
        if points is None:
            continue
        mid = points[2]
        if abs(mid.Z() - 15.0) < 0.1 and abs(math.hypot(mid.X(), mid.Y()) - 27.5) < 0.5:
            chamfer.Add(1.0, edge)
            break
    chamfer.Build()
    if chamfer.IsDone():
        cap = chamfer.Shape()

    cap = fuse(cap, _seven_seg_text("BRC", -17.2, -8.0, 16.0, 3.0, 14.9, 15.8))

    cap = cut(cap, cylinder(0.0, 0.0, -0.1, 23.0, 8.1))
    cap = cut(cap, cone(0.0, 0.0, -0.001, 24.0, 23.0, 1.001))

    gland = cut(cylinder(0.0, 0.0, 3.0, 24.8, 2.4), cylinder(0.0, 0.0, 2.9, 22.9, 2.6))
    cap = cut(cap, gland)

    cap = cut(cap, cylinder(0.0, -20.0, 7.9, 1.0, 7.2))

    for sign in (-1, 1):
        x = sign * 22.0
        cap = cut(cap, cylinder(x, 0.0, 13.0, 2.0, 2.1))
        cap = cut(cap, cone(x, 0.0, 13.0, 2.0, 0.0, 1.2, (0, 0, -1)))
    return cap


# -- freeform surfaces --------------------------------------------------------


@fixture("freeform_revolved_nozzle")
def build_freeform_revolved_nozzle() -> TopoDS_Shape:
    """A B-spline meridian turned about Z, with not one cylinder in it.

    The classifier has to reach "turned" from the revolved axis alone. The
    meridian wanders in and out so OpenCascade cannot quietly promote any
    band of it to a cylinder or a cone and hand the answer over.
    """
    meridian = _bspline(
        [
            (16, 0, 0),
            (19, 0, 10),
            (13, 0, 22),
            (17, 0, 34),
            (14, 0, 44),
            (12, 0, 48),
        ]
    )
    wire = _wire_of(
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(16, 0, 0)).Edge(),
        BRepBuilderAPI_MakeEdge(meridian).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(12, 0, 48), gp_Pnt(0, 0, 48)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 48), gp_Pnt(0, 0, 0)).Edge(),
    )
    profile = BRepBuilderAPI_MakeFace(wire).Face()
    return BRepPrimAPI_MakeRevol(
        profile, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    ).Shape()


@fixture("extruded_grip_rail")
def build_extruded_grip_rail() -> TopoDS_Shape:
    """A wavy profile swept 120 mm, giving a surface of linear extrusion on top.

    Two holes come up through that wavy face, which is the case a hole
    recognizer expecting a planar entry gets wrong. The drill is 5.5 rather
    than 5: 5 is the tap drill for M6 and the holes came back as threads,
    which is not what this part is about.
    """
    wave = _bspline(
        [
            (0, 30, 12.0),
            (0, 24, 14.5),
            (0, 15, 11.5),
            (0, 6, 14.5),
            (0, 0, 12.0),
        ]
    )
    wire = _wire_of(
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(0, 30, 0)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 30, 0), gp_Pnt(0, 30, 12)).Edge(),
        BRepBuilderAPI_MakeEdge(wave).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 12), gp_Pnt(0, 0, 0)).Edge(),
    )
    profile = BRepBuilderAPI_MakeFace(wire).Face()
    rail = BRepPrimAPI_MakePrism(profile, gp_Vec(120.0, 0, 0)).Shape()
    for x in (30.0, 90.0):
        rail = cut(rail, cylinder(x, 15.0, -0.1, 2.75, 16.0))
    return rail


@fixture("freeform_transition_bracket")
def build_freeform_transition_bracket() -> TopoDS_Shape:
    """A block whose top is a doubly curved sheet, with a pocket under the valley.

    The sheet is built oversize and subtracted downward, so the sides stay
    planar and only the top goes freeform. It overshoots its own control
    heights -- the valley dips to about 7.1 where the control point says
    9.5 -- so the pocket floor sits at 6.0 to leave a real 1.1 mm wall
    rather than a hole. The rib fused across the crest meets the sheet in
    sharp junctions on purpose, and the two holes pierce the curved top
    rather than a flat one.
    """
    part = box_between((0, 0, 0), (80, 50, 16))

    grid = TColgp_Array2OfPnt(1, 4, 1, 4)
    xs = (-5.0, 30.0, 60.0, 85.0)
    ys = (-5.0, 20.0, 35.0, 55.0)
    zs = (
        (12.0, 11.0, 12.0, 12.0),
        (10.0, 9.5, 11.0, 12.0),
        (14.0, 14.5, 14.0, 13.5),
        (13.0, 13.0, 12.5, 12.5),
    )
    for i in range(4):
        for j in range(4):
            grid.SetValue(i + 1, j + 1, gp_Pnt(xs[i], ys[j], zs[i][j]))
    sheet = GeomAPI_PointsToBSplineSurface(grid).Surface()
    sheet_face = BRepBuilderAPI_MakeFace(sheet, 1e-6).Face()
    part = cut(part, BRepPrimAPI_MakePrism(sheet_face, gp_Vec(0, 0, 12)).Shape())

    part = cut(part, box_between((8, 10, -0.1), (28, 26, 6.0)))
    part = fuse(part, box_between((45, 22, 10), (75, 28, 20)))
    for y in (10.0, 40.0):
        part = cut(part, cylinder(65, y, -0.1, 2.5, 22.0))
    return part


# -- production parts ---------------------------------------------------------


@fixture("aerospace_tensioner_pulley")
def build_aerospace_tensioner_pulley() -> TopoDS_Shape:
    """A belt pulley: flanges either side of a recessed belt face, bearing bore, ring groove.

    Every recess and groove is cut as the difference of two cylinders, so
    they come out as real annular channels rather than as steps. The
    retainer groove inside the bearing bore is the awkward one -- it is a
    concave feature inside a bore, which is the case the groove orientation
    gate exists to get right.
    """
    body = cylinder(0.0, 0.0, 0.0, 49.5, 38.0)

    belt_face = cut(
        cylinder(0.0, 0.0, 6.5, 49.7, 25.0), cylinder(0.0, 0.0, 6.4, 47.5, 25.2)
    )
    body = cut(body, belt_face)

    body = cut(body, cylinder(0.0, 0.0, 34.0, 39.0, 4.1))
    body = cut(body, cylinder(0.0, 0.0, -0.1, 23.5, 18.2))
    body = cut(body, cylinder(0.0, 0.0, 18.0, 25.0, 10.0))
    body = cut(body, cylinder(0.0, 0.0, 28.0, 6.5, 10.1))

    groove = cut(
        cylinder(0.0, 0.0, 13.725, 25.0, 1.55), cylinder(0.0, 0.0, 13.625, 23.5, 1.75)
    )
    return cut(body, groove)


@fixture("cf_knife_edge_flange")
def build_cf_knife_edge_flange() -> TopoDS_Shape:
    """A ConFlat vacuum flange, whose sharp edge is the point of it.

    The knife is a 70 degree included ridge that seals by biting into a
    copper gasket, so the sharp-edge rule has to be able to be told this is
    intended. The bolt holes sit 1.4 mm from the outside diameter because
    the standard puts them there, which makes the edge-distance findings
    true positives on a part nobody would redesign.
    """
    flange = revolved_profile(
        [
            (17.5, 0.0),
            (34.95, 0.0),
            (34.95, 12.7),
            (24.3, 12.7),
            (24.3, 11.2),
            (23.26, 11.2),
            (22.9, 12.2),
            (21.71, 11.2),
            (17.5, 11.2),
        ]
    )
    for index in range(6):
        angle = index * math.pi / 3.0
        flange = cut(
            flange,
            cylinder(
                29.36 * math.cos(angle), 29.36 * math.sin(angle), -1.0, 4.2, 14.7
            ),
        )
    return flange


@fixture("euro_lock_cylinder")
def build_euro_lock_cylinder() -> TopoDS_Shape:
    """A euro-profile lock body, where several of the "faults" are the design.

    The pin chambers break into the plug bore on purpose -- the pin stack
    has to cross the shear line -- and the 8 mm bridge under the cam cavity
    is the famous snap line, thin because the standard says so. Chambers
    are 3.0 rather than 3.1 so the nonstandard-diameter rule does not fire
    five times over something incidental.
    """
    body = cylinder(0.0, 0.0, 24.5, 8.5, 70.0, (1, 0, 0))
    stem = box(0.0, -5.0, 0.0, 70.0, 10.0, 20.0)
    part = fuse(body, stem)

    # The plug bores run all the way to the cam cavity, as they do on a
    # real cylinder: the plug tail engages the cam. Stopping them short
    # left the last chamber half-drilled into solid brass.
    part = cut(part, cylinder(-0.1, 0.0, 24.5, 6.5, 30.2, (1, 0, 0)))
    part = cut(part, cylinder(70.1, 0.0, 24.5, 6.5, 30.2, (-1, 0, 0)))

    part = cut(part, box(30.0, -8.6, 8.0, 10.0, 17.2, 26.0))

    radius = 1.5
    depth = 22.6
    cone_height = _drill_point_height(radius)
    for index in range(5):
        x = 6.0 + index * 5.5
        chamber = fuse(
            cylinder(x, 0.0, -0.1, radius, depth),
            cone(x, 0.0, -0.1 + depth, radius, 0.0, cone_height),
        )
        part = cut(part, chamber)

    return cut(part, cylinder(35.0, 0.0, -0.1, 2.1, 8.2))


@fixture("door_closer_body")
def build_door_closer_body() -> TopoDS_Shape:
    """A hydraulic door closer: piston bore, pinion cross-bore, metering ports.

    The pinion bore cuts straight through the piston bore because that is
    how a rack and pinion works, so the intersecting-bore finding is
    another one that has to be readable as intent. Mounting holes sit at
    20 and 170 to clear the end-cap counterbores; an earlier 12 and 178
    punched into them.
    """
    body = box(0, 0, 0, 190.0, 45.0, 45.0)

    body = cut(body, cylinder(-1.0, 22.5, 22.5, 14.0, 192.0, (1, 0, 0)))
    body = cut(body, cylinder(-0.1, 22.5, 22.5, 16.0, 10.1, (1, 0, 0)))
    body = cut(body, cylinder(190.1, 22.5, 22.5, 16.0, 10.1, (-1, 0, 0)))

    body = cut(body, cylinder(95.0, 22.5, -1.0, 11.0, 47.0))
    body = cut(body, cylinder(95.0, 22.5, -0.1, 14.0, 5.1))
    body = cut(body, cylinder(95.0, 22.5, 45.1, 14.0, 5.1, (0, 0, -1)))

    for x in (25.0, 165.0):
        body = cut(body, cylinder(x, -0.1, 22.5, 1.5, 24.0, (0, 1, 0)))
        body = cut(body, cylinder(x, -0.1, 22.5, 3.4, 6.1, (0, 1, 0)))

    for x, z in ((20.0, 10.0), (170.0, 10.0), (20.0, 35.0), (170.0, 35.0)):
        body = cut(body, cylinder(x, -1.0, z, 4.0, 47.0, (0, 1, 0)))
    return body


@fixture("heat_exchanger_tube_sheet")
def build_heat_exchanger_tube_sheet() -> TopoDS_Shape:
    """A tube sheet: a hundred and fifteen bores on triangular pitch, 3 mm apart.

    The point of it is scale. Every ligament between neighbouring tubes is
    a thin wall, every bore wants a weld-prep chamfer, and the rules have
    to say that once rather than a hundred and fifteen times. Round it out
    with a gasket groove, a partition slot, a bolt circle and a drain port.
    """
    plate = box(0, 0, 0, 240.0, 240.0, 40.0)

    pitch = 13.0
    row_step = pitch * math.sqrt(3.0) / 2.0
    array_w = 11 * pitch
    array_h = 9 * row_step
    x0 = (240.0 - array_w) / 2.0
    y0 = (240.0 - array_h) / 2.0
    for row in range(10):
        y = y0 + row * row_step
        offset = 0.0 if row % 2 == 0 else pitch / 2.0
        columns = 12 if row % 2 == 0 else 11
        for column in range(columns):
            x = x0 + column * pitch + offset
            plate = cut(plate, cylinder(x, y, -1.0, 5.0, 42.0))
            plate = cut(plate, cone(x, y, 39.5, 5.0, 5.5, 0.5))

    groove = cut(
        box(17.5, 17.5, -0.001, 205.0, 205.0, 3.001),
        box(22.5, 22.5, -0.002, 195.0, 195.0, 3.5),
    )
    plate = cut(plate, groove)

    slot_y = y0 + 4.5 * row_step
    plate = cut(
        plate, box(x0 - 2.0, slot_y - 10.0, 36.0, array_w + 4.0, 20.0, 4.001)
    )

    for index in range(6):
        angle = index * (math.pi / 3.0)
        plate = cut(
            plate,
            cylinder(
                120.0 + 110.0 * math.cos(angle),
                120.0 + 110.0 * math.sin(angle),
                -1.0,
                7.0,
                42.0,
            ),
        )

    return cut(plate, cylinder(120.0, -1.0, 20.0, 2.5, 13.0, (0, 1, 0)))


@fixture("aerospace_hydraulic_servo_valve_body")
def build_aerospace_hydraulic_servo_valve_body() -> TopoDS_Shape:
    """A servo valve body: spool bore, four O-ring glands, six ports, a threaded pilot.

    The R2 corner fillets go on before any of the cuts. Doing it the other
    way round means the fillet has to negotiate with the port openings, and
    OpenCascade gives up. Only the upright corners are rounded, for the
    same reason `fillet_edges` needs two passes: filleting all twelve edges
    of a box at once falls over on the three-way corners.
    """
    part = box(0, 0, 0, 120.0, 80.0, 60.0)
    part = _fillet_upright_corners(part, 2.0)

    part = cut(part, cylinder(-0.1, 40.0, 30.0, 4.0, 120.2, (1, 0, 0)))

    for centre in (15.0, 45.0, 75.0, 105.0):
        gland = cut(
            cylinder(centre - 1.2, 40.0, 30.0, 5.4, 2.4, (1, 0, 0)),
            cylinder(centre - 1.3, 40.0, 30.0, 4.0, 2.6, (1, 0, 0)),
        )
        part = cut(part, gland)

    for centre in (2.5, 117.5):
        ring = cut(
            cylinder(centre - 0.45, 40.0, 30.0, 4.4, 0.9, (1, 0, 0)),
            cylinder(centre - 0.55, 40.0, 30.0, 4.0, 1.1, (1, 0, 0)),
        )
        part = cut(part, ring)

    ports = (
        (20.0, 40.0, 2.0, 30.0),
        (100.0, 40.0, 2.0, 30.0),
        (40.0, 40.0, 1.75, 30.0),
        (60.0, 40.0, 1.75, 30.0),
        (80.0, 40.0, 1.75, 30.0),
        (60.0, 60.0, 1.5, 45.0),
    )
    for x, y, radius, depth in ports:
        part = cut(part, cylinder(x, y, 60.1, radius, depth + 0.2, (0, 0, -1)))

    part = cut(part, cylinder(-0.1, 40.0, 45.0, 5.1, 22.2, (1, 0, 0)))
    relief = cut(
        cylinder(5.0, 40.0, 45.0, 6.6, 2.0, (1, 0, 0)),
        cylinder(4.9, 40.0, 45.0, 5.1, 2.2, (1, 0, 0)),
    )
    return cut(part, relief)


def _fillet_upright_corners(shape: TopoDS_Shape, radius: float) -> TopoDS_Shape:
    """Round the four Z-spanning corner edges of a box, and only those."""
    fillet = BRepFilletAPI_MakeFillet(shape)
    added = 0
    for edge in _explored_edges(shape):
        points = _edge_points(edge)
        if points is None:
            continue
        start, end = points[0], points[1]
        if (
            abs(start.X() - end.X()) < 0.01
            and abs(start.Y() - end.Y()) < 0.01
            and abs(start.Z() - end.Z()) > 1.0
        ):
            fillet.Add(radius, edge)
            added += 1
    if not added:
        return shape
    fillet.Build()
    return fillet.Shape() if fillet.IsDone() else shape


_ENCLOSURE_BOSSES = ((12.5, 12.5), (72.5, 12.5), (72.5, 42.5), (12.5, 42.5))


@fixture("electronics_enclosure_billet")
def build_electronics_enclosure_billet() -> TopoDS_Shape:
    """A sealed handheld enclosure milled from solid, with the posts left standing.

    The pocket is cut as a rounded prism with the four boss columns removed
    from the tool, so the posts are what is left rather than something
    added back. A square-cornered draft fired the sharp-pocket rule, which
    is why the R3 corners are there. The gland loop sits entirely on the
    4 mm flange -- an earlier one overlapped the cavity mouth, and the
    recognizer was right to refuse it, because an open shoulder is not a
    channel.
    """
    part = box(0, 0, 0, 85.0, 55.0, 25.0)
    part = _fillet_upright_corners(part, 5.0)

    pocket = rounded_rect_prism(4.0, 4.0, 81.0, 51.0, 3.0, 4.0, 21.1)
    for cx, cy in _ENCLOSURE_BOSSES:
        pocket = cut(pocket, box_between((cx - 3.0, cy - 3.0, 4.0), (cx + 3.0, cy + 3.0, 19.0)))
    part = cut(part, pocket)

    root = BRepFilletAPI_MakeFillet(part)
    added = 0
    for edge in _explored_edges(part):
        points = _edge_points(edge)
        if points is None:
            continue
        start, end = points[0], points[1]
        if abs(start.Z() - 4.0) > 0.01 or abs(end.Z() - 4.0) > 0.01:
            continue
        for cx, cy in _ENCLOSURE_BOSSES:
            near_start = math.hypot(start.X() - cx, start.Y() - cy)
            near_end = math.hypot(end.X() - cx, end.Y() - cy)
            if near_start < 5.0 and near_end < 5.0:
                root.Add(2.0, edge)
                added += 1
                break
    if added:
        root.Build()
        if root.IsDone():
            part = root.Shape()

    tap_radius = 1.65
    cone_height = _drill_point_height(tap_radius)
    for cx, cy in _ENCLOSURE_BOSSES:
        part = cut(part, cylinder(cx, cy, 19.1, tap_radius, 15.2, (0, 0, -1)))
        part = cut(
            part,
            cone(cx, cy, 19.1 - 15.2, tap_radius, 0.0, cone_height, (0, 0, -1)),
        )
        part = cut(part, cone(cx, cy, 19.0, 2.15, 1.65, 0.5, (0, 0, -1)))

    gland = cut(
        rounded_rect_prism(1.0, 1.0, 84.0, 54.0, 4.2, 23.6, 1.5),
        rounded_rect_prism(3.0, 3.0, 82.0, 52.0, 2.2, 23.5, 1.7),
    )
    part = cut(part, gland)

    for cx, cy in ((6.5, 6.5), (78.5, 6.5), (78.5, 48.5), (6.5, 48.5)):
        part = cut(part, cylinder(cx, cy, -0.1, 2.15, 25.2))
        part = cut(part, cone(cx, cy, -0.001, 4.3, 2.15, 2.15))
    return part
