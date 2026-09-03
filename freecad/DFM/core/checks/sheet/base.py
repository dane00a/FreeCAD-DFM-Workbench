# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Shared plumbing for the sheet-metal checks.

A formed part is judged against the press and the brake rather than against a
cutter, so this family is the mirror image of the machining one: every rule
here stands down unless the part classified as sheet metal and a gauge was
actually measured off it. Without a gauge there is no threshold, because every
number in sheet-metal practice is quoted as a multiple of the material.

Most of the geometry helpers exist because a folded part does not arrive in
the shape the maths wants. A Boolean seam splits one physical panel into
several coplanar faces, so a panel has to be flooded before it can be
measured. A relief cut leaves no feature behind for a recognizer to find, so
its presence is read from the material itself rather than from a witness. Both
of those are the difference between a rule that works on real CAD and one that
works on the fixtures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec
from OCP.TopAbs import TopAbs_IN

from ...machining.aag import AagNode, SurfaceType
from ...machining.features import HOLE_TYPES, FeatureInstance, FeatureType
from ...machining.process_classifier import PartProcessType
from ..machining.base import MachiningCheck


# =============================================================================
# Gauge and imperial constants
# =============================================================================
#
# One home for every gauge and inch figure the sheet family quotes, so that no
# threshold below is a bare literal whose provenance has to be guessed. The
# steel figures are Manufacturers' Standard Gauge, which is the table a
# fabricator's own bend-deduction sheet speaks.
#
# Aluminium deliberately has no gauge table here. Shops order it by decimal
# thickness rather than by gauge number, so a converted table would answer in
# units nobody on the floor uses.

IN_TO_MM = 25.4

GA11_STEEL_MM = 3.038  # 0.1196"
GA14_STEEL_MM = 1.897  # 0.0747"
GA30_STEEL_MM = 0.305  # 0.0120"

QUARTER_IN_MM = 6.35  # 1/4"
EIGHTH_IN_MM = 3.175  # 0.125" = 1/8"


# =============================================================================
# Thresholds
# =============================================================================
#
# Every number the sheet rules compare against, named and commented. These are
# read through :func:`threshold`, which prefers the identically named field on
# `RuleThresholds` the moment the shop configuration grows one -- so adding the
# fields later changes nothing here beyond where the value comes from.

# Inside bend radius, as a multiple of the gauge. Below one gauge the outer
# fibre stretches past what the material takes; below half a gauge no standard
# press-brake punch will form it at all.
SHEET_BEND_RADIUS_WARN_FACTOR = 1.0
SHEET_BEND_RADIUS_ERROR_FACTOR = 0.5

# The flange the die has to grip, measured from the bend tangent line.
SHEET_MIN_FLANGE_FACTOR = 4.0

# Clearance from a hole edge to the bend tangent line, past which the hole no
# longer sits in the metal the fold deforms.
SHEET_HOLE_BEND_CLEARANCE_FACTOR = 2.5

# The punching minimum: a punch narrower than the stock it goes through snaps.
SHEET_MIN_HOLE_FACTOR = 1.0

# Web left between two punched holes.
SHEET_HOLE_PITCH_FACTOR = 2.0

# Countersink depth against the gauge, and the absolute floor of land that has
# to survive under the cone. The ratio guards manufacturability; the absolute
# floor guards fatigue, which does not care about ratios.
SHEET_MAX_COUNTERSINK_DEPTH_FACTOR = 0.6
SHEET_MIN_COUNTERSINK_LAND_MM = 0.3

# Press-brake angle ceiling, gauge-banded: interpolated between the two
# anchors and clamped outside them. Eleven gauge tops out around 125 degrees;
# fourteen gauge and thinner folds right back to 180, which makes the rule
# structurally silent on thin stock -- as intended.
SHEET_MAX_BEND_DEG_AT_GA11 = 125.0
SHEET_MAX_BEND_DEG_AT_GA14 = 180.0

# The sheet gauge range. The floor asks whether this is sheet at all; the
# ceiling is material-dependent, and steel is both the tighter answer and the
# fallback when nothing was declared.
SHEET_MIN_THICKNESS_MM = GA30_STEEL_MM
SHEET_MAX_THICKNESS_STEEL_MM = EIGHTH_IN_MM
SHEET_MAX_THICKNESS_ALU_MM = 6.0

# Outline tabs and notches.
SHEET_MIN_TAB_WIDTH_MM = 3.2
SHEET_TAB_WIDTH_FACTOR = 2.0
SHEET_TAB_MAX_ASPECT = 5.0
SHEET_MIN_NOTCH_FACTOR = 1.0
SHEET_NOTCH_MAX_DEPTH_RATIO = 10.0

