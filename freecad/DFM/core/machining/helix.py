# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Finding threads by the one thing only a thread does: winding.

A thread is the only feature on a machined part whose edges go round and round.
A fillet runs along an edge, a bore's rim closes in a single circle, a slot
runs straight. None of them wind. So an edge that turns through more than a
full revolution about an axis, at a steady radius, is a thread helix and there
is nothing else it can reasonably be.

Measuring the winding rather than pattern-matching the surface type has two
advantages worth the arithmetic. It gives the pitch for free -- axial advance
divided by turns -- and pitch is what the relief and run-out rules need. And
it is indifferent to how the thread was modelled, which matters because a
swept cut and a revolved profile leave quite different surfaces behind.

Both the internal and external thread paths read this. Which one a helix
belongs to is not a question this module answers: the flanks of a thread
groove look into that groove whether it is cut on a shaft or in a bore, so the
caller settles it from the surface the thread is cut on.
"""

from __future__ import annotations

import math
from typing import Optional

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.gp import gp_Ax1, gp_Dir, gp_Vec
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.TopTools import (
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_IndexedMapOfShape,
)

from .aag import AttributedAdjacencyGraph, SurfaceType


# A thread runs at least this far round before it counts. Just over one turn
# is the honest floor: a complete circular edge winds exactly one, so a
# threshold of one would make a thread of every hole on the part.
MIN_TURNS = 1.2

# Samples along an edge when measuring how far it winds. A thread helix is
# smooth, so this only has to beat the winding per sample: at 64 samples a
# ten-turn thread still advances well under half a turn between points, which
# is what keeps the unwrapping unambiguous.
_WINDING_SAMPLES = 64

# Two helices belong to the same thread when their radii agree this closely.
_RADIUS_TOLERANCE_MM = 0.05

# A helix has to sit at a real radius. Anything nearer the axis than this is
# numerical noise about a degenerate curve.
MIN_RADIUS_MM = 0.1


class Helix:
    """One thread's worth of winding edges."""

    def __init__(self, axis: gp_Ax1, radius: float):
        self.axis = axis
        self.radius = radius
        self.turns = 0.0
        self.axial_span = 0.0
        self.edges: list = []

    def add(self, turns: float, axial_span: float, edge) -> None:
        # The longest contributing edge describes the thread; the others are
        # the same helix traced at a different radius on the same form.
        if turns > self.turns:
            self.turns = turns
            self.axial_span = axial_span
        self.edges.append(edge)

    def pitch(self) -> Optional[float]:
        """Axial advance per turn."""
        if self.turns <= 0.0:
            return None
        return self.axial_span / self.turns


def candidate_axes(graph: AttributedAdjacencyGraph) -> list[gp_Ax1]:
    """Distinct revolution axes present in the part.

    A thread winds about one of them. Taking the axes from the graph rather
    than fitting one to every edge keeps this cheap, and a thread always
    leaves at least one turned face sharing its axis -- the shank it is cut
    on, or the bore it is tapped into.
    """
    axes: list[gp_Ax1] = []
    for node in graph.nodes:
        axis = None
        if node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
            axis = node.cyl_cone_axis
        elif node.surface_type is SurfaceType.TORUS:
            axis = node.torus_axis
        if axis is None:
            continue
        if not any(_coaxial(existing, axis) for existing in axes):
            axes.append(axis)
    return axes


def find_helices(shape, axes: list[gp_Ax1]) -> list[Helix]:
    """Every winding edge in the shape, grouped by axis and radius.

    A thread models as several separate edges -- crest, root and both flanks
    each contribute one -- and they are all the same thread. Grouping by axis
    and radius is what puts them back together.
    """
    groups: list[Helix] = []

    # An indexed map rather than an explorer: it visits each edge once however
    # many faces share it, and it is the identity OCCT itself uses.
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edges)

    for index in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(index))
        curve = BRepAdaptor_Curve(edge)
        # Only a spline can wind. Lines, circles, conics and the rest close
        # or run straight, and skipping them here is what keeps this cheap
        # enough to run on every part.
        if curve.GetType() != GeomAbs_CurveType.GeomAbs_BSplineCurve:
            continue

        for axis in axes:
            measured = measure_winding(edge, axis, curve)
            if measured is None:
                continue
            turns, radius, axial_span = measured
            if turns < MIN_TURNS or radius < MIN_RADIUS_MM:
                continue
            _absorb(groups, axis, radius, turns, axial_span, edge)
            break

    return groups


