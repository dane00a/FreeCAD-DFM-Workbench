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

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Ax1, gp_Pnt, gp_Vec

from .aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from .config import RuleThresholds
from ..utils.geometry import FaceIndex
from .features import FeatureType


# Two axes are the same axis when their directions agree within about 3
# degrees and the lines pass within half a millimetre of each other.
_DIRECTION_DOT_TOL = 0.9986
_LINE_DISTANCE_TOL = 0.5

# A plane counts as perpendicular to the axis when its normal is within about
# 8 degrees of parallel with it.
_NORMAL_AXIS_DOT_MIN = 0.99

# An end face is at most this many times the dominant cylinder's cross section.
_END_FACE_AREA_MULTIPLE = 3.0

# Sheet is formable: nearly all of it has to be flat, cylindrical or conical.
_SHEET_DEVELOPABLE_MIN = 0.90

# Thicker than this and it is plate to be machined, not sheet to be formed.
_SHEET_MAX_GAUGE_MM = 8.0

# Gauge as a share of the part's largest dimension. A short thick block can
# pass the absolute gate on its own.
_SHEET_GAUGE_MAX_REL = 0.15

# How much of the paired area has to sit within 30% of the gauge.
_SHEET_UNIFORM_MIN = 0.50

# Two faces are back to back when their normals are this opposed.
_ANTI_PARALLEL_DOT = -0.98

# The two flats a bend joins have to be at a real angle to each other.
_BEND_NON_PARALLEL_DOT = 0.95


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
    if gauge is None:
        # Second route: a shell whose folds were modelled square. Somebody
        # who draws sheet metal without radiusing the folds has still drawn
        # sheet metal, and reading it as a solid gives the part the wrong
        # voice -- thin wall firing on its own gauge, corner radius firing on
        # every fold. Better to recognize the intent and report the sharp
        # folds as the defect.
        gauge = detect_sharp_fold_shell(graph)
    if gauge is None:
        # Third route: a constant-gauge plate with no folds at all is a flat
        # blank, cut and never formed. Both routes above hunt for folds, so a
        # part laser-cut and left flat never reached sheet metal. The
        # solid-stock veto downstream decides whether it really was made that
        # way.
        gauge = detect_flat_blank(graph)
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

        # A profile bar lands here by construction: a significant round body
        # makes it read as partly turned, but it is not a body of revolution
        # and never saw a lathe. Re-labelling it milled keeps the turning
        # rules and the chuck-holding advice off a length of bar in a vise.
        if result.axis_of_revolution is not None and detect_profile_extrusion(
            graph, result.axis_of_revolution, limits
        ):
            result.type = PartProcessType.MILLED
            result.blank = "profile_extrusion"

    return result


# A flat blank thinner than this is foil, not a cut blank.
_BLANK_MIN_GAUGE_MM = 0.4
# The skin (planes facing along the thin axis) against the whole part's area.
_BLANK_SKIN_SHARE_MIN = 0.50
# A plane standing across the thin axis bigger than this share is a wall.
_BLANK_WALL_SHARE_MAX = 0.05