# Hem return above 14 gauge; at and below it the requirement is a flat quarter
# inch instead, because a short lip still needs a real length of material for
# the hemming die to catch and that length stops scaling down with the gauge.
SHEET_HEM_MIN_RETURN_FACTOR = 4.0

# Formed features.
SHEET_EMBOSS_MAX_DEPTH_FACTOR = 3.0
# A quarter inch at 14 gauge, scaled -- a data point rather than an absolute
# ceiling, so it moves with the material.
SHEET_LOUVER_MAX_HEIGHT_FACTOR = QUARTER_IN_MM / GA14_STEEL_MM
SHEET_FORMED_MIN_PITCH_FACTOR = 2.0
SHEET_FORMED_BEND_CLEARANCE_FACTOR = 3.0

# Modelling slack on every sheet dimension compared to a threshold. A STEP
# round trip delivers 1.499 for a modelled 1.5, and once thresholds sit at one
# gauge exact-equality cases are the norm rather than the exception: a 2.0 mm
# notch in 2.0 mm material lands precisely on its limit.
SHEET_DIMENSION_EPS_MM = 0.02

# Heavy-stock gate for the diagonal-bend caution. Gauge numbering runs the
# other way from thickness, so "14 gauge and heavier" means this figure and
# above. There is no metallurgical cutoff here -- it is one shop's brake,
# tooling and tolerance for distortion, which is why the finding reads as a
# caution to discuss rather than a verdict.
SHEET_DIAGONAL_BEND_MIN_THICKNESS_MM = GA14_STEEL_MM

# Slack on "the bend is longer than the body", deliberately far coarser than
# the dimension epsilon. That one absorbs STEP round-trip error in hundredths
# of a millimetre; this one absorbs the gap between a trimmed cylinder's span
# and a bounding box built from an entirely different set of faces. Almost
# every sheet part is a prism whose bend spans the whole blank, so the two
# figures are equal by construction and float noise alone would otherwise
# decide the verdict.
SHEET_BEND_OVER_LENGTH_EPS_MM = 0.5

# The material families the range check has a number for. Anything else, the
# empty string included, reads as steel. This is a declaration test and never
# a guess: no face in the graph carries a signal for alloy.
FAMILY_STEEL = "steel"
FAMILY_ALUMINIUM = "aluminium"


# A gauge below this is not a measurement, it is noise.
_MIN_GAUGE_MM = 1e-6

# Two planes are the same physical plane when their outward normals agree this
# closely and their centroids sit within this offset of one another.
_COPLANAR_MIN_DOT = 0.999
_COPLANAR_OFFSET_MM = 0.1

# A face whose second-smallest bounding-box extent is about one gauge is a
# sheared edge strip rather than a panel.
_GAUGE_THIN_FACTOR = 1.3


def threshold(context, name: str, default: float) -> float:
    """A configured sheet threshold, or the family default.

    The shop configuration owns these numbers wherever it carries them. Until
    it does, the module constant above stands in, and a rule reads the same
    way either way.
    """
    return float(getattr(context.config.thresholds, name, default))


# =============================================================================
# The base class
# =============================================================================


class SheetCheck(MachiningCheck):
    """Base for a rule that only speaks about formed sheet parts.

    The gate is the whole point. Sheet thresholds are multiples of the gauge,
    so a rule with no gauge has no threshold, and a rule applied to a part that
    was never near a brake is reporting on geometry it has misread.
    """

    def applies_to(self, context) -> bool:
        if context.process_type is not PartProcessType.SHEET_METAL:
            return False
        return self.gauge(context) > _MIN_GAUGE_MM

    @staticmethod
    def gauge(context) -> float:
        """The measured sheet thickness, in millimetres."""
        return float(getattr(context.part_process, "sheet_thickness_mm", 0.0) or 0.0)


# =============================================================================
# Bends
# =============================================================================


@dataclass(frozen=True)
class BendGeom:
    """A bend reduced to the line the brake folded it about.

    Every bend rule works from the tangent line rather than from the cylinder
    faces: the flange length, the hole clearance and the relief position are
    all measured from where the fold starts, not from where the metal curves.
    """

    feature: FeatureInstance
    origin: gp_Pnt
    axis: gp_Dir
    inner_radius: float
    panel_a: Optional[int]
    panel_b: Optional[int]

    @property
    def panels(self) -> tuple[Optional[int], Optional[int]]:
        return (self.panel_a, self.panel_b)

    def point_at(self, distance: float) -> gp_Pnt:
        """A point on the fold line, that far along the axis from the origin."""
        return gp_Pnt(
            self.origin.X() + self.axis.X() * distance,
            self.origin.Y() + self.axis.Y() * distance,
            self.origin.Z() + self.axis.Z() * distance,
        )


