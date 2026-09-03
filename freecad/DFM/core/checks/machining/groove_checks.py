# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about grooves and seal glands.

Two quite different problems live here because they share a feature type.

A thread relief is judged against the thread it serves: too narrow and the
grooving tool rubs the tail-off of the thread as it runs out, chipping the
last engaged form -- exactly the thread the fastener bears on.

A gasket-groove loop is judged against the cutter that has to clear it. The
concave side of every corner in a milled loop carries the cutter's radius, so
a square corner on the drawing is a request for a second process rather than a
tolerance to tighten.
"""

from __future__ import annotations

import math

from ...machining.aag import Concavity, SurfaceType
from ...machining.features import GROOVE_TYPES, FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# A wall stands square to the gland axis; the floor is what does not.
_WALL_AXIS_TOLERANCE = 0.3

# Corner arcs run parallel to the gland axis.
_CORNER_AXIS_ALIGNMENT = 0.95


@register_check(Rulebook.THREAD_RELIEF_WIDTH)
class ThreadReliefWidthCheck(MachiningCheck):
    """A relief groove has to be wide enough for the tool to run out.

    Where the adjacent thread is known the requirement scales with its pitch,
    which is the shop rule of thumb. Where it is not, a fixed floor stands in
    -- chosen as the worst case across the small metric sizes, so the finding
    says plainly that confirming the thread spec would sharpen it.
    """

    @property
    def name(self) -> str:
        return "Thread Relief Width Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        multiplier = thresholds.thread_relief_pitch_multiplier
        floor = self.safe_float(rule_config.limit)
        if floor is None:
            floor = thresholds.thread_relief_min_width_mm

        results: list[CheckResult] = []
        for groove in context.recognition.of_type(FeatureType.THREAD_RELIEF_GROOVE):
            width = groove.number("width_mm") or 0.0
            if width <= 0.0:
                continue

            pitch = groove.number("adjacent_thread_pitch_mm") or 0.0
            required = pitch * multiplier if pitch > 0.0 else floor
            if width >= required:
                continue

            if pitch > 0.0:
                basis = (
                    f"{multiplier:g} times the {pitch:.2f} mm pitch of the "
                    "thread it serves"
                )
            else:
                basis = (
                    "the default minimum -- confirming the adjacent thread "
                    "spec would let this be judged against its actual pitch"
                )

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{width:.2f} mm wide",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        width,
                        required,
                        required,
                        "mm",
                        f"This relief groove is {width:.2f} mm wide against a "
                        f"recommended {required:.2f} mm, which is {basis}. A relief "
                        "that narrow leaves the grooving tool rubbing the tail-off "
                        "of the thread, which chips the last engaged form -- the "
                        "one the fastener actually bears on. Widen the groove or "
                        "move it clear of the thread run-out.",
                    ),
                    faces=groove.faces,
                    value=width,
                    limit=required,
                    comparison="<",
                )
            )
        return results


@register_check(Rulebook.GROOVE_SQUARE_CORNER)
class GrooveSquareCornerCheck(MachiningCheck):
    """A milled gasket loop cannot have square plan-view corners.

    Only loop glands are considered. A circular face gland and a turned groove
    have no plan-view corners at all, and the floor-to-wall edges every groove
    has are square by the end mill's own geometry -- reporting those would flag
    every groove on every part.
    """

    @property
    def name(self) -> str:
        return "Groove Square Corner Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.pocket_corner_radius_min_mm
        sharp_min = math.radians(context.config.thresholds.sharp_edge_min_deviation_deg)

        results: list[CheckResult] = []
        for groove in context.recognition.of_type(*GROOVE_TYPES):
            if groove.param("gland_shape") != "loop":
                continue
            axis = groove.direction("axis")
            if axis is None:
                continue

            walls = self._wall_faces(context, groove, axis)
            if len(walls) < 2:
                continue

            corners = self._sharp_corners(context, walls, sharp_min)
            radius = 0.0 if corners else self._smallest_arc(context, groove, axis)
            if radius is None or radius > limit:
                continue

            width = groove.number("width_mm") or 0.0
            if corners:
                count = len(corners)
                opening = (
                    f"This gasket groove loop has {count} square "
                    f"corner{'' if count == 1 else 's'}. "
                )
                faces = sorted({f for edge in corners for f in (edge.face_id_a, edge.face_id_b)})
            else:
                opening = (
                    f"This gasket groove loop turns on a {radius:.2f} mm corner "
                    "radius, below what any tool in the library can cut. "
                )
                faces = groove.faces

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    "square corner" if corners else f"r{radius:.2f} mm",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        radius,
                        limit,
                        limit,
                        "mm",
                        opening
                        + (f"The groove is {width:.1f} mm wide, so " if width else "")
                        + "the concave side of every corner carries the cutter's "
                        "radius -- a rotating tool cannot leave a square one. Add a "
                        "corner radius, or, if the square corner is a sealing "
                        "requirement, this groove needs sinker EDM rather than "
                        "milling.",
                    ),
                    faces=faces,
                    value=radius,
                    limit=limit,
                    comparison="<",
                )
            )
        return results

    # -- geometry -----------------------------------------------------------

    @staticmethod
    def _wall_faces(context, groove, axis) -> set[int]:
        """Planar member faces standing square to the gland axis.

        The floor is excluded deliberately: its edges against the walls are
        square on every milled groove ever cut, and are not the problem.
        """
        walls = set()
        for face_id in groove.faces:
            if not context.graph.has_node(face_id):
                continue
            node = context.graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            if abs(normal.Dot(axis)) < _WALL_AXIS_TOLERANCE:
                walls.add(face_id)
        return walls

    @staticmethod
    def _sharp_corners(context, walls: set[int], sharp_min: float) -> list:
        """Concave wall-to-wall edges that turn a real corner.

        Two walls both parallel to the axis meet along a line parallel to it,
        so any such edge is a plan-view corner by construction. Shallow bends
        are skipped -- a loop that eases round by a few degrees is not what
        this rule is about.
        """
        corners = []
        for edge in context.graph.edges:
            if edge.face_id_a not in walls or edge.face_id_b not in walls:
                continue
            if edge.concavity is not Concavity.CONCAVE or edge.is_tangent:
                continue
            if abs(edge.dihedral_angle - math.pi) < sharp_min:
                continue
            corners.append(edge)
        return corners

    @staticmethod
    def _smallest_arc(context, groove, axis):
        """The tightest concave corner arc in a rounded loop.

        Convex arcs are the outside of the island in the middle of the loop.
        A cutter has no trouble with those, so they are not measured.
        """
        smallest = None
        for face_id in groove.faces:
            if not context.graph.has_node(face_id):
                continue
            node = context.graph.node(face_id)
            if node.surface_type is not SurfaceType.CYLINDER:
                continue
            if not node.is_internal or node.cyl_cone_axis is None:
                continue
            if abs(node.cyl_cone_axis.Direction().Dot(axis)) < _CORNER_AXIS_ALIGNMENT:
                continue
            if smallest is None or node.cyl_radius < smallest:
                smallest = node.cyl_radius
        return smallest