def detect_flat_blank(graph: AttributedAdjacencyGraph) -> Optional[float]:
    """The gauge of a flat blank -- sheet cut and never formed -- or nothing.

    Geometry alone cannot finish this argument: a flat plate of constant
    thickness is the same B-rep whether it left a laser or a mill. What
    settles it is that nothing on it needs a spindle, and that test already
    exists downstream as the solid-stock veto. So this only has to recognise
    the blank, narrowed two ways read off inner wires: it has been CUT (a
    featureless rectangle is stock, and the clamping rules exist for it), and
    the cut goes THROUGH (a hole leaves an inner wire on both skins;
    engraving leaves one on the face it was cut into and nothing behind).
    """
    # The part box is built from PLANE faces only. OCCT pads a curved face's
    # box -- a B-spline's is its control hull -- so lofted walls report an
    # envelope larger than the part: a 1.2 mm panel with pads 0.7 mm proud
    # read 2.17 mm instead of 1.9.
    total = 0.0
    part = Bnd_Box()
    for node in graph.nodes:
        total += node.area
        if node.surface_type is not SurfaceType.PLANE or node.bbox.IsVoid():
            continue
        xn, yn, zn, xx, yx, zx = node.bbox.Get()
        part.Update(xn, yn, zn)
        part.Update(xx, yx, zx)
    if total < 1e-6 or part.IsVoid():
        return None

    pxn, pyn, pzn, pxx, pyx, pzx = part.Get()
    dims = (pxx - pxn, pyx - pyn, pzx - pzn)
    thin = 0
    for i in (1, 2):
        if dims[i] < dims[thin]:
            thin = i
    # The ENVELOPE along the thin axis, which is not the gauge (that is
    # measured below). It only shows the part fits a thin box at all.
    envelope = dims[thin]
    widest = max(dims)
    if envelope < _BLANK_MIN_GAUGE_MM or envelope > _SHEET_MAX_GAUGE_MM:
        return None
    if widest < 1e-6 or envelope / widest > _SHEET_GAUGE_MAX_REL:
        return None

    axis = gp_Vec(
        1.0 if thin == 0 else 0.0,
        1.0 if thin == 1 else 0.0,
        1.0 if thin == 2 else 0.0,
    )
    skin = 0.0
    developable = 0.0
    cut_front = 0
    cut_back = 0
    # One record per skin face for the gauge measurement: where it sits along
    # the thin axis, which way it faces, its area, centroid and half its
    # widest extent across the axis.
    skins: list[tuple[float, float, float, gp_Pnt, float]] = []
    for node in graph.nodes:
        if node.surface_type in (
            SurfaceType.PLANE,
            SurfaceType.CYLINDER,
            SurfaceType.CONE,
            SurfaceType.EXTRUDED,
        ):
            developable += node.area
        if node.surface_type is not SurfaceType.PLANE or node.plane_normal is None:
            continue
        along = abs(gp_Vec(node.plane_normal.XYZ()).Dot(axis))
        if along > 0.9:
            skin += node.area
            # Which side of the sheet, so a cut can be shown to go through.
            outward = node.outward_normal
            facing_front = gp_Vec(outward.XYZ()).Dot(axis) > 0.0
            if facing_front:
                cut_front += node.inner_loop_count
            else:
                cut_back += node.inner_loop_count
            ext = node.bbox_dims()
            lateral = max(ext[i] for i in range(3) if i != thin)
            skins.append(
                (
                    gp_Vec(node.centroid.XYZ()).Dot(axis),
                    1.0 if facing_front else -1.0,
                    node.area,
                    node.centroid,
                    0.5 * lateral,
                )
            )
        elif node.area > total * _BLANK_WALL_SHARE_MAX:
            # A big plane standing across the thin axis is a fold or a milled
            # wall; either way not a flat blank, and the fold routes own folds.
            return None
    if cut_front < 1 or cut_back < 1:
        return None
    if skin / total < _BLANK_SKIN_SHARE_MIN:
        return None
    if developable / total < _SHEET_DEVELOPABLE_MIN:
        return None

    # THE GAUGE IS MEASURED, never read off the envelope: a 1.2 mm panel with
    # a 0.7 mm emboss has a 1.9 mm envelope. The error hides itself -- the
    # wrong gauge makes the formed pass's skin test reject every pair, so the
    # very features that inflated the envelope vanish. For each skin face, the
    # nearest opposed skin on its material side; then the area-weighted
    # median, so a pad's crest cannot outvote the deck and one bad pair cannot
    # move the answer. The lateral gate uses the SMALLER face's reach: with
    # the larger's, a deck pairs with a small pad ceiling far across the part.
    thick_area: list[tuple[float, float]] = []
    for i, (at_i, sign_i, area_i, centre_i, half_i) in enumerate(skins):
        best = 1.0e18
        for j, (at_j, sign_j, _area_j, centre_j, half_j) in enumerate(skins):
            if i == j or sign_i * sign_j > 0.0:
                continue
            gap = (at_i - at_j) * sign_i  # j on i's material side
            if gap <= 0.01:
                continue
            d = gp_Vec(centre_i, centre_j)
            lateral = (d - axis * d.Dot(axis)).Magnitude()
            if lateral > min(half_i, half_j):
                continue
            best = min(best, gap)
        if best < 1.0e17:
            thick_area.append((best, area_i))
    if not thick_area:
        return None
    thick_area.sort()
    paired = sum(area for _, area in thick_area)
    gauge = thick_area[-1][0]
    acc = 0.0
    for thickness, area in thick_area:
        acc += area
        if acc >= 0.5 * paired:
            gauge = thickness
            break
    if gauge < _BLANK_MIN_GAUGE_MM or gauge > _SHEET_MAX_GAUGE_MM:
        return None
    return gauge


def detect_sheet_metal(
    graph: AttributedAdjacencyGraph, shape=None  # noqa: ARG001 - kept for parity
) -> Optional[float]:
    """The gauge of a formed sheet part, or nothing if it is not one.

    Sheet has to be told apart from a thin-walled milled shell, which is also
    a uniform thin skin with rounded corners. Four things must hold, and the
    last is what actually does the work.

    The part is developable: nearly all its area is flat, cylindrical or
    conical, because those are the shapes a brake and a die can make. It has
    a consistent gauge, taken as the area-weighted median distance between
    opposed skins. That gauge is small, both absolutely and against the size
    of the part.

    And it carries at least one real bend. Sheet wraps a fold as *two
    concentric cylinders* one gauge apart -- the inside and the outside of
    the same bend, sharing an axis -- joining two flats that are at an angle
    to each other. A milled fillet has only the inner cylinder; its outside
    corner is sharp, or some unrelated radius. That concentric pair is the
    signature, and without demanding it every milled enclosure reads as sheet.
    """
    areas = _area_by_surface_type(graph)
    total = sum(areas.values())
    if total < 1e-6:
        return None

    # Formable on a brake or in a die. Extruded walls are single-curvature
    # and so stampable, though they are never evidence of a bend.
    developable = (
        areas.get(SurfaceType.PLANE, 0.0)
        + areas.get(SurfaceType.CYLINDER, 0.0)
        + areas.get(SurfaceType.CONE, 0.0)
        + areas.get(SurfaceType.EXTRUDED, 0.0)
    )
    sculpted = total - developable
    share = developable / total

    # The cheap ceiling first. Counting every sculpted face as formed is the
    # most generous answer the expensive test below could give; if even that
    # misses the gate the part is genuinely freeform and leaves here having
    # cost nothing.
    if share < _SHEET_DEVELOPABLE_MIN and (
        sculpted <= 0.0 or (developable + sculpted) / total < _SHEET_DEVELOPABLE_MIN
    ):
        return None
    # A fold band is a cylinder OR a cone: a tapered flange folds on a cone
    # pair, and a cylinder-only gate left a part whose only fold is conical
    # classified milled, with the bend pass never run on it.
    if areas.get(SurfaceType.CYLINDER, 0.0) + areas.get(SurfaceType.CONE, 0.0) <= 0.0:
        return None  # no fold band and no hole: nothing was formed here

    gauge = _median_gauge(graph)
    if gauge is None or gauge > _SHEET_MAX_GAUGE_MM:
        return None

    extents = _part_extents(graph)
    if extents is not None and gauge > _SHEET_GAUGE_MAX_REL * max(extents):
        return None

    if not _has_concentric_bend(graph, gauge):
        return None

    if share >= _SHEET_DEVELOPABLE_MIN:
        return gauge

    # A uniform-gauge shell with a real bend whose shortfall against the area
    # gate is sculpture. A drawn louver or a swept hood is formed sheet
    # however its surface is modelled, so ask the material which of those
    # faces are one skin of a gauge-thick pair, and count only those.
    skin = freeform_skin_area(graph, shape, gauge)
    if (developable + skin) / total < _SHEET_DEVELOPABLE_MIN:
        return None
    return gauge


