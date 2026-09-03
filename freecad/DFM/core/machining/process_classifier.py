# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Decides how a part is made: milled, turned, mill-turn or sheet metal.

This runs before any rule, and a dozen rules branch on its verdict -- a bore
on the axis of a turned part is made with a boring bar, not a drill, and is
judged against different limits; a flat-bottomed bore is routine on a lathe
and suspicious on a mill; datum-face rules stand down entirely on a turned
part, which is held by its outside diameter.

Getting this wrong therefore makes turning results actively wrong rather than
merely absent, which is why it comes first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from OCP.gp import gp_Ax1, gp_Vec

from .aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from .config import RuleThresholds


# Two axes are the same axis when their directions agree within about 3
# degrees and the lines pass within half a millimetre of each other.
_DIRECTION_DOT_TOL = 0.9986
_LINE_DISTANCE_TOL = 0.5

# A plane counts as perpendicular to the axis when its normal is within about
# 8 degrees of parallel with it.
_NORMAL_AXIS_DOT_MIN = 0.99

# An end face is at most this many times the dominant cylinder's cross section.
_END_FACE_AREA_MULTIPLE = 3.0


class PartProcessType(Enum):
    """The manufacturing family a part belongs to."""

    UNKNOWN = "UNKNOWN"
    MILLED = "MILLED"
    TURNED = "TURNED"
    MILL_TURN = "MILL_TURN"
    SHEET_METAL = "SHEET_METAL"

    @property
    def is_turning_family(self) -> bool:
        """True when the part sees a lathe at some point."""
        return self in (PartProcessType.TURNED, PartProcessType.MILL_TURN)


@dataclass
class PartProcessResult:
    """The verdict, plus what the rules need in order to act on it."""

    type: PartProcessType = PartProcessType.UNKNOWN
    # The dominant axis of revolution. Normally absent on a milled part,
    # except when the blank is a profile extrusion, where it carries the
    # extrusion direction.
    axis_of_revolution: Optional[gp_Ax1] = None
    turned_surface_fraction: float = 0.0
    # Ancillary stock form. Empty means the default billet assumption.
    blank: str = ""
    sheet_thickness_mm: float = 0.0

    @property
    def has_axis(self) -> bool:
        return self.axis_of_revolution is not None


# =============================================================================
# Axis geometry
# =============================================================================


def axes_colinear(a: gp_Ax1, b: gp_Ax1) -> bool:
    """True when two axes are the same line, ignoring direction sense."""
    if abs(a.Direction().Dot(b.Direction())) < _DIRECTION_DOT_TOL:
        return False
    offset = gp_Vec(a.Location(), b.Location())
    # The axis direction is a unit vector, so the cross product's magnitude
    # is the perpendicular distance between the two lines.
    return offset.Crossed(gp_Vec(a.Direction())).Magnitude() < _LINE_DISTANCE_TOL


def face_axis(node: AagNode) -> Optional[gp_Ax1]:
    """The revolution axis a face carries, if any.

    A revolved B-spline face is exactly as turnable as a cylinder -- the lathe
    does not care that the generatrix is a spline -- so those seed and join
    clusters too. Without them a knob whose whole profile is freeform has no
    cylinder at all and reads as milled.
    """
    if node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
        return node.cyl_cone_axis
    if node.surface_type is SurfaceType.REVOLVED:
        return node.revolved_axis
    return None


def face_axially_symmetric(node: AagNode, axis: gp_Ax1) -> bool:
    node_axis = face_axis(node)
    return node_axis is not None and axes_colinear(node_axis, axis)


def plane_contributes_to_turned_fraction(
    node: AagNode, axis: gp_Ax1, dominant_cross_section: float
) -> bool:
    """Whether a planar face is a genuine end face or shoulder of the profile.

    The size gate is what stops a milled box counting as turned: a box's top
    face is also perpendicular to a vertical bore's axis, but it is many times
    larger than that bore's cross section, so it is not an end face of it.
    """
    if node.surface_type is not SurfaceType.PLANE:
        return False
    normal = node.outward_normal
    if normal is None or abs(normal.Dot(axis.Direction())) <= _NORMAL_AXIS_DOT_MIN:
        return False
    if dominant_cross_section > 0.0:
        return node.area <= dominant_cross_section * _END_FACE_AREA_MULTIPLE
    return True


