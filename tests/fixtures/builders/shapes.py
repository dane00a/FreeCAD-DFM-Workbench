# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The vocabulary the fixture builders are written in.

Kept deliberately small and blunt. A fixture is a description of a part, and
it should read like one -- `cut(plate, drill(40, 30, 5))` rather than four
lines of OpenCascade ceremony. Everything here is millimetres.

The one piece of real geometry is `rounded_rect_prism`, and it exists for a
reason worth knowing. A rounded pocket built by fusing boxes and cylinders
imprints the seams of that fusion onto the floor and walls of whatever it
cuts, fragmenting them into shards that the recognizers then read as
separate features. Sweeping a single profile wire instead gives one face per
wall, which is what a real CAM operation would leave.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeRevol,
    BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
)
from OCP.GC import GC_MakeArcOfCircle
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape


# -- primitives ---------------------------------------------------------------


def box(x0: float, y0: float, z0: float, dx: float, dy: float, dz: float) -> TopoDS_Shape:
    """A box by its near corner and its size."""
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x0 + dx, y0 + dy, z0 + dz)).Shape()


def box_between(p0: Sequence[float], p1: Sequence[float]) -> TopoDS_Shape:
    """A box by two opposite corners."""
    return BRepPrimAPI_MakeBox(gp_Pnt(*p0), gp_Pnt(*p1)).Shape()


def cylinder(
    x: float,
    y: float,
    z: float,
    radius: float,
    height: float,
    direction: Sequence[float] = (0, 0, 1),
) -> TopoDS_Shape:
    axis = gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*direction))
    return BRepPrimAPI_MakeCylinder(axis, radius, height).Shape()


def cone(
    x: float,
    y: float,
    z: float,
    lower_radius: float,
    upper_radius: float,
    height: float,
    direction: Sequence[float] = (0, 0, 1),
) -> TopoDS_Shape:
    axis = gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*direction))
    return BRepPrimAPI_MakeCone(axis, lower_radius, upper_radius, height).Shape()


def sphere(x: float, y: float, z: float, radius: float) -> TopoDS_Shape:
    return BRepPrimAPI_MakeSphere(gp_Pnt(x, y, z), radius).Shape()


def torus(
    x: float,
    y: float,
    z: float,
    major: float,
    minor: float,
    direction: Sequence[float] = (0, 0, 1),
) -> TopoDS_Shape:
    axis = gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*direction))
    return BRepPrimAPI_MakeTorus(axis, major, minor).Shape()


# -- booleans -----------------------------------------------------------------


def cut(base: TopoDS_Shape, *tools: TopoDS_Shape) -> TopoDS_Shape:
    """Remove each tool from the base, in order."""
    result = base
    for tool in tools:
        operation = BRepAlgoAPI_Cut(result, tool)
        operation.Build()
        result = operation.Shape()
    return result


def fuse(base: TopoDS_Shape, *others: TopoDS_Shape) -> TopoDS_Shape:
    result = base
    for other in others:
        operation = BRepAlgoAPI_Fuse(result, other)
        operation.Build()
        result = operation.Shape()
    return result


def common(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    operation = BRepAlgoAPI_Common(a, b)
    operation.Build()
    return operation.Shape()


# -- placement ----------------------------------------------------------------


def moved(shape: TopoDS_Shape, dx: float, dy: float, dz: float) -> TopoDS_Shape:
    transform = gp_Trsf()
    transform.SetTranslation(gp_Vec(dx, dy, dz))
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def rotated(
    shape: TopoDS_Shape,
    origin: Sequence[float],
    direction: Sequence[float],
    degrees: float,
) -> TopoDS_Shape:
    transform = gp_Trsf()
    transform.SetRotation(
        gp_Ax1(gp_Pnt(*origin), gp_Dir(*direction)), math.radians(degrees)
    )
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def repeated(
    shape: TopoDS_Shape, count: int, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0
) -> list[TopoDS_Shape]:
    """One shape stepped along a vector -- a row of holes, a rib field."""
    return [moved(shape, dx * i, dy * i, dz * i) for i in range(count)]


def grid(
    shape: TopoDS_Shape, columns: int, rows: int, pitch_x: float, pitch_y: float
) -> list[TopoDS_Shape]:
    return [
        moved(shape, pitch_x * column, pitch_y * row, 0.0)
        for row in range(rows)
        for column in range(columns)
    ]


def ring_of(
    shape: TopoDS_Shape, count: int, radius: float, centre_x: float, centre_y: float
) -> list[TopoDS_Shape]:
    """A bolt circle: one feature placed round a pitch circle."""
    placed = []
    for index in range(count):
        angle = 2.0 * math.pi * index / count
        placed.append(
            moved(
                shape,
                centre_x + radius * math.cos(angle),
                centre_y + radius * math.sin(angle),
                0.0,
            )
        )
    return placed


# -- profiles -----------------------------------------------------------------


def rounded_rect_prism(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    radius: float,
    z0: float,
    dz: float,
) -> TopoDS_Shape:
    """A rectangular prism with rounded plan-view corners.

    Swept from a single profile wire rather than fused from boxes and
    cylinders. A fused tool imprints its own internal seams on whatever it
    cuts, fragmenting the pocket floor and walls into shards the recognizers
    then read as separate features; one wire gives one face per wall, which
    is what the machine would actually leave.
    """
    wire = BRepBuilderAPI_MakeWire()

    def segment(ax, ay, bx, by):
        wire.Add(
            BRepBuilderAPI_MakeEdge(gp_Pnt(ax, ay, z0), gp_Pnt(bx, by, z0)).Edge()
        )

    def arc(ax, ay, mx, my, bx, by):
        curve = GC_MakeArcOfCircle(
            gp_Pnt(ax, ay, z0), gp_Pnt(mx, my, z0), gp_Pnt(bx, by, z0)
        ).Value()
        wire.Add(BRepBuilderAPI_MakeEdge(curve).Edge())

    # How far a quarter-arc's midpoint sags in from the corner.
    sag = radius * (1.0 - math.cos(math.pi / 4.0))

    segment(x0 + radius, y0, x1 - radius, y0)
    arc(x1 - radius, y0, x1 - sag, y0 + sag, x1, y0 + radius)
    segment(x1, y0 + radius, x1, y1 - radius)
    arc(x1, y1 - radius, x1 - sag, y1 - sag, x1 - radius, y1)
    segment(x1 - radius, y1, x0 + radius, y1)
    arc(x0 + radius, y1, x0 + sag, y1 - sag, x0, y1 - radius)
    segment(x0, y1 - radius, x0, y0 + radius)
    arc(x0, y0 + radius, x0 + sag, y0 + sag, x0 + radius, y0)

    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, dz)).Shape()