def _area_by_surface_type(
    graph: AttributedAdjacencyGraph,
) -> dict[SurfaceType, float]:
    areas: dict[SurfaceType, float] = {}
    for node in graph.nodes:
        areas[node.surface_type] = areas.get(node.surface_type, 0.0) + node.area
    return areas


def _part_extents(graph: AttributedAdjacencyGraph) -> Optional[tuple]:
    """The part's extent, measured from its PLANE faces only.

    OpenCascade pads a curved face's box to its control hull rather than to
    the surface, so a box over every face is larger than the part, and the
    larger the box the more permissive the gauge-to-size gate it feeds.
    ``detect_flat_blank`` already builds its box this way; both sheet
    detectors now agree. A sheet part always has planar skins on both sides,
    so the plane-only box is the part.
    """
    box = Bnd_Box()
    for node in graph.nodes:
        if node.surface_type is not SurfaceType.PLANE:
            continue
        if not node.bbox.IsVoid():
            box.Add(node.bbox)
    if box.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return (xmax - xmin, ymax - ymin, zmax - zmin)


class _Skin:
    """A flat face, reduced to what measuring the gauge needs."""

    __slots__ = ("normal", "centroid", "area", "reach")

    def __init__(self, normal, centroid, area, reach):
        self.normal = normal
        self.centroid = centroid
        self.area = area
        self.reach = reach


def _median_gauge(graph: AttributedAdjacencyGraph) -> Optional[float]:
    """The part's thickness, as the area-weighted median of the local ones.

    For each flat face the nearest anti-parallel face it overlaps sideways is
    the skin on the other side, and that gap is the thickness there. Median
    rather than mean, so a part with one thick boss still reports its gauge
    instead of an average of the two.
    """
    skins: list[_Skin] = []
    for node in graph.nodes:
        if node.surface_type is not SurfaceType.PLANE:
            continue
        normal = node.outward_normal
        if normal is None or node.centroid is None or node.bbox.IsVoid():
            continue
        xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
        reach = 0.5 * max(xmax - xmin, ymax - ymin, zmax - zmin)
        skins.append(_Skin(normal, node.centroid, node.area, reach))

    measured: list[tuple[float, float]] = []
    for skin in skins:
        outward = gp_Vec(skin.normal)
        nearest = math.inf
        for other in skins:
            if other is skin:
                continue
            if skin.normal.Dot(other.normal) > _ANTI_PARALLEL_DOT:
                continue
            offset = gp_Vec(skin.centroid, other.centroid)
            # Positive when the other face lies on the material side.
            gap = -offset.Dot(outward)
            if gap <= 0.01:
                continue
            sideways = offset - outward * offset.Dot(outward)
            # Both faces' reach, not this one's alone. The one-sided test
            # admits more candidate pairs, and since the gauge is the
            # NEAREST of them it could only ever read the gauge too small.
            if sideways.Magnitude() > min(skin.reach, other.reach):
                continue  # not actually across from one another
            nearest = min(nearest, gap)
        if nearest < math.inf:
            measured.append((nearest, skin.area))

    if not measured:
        return None

    measured.sort()
    paired_area = sum(area for _, area in measured)
    gauge = measured[-1][0]
    running = 0.0
    for thickness, area in measured:
        running += area
        if running >= 0.5 * paired_area:
            gauge = thickness
            break
    if gauge <= 1e-6:
        return None

    # Most of the part has to actually be at that gauge. A shell with one
    # uniform face and a lot of thick section was machined, not formed.
    at_gauge = sum(
        area for thickness, area in measured if 0.7 * gauge <= thickness <= 1.3 * gauge
    )
    if at_gauge / paired_area < _SHEET_UNIFORM_MIN:
        return None
    return gauge


