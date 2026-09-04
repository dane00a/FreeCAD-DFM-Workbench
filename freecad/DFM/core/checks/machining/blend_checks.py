# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about blends: the radii and bevels where two faces meet.

An inside corner radius is not a styling choice, it is a tool order. A
rotating cutter leaves its own radius behind, so the plan-view corner radius
on the drawing *is* half the diameter of the cutter that has to fit there --
and if no cutter in the library is that small, the corner is not a milling
job at all.

The other two rules here are notes rather than defects. A chamfer taken at
something other than 45 degrees is real geometry that a standard chamfer mill
cannot cut, and a revolved knife edge is a sealing feature that has to survive
deburring rather than be removed by it. Both are usually deliberate, and both
change what happens on the floor, so both get said out loud.
"""

from __future__ import annotations

import math

from OCP.gp import gp_Vec

from ...machining.aag import Concavity, SurfaceType
from ...machining.features import CAVITY_TYPES, FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# A corner radius within this of the smallest the library can cut counts as
# achievable. Below it, no cutter reaches the corner at all.
_FEASIBILITY_TOL_MM = 0.01

# How close a design radius has to sit to a stocked cutter's half-diameter to
# count as that size. Deliberately a micron: loosen it and a 1.2 mm corner
# matches a 1.25 mm cutter, which is the whole gap between adjacent standard
# sizes, and the rule passes everything.
_STANDARD_MATCH_TOL_MM = 0.001

# A plan-view corner is capped by a face standing square across it -- the
# cavity floor the cutter bottoms out on, or the surface it opens onto. This
# is what separates it from a floor-to-wall blend, which is cut with a
# corner-radius tool and has nothing to do with cutter diameter.
_CORNER_CAP_ALIGNMENT = 0.9

# Two seal cones share an axis: near-parallel directions, and the second axis
# line standing this close to the first.
_SEAL_AXIS_ALIGNMENT = 0.99
_SEAL_AXIS_OFFSET_MM = 0.5

# Material wedge at a sealing edge. Past this the edge is an ordinary corner
# between two tapers rather than a knife.
_SEAL_MAX_WEDGE_DEG = 100.0


# =============================================================================
# Inside corners
# =============================================================================


def _is_inside_corner(context, fillet) -> bool:
    """Whether this fillet is a plan-view corner rather than a floor blend.

    Both look identical locally: a cylinder running tangent into two faces,
    with its axis square to both their normals -- that is what tangency means,
    and it holds either way round. What tells them apart is what caps the
    ends. A corner radius runs the depth of the cavity and butts into the
    floor, whose normal points along the fillet axis; a floor blend runs along
    the wall and mitres into the blends beside it, meeting nothing square to
    its axis at all.

    The distinction matters because the two are cut by different tools. A
    corner radius is set by the cutter's diameter and cannot be smaller than
    half of it. A floor blend is set by the cutter's corner radius, which a
    bull-nose or ball mill supplies in far smaller sizes.
    """
    graph = context.graph
    for face_id in sorted(fillet.faces):
        if not graph.has_node(face_id):
            continue
        node = graph.node(face_id)
        # Only a cylinder turns a plan-view corner. A torus wraps around one
        # and is a floor blend by construction.
        if node.surface_type is not SurfaceType.CYLINDER:
            continue
        # An external radius is milled by the tool's flank, not its corner,
        # so no cutter size limits it.
        if not node.is_internal or node.cyl_cone_axis is None:
            continue

        axis = node.cyl_cone_axis.Direction()
        for edge in graph.edges_of(face_id):
            if edge.is_tangent:
                continue  # that is the wall the corner blends into
            other_id = edge.other_face(face_id)
            if not graph.has_node(other_id):
                continue
            normal = graph.node(other_id).outward_normal
            if normal is None:
                continue
            if abs(normal.Dot(axis)) >= _CORNER_CAP_ALIGNMENT:
                return True
    return False


def _owning_cavity(context, fillet):
    """The pocket or slot whose corner this fillet turns, if any.

    The blend recognizer claims only the blend faces themselves, so a corner
    radius never appears in its cavity's face list. The walls either side of
    it do, and those are one hop away.
    """
    neighbours: set[int] = set()
    for face_id in fillet.faces:
        neighbours.update(context.graph.neighbors_of(face_id))
    for cavity in context.recognition.of_type(*CAVITY_TYPES):
        if neighbours.intersection(cavity.faces):
            return cavity
    return None




def _edm_process(cavity) -> str:
    """Which EDM a corner would need if the radius has to stand.

    A profile that passes through the part can be cut with wire; a blind
    cavity has to be sunk with an electrode shaped like the corner.
    """
    if cavity is None:
        return "sinker EDM"
    through = (
        cavity.type == FeatureType.THROUGH_CAVITY
        or bool(cavity.param("is_through"))
        or bool(cavity.param("is_open"))
    )
    return "wire EDM" if through else "sinker EDM"


def _cavities_with_corners(context):
    """The cavities whose corner radius is a fact about them.

    Read off the cavity rather than off the blend faces. A pocket has one
    corner radius and four corners; the recognizer measures it once and
    records it on the pocket, which is where a machinist would look for it
    and where the reference engine reads it from. Iterating blends instead
    said the same thing once per corner, and stopped working entirely the
    moment the pocket started owning its corners.

    An open slot has no closed corner to round, so it is skipped -- the
    cutter enters one end and leaves the other without turning anything.
    """
    for cavity in context.recognition.of_type(
        FeatureType.POCKET, FeatureType.SLOT, FeatureType.THROUGH_CAVITY
    ):
        if cavity.type == FeatureType.SLOT and cavity.param("is_open"):
            continue
        yield cavity


def _corner_phrase(cavity) -> str:
    """How to name what the finding is about."""
    return "the corners of this %s" % _readable_type(cavity.type)


def _readable_type(feature_type: str) -> str:
    return str(feature_type).replace("_", " ").lower()


@register_check(Rulebook.CUTTER_RADIUS_INFEASIBLE)
class CutterRadiusInfeasibleCheck(MachiningCheck):
    """An inside corner tighter than the smallest cutter in the shop.

    The limit is not a material property. It is half the smallest end mill
    diameter the library carries, and it moves when the shop's tooling does.
    """

    @property
    def name(self) -> str:
        return "Cutter Radius Infeasible Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        achievable = context.config.smallest_internal_corner_radius()
        if achievable is None:
            return []  # no end mills configured: nothing to judge against

        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = achievable

        results: list[CheckResult] = []
        for cavity in _cavities_with_corners(context):
            radius = cavity.number("corner_radius_mm") or 0.0
            # A square corner is the cavity rules' business, not this one:
            # there is no radius here to compare against a cutter.
            if radius <= 0.0:
                continue
            if radius >= limit - _FEASIBILITY_TOL_MM:
                continue

            severity = Severity.ERROR
            process = _edm_process(cavity)
            subject = _corner_phrase(cavity)
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"R{radius:.2f} mm corner",
                    self.render(
                        feedback,
                        severity,
                        radius,
                        limit,
                        limit,
                        "mm",
                        f"{subject[0].upper()}{subject[1:]} is R{radius:.2f} mm. "
                        f"The smallest end mill in the library is "
                        f"{limit * 2.0:.2f} mm across, and a cutter leaves its own "
                        f"radius behind in a corner, so R{limit:.2f} is the "
                        "tightest corner the shop can mill. As drawn this needs "
                        f"{process} rather than milling. Opening it to R{limit:.2f} "
                        "or more puts it back on the machine.",
                    ),
                    faces=cavity.faces,
                    value=radius,
                    limit=limit,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.CUTTER_RADIUS_SUBOPTIMAL)
class CutterRadiusSuboptimalCheck(MachiningCheck):
    """An inside corner that fits no stocked cutter exactly.

    Not a defect. Any cutter that fits inside the corner can trace the arc by
    interpolating round it, so the corner gets made either way. What it costs
    is a separate contouring pass where a matching cutter would have ridden
    straight through, which is programming time and cycle time rather than a
    manufacturability problem.
    """

    @property
    def name(self) -> str:
        return "Cutter Radius Suboptimal Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        achievable = context.config.smallest_internal_corner_radius()
        if achievable is None:
            return []

        floor = self.safe_float(rule_config.limit)
        if floor is None:
            floor = achievable

        standard = context.config.standard_corner_radii()
        if not standard:
            return []

        results: list[CheckResult] = []
        for cavity in _cavities_with_corners(context):
            radius = cavity.number("corner_radius_mm") or 0.0
            if radius <= 0.0:
                continue
            # Below the floor the corner cannot be milled at all, and the
            # infeasibility rule owns it. Saying "round it up a little" about
            # a corner that needs EDM would be worse than saying nothing.
            if radius < floor - _FEASIBILITY_TOL_MM:
                continue
            if any(abs(size - radius) <= _STANDARD_MATCH_TOL_MM for size in standard):
                continue
            subject = _corner_phrase(cavity)

            below = max((s for s in standard if s < radius), default=None)
            above = min((s for s in standard if s > radius), default=None)
            # The biggest cutter that still fits is the one a programmer
            # reaches for: it interpolates the corner in the fewest passes.
            fitting = max(
                (s for s in standard if s <= radius + _STANDARD_MATCH_TOL_MM),
                default=None,
            )
            if below is None and above is None:
                continue

            if below is not None and above is not None:
                suggestion = f"R{below:.2f} or R{above:.2f}"
            elif above is not None:
                suggestion = f"R{above:.2f}"
            else:
                suggestion = f"R{below:.2f}"

            severity = Severity.INFO
            fits = (
                f"the largest cutter that fits it is {fitting * 2.0:.2f} mm across, "
                "and it can trace the arc by interpolating round it"
                if fitting is not None
                else "a cutter small enough to fit can trace the arc by "
                "interpolating round it"
            )
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"R{radius:.2f} mm corner",
                    self.render(
                        feedback,
                        severity,
                        radius,
                        floor,
                        floor,
                        "mm",
                        f"{subject[0].upper()}{subject[1:]} is R{radius:.2f} mm, "
                        "which is not half the diameter of any end mill in the "
                        f"library. It still gets cut -- {fits} -- but that is a "
                        "separate contouring pass "
                        "instead of a cutter riding straight through the corner. "
                        f"Rounding to {suggestion} would let a stocked cutter sweep "
                        "it in one go.",
                    ),
                    faces=cavity.faces,
                    value=radius,
                    limit=above if above is not None else radius,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


# =============================================================================
# Bevels and sealing edges
# =============================================================================


@register_check(Rulebook.CHAMFER_NONSTANDARD_ANGLE)
class ChamferNonstandardAngleCheck(MachiningCheck):
    """A bevel a standard chamfer mill cannot cut.

    Only the revolved chamfers carry a measured angle. A flat chamfer strip is
    reported by the distance it takes off the edge, which says nothing about
    the angle it was taken at, so those are not judged here.
    """

    @property
    def name(self) -> str:
        return "Chamfer Nonstandard Angle Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        standard = thresholds.chamfer_standard_angle_deg
        tolerance = thresholds.chamfer_angle_tol_deg

        severity = self.severity_from_rule_config(rule_config)
        if severity is Severity.ERROR:
            # A note about tooling, not a defect: these bevels are nearly
            # always deliberate sealing or lead-in geometry.
            severity = Severity.INFO

        results: list[CheckResult] = []
        for chamfer in context.recognition.of_type(FeatureType.CHAMFER):
            angle = chamfer.number("chamfer_angle_deg")
            if angle is None or angle < 0.0:
                continue
            if abs(angle - standard) <= tolerance:
                continue

            width = chamfer.number("width_mm") or 0.0
            size = f", {width:.2f} mm wide," if width > 0.0 else ""
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{angle:.0f} degree chamfer",
                    self.render(
                        feedback,
                        severity,
                        angle,
                        standard,
                        standard,
                        "deg",
                        f"This chamfer{size} is cut at {angle:.0f} degrees off the "
                        f"face rather than {standard:.0f}, so a standard chamfer "
                        "mill will not produce it. It needs a dedicated angle "
                        "cutter, or a single-point pass if the feature is turned. "
                        "That is usually deliberate on a sealing lip or a lead-in "
                        "bevel -- worth confirming the angle is required before it "
                        "buys a special tool.",
                    ),
                    faces=chamfer.faces,
                    value=angle,
                    limit=standard,
                    comparison="!=",
                    unit="deg",
                )
            )
        return results


@register_check(Rulebook.METAL_SEAL_WITNESS)
class MetalSealWitnessCheck(MachiningCheck):
    """A revolved knife edge, which seals by being sharp.

    Recognized positively rather than inferred: a full circle where two
    coaxial cones meet at a thin material wedge is the knife edge on a
    metal-seal flange and nothing else. Saying so is the point -- every other
    sharp edge on the part gets broken, and this one must not be.
    """

    @property
    def name(self) -> str:
        return "Metal Seal Witness Edge Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        witnesses = self._witness_edges(context)
        if not witnesses:
            return []

        severity = self.severity_from_rule_config(rule_config)
        if severity is Severity.ERROR:
            # Nothing is wrong with the part. The finding exists so the edge
            # survives the deburring bench.
            severity = Severity.INFO

        count = len(witnesses)
        faces = sorted({face_id for _, _, ids in witnesses for face_id in ids})
        diameter, wedge, _ = witnesses[0]
        plural = "" if count == 1 else "s"

        return [
            self.finding(
                rule,
                severity,
                f"{count} knife edge{plural}",
                self.render(
                    feedback,
                    severity,
                    float(count),
                    0.0,
                    0.0,
                    "",
                    f"This part carries {count} revolved knife edge{plural}: two "
                    f"conical faces meeting at a {wedge:.0f} degree included angle "
                    f"on a {diameter:.1f} mm circle. That edge is the seal, so it "
                    "has to stay sharp -- keep it out of the tumbler, do not break "
                    "it with the rest of the edges, and finish it with a "
                    "single-point pass. It also damages easily, so it wants "
                    "protecting in handling and packing.",
                ),
                faces=faces,
                value=float(count),
                comparison=">",
            )
        ]

    # -- geometry -----------------------------------------------------------

    @staticmethod
    def _witness_edges(context) -> list:
        """Every (diameter, wedge angle, faces) a sealing edge is made of.

        Sorted by face id so the sample quoted in the finding does not depend
        on the order the graph happened to be built in.
        """
        found = []
        for edge in context.graph.edges:
            if edge.concavity is not Concavity.CONVEX:
                continue
            # The edge has to close on itself: a seal runs all the way round.
            if edge.edge_curve_type != "circle":
                continue
            if not context.graph.has_node(edge.face_id_a):
                continue
            if not context.graph.has_node(edge.face_id_b):
                continue

            first = context.graph.node(edge.face_id_a)
            second = context.graph.node(edge.face_id_b)
            if first.surface_type is not SurfaceType.CONE:
                continue
            if second.surface_type is not SurfaceType.CONE:
                continue
            if first.cyl_cone_axis is None or second.cyl_cone_axis is None:
                continue
            if not _coaxial(first.cyl_cone_axis, second.cyl_cone_axis):
                continue

            # The material left at the edge. A wedge this thin has no flat on
            # it at all, which is exactly what a metal seal needs and exactly
            # what a deburring operation would destroy.
            wedge = math.degrees(edge.dihedral_angle)
            if wedge <= 0.0 or wedge >= _SEAL_MAX_WEDGE_DEG:
                continue

            diameter = _edge_diameter(edge, first)
            if diameter <= 0.0:
                continue
            found.append((diameter, wedge, (edge.face_id_a, edge.face_id_b)))

        return sorted(found, key=lambda item: item[2])


def _coaxial(first, second) -> bool:
    """Whether two cone axes describe the same line."""
    if abs(first.Direction().Dot(second.Direction())) < _SEAL_AXIS_ALIGNMENT:
        return False
    offset = gp_Vec(first.Location(), second.Location())
    return offset.Crossed(gp_Vec(first.Direction())).Magnitude() < _SEAL_AXIS_OFFSET_MM


def _edge_diameter(edge, node) -> float:
    """The circle's diameter, measured off the axis rather than the length.

    Edge lengths in the graph are polyline approximations, which understate a
    circle by a few percent. The seal diameter is a number a machinist will
    check against the drawing, so it is taken as twice the distance from the
    axis to a point on the edge.
    """
    if edge.midpoint is None or node.cyl_cone_axis is None:
        return 0.0
    axis = node.cyl_cone_axis
    offset = gp_Vec(axis.Location(), edge.midpoint)
    return 2.0 * offset.CrossMagnitude(gp_Vec(axis.Direction()))