@dataclass
class _AxisCluster:
    """Faces sharing one axis, with the outward-facing share tracked."""

    representative: gp_Ax1
    total_area: float = 0.0
    # The share of the cluster a lathe would present on the part's exterior.
    convex_area: float = 0.0
    members: list[int] = field(default_factory=list)


def _cluster_axes(graph: AttributedAdjacencyGraph) -> list[_AxisCluster]:
    clusters: list[_AxisCluster] = []
    for node in graph.nodes:  # ascending face id, so clustering is deterministic
        axis = face_axis(node)
        if axis is None:
            continue
        home = next((c for c in clusters if axes_colinear(c.representative, axis)), None)
        if home is None:
            home = _AxisCluster(representative=axis)
            clusters.append(home)
        home.total_area += node.area
        if not node.is_internal:
            # The share of the cluster a lathe would present on the outside
            # of the part, as opposed to bores that say nothing about turning.
            home.convex_area += node.area
        home.members.append(node.face_id)
    return clusters


# =============================================================================
# Classification
# =============================================================================


def classify_part_process(
    graph: AttributedAdjacencyGraph,
    thresholds: Optional[RuleThresholds] = None,
    shape=None,
) -> PartProcessResult:
    """Classify a part from its adjacency graph alone.

    Deliberately independent of feature recognition, so it can run first and
    let the recognizers use its verdict. `shape` is accepted for the sheet
    detection path, which needs the faces themselves.
    """
    limits = thresholds if thresholds is not None else RuleThresholds()
    result = PartProcessResult()

    gauge = detect_sheet_metal(graph, shape)
    if gauge is not None:
        result.type = PartProcessType.SHEET_METAL
        result.blank = "sheet_metal"
        result.sheet_thickness_mm = gauge
        return result

    total_area = graph.total_area()
    if total_area < 1e-6:
        return result  # degenerate

    clusters = _cluster_axes(graph)
    if not clusters:
        result.type = PartProcessType.MILLED
        return result

    best = max(clusters, key=lambda c: c.total_area)

    # A turned part must have a turned *exterior*. Internal bores exist on
    # every milled part and say nothing about turning, so a dominant cluster
    # made purely of bores means prismatic-with-bores. This is a veto on the
    # dominant cluster rather than a filter on cluster choice: falling through
    # to a smaller external boss cluster would promote a minor axis and read a
    # housing with one boss as mill-turn.
    if best.convex_area < best.total_area * limits.turned_convex_share_min:
        result.type = PartProcessType.MILLED
        return result

    dominant_radius = max(
        (
            node.cyl_radius
            for node in graph.nodes
            if node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE)
            and node.cyl_cone_axis is not None
            and axes_colinear(node.cyl_cone_axis, best.representative)
        ),
        default=0.0,
    )
    dominant_cross_section = math.pi * dominant_radius * dominant_radius

    # The denominator is the whole part's area, including end faces, so a flat
    # turned disc with a thick web still scores high.
    turned_area = sum(
        node.area
        for node in graph.nodes
        if face_axially_symmetric(node, best.representative)
        or plane_contributes_to_turned_fraction(
            node, best.representative, dominant_cross_section
        )
    )

    result.axis_of_revolution = best.representative
    result.turned_surface_fraction = turned_area / total_area

    if result.turned_surface_fraction >= limits.turned_fraction_turned_min:
        result.type = PartProcessType.TURNED
    elif result.turned_surface_fraction <= limits.turned_fraction_milled_max:
        result.type = PartProcessType.MILLED
        result.axis_of_revolution = None  # no meaningful axis on a milled part
    else:
        result.type = PartProcessType.MILL_TURN

    return result


def detect_sheet_metal(
    graph: AttributedAdjacencyGraph, shape=None  # noqa: ARG001 - hook signature
) -> Optional[float]:
    """Detect a constant-gauge formed shell, returning its gauge in mm.

    Not yet implemented. Sheet metal is a distinct process family that mutes
    the machining rules entirely, and it lands with the sheet rule family in a
    later phase. Until then a sheet part classifies by its geometry like any
    other, which means it will attract machining findings that do not apply.
    """
    return None