def _has_concentric_bend(graph: AttributedAdjacencyGraph, gauge: float) -> bool:
    """Whether the part carries at least one genuine formed bend.

    The discriminator against a thin milled shell. A bend is two coaxial
    developable bands one gauge apart -- the inside and outside of the same
    fold -- with two flats among their neighbours that are at an angle to
    each other. A fillet has one cylinder, so it never matches.

    A band is a cylinder or a cone, never one against the other. The cone
    test is the bend recognizer's own conical predicate, gap measured normal
    to the surface; change one and change the other, or the recognizer never
    runs on the part at all.
    """
    bends: list[tuple] = []
    for node in graph.nodes:
        if node.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
            continue
        if node.cyl_cone_axis is None:
            continue
        flats = []
        for edge in graph.edges_of(node.face_id):
            other_id = edge.other_face(node.face_id)
            if not graph.has_node(other_id):
                continue
            other = graph.node(other_id)
            if other.surface_type is not SurfaceType.PLANE:
                continue
            normal = other.outward_normal
            if normal is not None:
                flats.append(normal)
        bends.append((node.cyl_cone_axis, node, flats))

    for index, (axis, node, flats) in enumerate(bends):
        for other_axis, other_node, other_flats in bends[index + 1 :]:
            if not axes_colinear(axis, other_axis):
                continue
            separation = _band_separation(node, other_node)
            if separation is None:
                continue
            if not 0.5 * gauge <= separation <= 1.5 * gauge:
                continue
            joined = flats + other_flats
            for position, first in enumerate(joined):
                for second in joined[position + 1 :]:
                    if abs(first.Dot(second)) < _BEND_NON_PARALLEL_DOT:
                        return True
    return False


def _band_separation(a: AagNode, b: AagNode) -> Optional[float]:
    """Radial separation of two coaxial fold bands, or None if they cannot pair.

    Cylinders: the difference of radii. Cones: equal half-angles, overlapping
    along the axis, and the radius gap at the middle of the overlap times
    cos(half-angle) -- two cones one gauge apart differ in RADIUS by
    gauge / cos(half-angle), out by 41% at a 45-degree taper.
    """
    from .sheet_folds import band_axial_span, cone_radius_at, cone_semi_angle_along

    if a.surface_type is not b.surface_type:
        return None  # never a cone against a cylinder
    if a.surface_type is SurfaceType.CYLINDER:
        return abs(a.cyl_radius - b.cyl_radius)
    direction = a.cyl_cone_axis.Direction()
    origin = a.cyl_cone_axis.Location()
    semi_a = cone_semi_angle_along(a, direction)
    if abs(semi_a - cone_semi_angle_along(b, direction)) > 0.02:
        return None
    a_lo, a_hi = band_axial_span(a, origin, direction)
    b_lo, b_hi = band_axial_span(b, origin, direction)
    s_lo = max(a_lo, b_lo)
    s_hi = min(a_hi, b_hi)
    if s_hi < s_lo + 0.5:
        return None
    s = 0.5 * (s_lo + s_hi)
    return abs(
        cone_radius_at(b, origin, direction, s) - cone_radius_at(a, origin, direction, s)
    ) * math.cos(semi_a)


# =============================================================================
# Refinement, once the features are known
# =============================================================================


# A turned part cannot have these: nothing on a lathe makes a slot or a
# pocket. Their presence means the part visits a mill as well.
_PRISMATIC_TYPES = frozenset(
    {
        FeatureType.SLOT,
        FeatureType.POCKET,
        FeatureType.SPHERICAL_POCKET,
        FeatureType.MARKING_TEXT,
        FeatureType.THROUGH_CAVITY,
        FeatureType.RIB,
        FeatureType.FLEXURE_SLIT,
        FeatureType.BROACHED_SLOT,
        FeatureType.V_GROOVE,
    }
)

# Features that can only be made by cutting into solid stock. Formed sheet
# cannot contain one, so finding one means the shell was milled from billet.
_SOLID_STOCK_TYPES = frozenset(
    {
        FeatureType.BLIND_HOLE,
        FeatureType.COUNTERBORE,
        FeatureType.BREAKTHROUGH_HOLE,
        FeatureType.POCKET,
        FeatureType.SPHERICAL_POCKET,
        FeatureType.O_RING_GLAND,
        FeatureType.RETAINING_RING_GROOVE,
        FeatureType.GROOVE,
        FeatureType.THREAD_RELIEF_GROOVE,
        FeatureType.BOSS,
        FeatureType.RIB,
        FeatureType.STEP,
        FeatureType.V_GROOVE,
    }
)

# Types whose sheet reading is a formed feature -- an emboss, a louver, a
# dimple -- rather than a cut one, if the face carries a gauge-thick skin.
_FORMABLE_TYPES = frozenset(
    {
        FeatureType.BOSS,
        FeatureType.POCKET,
        FeatureType.SPHERICAL_POCKET,
        FeatureType.BLIND_HOLE,
        FeatureType.BREAKTHROUGH_HOLE,
        FeatureType.STEP,
        FeatureType.GROOVE,
        FeatureType.O_RING_GLAND,
        FeatureType.RETAINING_RING_GROOVE,
    }
)

# A boss or step is still lathe work while all its flats are annular and all
# its curved faces coaxial. Anything off-axis needs a mill.
_ON_AXIS_DOT = 0.99


def refine_part_process_with_features(
    base: PartProcessResult,
    features,
    graph: Optional[AttributedAdjacencyGraph] = None,
    shape=None,
    declared_blank: str = "",
) -> PartProcessResult:
    """Revisit the classification now that the features are known.

    The geometric pass runs before recognition because the recognizers need
    its verdict, which means it decides on shape alone. Two of its answers
    can only be checked once the features exist.

    A turned part with a slot in it is a mill-turn part: the lathe cannot
    make a slot, so the part visits both machines and the setup rules need to
    know. And a shell that looked like sheet is really milled if it contains
    anything that had to be cut into solid stock.
    """
    if base.type is PartProcessType.SHEET_METAL:
        return _confirm_sheet(base, features, graph, shape, declared_blank)
    if base.type is not PartProcessType.TURNED:
        return base

    for feature in features:
        # A rib is prismatic *unless* it is the web of a revolved part. The
        # web between two flanges of a pulley is faced and grooved on the
        # lathe, and the rib recognizer reads it as a rib because that is
        # what it looks like: two opposed faces with material between. Sent
        # through the same on-axis test as a boss it comes out turned.
        if feature.type is FeatureType.RIB:
            if _milled_protrusion(feature, base, graph, on_axis_types=(FeatureType.RIB,)):
                base.type = PartProcessType.MILL_TURN
                return base
            continue
        if feature.type in _PRISMATIC_TYPES:
            base.type = PartProcessType.MILL_TURN
            return base
        if _milled_protrusion(feature, base, graph):
            base.type = PartProcessType.MILL_TURN
            return base
    return base