def bend_geom(feature: FeatureInstance) -> Optional[BendGeom]:
    """The fold line of a bend feature, or None when it carries no axis."""
    if feature.type != FeatureType.BEND:
        return None
    axis = feature.direction("axis")
    if axis is None:
        return None
    raw = feature.param("axis_origin")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return None
    try:
        origin = gp_Pnt(float(raw[0]), float(raw[1]), float(raw[2]))
    except (TypeError, ValueError):
        return None
    return BendGeom(
        feature=feature,
        origin=origin,
        axis=axis,
        inner_radius=feature.number("inner_radius_mm", 0.0) or 0.0,
        panel_a=_face_id(feature.param("panel_a")),
        panel_b=_face_id(feature.param("panel_b")),
    )


def bends_of(context) -> list[BendGeom]:
    """Every bend on the part, in a stable order."""
    found = []
    for feature in sorted_features(context, FeatureType.BEND):
        bend = bend_geom(feature)
        if bend is not None:
            found.append(bend)
    return found


def sorted_features(context, *types: str) -> list[FeatureInstance]:
    """Features of the given types, ordered so the same part reads the same."""
    wanted = set(types)
    return sorted(
        (f for f in context.recognition.features if not wanted or f.type in wanted),
        key=lambda f: f.instance_id,
    )


def panel_away_dir(context, bend: BendGeom, panel_id: Optional[int]) -> Optional[gp_Dir]:
    """The in-plane direction running from the fold line out into a panel.

    The panel's own plane and the fold axis fix the line; which way along it is
    settled by the panel's centroid, since a flange only ever extends one way
    from the bend it hangs off.
    """
    node = _node(context, panel_id)
    if node is None:
        return None
    normal = node.outward_normal
    if normal is None:
        return None
    direction = gp_Vec(normal).Crossed(gp_Vec(bend.axis))
    if direction.Magnitude() < 1e-9:
        return None
    direction.Normalize()
    if gp_Vec(bend.origin, node.centroid).Dot(direction) < 0.0:
        direction.Reverse()
    return gp_Dir(direction)


def bend_axial_span(context, bend: BendGeom) -> Optional[tuple[float, float]]:
    """How far the fold runs along its own axis, from the axis origin.

    Read off the inside face of the pair, which is the one whose extent is the
    bend rather than the bend plus a gauge of wrap.
    """
    faces = bend.feature.faces
    if not faces:
        return None
    node = _node(context, faces[0])
    if node is None:
        return None
    return _projected_span(node, bend.origin, bend.axis)


# =============================================================================
# Panels
# =============================================================================


def patch_extents_along(
    context, panel_id: Optional[int], origin: gp_Pnt, direction: gp_Dir
) -> Optional[tuple[float, float]]:
    """How far the coplanar patch containing a panel reaches along a direction.

    The flood is not a refinement, it is the measurement. A Boolean fuse seam
    splits one flat sheet face into fragments -- the base plate and the shelf a
    flange stands on leave a seam edge along the weld line -- and the physical
    panel is the whole coplanar patch, not whichever fragment the bend happens
    to touch. Measured fragment by fragment, a full-length flange reads as a
    stub.
    """
    seed = _node(context, panel_id)
    if seed is None or seed.surface_type is not SurfaceType.PLANE:
        return None
    seed_normal = seed.outward_normal
    if seed_normal is None:
        return None

    low = math.inf
    high = -math.inf
    visited = {seed.face_id}
    queue = [seed]
    while queue:
        current = queue.pop()
        span = _projected_span(current, origin, direction)
        if span is not None:
            low = min(low, span[0])
            high = max(high, span[1])
        for neighbour_id in sorted(context.graph.neighbors_of(current.face_id)):
            if neighbour_id in visited:
                continue
            neighbour = _node(context, neighbour_id)
            if neighbour is None or neighbour.surface_type is not SurfaceType.PLANE:
                continue
            normal = neighbour.outward_normal
            if normal is None or normal.Dot(seed_normal) < _COPLANAR_MIN_DOT:
                continue
            offset = gp_Vec(seed.centroid, neighbour.centroid).Dot(gp_Vec(seed_normal))
            if abs(offset) > _COPLANAR_OFFSET_MM:
                continue
            visited.add(neighbour_id)
            queue.append(neighbour)

    if high <= low:
        return None
    return (low, high)