def polygon_prism(
    points: Iterable[Sequence[float]], z0: float, dz: float
) -> TopoDS_Shape:
    """A prism swept from a closed polygon in the XY plane."""
    polygon = BRepBuilderAPI_MakePolygon()
    for point in points:
        polygon.Add(gp_Pnt(point[0], point[1], z0))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, dz)).Shape()


def revolved_profile(
    points: Iterable[Sequence[float]],
    origin: Sequence[float] = (0, 0, 0),
    direction: Sequence[float] = (0, 0, 1),
) -> TopoDS_Shape:
    """A solid of revolution from a closed profile in the XZ plane.

    Points are (radius, height). The profile must close back on the axis.
    """
    polygon = BRepBuilderAPI_MakePolygon()
    for radius, height in points:
        polygon.Add(gp_Pnt(radius, 0.0, height))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
    axis = gp_Ax1(gp_Pnt(*origin), gp_Dir(*direction))
    return BRepPrimAPI_MakeRevol(face, axis).Shape()


# -- edge treatments ----------------------------------------------------------


def edges_of(shape: TopoDS_Shape) -> list:
    """Every edge, once each, in a stable order."""
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edges)
    return [TopoDS.Edge_s(edges.FindKey(i)) for i in range(1, edges.Extent() + 1)]


def faces_of(shape: TopoDS_Shape) -> list:
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, faces)
    return [TopoDS.Face_s(faces.FindKey(i)) for i in range(1, faces.Extent() + 1)]


def edge_endpoints(edge) -> tuple:
    curve = BRepAdaptor_Curve(edge)
    return curve.Value(curve.FirstParameter()), curve.Value(curve.LastParameter())


def is_straight(edge) -> bool:
    return BRepAdaptor_Curve(edge).GetType() == GeomAbs_CurveType.GeomAbs_Line


def fillet_edges(shape: TopoDS_Shape, radius: float, select=None) -> TopoDS_Shape:
    """Round the edges `select` accepts, or all of them.

    `select` is called with the edge and its two endpoints, so a builder can
    say "the vertical ones inside this footprint" without repeating the
    endpoint arithmetic.
    """
    maker = BRepFilletAPI_MakeFillet(shape)
    added = 0
    for edge in edges_of(shape):
        start, end = edge_endpoints(edge)
        if select is not None and not select(edge, start, end):
            continue
        try:
            maker.Add(radius, edge)
            added += 1
        except Exception:
            continue
    if not added:
        return shape
    maker.Build()
    return maker.Shape() if maker.IsDone() else shape


def chamfer_edges(shape: TopoDS_Shape, distance: float, select=None) -> TopoDS_Shape:
    maker = BRepFilletAPI_MakeChamfer(shape)
    added = 0
    for edge in edges_of(shape):
        start, end = edge_endpoints(edge)
        if select is not None and not select(edge, start, end):
            continue
        try:
            maker.Add(distance, edge)
            added += 1
        except Exception:
            continue
    if not added:
        return shape
    maker.Build()
    return maker.Shape() if maker.IsDone() else shape


def vertical(edge, start, end) -> bool:
    return abs(start.Z() - end.Z()) > 1e-6


def horizontal(edge, start, end) -> bool:
    return abs(start.Z() - end.Z()) <= 1e-6


def within(x0: float, y0: float, x1: float, y1: float, slack: float = 0.1):
    """A selector for edges whose endpoints lie inside a plan-view box."""

    def inside(edge, start, end) -> bool:
        for point in (start, end):
            if not (x0 - slack <= point.X() <= x1 + slack):
                return False
            if not (y0 - slack <= point.Y() <= y1 + slack):
                return False
        return True

    return inside


def at_height(z: float, slack: float = 1e-6):
    def matches(edge, start, end) -> bool:
        return abs(start.Z() - z) <= slack and abs(end.Z() - z) <= slack

    return matches


def both(*selectors):
    def matches(edge, start, end) -> bool:
        return all(selector(edge, start, end) for selector in selectors)

    return matches