def _milled_protrusion(
    feature,
    base: PartProcessResult,
    graph,
    on_axis_types=(FeatureType.BOSS, FeatureType.STEP),
) -> bool:
    """Whether a protrusion could not have come off a lathe.

    A revolved boss, step or web has annular flats and coaxial curves. A
    chordal shelf, a rectangular pad, an off-axis boss -- any of those needs
    a mill. Blend faces carry no signal either way and are skipped.
    """
    if graph is None or base.axis_of_revolution is None:
        return False
    if feature.type not in on_axis_types:
        return False

    axis = base.axis_of_revolution
    for face_id in feature.faces:
        if not graph.has_node(face_id):
            continue
        node = graph.node(face_id)
        if node.surface_type is SurfaceType.PLANE:
            normal = node.outward_normal
            if normal is not None and abs(normal.Dot(axis.Direction())) < _ON_AXIS_DOT:
                return True
        elif node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
            if node.cyl_cone_axis is not None and not axes_colinear(
                node.cyl_cone_axis, axis
            ):
                return True
    return False


def _confirm_sheet(
    base: PartProcessResult,
    features,
    graph,
    shape=None,
    _declared_blank: str = "",
) -> PartProcessResult:
    """Keep the sheet verdict only if nothing had to be cut into solid stock.

    The geometric detector keys on a uniform shell with concentric bends, and
    a thin-walled enclosure milled from billet shares that signature. What it
    cannot share is a blind hole or a machined gland: those require solid
    material to cut into, and formed sheet has none.

    Most of this function is the exemptions, because a formed feature looks
    like a cut one to a recognizer that is not thinking about gauge. An
    emboss reads as a boss, its back as a pocket, a louver's hood as a blind
    hole. What tells them apart is that drawn sheet keeps its thickness: the
    face of a formed feature has a matching skin exactly one gauge behind it,
    and a machined face sits on solid stock with nothing behind it at all.
    """
    # A declared solid blank settles it outright. A thin-walled enclosure
    # milled from billet is a uniform-gauge shell with concentric corner
    # radii, which is the same thing a folded box is by every local
    # measurement -- probing the material confirms the wall really is one
    # gauge thick with air behind it. What separates them is not in the
    # geometry at all: it is knowing what stock was ordered. The shop can
    # say so, and when it has, that is the answer.
    declared = _declared_blank
    if declared and declared != "sheet_metal":
        base.type = PartProcessType.MILLED
        base.blank = declared
        base.sheet_thickness_mm = 0.0
        return base

    gauge = base.sheet_thickness_mm
    for feature in features:
        if feature.type not in _SOLID_STOCK_TYPES:
            continue

        # A bore that stops in open air and is no deeper than a couple of
        # gauges is a punched hole, not a drilling. Sheet is too thin to
        # hold a real blind hole anyway. Such a bore now carries its own
        # type, so it is asked of a breakthrough hole: a blind hole can no
        # longer also terminate in a cavity.
        if (
            feature.type == FeatureType.BREAKTHROUGH_HOLE
            and feature.param("terminates_in_cavity", False)
            and (feature.number("depth_mm") or 1e9) <= 2.0 * gauge
        ):
            continue

        if feature.type in _FORMABLE_TYPES and _carries_skin(
            feature, graph, gauge, shape
        ):
            continue

        # A rib whose thickness *is* the gauge is the sheet's own wall, seen
        # by the rib recognizer: a closed sheet profile always presents its
        # two skins as an opposed thin pair. Only a rib meaningfully off the
        # gauge is evidence of solid stock.
        if feature.type == FeatureType.RIB:
            thickness = feature.number("thickness_mm") or 0.0
            if thickness > 0.0 and abs(thickness - gauge) < 0.3 * gauge:
                continue

        # Likewise a step only one gauge wide is a sheared edge -- a flange
        # ending mid-panel reads as a terrace. A machined shelf is far wider
        # than the material is thick.
        if feature.type == FeatureType.STEP:
            width = feature.number("step_width_mm") or 0.0
            if width > 0.0 and width <= gauge * 1.3:
                continue

        base.type = PartProcessType.MILLED
        base.blank = ""
        base.sheet_thickness_mm = 0.0
        return base

    return base


