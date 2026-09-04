# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""More rules about holes: what size they are, where they start, where they end.

The rules in :mod:`hole_checks` ask whether a bore can be drilled at all.
These ask the questions that follow. Is it a size the shop stocks a drill for,
or does it have to be reamed to a number nobody grinds? Does the drill have a
flat face to start on? Does the bore run into something part way down? None of
these condemns a hole outright -- they are the ones that show up on a quote as
an extra operation, a special tool, or a second setup.

Two of them read the bore's mouths off the adjacency graph rather than off the
feature's face list, and the reason is worth stating once here. The reference
engine found a hole's entry faces by looking for planar neighbours that were
*not* members of the feature; this recognizer counts a bore's host faces as
part of the hole, so that test would find nothing. What separates a mouth from
a floor here is edge concavity: the rim of an opening is convex -- it is the
edge you deburr -- while a floor meets the wall concavely and a fillet band
meets it tangentially.
"""

from __future__ import annotations

import math
from typing import Optional

from OCP.gp import gp_Dir

from ...machining.aag import AagNode, Concavity, SurfaceType
from ...machining.context import MachiningContext
from ...machining.features import HOLE_TYPES, FeatureInstance, FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck
from .hole_checks import HoleIntersectingCheck, HoleWebThicknessCheck


# A tapped hole is drilled at its tap drill and finished by the tap, so the
# diameter carried on the feature is a thread size and never a drill size.
# Judging one against the drill catalogue would flag every thread on the part.
_SIZED_BORE_TYPES = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK,
)

# A shop working in metric stocks a full drill index in 0.1 mm steps, so any
# diameter landing on that grid is an off-the-shelf tool rather than a grind.
_METRIC_GRID_STEP_MM = 0.1
# Tight on purpose. Drills are exact sizes, and grid points only 0.1 mm apart
# would swallow every diameter on the part if this were opened up.
_METRIC_GRID_TOLERANCE_MM = 0.02

# Included angles a stock countersink comes ground to.
_STANDARD_COUNTERSINK_ANGLES_DEG = (60.0, 82.0, 90.0, 100.0, 110.0, 120.0)

# Two bores count as touching when their axes pass this much inside the sum of
# their radii. The same slack the intersecting-holes rule uses, so a crossing
# reported there is not reported a second time here.
_TOUCH_TOLERANCE_MM = 0.1

# The cavity families a bore can break into. No hole type appears here on
# purpose: a bore running into another bore is the intersecting-holes rule's
# business, and PARTIAL_BORE is a milled saddle rather than a drilled void.
_CAVITY_TYPES = (
    FeatureType.POCKET,
    FeatureType.SLOT,
    FeatureType.CHANNEL,
    FeatureType.THROUGH_CAVITY,
    FeatureType.SPHERICAL_POCKET,
    FeatureType.FLEXURE_SLIT,
    FeatureType.BROACHED_SLOT,
    FeatureType.V_GROOVE,
    FeatureType.UNDERCUT,
)


def _cylinder_nodes(context: MachiningContext, hole: FeatureInstance) -> list[AagNode]:
    """The bore walls of a hole, ascending by face id."""
    nodes: list[AagNode] = []
    for face_id in sorted(hole.faces):
        if not context.graph.has_node(face_id):
            continue
        node = context.graph.node(face_id)
        if node.surface_type is SurfaceType.CYLINDER:
            nodes.append(node)
    return nodes


def _bore_axis(context: MachiningContext, hole: FeatureInstance) -> Optional[gp_Dir]:
    for node in _cylinder_nodes(context, hole):
        if node.cyl_cone_axis is not None:
            return node.cyl_cone_axis.Direction()
    return None


def _rim_neighbours(context: MachiningContext, hole: FeatureInstance) -> list[int]:
    """Faces the bore's mouths open onto, ascending.

    Convex and untangent is the whole test. A floor sits behind a concave
    junction and a bull-nose fillet behind a tangent one, so neither reaches
    this list; what remains is the surface the drill would break through.
    """
    walls = {node.face_id for node in _cylinder_nodes(context, hole)}
    hosts: set[int] = set()
    for face_id in sorted(walls):
        for edge in context.graph.edges_of(face_id):
            if edge.concavity is not Concavity.CONVEX or edge.is_tangent:
                continue
            other = edge.other_face(face_id)
            if other in walls or not context.graph.has_node(other):
                continue
            hosts.add(other)
    return sorted(hosts)


# =============================================================================


@register_check(Rulebook.HOLE_NONSTANDARD_DIAMETER)
class HoleNonstandardDiameterCheck(MachiningCheck):
    """A diameter no stocked drill produces costs an operation or a tool.

    The question is not whether the size is a *preferred* one -- plenty of
    perfectly ordinary holes are not -- but whether it can be sourced at all.
    Two things make a diameter easy to get: it is in the catalogue of
    fractional, number, letter and tap-drill sizes the shop keeps, or, in a
    metric shop, it lands on the 0.1 mm grid a full drill index covers.
    Anything off both has to be reamed, bored, or drilled with a special
    grind.

    Large holes are exempt because they are not drilled at all: past about an
    inch a hole is bored or interpolated to whatever size the drawing asks.
    """

    @property
    def name(self) -> str:
        return "Hole Nonstandard Diameter Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        tolerance = thresholds.standard_size_match_tol_mm
        catalogue = context.config.all_drill_sizes_mm()
        metric_shop = context.config.unit_system in ("metric", "both")
        severity = self.severity_from_rule_config(rule_config)

        results: list[CheckResult] = []
        for hole in context.recognition.of_type(*_SIZED_BORE_TYPES):
            diameter = hole.number("diameter_mm") or 0.0
            if diameter <= 0.0:
                continue
            if diameter > thresholds.hole_nonstandard_max_diameter_mm:
                continue
            if self._is_catalogue(diameter, catalogue, tolerance, metric_shop):
                continue

            nearest = self._nearest(diameter, catalogue, metric_shop)
            if nearest is None:
                continue

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{diameter:.2f} mm, nearest stocked {nearest:.2f} mm",
                    self.render(
                        feedback,
                        severity,
                        diameter,
                        nearest,
                        nearest,
                        "mm",
                        f"This hole is {diameter:.2f} mm, which is not a size any "
                        "drill in the library comes in. As drawn it has to be "
                        "drilled undersize and reamed or bored to finish, or cut "
                        "with a drill ground specially -- an extra operation either "
                        f"way. The nearest stocked size is {nearest:.2f} mm; moving "
                        "the hole onto it would let it be drilled in one hit. If "
                        "the odd size is a fit requirement, say so on the drawing "
                        "so it gets reamed rather than queried.",
                    ),
                    faces=hole.faces,
                    value=diameter,
                    limit=nearest,
                    comparison="=",
                    unit="mm",
                )
            )
        return results

    @staticmethod
    def _nearest_grid_point(diameter: float) -> float:
        return round(diameter / _METRIC_GRID_STEP_MM) * _METRIC_GRID_STEP_MM

    @classmethod
    def _is_catalogue(
        cls, diameter: float, catalogue, tolerance: float, metric_shop: bool
    ) -> bool:
        if any(abs(diameter - size) <= tolerance for size in catalogue):
            return True
        if metric_shop:
            grid = cls._nearest_grid_point(diameter)
            if abs(diameter - grid) <= _METRIC_GRID_TOLERANCE_MM:
                return True
        return False

    @classmethod
    def _nearest(cls, diameter: float, catalogue, metric_shop: bool) -> Optional[float]:
        """The stocked size to recommend: nearer of the list and the grid."""
        nearest: Optional[float] = None
        smallest_gap = float("inf")
        for size in catalogue:
            gap = abs(diameter - size)
            if gap < smallest_gap:
                smallest_gap, nearest = gap, size
        if metric_shop:
            grid = cls._nearest_grid_point(diameter)
            if abs(diameter - grid) < smallest_gap:
                nearest = grid
        return nearest


@register_check(Rulebook.HOLE_PARTIAL_ENTRY)
class HolePartialEntryCheck(MachiningCheck):
    """A drill started on a slope walks downhill before it bites.

    The lip on the low side takes the whole cut while the high side is still
    in air, so the drill deflects, the hole comes in off position, and a small
    one snaps. What matters is whether there is *any* square face to start
    from: a through hole with one flat end and one sloped one is drilled from
    the flat end, and the slope is only an exit. So the rule fires only when
    every mouth is off square.

    Slender holes only. A shallow hole started on a slope is corrected by the
    time it is at depth; a deep one carries the error all the way down.
    """

    @property
    def name(self) -> str:
        return "Hole Partial Entry Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        square_enough = thresholds.hole_partial_entry_perpendicular_dot
        limit_deg = math.degrees(math.acos(min(1.0, max(0.0, square_enough))))
        severity = self.severity_from_rule_config(rule_config)

        results: list[CheckResult] = []
        for hole in context.recognition.of_type(
            FeatureType.THROUGH_HOLE, FeatureType.BLIND_HOLE
        ):
            diameter = hole.number("diameter_mm") or 0.0
            depth = hole.number("depth_mm") or 0.0
            if diameter <= 0.0:
                continue
            if depth / diameter < thresholds.hole_partial_entry_min_depth_ratio:
                continue

            axis = _bore_axis(context, hole)
            if axis is None:
                continue

            entries = self._entries(context, hole, axis)
            if not entries:
                continue
            # One square mouth is enough: the machinist drills from that end.
            if any(alignment >= square_enough for _, alignment in entries):
                continue

            face_id, alignment = min(entries, key=lambda entry: (entry[1], entry[0]))
            tilt_deg = math.degrees(math.acos(min(1.0, max(0.0, alignment))))
            through = bool(hole.param("is_through"))

            if through:
                opening = (
                    f"This {diameter:.1f} mm hole runs {depth:.1f} mm through the "
                    "part and comes out on a slope at both ends -- the squarest "
                    f"face it could be started from is {tilt_deg:.0f} degrees off "
                    "the axis. There is no flat to drill from in either direction."
                )
            else:
                opening = (
                    f"This {diameter:.1f} mm hole is {depth:.1f} mm deep and starts "
                    f"on a face tilted {tilt_deg:.0f} degrees off square to its "
                    "axis."
                )

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"entry {tilt_deg:.0f} degrees off square",
                    self.render(
                        feedback,
                        severity,
                        tilt_deg,
                        limit_deg,
                        limit_deg,
                        "deg",
                        opening
                        + " A drill landing on a slope cuts on one lip while the "
                        "other is still in air, so it walks downhill before it "
                        "bites: the hole comes in off position, out of square, and "
                        "a small drill will snap. Mill a flat pad square to the "
                        "axis first, or spot the entry with a stub drill and feed "
                        "in gently.",
                    ),
                    faces=sorted(set(hole.faces) | {face_id}),
                    value=tilt_deg,
                    limit=limit_deg,
                    comparison=">",
                    unit="deg",
                )
            )
        return results

    @staticmethod
    def _entries(
        context: MachiningContext, hole: FeatureInstance, axis: gp_Dir
    ) -> list[tuple[int, float]]:
        """Every planar mouth of the bore, with how square it is to the axis.

        `outward_normal` rather than the raw plane normal: the stored sense
        depends on which kernel built the solid, and the corrected one does
        not. The magnitude of the dot is what is wanted either way -- entering
        from above and below a plate are the same geometry.
        """
        entries: list[tuple[int, float]] = []
        for face_id in _rim_neighbours(context, hole):
            node = context.graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            entries.append((face_id, abs(axis.Dot(normal))))
        return entries


@register_check(Rulebook.HOLE_COUNTERSINK_ANGLE)
class HoleCountersinkAngleCheck(MachiningCheck):
    """A countersink is cut by a tool ground to a fixed angle.

    There is no adjusting it on the machine -- the angle in the model is the
    angle of the cutter, so a value off the standard list means a tool ground
    to order. The tolerance is generous because a modelled cone rarely lands
    exactly on its nominal angle once it has been through a boolean.
    """

    @property
    def name(self) -> str:
        return "Countersink Angle Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        tolerance = context.config.thresholds.hole_countersink_angle_tol_deg
        severity = self.severity_from_rule_config(rule_config)
        standard = ", ".join(f"{a:.0f}" for a in _STANDARD_COUNTERSINK_ANGLES_DEG)

        results: list[CheckResult] = []
        for hole in context.recognition.of_type(FeatureType.COUNTERSINK):
            angle = hole.number("included_angle") or 0.0
            if angle <= 0.0:
                continue
            nearest = min(
                _STANDARD_COUNTERSINK_ANGLES_DEG, key=lambda a: (abs(angle - a), a)
            )
            if abs(angle - nearest) < tolerance:
                continue

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{angle:.0f} degree included angle",
                    self.render(
                        feedback,
                        severity,
                        angle,
                        nearest,
                        nearest,
                        "deg",
                        f"This countersink is drawn at {angle:.0f} degrees "
                        f"included. Stock countersinks come at {standard} degrees, "
                        "and the angle is ground into the tool -- it cannot be "
                        "dialled in at the machine. As drawn this needs a cutter "
                        "made to order, or single-point boring the cone on a lathe. "
                        f"Moving it to {nearest:.0f} degrees would let it be cut "
                        "with a tool off the shelf; check first which fastener head "
                        "it has to suit.",
                    ),
                    faces=hole.faces,
                    value=angle,
                    limit=nearest,
                    comparison="=",
                    unit="deg",
                )
            )
        return results


@register_check(Rulebook.HOLE_MULTI_PASS)
class HoleMultiPassCheck(MachiningCheck):
    """A hole broken by a wide void is drilled twice, not once.

    A drill crosses a narrow gap and picks up the far side on its own margins.
    Once the gap is wider than a couple of diameters there is nothing left
    guiding it, so the bore has to be started again from the other end. That
    is a second operation, and if the far end is not reachable from the same
    setup it is a second setup too.

    Reported for information, not as a defect: the hole is perfectly
    manufacturable. The point is that whoever quotes it should see which
    feature is driving the extra operation.
    """

    @property
    def name(self) -> str:
        return "Hole Multi-Pass Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds

        results: list[CheckResult] = []
        for hole in context.recognition.of_type(FeatureType.THROUGH_HOLE):
            passes = int(hole.number("fragment_count") or 1)
            if passes <= 1:
                continue

            diameter = hole.number("diameter_mm") or 0.0
            void = hole.number("max_void_mm") or 0.0
            if diameter <= 0.0:
                continue
            # The same gate the depth rule uses to decide whether the drill
            # crosses the gap or has to be restarted beyond it.
            if void <= diameter * thresholds.hole_deep_single_pass_void_ratio:
                continue

            contiguous = hole.number("max_contiguous_depth_mm") or 0.0
            depth = hole.number("depth_mm") or 0.0
            spans = ""
            if contiguous > 0.0 and depth > contiguous + 0.01:
                spans = (
                    f" Entry to exit is {depth:.1f} mm; the longest run of solid "
                    f"material is {contiguous:.1f} mm."
                )

            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    f"{passes} drilling passes",
                    self.render(
                        feedback,
                        Severity.INFO,
                        float(passes),
                        1.0,
                        1.0,
                        "",
                        f"This {diameter:.1f} mm through hole is interrupted by a "
                        f"{void:.1f} mm cavity, {void / diameter:.1f} times the "
                        "drill diameter. The drill has nothing to steer on crossing "
                        f"a gap that wide, so the bore comes in as {passes} separate "
                        "drillings rather than one -- each its own operation, and "
                        "its own setup if the far end cannot be reached from the "
                        "same side." + spans,
                    ),
                    faces=hole.faces,
                    value=float(passes),
                    limit=1.0,
                    comparison=">",
                )
            )
        return results


# How closely a bore has to follow the part's rotation axis to have been
# opened on the lathe rather than drilled.
_LATHE_AXIS_DOT = 0.95


@register_check(Rulebook.HOLE_INTERSECTS_CAVITY)
class HoleIntersectsCavityCheck(MachiningCheck):
    """Bores that open into a milled cavity rather than into air.

    Two shapes of the same thing. A blind bore that stops on nothing has run
    into a void; a through passage whose every mouth lands inside a cavity
    links two chambers. Both read identically in the geometry whether they
    were designed that way -- ejector pins, oilways, clearance into a chamber
    -- or arrived when a hole got moved.

    So it is one note for the part rather than one per hole, and it is
    information rather than a fault. Bore-into-bore crossings are left to the
    intersecting-holes rule; reporting them here as well would count the same
    breakthrough twice.
    """

    @property
    def name(self) -> str:
        return "Hole Intersects Cavity Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        holes = context.recognition.of_type(*HOLE_TYPES)
        if not holes:
            return []

        breaching = self._blind_breaches(context, holes)
        breaching += self._through_breaches(
            context, holes, {hole.instance_id for hole in breaching}
        )
        if not breaching:
            return []

        breaching.sort(key=lambda hole: hole.instance_id)
        count = len(breaching)
        faces = sorted({face for hole in breaching for face in hole.faces})

        return [
            self.finding(
                rule,
                Severity.INFO,
                f"{count} bore{'' if count == 1 else 's'} into a cavity",
                self.render(
                    feedback,
                    Severity.INFO,
                    float(count),
                    0.0,
                    0.0,
                    "",
                    f"{count} bore{' ends' if count == 1 else 's end'} in a milled "
                    "cavity rather than on a floor of its own or out in open air. "
                    "Ejector-pin holes, clearance into a chamber and cross-drilled "
                    "oilways all look exactly like this, so most of the time it is "
                    "the design working -- but so does a hole that was moved and "
                    "now breaks into a pocket by accident. Worth confirming each "
                    "one is meant. Where they are, the drill breaks into the void "
                    "as an interrupted cut: ease the feed through the breakout and "
                    "allow for deburring inside the cavity, which is usually the "
                    "awkward part. Bores running into other bores are reported "
                    "separately.",
                ),
                faces=faces,
                value=float(count),
                comparison=">",
            )
        ]

    # -- the two shapes of breach -------------------------------------------

    def _blind_breaches(
        self, context: MachiningContext, holes: list[FeatureInstance]
    ) -> list[FeatureInstance]:
        """Bores the recognizer found stopping on nothing."""
        found: list[FeatureInstance] = []
        for hole in holes:
            if not hole.param("terminates_in_cavity"):
                continue
            if self._blend_capped(context, hole):
                continue
            if self._bored_on_the_lathe(context, hole):
                continue
            if self._touches_another_bore(context, hole, holes):
                continue
            found.append(hole)
        return found

    @staticmethod
    def _bored_on_the_lathe(context: MachiningContext, hole: FeatureInstance) -> bool:
        """Whether this bore was made on the axis the part was turned about.

        A bore down the middle of a turned part is opened with a boring bar
        while the part spins, and it stops wherever the previous turned form
        left off -- a counterbore, a recess, the back of a flange. Nothing
        broke into anything: that is the order the operations were done in.

        The concern this rule carries is a drill arriving somewhere it was
        not expected, and that is a milling concern. A cross-drilled hole on
        the same part still fires, because it is not on the lathe axis and
        does meet whatever it meets by surprise.
        """
        process = context.part_process
        if not process.has_axis or not context.is_turning_family:
            return False
        axis = HoleWebThicknessCheck._axis_line(context, hole)
        if axis is None:
            return False
        rotation = process.axis_of_revolution
        return abs(axis.Direction().Dot(rotation.Direction())) > _LATHE_AXIS_DOT

    def _through_breaches(
        self,
        context: MachiningContext,
        holes: list[FeatureInstance],
        already: set[str],
    ) -> list[FeatureInstance]:
        """Passages whose every mouth opens inside a cavity.

        A bore breaking through a *planar* cavity wall has two openings and
        classifies as an ordinary through hole, so the blind flag cannot see
        it. What makes one reportable is that no mouth reaches the outside:
        a bolt hole through a pocket floor, a drain, a counterbore stack all
        have at least one ordinary opening and stay silent, which is what
        keeps this from firing on half the holes on the part.

        Reach is narrower than it looks today. The pocket pass will not read
        a cavity whose floor a bore has pierced, so a passage between two
        blind pockets leaves neither pocket recognized and nothing here to
        match against. Passages between cavities the piercing does not
        unseat -- channels, through cavities -- are found.
        """
        cavity_faces = context.recognition.faces_of_type(*_CAVITY_TYPES)
        if not cavity_faces:
            return []

        found: list[FeatureInstance] = []
        for hole in holes:
            if hole.instance_id in already:
                continue
            if hole.type != FeatureType.THROUGH_HOLE and not hole.param("is_through"):
                continue

            hosts = _rim_neighbours(context, hole)
            # Fewer than two means the rims are inside the hole's own stack of
            # steps, which is a counterbore rather than a breach.
            if len(hosts) < 2:
                continue
            if any(host not in cavity_faces for host in hosts):
                continue
            found.append(hole)
        return found

    # -- guards --------------------------------------------------------------

    @staticmethod
    def _blend_capped(context: MachiningContext, hole: FeatureInstance) -> bool:
        """Whether the bore's floor or rim carries a fillet.

        A torus band across the end of a bore severs the wall's adjacency to
        its own floor, so the recognizer sees a hole that stops on nothing and
        marks it as running into a cavity. The cavity is the hole's own
        bull-nosed floor. Without this a radiused blind hole reports as
        breaking into itself.
        """
        for node in _cylinder_nodes(context, hole):
            for edge in context.graph.edges_of(node.face_id):
                if edge.concavity is not Concavity.TANGENT and not edge.is_tangent:
                    continue
                other = edge.other_face(node.face_id)
                if not context.graph.has_node(other):
                    continue
                if context.graph.node(other).surface_type is SurfaceType.TORUS:
                    return True
        return False

    @staticmethod
    def _touches_another_bore(
        context: MachiningContext, hole: FeatureInstance, holes: list[FeatureInstance]
    ) -> bool:
        """Whether the void this bore runs into is another recognized hole.

        The same touch test the intersecting-holes rule applies, so the two
        rules agree on which crossings belong to which. Coaxial stepped bores
        fall out of it too -- their axis distance is zero -- because a
        counterbore stack is not a breach into anything.
        """
        axis = HoleWebThicknessCheck._axis_line(context, hole)
        if axis is None:
            return False
        radius = (hole.number("diameter_mm") or 0.0) / 2.0

        for other in holes:
            if other is hole:
                continue
            other_axis = HoleWebThicknessCheck._axis_line(context, other)
            if other_axis is None:
                continue
            distance = HoleIntersectingCheck._axis_distance(axis, other_axis)
            if distance is None:
                continue
            reach = radius + (other.number("diameter_mm") or 0.0) / 2.0
            if distance > reach + _TOUCH_TOLERANCE_MM:
                continue
            if HoleIntersectingCheck._spans_overlap(
                context, hole, other, axis, other_axis
            ):
                return True
        return False
