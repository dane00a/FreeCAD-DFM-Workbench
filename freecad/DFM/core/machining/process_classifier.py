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
from OCP.gp import gp_Ax1, gp_Vec

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
    if areas.get(SurfaceType.CYLINDER, 0.0) <= 0.0:
        return None  # no bends and no holes: nothing was formed here

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
    box = Bnd_Box()
    for node in graph.nodes:
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
            if sideways.Magnitude() > skin.reach:
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
    cylinders one gauge apart -- the inside and outside of the same fold --
    with two flats among their neighbours that are at an angle to each other.
    A fillet has one cylinder, so it never matches.
    """
    bends: list[tuple] = []
    for node in graph.nodes:
        if node.surface_type is not SurfaceType.CYLINDER:
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
        bends.append((node.cyl_cone_axis, node.cyl_radius, flats))

    for index, (axis, radius, flats) in enumerate(bends):
        for other_axis, other_radius, other_flats in bends[index + 1 :]:
            if not axes_colinear(axis, other_axis):
                continue
            separation = abs(radius - other_radius)
            if not 0.5 * gauge <= separation <= 1.5 * gauge:
                continue
            joined = flats + other_flats
            for position, first in enumerate(joined):
                for second in joined[position + 1 :]:
                    if abs(first.Dot(second)) < _BEND_NON_PARALLEL_DOT:
                        return True
    return False


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
        return _confirm_sheet(base, features, graph)
    if base.type is not PartProcessType.TURNED:
        return base

    for feature in features:
        if feature.type in _PRISMATIC_TYPES:
            base.type = PartProcessType.MILL_TURN
            return base
        if _milled_protrusion(feature, base, graph):
            base.type = PartProcessType.MILL_TURN
            return base
    return base


def _milled_protrusion(feature, base: PartProcessResult, graph) -> bool:
    """Whether a boss or step could not have come off a lathe.

    A revolved boss has annular flats and coaxial curves. A chordal shelf, a
    rectangular pad, an off-axis boss -- any of those needs a mill. Blend
    faces carry no signal either way and are skipped.
    """
    if graph is None or base.axis_of_revolution is None:
        return False
    if feature.type not in (FeatureType.BOSS, FeatureType.STEP):
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


def _confirm_sheet(base: PartProcessResult, features, graph) -> PartProcessResult:
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
    gauge = base.sheet_thickness_mm
    for feature in features:
        if feature.type not in _SOLID_STOCK_TYPES:
            continue

        # A bore that stops in open air and is no deeper than a couple of
        # gauges is a punched hole, not a drilling. Sheet is too thin to
        # hold a real blind hole anyway.
        if (
            feature.type == FeatureType.BLIND_HOLE
            and feature.param("terminates_in_cavity", False)
            and (feature.number("depth_mm") or 1e9) <= 2.0 * gauge
        ):
            continue

        if feature.type in _FORMABLE_TYPES and _carries_skin(feature, graph, gauge):
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


def _carries_skin(feature, graph, gauge: float) -> bool:
    """Whether any face of a feature has material exactly one gauge behind it.

    Any face is enough, not the largest: a step whose shear wall outweighs
    its skinned terrace would otherwise be missed.
    """
    if graph is None or gauge <= 0.0:
        return False
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
        if has_constant_gauge_skin(graph, node, gauge):
            return True
    return False


def has_constant_gauge_skin(
    graph: AttributedAdjacencyGraph, node: AagNode, gauge: float
) -> bool:
    """Whether a face has material exactly one gauge behind it.

    That is what makes a feature formed rather than cut. Drawn sheet keeps
    its thickness, so the plateau of an emboss has its own back face one
    gauge behind it, and a dimple has a matching bulge on the far side. A
    milled pad sits on solid stock with no partner at any distance.

    Each surface asks the question in its own terms, because "one gauge
    behind" means something different for each. A flat face steps inward
    along its normal. A sphere and a cylinder are concentric with their far
    skin, so the question is whether a second one exists a gauge away -- and
    those two cases matter: a drawn dimple is a sphere and a swept louver
    hood is a cylinder, and without them every formed part vetoes to milled.
    """
    if gauge <= 0.0:
        return False

    if node.surface_type is SurfaceType.SPHERE:
        return _concentric_partner(
            graph, node, gauge, SurfaceType.SPHERE, node.sphere_center, node.sphere_radius
        )

    if node.surface_type is SurfaceType.CYLINDER and node.cyl_cone_axis is not None:
        for other in graph.nodes:
            if other.face_id == node.face_id:
                continue
            if other.surface_type is not SurfaceType.CYLINDER:
                continue
            if other.cyl_cone_axis is None:
                continue
            if not axes_colinear(other.cyl_cone_axis, node.cyl_cone_axis):
                continue
            if _about_one_gauge(abs(other.cyl_radius - node.cyl_radius), gauge):
                return True
        return False

    normal = node.outward_normal
    if normal is None or node.centroid is None:
        return False

    # One gauge in from the face, which is where the far skin should be.
    behind = node.centroid.Translated(gp_Vec(normal).Multiplied(-gauge))

    for other in graph.nodes:
        if other.face_id == node.face_id:
            continue
        other_normal = other.outward_normal
        if other_normal is None or other.bbox.IsVoid():
            continue
        if normal.Dot(other_normal) > _ANTI_PARALLEL_DOT:
            continue
        # Reached by position, not by comparing centroids: the back of an
        # emboss is often not a face of its own but part of the large bottom
        # skin, whose centre is somewhere else entirely.
        if not other.bbox.IsOut(behind):
            return True
        if _distance_to_box(other.bbox, behind) <= 0.35 * gauge:
            return True
    return False


def _concentric_partner(
    graph: AttributedAdjacencyGraph,
    node: AagNode,
    gauge: float,
    kind: SurfaceType,
    centre,
    radius: float,
) -> bool:
    """Whether a concentric surface sits one gauge away."""
    if centre is None or radius <= 0.0:
        return False
    for other in graph.nodes:
        if other.face_id == node.face_id or other.surface_type is not kind:
            continue
        if other.sphere_center is None:
            continue
        if other.sphere_center.Distance(centre) > 0.5:
            continue
        if _about_one_gauge(abs(other.sphere_radius - radius), gauge):
            return True
    return False


def _about_one_gauge(separation: float, gauge: float) -> bool:
    return 0.7 * gauge <= separation <= 1.3 * gauge


def _distance_to_box(box, point) -> float:
    """How far a point lies outside an axis-aligned box."""
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    dx = max(xmin - point.X(), 0.0, point.X() - xmax)
    dy = max(ymin - point.Y(), 0.0, point.Y() - ymax)
    dz = max(zmin - point.Z(), 0.0, point.Z() - zmax)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


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