def _carries_skin(feature, graph, gauge: float, shape=None) -> bool:  # noqa: ARG001
    """Whether any face of a feature has a constant-gauge skin partner.

    Any face is enough, not the largest: a step whose shear wall outweighs
    its skinned terrace would otherwise be missed.

    The partner is the analytic one the formed-feature pass uses: an
    anti-parallel plane, a concentric sphere or a coaxial cylinder exactly
    one gauge away (within 0.25 mm), footprints touching. Nothing is sampled
    and the solid is not probed, as in the reference's veto: a shallow gland
    in a thick plate has the bottom face a little under one gauge behind its
    floor, and only the exact-offset test keeps that from reading as formed.
    ``shape`` is accepted and ignored so callers need not change.
    """
    if graph is None or gauge <= 0.0:
        return False
    # Imported here: the formed-feature recognizer imports this module.
    from .recognizers.sheet_formed_recognizer import _gauge_skin_partner

    for face_id in feature.faces:
        if not graph.has_node(face_id):
            continue
        node = graph.node(face_id)
        if node.surface_type not in (
            SurfaceType.PLANE,
            SurfaceType.SPHERE,
            SurfaceType.CYLINDER,
        ):
            continue
        if _gauge_skin_partner(graph, node, gauge) is not None:
            return True
    return False


def freeform_skin_area(
    graph: AttributedAdjacencyGraph, shape, gauge: float
) -> float:
    """Area of the sculpted faces that are one skin of a constant-gauge pair.

    A drawn louver or a swept hood is a formed sheet feature, but its surface
    is a spline and so counts against the developable area gate -- which
    would throw out exactly the parts most obviously made on a press.

    The question is asked of the material rather than of the surface type: a
    formed face has metal for one gauge behind it and air past that, because
    that is what forming does. A machined face has solid stock behind it as
    far as the probe goes.
    """
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN

    from .aag_builder import _face_sample_point

    if shape is None or gauge <= 0.0:
        return 0.0

    face_index = FaceIndex(shape)
    classifier = BRepClass3d_SolidClassifier(shape)
    tolerance = 1e-6
    total = 0.0

    for node in graph.nodes:
        # Extruded walls already count as developable, so counting them again
        # would let a part clear the gate on area it does not have.
        if node.surface_type not in (
            SurfaceType.BSPLINE,
            SurfaceType.REVOLVED,
            SurfaceType.OTHER,
        ):
            continue
        try:
            face = face_index.face_at(node.face_id)
        except Exception:
            continue
        sample = _face_sample_point(face)
        if sample is None:
            continue
        point, normal = sample
        if node.is_reversed:
            normal.Reverse()

        inward = gp_Vec(normal).Multiplied(-1.0)
        near = point.Translated(inward.Multiplied(0.5 * gauge))
        far = point.Translated(inward.Multiplied(1.5 * gauge))

        try:
            classifier.Perform(near, tolerance)
            near_inside = classifier.State() == TopAbs_IN
            classifier.Perform(far, tolerance)
            far_inside = classifier.State() == TopAbs_IN
        except Exception:
            continue

        # Metal at half a gauge, air at one and a half: a skin exactly one
        # gauge thick, which is what forming leaves and machining does not.
        if near_inside and not far_inside:
            total += node.area

    return total


# A fold modelled with no radius is still sheet-metal intent, and the gauge
# it implies has to be plausible for a brake.
_FOLD_MIN_GAUGE_MM = 0.5
_FOLD_MAX_GAUGE_MM = 6.0

# The dominant gauge has to account for this much of the paired area before
# the part counts as one consistent shell rather than an assortment of slabs.
_FOLD_DOMINANT_SHARE = 0.7

# Two panels are the same orientation within about 25 degrees, and square to
# each other below 45.
_FOLD_SAME_ORIENTATION = 0.9
_FOLD_SQUARE_ORIENTATION = 0.7


def detect_sharp_fold_shell(
    graph: AttributedAdjacencyGraph,
) -> Optional[float]:
    """The gauge of a shell whose folds were modelled square.

    Somebody who draws sheet metal without radiusing the folds has still
    drawn sheet metal, and reading it as a solid gives the part the wrong
    voice entirely -- thin wall firing on its own gauge, the corner-radius
    rule firing on every fold. Better to recognize the intent and report the
    sharp folds as the defect they are.

    The evidence is a consistent gauge across panels that meet at an angle.
    That alone would also describe a milled box, so the last test is what
    separates them: a foldable blank has at most two of its panel pairs
    joined, because a third join is a corner that no single flat blank can
    make. Three connected pairs means the part was carved, not folded.
    """
    planes = graph.nodes_by_surface_type(SurfaceType.PLANE)
    total_area = sum(node.area for node in planes)
    if len(planes) < 4 or total_area < 1e-6:
        return None

    histogram, members, paired_area = _gauge_histogram(planes)
    gauge = _dominant_gauge(histogram, paired_area)
    if gauge is None:
        return None

    panels = _dominant_panels(members, gauge)
    if not panels:
        return None

    groups = _orientation_groups(panels, gauge)
    if len(groups) < 2:
        return None

    return gauge if _folds_rather_than_carvings(graph, groups) else None


def _gauge_histogram(planes):
    """Every opposed overlapping plane pair, bucketed by the gap between them.

    Credited by the smaller of the two areas, and bucketed rather than
    assigned to a nearest partner: a shallow pocket floor a millimetre above
    the bottom skin would otherwise drag that entire panel into the wrong
    bucket and sink the whole reading.
    """
    histogram: dict[int, float] = {}
    members: dict[int, list] = {}
    paired_area = 0.0

    for index, first in enumerate(planes):
        first_normal = first.outward_normal
        if first_normal is None:
            continue
        for second in planes[index + 1 :]:
            second_normal = second.outward_normal
            if second_normal is None or second_normal.Dot(first_normal) > -0.999:
                continue
            gap = abs(
                gp_Vec(first.centroid, second.centroid).Dot(gp_Vec(first_normal))
            )
            if gap < 0.3 or gap > _FOLD_MAX_GAUGE_MM:
                continue
            reach = Bnd_Box()
            reach.Add(first.bbox)
            reach.Enlarge(gap + 0.5)
            if reach.IsOut(second.bbox):
                continue

            bucket = int(round(gap * 10.0))
            weight = min(first.area, second.area)
            histogram[bucket] = histogram.get(bucket, 0.0) + weight
            paired_area += weight

            # Membership needs a real two-sided skin: both partners must
            # carry sheet-flat area at this gauge. A block's big outer side
            # pairing with a small cutout wall is not a panel, and letting it
            # in fakes a second fold orientation.
            if weight >= 25.0 * (bucket / 10.0) ** 2:
                members.setdefault(bucket, []).extend((first, second))

    return histogram, members, paired_area