def panels_coplanar(context, first: Optional[int], second: Optional[int]) -> bool:
    """Whether two panel faces are fragments of one physical plane.

    Comparing face ids undercounts for the same reason the patch flood exists:
    a seam gives one flat several ids, and two bends off the same base plate
    then look as though they share nothing.
    """
    if first is None or second is None:
        return False
    if first == second:
        return True
    a = _node(context, first)
    b = _node(context, second)
    if a is None or b is None:
        return False
    if a.surface_type is not SurfaceType.PLANE or b.surface_type is not SurfaceType.PLANE:
        return False
    a_normal = a.outward_normal
    b_normal = b.outward_normal
    if a_normal is None or b_normal is None:
        return False
    if a_normal.Dot(b_normal) < _COPLANAR_MIN_DOT:
        return False
    return abs(gp_Vec(a.centroid, b.centroid).Dot(gp_Vec(a_normal))) <= _COPLANAR_OFFSET_MM


def gauge_thin_face(node: AagNode, gauge: float) -> bool:
    """Whether a face is a sheared edge strip rather than a panel.

    A strip is one gauge across its second-smallest dimension, which is what
    the cut edge of a blank always is. Panels are many gauges across, and folds
    join panels.
    """
    dims = sorted(node.bbox_dims())
    return dims[1] <= gauge * _GAUGE_THIN_FACTOR


# =============================================================================
# Probing the material
# =============================================================================


class SolidProbe:
    """Asks the solid whether there is metal at a point.

    Relief cuts are what this exists for. A relief leaves no feature behind for
    a recognizer to claim -- a scallop, a square notch, an open corner cutback
    and a V are four different shapes for the same intent -- so testing for a
    witness feature misses most real ones and fires on parts that are perfectly
    relieved. Asking the material instead is right for all four.
    """

    def __init__(self, shape):
        self._classifier = BRepClass3d_SolidClassifier(shape) if shape is not None else None

    def inside(self, point: gp_Pnt) -> bool:
        if self._classifier is None:
            return False
        self._classifier.Perform(point, 1e-4)
        return self._classifier.State() == TopAbs_IN

    def material_at_mid_gauge(self, panel: AagNode, probe_base: gp_Pnt, gauge: float) -> bool:
        """Whether metal continues at a point, tested at the panel's mid-gauge.

        The probe has to sit inside the thickness rather than on the surface,
        or the classifier is answering about a point on the boundary and the
        verdict turns on tolerance.
        """
        normal = panel.outward_normal
        if normal is None:
            return False
        offset = gp_Vec(panel.centroid, probe_base).Dot(gp_Vec(normal))
        probe = gp_Pnt(probe_base.X(), probe_base.Y(), probe_base.Z())
        probe.Translate(gp_Vec(normal).Multiplied(-(offset + 0.5 * gauge)))
        return self.inside(probe)

    def material_in_fold_strip(
        self, context, bend: BendGeom, panel_id: Optional[int], beyond: gp_Pnt, gauge: float
    ) -> bool:
        """Whether metal survives anywhere across the fold strip at a point.

        The strip runs from the panel's own tangent line back across the bend
        footprint to the far tangent, and it is sampled rather than probed once
        because a partial relief -- a round scallop clearing only the middle of
        the strip -- leaves flat blank hugging one side of the fold. That metal
        still tears when the neighbouring span folds, so a single probe on the
        tangent line reports a relief that is not there.
        """
        panel = _node(context, panel_id)
        if panel is None:
            return False
        direction = panel_away_dir(context, bend, panel_id)
        if direction is None:
            return self.material_at_mid_gauge(panel, beyond, gauge)
        span = bend.inner_radius + gauge
        for step in (0.0, 1.0 / 3.0, 2.0 / 3.0, 0.9):
            probe = gp_Pnt(beyond.X(), beyond.Y(), beyond.Z())
            probe.Translate(gp_Vec(direction).Multiplied(-span * step))
            if self.material_at_mid_gauge(panel, probe, gauge):
                return True
        return False


# =============================================================================
# Holes
# =============================================================================


def is_hole_type(feature_type: str) -> bool:
    """Whether a feature is a hole through the gauge.

    Everything a sheet part carries goes through: there is no blind depth to
    speak of in material this thin, so the whole hole family counts.
    """
    return feature_type in HOLE_TYPES


def hole_cyl_node(context, feature: FeatureInstance) -> Optional[AagNode]:
    """The bore cylinder of a hole: the largest cylindrical face it owns."""
    best: Optional[AagNode] = None
    for face_id in sorted(feature.faces):
        node = _node(context, face_id)
        if node is None or node.surface_type is not SurfaceType.CYLINDER:
            continue
        if best is None or node.area > best.area:
            best = node
    return best