def _absorb(groups, axis, radius, turns, axial_span, edge) -> None:
    for group in groups:
        if not _coaxial(group.axis, axis):
            continue
        if abs(group.radius - radius) > _RADIUS_TOLERANCE_MM:
            continue
        group.add(turns, axial_span, edge)
        return
    fresh = Helix(axis=axis, radius=radius)
    fresh.add(turns, axial_span, edge)
    groups.append(fresh)


def measure_winding(
    edge, axis: gp_Ax1, curve=None
) -> Optional[tuple[float, float, float]]:
    """How far an edge winds about an axis, and at what radius.

    Returns turns, mean radius and axial span, or nothing when the edge does
    not keep a steady radius. That steady-radius test is what rejects a
    spiral face boundary, which winds but runs outward as it goes.
    """
    # The adaptor rather than the raw curve: an edge may carry its geometry
    # as a pcurve on a surface with no 3D curve of its own, and the adaptor
    # evaluates either without the caller having to know which.
    if curve is None:
        curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    if not last > first:
        return None

    origin = axis.Location()
    direction = axis.Direction()
    reference, transverse = _frame(direction)
    axis_vector = gp_Vec(direction)

    previous_angle: Optional[float] = None
    total_angle = 0.0
    radii: list[float] = []
    heights: list[float] = []

    for index in range(_WINDING_SAMPLES + 1):
        parameter = first + (last - first) * index / _WINDING_SAMPLES
        point = curve.Value(parameter)
        offset = gp_Vec(origin, point)
        height = offset.Dot(axis_vector)
        radial = offset - axis_vector * height
        radius = radial.Magnitude()
        if radius < MIN_RADIUS_MM:
            return None

        angle = math.atan2(radial.Dot(transverse), radial.Dot(reference))
        if previous_angle is not None:
            step = angle - previous_angle
            # Unwrap: a smooth helix never really jumps half a revolution
            # between samples, so a jump that size is the branch cut.
            if step > math.pi:
                step -= 2.0 * math.pi
            elif step < -math.pi:
                step += 2.0 * math.pi
            total_angle += step
        previous_angle = angle
        radii.append(radius)
        heights.append(height)

    mean_radius = sum(radii) / len(radii)
    if mean_radius < MIN_RADIUS_MM:
        return None
    # A thread stays at its radius. A spiral does not, and is not a thread.
    if max(radii) - min(radii) > max(0.1, 0.05 * mean_radius):
        return None

    turns = abs(total_angle) / (2.0 * math.pi)
    return turns, mean_radius, abs(max(heights) - min(heights))


def _frame(direction: gp_Dir) -> tuple[gp_Vec, gp_Vec]:
    """Two unit vectors spanning the plane square to a direction."""
    seed = gp_Vec(1.0, 0.0, 0.0)
    if abs(direction.X()) > 0.9:
        seed = gp_Vec(0.0, 1.0, 0.0)
    axis_vector = gp_Vec(direction)
    reference = seed - axis_vector * seed.Dot(axis_vector)
    reference.Normalize()
    return reference, axis_vector.Crossed(reference)


def _coaxial(a: gp_Ax1, b: gp_Ax1) -> bool:
    """Whether two axes are the same line, to a machining tolerance."""
    if abs(a.Direction().Dot(b.Direction())) < 0.98:
        return False
    offset = gp_Vec(a.Location(), b.Location())
    return offset.Crossed(gp_Vec(a.Direction())).Magnitude() < 0.5


def faces_touching(shape, edges, graph: AttributedAdjacencyGraph) -> list:
    """The graph nodes for every face the given edges bound."""
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)

    # The same 1-based face map the graph was keyed by, so an index found
    # here names the same face the rules will highlight.
    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)

    found: set[int] = set()
    for edge in edges:
        if not ancestors.Contains(edge):
            continue
        for face in ancestors.FindFromKey(edge):
            face_id = face_map.FindIndex(face)
            if face_id > 0:
                found.add(face_id)

    return [graph.node(face_id) for face_id in sorted(found) if graph.has_node(face_id)]