def _dominant_gauge(histogram, paired_area: float) -> Optional[float]:
    """The one gauge that most of the part is at, if there is one."""
    if paired_area < 1e-6:
        return None
    best_bucket, best_area = None, 0.0
    for bucket, area in histogram.items():
        # Neighbouring buckets count: a real gauge spreads across a tenth
        # either side once booleans have had their way with it.
        spread = area + histogram.get(bucket - 1, 0.0) + histogram.get(bucket + 1, 0.0)
        if spread > best_area:
            best_bucket, best_area = bucket, spread
    if best_bucket is None:
        return None
    if best_area / paired_area < _FOLD_DOMINANT_SHARE:
        return None
    gauge = best_bucket / 10.0
    return gauge if gauge >= _FOLD_MIN_GAUGE_MM else None


def _dominant_panels(members, gauge: float) -> list:
    """The faces at the dominant gauge, with the sheared edge strips dropped.

    A face whose second-smallest extent is about the gauge is the edge of the
    sheet seen side-on, not a panel of it.
    """
    wanted = int(round(gauge * 10.0))
    panels, seen = [], set()
    for bucket in (wanted - 1, wanted, wanted + 1):
        for node in members.get(bucket, ()):
            if node.face_id in seen:
                continue
            extents = sorted(node.bbox_dims())
            if extents[1] <= gauge * 1.3:
                continue
            seen.add(node.face_id)
            panels.append(node)
    return panels


def _orientation_groups(panels, gauge: float) -> list:
    """Panels grouped by which way they face, keeping only the substantial ones."""
    groups: list[tuple] = []
    for node in panels:
        normal = node.outward_normal
        if normal is None:
            continue
        for existing in groups:
            if abs(existing[0].Dot(normal)) > _FOLD_SAME_ORIENTATION:
                existing[1].add(node.face_id)
                break
        else:
            groups.append((normal, {node.face_id}, [node]))
            continue
        for existing in groups:
            if abs(existing[0].Dot(normal)) > _FOLD_SAME_ORIENTATION:
                existing[2].append(node)
                break

    total = sum(node.area for node in panels)
    floor = max(25.0 * gauge * gauge, 0.05 * total)
    return [
        group for group in groups if sum(n.area for n in group[2]) >= floor
    ]


def _folds_rather_than_carvings(graph, groups) -> bool:
    """Whether the joins between panels look folded rather than carved.

    A sharp join between two panels at an angle is the fold-intent tell. But
    a milled enclosure also has square wall-to-floor corners, so the count is
    what separates them: three connected panel pairs is a corner, and no
    single flat blank folds into a corner. Blend-bridged joins count toward
    that veto even though they are not sharp, because a carved box's rounded
    wall-to-wall corners are still corners.
    """
    sharp_pairs = connected_pairs = 0
    for index, first in enumerate(groups):
        for second in groups[index + 1 :]:
            if abs(first[0].Dot(second[0])) > _FOLD_SQUARE_ORIENTATION:
                continue
            sharp = bridged = False
            for face_id in sorted(first[1]):
                for edge in graph.edges_of(face_id):
                    other = edge.other_face(face_id)
                    if other in second[1]:
                        if edge.concavity is Concavity.TANGENT:
                            bridged = True
                        else:
                            sharp = True
                    elif graph.has_node(other):
                        middle = graph.node(other)
                        if middle.surface_type in (
                            SurfaceType.CYLINDER,
                            SurfaceType.TORUS,
                        ):
                            for hop in graph.edges_of(other):
                                if hop.other_face(other) in second[1]:
                                    bridged = True
                                    break
                    if sharp:
                        break
                if sharp:
                    break
            if sharp:
                sharp_pairs += 1
            if sharp or bridged:
                connected_pairs += 1

    if sharp_pairs < 1:
        return False
    # Three joined pairs is a carved corner, not a folded blank.
    return connected_pairs < 3


# =============================================================================
# Profile-extrusion blanks
# =============================================================================


# Drawn or broached stock is *bar*: longer along its axis than its section is
# wide. A short part with a swept profile -- a bladed disk with broached
# dovetails is a perfect constant section -- was turned and then broached,
# and keeps its turning label.
_PROFILE_MIN_LENGTH_RATIO = 2.0

# The two ends of a constant section are the same profile. Each may lose the
# same bore to end-drilling, so a modest difference is allowed.
_PROFILE_CAP_AREA_TOLERANCE = 0.15

# How close to an axial extreme a face has to sit to be part of that end cap.
_PROFILE_CAP_BAND_MM = 0.5

# A cap stands square to the axis; an extrusion's flanks run along it.
_PROFILE_CAP_ALIGNMENT = 0.99
_PROFILE_FLANK_PLANE_MAX = 0.05
_PROFILE_FLANK_AXIS_MIN = 0.99