def hole_in_panel(context, hole: FeatureInstance, panel_id: Optional[int]) -> bool:
    """Whether a hole pierces a given panel, by adjacency to its face."""
    if panel_id is None:
        return False
    for face_id in sorted(hole.faces):
        if panel_id in context.graph.neighbors_of(face_id):
            return True
    return False


# =============================================================================
# Gauge-banded figures
# =============================================================================


def max_std_bend_deg(gauge: float, context) -> float:
    """The press-brake angle ceiling for a measured gauge.

    Linear between the 11-gauge and 14-gauge anchors and clamped outside them.
    Since the recognizer's angle is bounded by 180 degrees and the thin anchor
    sits there, the rule is silent on 14 gauge and thinner -- which is the
    shop's own statement rather than an accident of the arithmetic.
    """
    thin = GA14_STEEL_MM
    heavy = GA11_STEEL_MM
    at_thin = threshold(context, "sheet_max_bend_deg_at_ga14", SHEET_MAX_BEND_DEG_AT_GA14)
    at_heavy = threshold(context, "sheet_max_bend_deg_at_ga11", SHEET_MAX_BEND_DEG_AT_GA11)
    if gauge <= thin:
        return at_thin
    if gauge >= heavy:
        return at_heavy
    fraction = (gauge - thin) / (heavy - thin)
    return at_thin + fraction * (at_heavy - at_thin)


def hem_min_return_mm(gauge: float, context) -> float:
    """The shortest hem return the hemming die can catch, for this gauge.

    Piecewise, and the step upward crossing 14 gauge is deliberate: below it a
    flat quarter inch, above it a multiple of the material. A thin lip still
    needs a real length of metal in the die, and that length stops scaling down
    with the gauge.
    """
    if gauge <= GA14_STEEL_MM:
        return QUARTER_IN_MM
    return gauge * threshold(context, "sheet_hem_min_return_factor", SHEET_HEM_MIN_RETURN_FACTOR)


def material_is_aluminium(family: str) -> bool:
    return (family or "").strip().lower() == FAMILY_ALUMINIUM


def material_declared(family: str) -> bool:
    """Whether a family the range check has a number for was actually named."""
    return (family or "").strip().lower() in (FAMILY_STEEL, FAMILY_ALUMINIUM)


def max_thickness_mm(family: str, context) -> float:
    """The gauge ceiling for a declared material family.

    Steel is both the tighter ceiling and the fallback, so a part that arrives
    with nothing declared is judged strictly rather than leniently.
    """
    if material_is_aluminium(family):
        return threshold(context, "sheet_max_thickness_alu_mm", SHEET_MAX_THICKNESS_ALU_MM)
    return threshold(context, "sheet_max_thickness_steel_mm", SHEET_MAX_THICKNESS_STEEL_MM)


# =============================================================================
# Small shared geometry
# =============================================================================


def bbox_corners(node: AagNode) -> list[gp_Pnt]:
    """The eight corners of a face's bounding box."""
    if node.bbox.IsVoid():
        return []
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    return [
        gp_Pnt(x, y, z)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
        for z in (zmin, zmax)
    ]


def box_of_faces(context, faces: Sequence[int]):
    """The union bounding box of a feature's faces, or None when it has none."""
    from OCP.Bnd import Bnd_Box

    box = Bnd_Box()
    found = False
    for face_id in sorted(faces):
        node = _node(context, face_id)
        if node is None or node.bbox.IsVoid():
            continue
        box.Add(node.bbox)
        found = True
    return box if found and not box.IsVoid() else None


def distance_to_box(box, point: gp_Pnt) -> float:
    """How far a point lies outside an axis-aligned box; zero when inside."""
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    dx = max(xmin - point.X(), 0.0, point.X() - xmax)
    dy = max(ymin - point.Y(), 0.0, point.Y() - ymax)
    dz = max(zmin - point.Z(), 0.0, point.Z() - zmax)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _projected_span(
    node: AagNode, origin: gp_Pnt, direction: gp_Dir
) -> Optional[tuple[float, float]]:
    corners = bbox_corners(node)
    if not corners:
        return None
    values = [gp_Vec(origin, corner).Dot(gp_Vec(direction)) for corner in corners]
    return (min(values), max(values))


def _node(context, face_id: Optional[int]) -> Optional[AagNode]:
    if face_id is None:
        return None
    try:
        key = int(face_id)
    except (TypeError, ValueError):
        return None
    if not context.graph.has_node(key):
        return None
    return context.graph.node(key)


def _face_id(raw) -> Optional[int]:
    """A stored panel reference as a face id, or None when absent."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
