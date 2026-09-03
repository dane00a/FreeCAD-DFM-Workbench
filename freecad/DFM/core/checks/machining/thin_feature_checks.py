# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Thin-wall detection.

The single highest-value geometric machining rule, and the one most prone to
false positives. Two faces being close together is not enough -- two walls of
a pocket are close and there is nothing between them. What matters is
material between the faces, and that is what the sign test below establishes.

This is the planar pass. Walls bounded by bores or freeform surfaces need
feature recognition and arrive with it.
"""

from __future__ import annotations


from typing import Iterator, Optional

from OCP.BRepExtrema import BRepExtrema_DistShapeShape

from ...machining.aag import AagNode, SurfaceType
from ...machining.context import MachiningContext
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# Faces must be substantially opposed before they can bound a wall.
_ANTIPARALLEL_DOT_MAX = -0.8

# Bounding boxes must genuinely overlap in the two in-plane directions;
# a shared corner is not a wall.
_OVERLAP_MARGIN_MM = 0.1


@register_check(Rulebook.THIN_WALL)
class ThinWallCheck(MachiningCheck):
    """Material left between two opposed faces.

    Reported two ways. A wall thinner than the absolute limit is a problem at
    any size. A thicker wall can still be a problem if it is also broad: a
    3 mm wall 200 mm across will drum and deflect even though 3 mm alone
    would pass. The aspect path carries a thickness cap because stiffness
    scales with the cube of thickness -- a 6 mm wall is rigid at any
    practical length, and without the cap large parts collect findings on
    sections no machinist would call thin.
    """

    @property
    def name(self) -> str:
        return "Thin Wall Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None:
            target = thresholds.thin_wall_warn_mm
        if limit is None:
            limit = thresholds.thin_wall_error_mm

        results: list[CheckResult] = []
        reported: set[tuple[int, int]] = set()

        for first, second, thickness in self._opposed_planar_pairs(context):
            pair = (min(first.face_id, second.face_id), max(first.face_id, second.face_id))
            if pair in reported:
                continue

            verdict = self._grade(context, first, second, thickness, target, limit)
            if verdict is None:
                continue
            reported.add(pair)

            severity, threshold, reason = verdict
            message = self.render(
                feedback,
                severity,
                thickness,
                target,
                limit,
                "mm",
                reason,
            )
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{thickness:.2f} mm wall",
                    message,
                    faces=[first.face_id, second.face_id],
                    value=thickness,
                    limit=threshold,
                    comparison="<",
                    unit="mm",
                )
            )

        return results

    # -- grading ------------------------------------------------------------

    def _grade(
        self,
        context: MachiningContext,
        first: AagNode,
        second: AagNode,
        thickness: float,
        target: float,
        limit: float,
    ) -> Optional[tuple[Severity, float, str]]:
        thresholds = context.config.thresholds

        if thickness <= limit:
            return (
                Severity.ERROR,
                limit,
                f"Only {thickness:.2f} mm of material is left between these two "
                f"faces, under the {limit:.2f} mm floor. A wall this thin will "
                "deflect away from the cutter and is likely to distort or break "
                "out during machining.",
            )

        if thickness <= target:
            return (
                Severity.WARNING,
                target,
                f"The wall between these faces is {thickness:.2f} mm, below the "
                f"{target:.2f} mm target. Expect deflection and chatter; it will "
                "need light finishing passes and may not hold flatness.",
            )

        # The broad-and-thin path. Both the span and the thickness have to
        # qualify, or every large flat face on a normal part would fire.
        if thickness > thresholds.thin_wall_aspect_max_thickness_mm:
            return None

        span = self._in_plane_span(first, second)
        if span <= 0.0:
            return None
        aspect = span / thickness
        if aspect < thresholds.thin_wall_aspect_warn:
            return None

        return (
            Severity.WARNING,
            target,
            f"This wall is {thickness:.2f} mm thick but {span:.0f} mm across, an "
            f"aspect ratio of {aspect:.0f}:1. Thin broad sections drum under the "
            "cutter and relieve residual stress as material comes off, so expect "
            "chatter marks and bow even though the thickness itself is acceptable.",
        )

    @staticmethod
    def _in_plane_span(first: AagNode, second: AagNode) -> float:
        """The smaller in-plane extent of the wall.

        The shorter direction is the right one: a wall 200 mm long and 5 mm
        tall is a rib, and it is the 5 mm that decides whether it is floppy.
        """
        dims_a = first.bbox_dims()
        dims_b = second.bbox_dims()
        shared = [min(a, b) for a, b in zip(dims_a, dims_b)]
        shared.sort()
        # The smallest is the wall's own thickness direction; take the next.
        return shared[1] if len(shared) > 1 else 0.0

    # -- geometry -----------------------------------------------------------

    def _opposed_planar_pairs(
        self, context: MachiningContext
    ) -> Iterator[tuple[AagNode, AagNode, float]]:
        """Yield planar face pairs with material between them, and how much."""
        planes = [n for n in context.graph.nodes if n.surface_type is SurfaceType.PLANE]
        ceiling = max(
            context.config.thresholds.thin_wall_warn_mm,
            context.config.thresholds.thin_wall_aspect_max_thickness_mm,
        )

        for index, first in enumerate(planes):
            normal_a = first.outward_normal
            if normal_a is None or first.bbox.IsVoid():
                continue

            for second in planes[index + 1 :]:
                normal_b = second.outward_normal
                if normal_b is None or second.bbox.IsVoid():
                    continue
                if normal_a.Dot(normal_b) > _ANTIPARALLEL_DOT_MAX:
                    continue

                offset = (
                    second.centroid.X() - first.centroid.X(),
                    second.centroid.Y() - first.centroid.Y(),
                    second.centroid.Z() - first.centroid.Z(),
                )
                along = (
                    offset[0] * normal_a.X()
                    + offset[1] * normal_a.Y()
                    + offset[2] * normal_a.Z()
                )
                # Material lies between the faces only when the second sits
                # behind the first's outward normal. A positive projection
                # means they face each other across a cavity -- two pocket
                # walls, not a wall.
                if along >= 0.0:
                    continue

                separation = abs(along)
                if separation > ceiling * 2.0:
                    continue  # far too thick to be interesting; skip the solver
                if not self._overlaps_in_plane(first, second, normal_a):
                    continue

                thickness = self._measured_distance(context, first, second)
                if thickness is None or thickness <= 1e-6 or thickness > ceiling:
                    continue
                yield (first, second, thickness)

    @staticmethod
    def _overlaps_in_plane(first: AagNode, second: AagNode, normal) -> bool:
        """Whether the two faces actually shadow each other.

        Compared in world axes, skipping the one the wall's thickness runs
        along. Two faces that only meet at a corner are not a wall.
        """
        if first.bbox.IsVoid() or second.bbox.IsVoid():
            return False
        a = first.bbox.Get()
        b = second.bbox.Get()
        components = (abs(normal.X()), abs(normal.Y()), abs(normal.Z()))
        thickness_axis = components.index(max(components))

        for axis in range(3):
            if axis == thickness_axis:
                continue
            lo_a, hi_a = a[axis], a[axis + 3]
            lo_b, hi_b = b[axis], b[axis + 3]
            if min(hi_a, hi_b) - max(lo_a, lo_b) < _OVERLAP_MARGIN_MM:
                return False
        return True

    @staticmethod
    def _measured_distance(
        context: MachiningContext, first: AagNode, second: AagNode
    ) -> Optional[float]:
        """True minimum distance between the two faces."""
        try:
            solver = BRepExtrema_DistShapeShape(
                context.face_index.face_at(first.face_id),
                context.face_index.face_at(second.face_id),
            )
            if not solver.IsDone():
                return None
            return solver.Value()
        except Exception:
            return None