def detect_profile_extrusion(
    graph: AttributedAdjacencyGraph, axis: gp_Ax1, thresholds: RuleThresholds
) -> bool:
    """Whether the part was cut from drawn or broached bar.

    Only asked of a part that scored in the mill-turn band, because that is
    exactly where a profile bar lands: it has a significant round body, so it
    reads as partly turned, but it is not a body of revolution and was never
    put on a lathe. A euro lock cylinder is the case -- a round body with a
    flat stem, drawn as one section and then drilled and milled in a vise.

    Getting it right matters beyond the label. A part called mill-turn gets
    turning rules, an axis of revolution, and setup advice about holding it
    in a chuck, none of which applies to a length of bar in a vise.
    """
    ratio = getattr(thresholds, "profile_bar_min_length_ratio", _PROFILE_MIN_LENGTH_RATIO)
    span = _axial_span_and_reach(graph, axis)
    if span is None:
        return False
    low, high, reach = span
    # Conservative on purpose: a profile cut shorter than its own section is
    # wide stays mill-turn rather than being called bar.
    if (high - low) < ratio * reach:
        return False

    caps = _end_cap_clusters(graph, axis, low, high)
    if not caps[0] or not caps[1]:
        return False
    if not _caps_are_congruent(caps):
        return False
    return all(_cap_boundary_is_drawable(graph, cluster, axis) for cluster in caps)


def _axial_span_and_reach(graph: AttributedAdjacencyGraph, axis: gp_Ax1):
    """How far the part runs along an axis, and how far it reaches off it."""
    direction = gp_Vec(axis.Direction())
    origin = axis.Location()
    low, high, reach = math.inf, -math.inf, 0.0

    for node in graph.nodes:
        if node.bbox.IsVoid():
            continue
        xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
        for x in (xmin, xmax):
            for y in (ymin, ymax):
                for z in (zmin, zmax):
                    offset = gp_Vec(origin, gp_Pnt(x, y, z))
                    along = offset.Dot(direction)
                    low = min(low, along)
                    high = max(high, along)
                    radial = offset - direction.Multiplied(along)
                    reach = max(reach, radial.Magnitude())

    if low is math.inf or reach <= 0.0:
        return None
    return low, high, reach


def _end_cap_clusters(
    graph: AttributedAdjacencyGraph, axis: gp_Ax1, low: float, high: float
) -> tuple[list, list]:
    """The coplanar faces making up each end of the bar.

    A cluster rather than a single face: a boolean fuse leaves the section
    split into several coplanar pieces -- the euro blank's end is a cylinder
    cap and a stem cap, never merged -- and the profile boundary is the
    cluster's edges to everything outside it.
    """
    direction = gp_Vec(axis.Direction())
    origin = axis.Location()
    caps: tuple[list, list] = ([], [])

    for node in graph.nodes_by_surface_type(SurfaceType.PLANE):
        normal = node.outward_normal
        if normal is None or node.centroid is None:
            continue
        if abs(normal.Dot(axis.Direction())) < _PROFILE_CAP_ALIGNMENT:
            continue
        along = gp_Vec(origin, node.centroid).Dot(direction)
        if abs(along - low) < _PROFILE_CAP_BAND_MM:
            caps[0].append(node)
        elif abs(along - high) < _PROFILE_CAP_BAND_MM:
            caps[1].append(node)
    return caps


def _caps_are_congruent(caps) -> bool:
    areas = [sum(node.area for node in cluster) for cluster in caps]
    largest = max(areas)
    if largest <= 0.0:
        return False
    return abs(areas[0] - areas[1]) <= _PROFILE_CAP_AREA_TOLERANCE * largest


def _cap_boundary_is_drawable(
    graph: AttributedAdjacencyGraph, cluster, axis: gp_Ax1
) -> bool:
    """Whether an end cap's outline is a section a die could draw.

    Every edge of the outline has to be a line or an arc -- a spline or an
    ellipse is not a die profile -- and every face across it has to be a
    flank running along the axis. A cone is deliberately refused: a taper
    changes the cross-section, and no die draws that.

    Inner-wire edges are skipped. They are bores drilled into the end, which
    are machined features rather than part of the section.

    The section must mix arcs and lines. All arcs is round bar, which is
    turning; all lines is rectangular bar, which is milling and sawing.
    Neither needs this label.
    """
    members = {node.face_id for node in cluster}
    arcs = lines = 0

    for node in cluster:
        for edge in graph.edges_of(node.face_id):
            if edge.is_inner_wire_edge:
                continue
            other_id = edge.other_face(node.face_id)
            if other_id in members or not graph.has_node(other_id):
                continue

            if edge.edge_curve_type == "circle":
                arcs += 1
            elif edge.edge_curve_type == "line":
                lines += 1
            else:
                return False

            if not _is_extrusion_flank(graph.node(other_id), axis):
                return False

    return arcs >= 1 and lines >= 1


def _is_extrusion_flank(node: AagNode, axis: gp_Ax1) -> bool:
    """Whether a face is a side of the extrusion rather than something cut."""
    if node.surface_type is SurfaceType.PLANE:
        normal = node.outward_normal
        if normal is None:
            return False
        return abs(normal.Dot(axis.Direction())) < _PROFILE_FLANK_PLANE_MAX
    if node.surface_type is SurfaceType.CYLINDER and node.cyl_cone_axis is not None:
        return (
            abs(node.cyl_cone_axis.Direction().Dot(axis.Direction()))
            > _PROFILE_FLANK_AXIS_MIN
        )
    return False
