# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about tapped holes.

A tap is the most fragile tool in the shop and the one that breaks in the
most expensive place -- at the bottom of a finished part, welded into it by
its own flutes. Every rule here is about giving it room: room below the
thread for the lead to run out, room around the entry for the holder, and
enough material around the bore that the thread has something to hold on to.

All three fire only on a thread the workbench can actually see. A tapped hole
is modelled as a plain bore at the tap-drill size, so unless somebody cut a
real helix in the model there is nothing to distinguish it from a clearance
hole, and guessing from diameter alone would put a thread callout on half the
holes in a plate. The recognizer refuses to guess; these rules inherit that
refusal by reading only features that carry a resolved thread designation.
"""

from __future__ import annotations

import math
from typing import Optional

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.gp import gp_Pnt, gp_Vec

from ...machining.aag import AagNode, SurfaceType
from ...machining.context import MachiningContext
from ...machining.features import FeatureInstance, FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck
from .hole_checks import HoleEdgeDistanceCheck


# Every feature type that can carry a resolved thread spec. A counterbore is
# included so that a thread cut in its inner bore is judged like any other
# tapped hole; the designation gate below drops the ones that carry none.
_THREADED_TYPES = (FeatureType.THREADED_HOLE, FeatureType.COUNTERBORE)

# A thread inferred from diameter alone is a guess, and a guess is not
# grounds for a finding. No recognizer in the workbench emits this today --
# the diameter-only heuristic was deliberately left out -- so the guard is a
# defence against a future recognizer growing one back.
_GUESSED_EVIDENCE = "diameter_heuristic"

# The face a tap enters through stands square to the bore: within about 45
# degrees of the axis by this dot, and large enough that it is the outside of
# the part rather than the hole's own floor.
_ENTRY_AXIS_ALIGNMENT = 0.7
_ENTRY_AREA_MULTIPLE = 3.0

# How far above the entry plane to look for something the tap holder could
# foul. Three thread diameters covers a stub holder; the floor keeps the
# probe meaningful on the smallest threads.
_HOLDER_PROBE_DIAMETERS = 3.0
_HOLDER_PROBE_MIN_MM = 10.0

# Slack on the cheap bounding-box test that decides whether a face is worth
# measuring exactly. Generous on purpose: it only has to avoid discarding a
# face that the exact distance would have flagged.
_PREFILTER_SLACK_MM = 0.5

# A plane this nearly square to the bore axis is one of the hole's own caps,
# not a wall beside it.
_CAP_PLANE_ALIGNMENT = 0.9

# Below this the solver has put the plane on the cylinder's surface, and the
# real wall has to be read from the axis instead.
_TANGENT_DISTANCE_MM = 0.01


def _threaded_features(context: MachiningContext) -> list[FeatureInstance]:
    """Features carrying a thread spec the rules can reason about.

    The trigger is the presence of a designation rather than the feature
    type: that is what lets a thread cut in a counterbore's inner bore be
    judged like any other tapped hole, and what keeps a bore mistyped
    upstream from being measured against a pitch nobody resolved.
    """
    found = []
    for feature in context.recognition.of_type(*_THREADED_TYPES):
        if not feature.param("thread_designation"):
            continue
        if feature.param("thread_evidence") == _GUESSED_EVIDENCE:
            continue
        found.append(feature)
    return found


def _bore_wall(context: MachiningContext, hole: FeatureInstance) -> Optional[AagNode]:
    """The cylindrical face the tap runs in.

    The smallest internal cylinder the feature owns. Face ids arrive sorted
    rather than in the order the recognizer collected them, so the first of
    them is not reliably the bore; and on a counterbore the smallest one is
    the inner bore rather than the mouth, which is where the thread is.
    """
    best: Optional[AagNode] = None
    for face_id in sorted(hole.faces):
        if not context.graph.has_node(face_id):
            continue
        node = context.graph.node(face_id)
        if node.surface_type is not SurfaceType.CYLINDER:
            continue
        if not node.is_internal or node.cyl_cone_axis is None:
            continue
        if best is None or node.cyl_radius < best.cyl_radius:
            best = node
    return best


@register_check(Rulebook.THREAD_RUNOUT)
class ThreadRunoutCheck(MachiningCheck):
    """A blind tapped hole needs drilling deeper than it is tapped.

    The first few threads on a tap are ground away to lead it in, so they
    never cut a full form and have to finish up somewhere below the last
    good thread. Deny them that and the tap reaches the bottom of the hole
    while it is still cutting, which is how taps end up snapped off in a
    finished part.

    A through hole runs out into fresh air and is exempt.
    """

    @property
    def name(self) -> str:
        return "Thread Runout Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        configured = self.safe_float(rule_config.limit)

        results: list[CheckResult] = []
        for hole in _threaded_features(context):
            if hole.param("is_through"):
                continue

            diameter = hole.number("diameter_mm") or 0.0
            depth = hole.number("depth_mm") or 0.0
            thread_depth = hole.number("thread_depth_mm") or 0.0
            if diameter <= 0.0:
                continue
            if depth <= 0.0 and thread_depth <= 0.0:
                continue
            # A hole whose tapped length was never established is taken as
            # tapped all the way down. That is the worst case and the honest
            # one: a plain bore carries no record of where the thread stops,
            # so anything else would be inventing a run-out that may not be
            # there.
            if thread_depth <= 0.0:
                thread_depth = depth

            pitch = hole.number("thread_pitch_mm") or 0.0
            if pitch > 0.0:
                # The lead is ground as a number of threads, so it scales
                # with pitch. This is the figure that governs whenever the
                # thread has been resolved.
                required = thresholds.thread_runout_min_pitches * pitch
                basis = (
                    f"{thresholds.thread_runout_min_pitches:g} times the "
                    f"{pitch:.2f} mm pitch"
                )
            else:
                required = thresholds.thread_runout_min_diameters * diameter
                basis = (
                    f"{thresholds.thread_runout_min_diameters:g} times the "
                    f"{diameter:.2f} mm bore, the pitch being unknown"
                )
            # A configured limit is a floor the shop will not go below,
            # never a licence to tap closer than the pitch demands: a fine
            # thread whose lead comes to less than the shop minimum still
            # has to clear the minimum.
            if configured is not None and configured > required:
                required = configured
                basis = "the shop minimum for any thread"

            available = max(0.0, depth - thread_depth)
            if available >= required:
                continue

            designation = hole.param("thread_designation")
            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{available:.2f} mm of runout",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        available,
                        required,
                        required,
                        "mm",
                        f"This blind {designation} hole is drilled only "
                        f"{available:.2f} mm past the end of its thread, against "
                        f"the {required:.2f} mm the tap needs -- {basis}. The lead "
                        "threads on a tap never cut a full form and have to finish "
                        "below the last good one; with nowhere to go the tap "
                        "bottoms out while it is still cutting, and that is how it "
                        "snaps off in the part. Drill deeper, tap shallower, or "
                        "call for a bottoming tap on the print.",
                    ),
                    faces=hole.faces,
                    value=available,
                    limit=required,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.THREAD_SHOULDER_PROXIMITY)
class ThreadShoulderProximityCheck(MachiningCheck):
    """A tap needs room around the hole, not only down it.

    Two ways that room goes missing. A thread down the middle of a
    counterbore has to clear the counterbore wall, or the tap's lead chamfer
    rubs it before full threads form at the top of the bore. And anything
    standing proud beside the entry -- a boss, a rib, a raised pad -- is in
    the way of the holder long before the tap is at depth.
    """

    @property
    def name(self) -> str:
        return "Thread Shoulder Proximity Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.thread_shoulder_min_mm

        results: list[CheckResult] = []
        for hole in _threaded_features(context):
            nominal = hole.number("thread_nominal_mm") or 0.0
            if nominal <= 0.0:
                continue

            outer = hole.number("outer_diameter_mm") or 0.0
            if outer > 0.0:
                finding = self._counterbore_wall(
                    hole, nominal, outer, limit, rule, feedback
                )
            else:
                finding = self._entry_shoulder(
                    context, hole, nominal, limit, rule, feedback
                )
            if finding is not None:
                results.append(finding)
        return results

    # -- the counterbore wall ------------------------------------------------

    def _counterbore_wall(
        self, hole, nominal: float, outer: float, limit: float, rule, feedback
    ) -> Optional[CheckResult]:
        """Clearance between the thread OD and the mouth around it.

        Pure arithmetic on the two diameters, which is exact and costs
        nothing -- worth doing before reaching for the solver.
        """
        gap = (outer - nominal) / 2.0
        if gap >= limit:
            return None

        designation = hole.param("thread_designation")
        return self.finding(
            rule,
            Severity.WARNING,
            f"{gap:.2f} mm to the counterbore wall",
            self.render(
                feedback,
                Severity.WARNING,
                gap,
                limit,
                limit,
                "mm",
                f"This {designation} thread leaves only {gap:.2f} mm between its "
                f"outside diameter and the wall of the counterbore around it, "
                f"under the {limit:.2f} mm minimum. The tap's lead chamfer "
                "touches that wall before the thread has come to full form, so "
                "the top of the bore finishes with partial threads -- the ones "
                "the fastener bears on first. Open the counterbore out, or start "
                "the thread further down the bore.",
            ),
            faces=hole.faces,
            value=gap,
            limit=limit,
            comparison="<",
            unit="mm",
        )

    # -- raised geometry beside the entry ------------------------------------

    def _entry_shoulder(
        self,
        context: MachiningContext,
        hole,
        nominal: float,
        limit: float,
        rule,
        feedback,
    ) -> Optional[CheckResult]:
        """The nearest face standing above the entry plane, if it is too near.

        Measured from a probe run up the bore axis out of the hole: the tap
        holder occupies that cylinder, so the distance from the axis to the
        nearest raised face, less the bore radius, is the clearance the
        holder has. A hole open at both ends is probed from both, since the
        tap may come in either way.
        """
        bore = _bore_wall(context, hole)
        if bore is None:
            return None

        nearest: Optional[float] = None
        nearest_face = 0
        for approach in self._approaches(context, hole, bore):
            found = self._nearest_shoulder(context, hole, bore, nominal, limit, approach)
            if found is None:
                continue
            gap, face_id = found
            if nearest is None or gap < nearest:
                nearest, nearest_face = gap, face_id

        if nearest is None or nearest >= limit:
            return None

        clearance = max(0.0, nearest)
        designation = hole.param("thread_designation")
        return self.finding(
            rule,
            Severity.WARNING,
            f"{clearance:.2f} mm to shoulder",
            self.render(
                feedback,
                Severity.WARNING,
                clearance,
                limit,
                limit,
                "mm",
                f"Something stands proud only {clearance:.2f} mm from the edge of "
                f"this {designation} thread, inside the {limit:.2f} mm the tap "
                "holder needs around it. The holder fouls it before the tap is at "
                "depth, so the hole wants an extension tap, an angle head, or a "
                "setup of its own -- all of them dearer than moving the hole clear "
                "of the raised feature.",
            ),
            faces=sorted(set(hole.faces) | {nearest_face}),
            value=clearance,
            limit=limit,
            comparison="<",
            unit="mm",
        )

    # -- which way the tap comes in ------------------------------------------

    @staticmethod
    def _approaches(
        context: MachiningContext, hole: FeatureInstance, bore: AagNode
    ) -> list[gp_Vec]:
        """Unit vectors pointing out of the bore, one per open end.

        A blind hole is settled by its floor: the tap comes in from the other
        end. A hole open at both ends offers the tap a choice, so both are
        probed and the worse answer is the one that counts.
        """
        direction = bore.cyl_cone_axis.Direction()
        axis = gp_Vec(direction)
        cross_section = math.pi * bore.cyl_radius * bore.cyl_radius

        floor: Optional[AagNode] = None
        for face_id in sorted(hole.faces):
            if not context.graph.has_node(face_id):
                continue
            node = context.graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None or abs(normal.Dot(direction)) < _ENTRY_AXIS_ALIGNMENT:
                continue
            # The floor is about the size of the bore. A large planar member
            # face is the surface the hole was drilled into, not its bottom.
            if node.area >= cross_section * _ENTRY_AREA_MULTIPLE:
                continue
            if floor is None or node.area < floor.area:
                floor = node

        if floor is not None:
            outward = gp_Vec(floor.centroid, bore.centroid)
            return [axis if outward.Dot(axis) > 0.0 else axis.Reversed()]
        return [axis, axis.Reversed()]

    @staticmethod
    def _entry_plane(
        context: MachiningContext, hole: FeatureInstance, bore: AagNode, up: gp_Vec
    ) -> Optional[tuple[AagNode, float]]:
        """The face the tap enters through, and its height along the approach.

        The reference finds this as a large planar neighbour of the bore
        wall. That does not survive the port: a thread modelled as a real
        helix cuts the bore into fragments, and the fragment that carries the
        thread spec no longer touches the surface the hole was drilled into.
        So the entry is found by position instead -- the nearest large plane
        square to the axis that the axis passes through, beyond the far end
        of the bore.
        """
        origin = bore.cyl_cone_axis.Location()
        cross_section = math.pi * bore.cyl_radius * bore.cyl_radius
        reach = max(
            gp_Vec(origin, corner).Dot(up)
            for face_id in hole.faces
            if context.graph.has_node(face_id)
            for corner in _bbox_corners(context.graph.node(face_id))
        )

        best: Optional[tuple[AagNode, float]] = None
        for node in sorted(
            context.graph.nodes_by_surface_type(SurfaceType.PLANE),
            key=lambda n: n.face_id,
        ):
            if hole.has_face(node.face_id) or node.bbox.IsVoid():
                continue
            normal = node.outward_normal
            if normal is None or abs(gp_Vec(normal).Dot(up)) < _ENTRY_AXIS_ALIGNMENT:
                continue
            if node.area < cross_section * _ENTRY_AREA_MULTIPLE:
                continue
            height = gp_Vec(origin, node.centroid).Dot(up)
            if height < reach:
                continue  # behind the hole: the tap never passes through it
            point = origin.Translated(up.Multiplied(height))
            if not _within_bbox(node, point):
                continue  # square to the axis but off to one side of it
            if best is None or height < best[1]:
                best = (node, height)
        return best

    def _nearest_shoulder(
        self,
        context: MachiningContext,
        hole: FeatureInstance,
        bore: AagNode,
        nominal: float,
        limit: float,
        up: gp_Vec,
    ) -> Optional[tuple[float, int]]:
        """Closest raised face to the tap holder coming in this way."""
        entry = self._entry_plane(context, hole, bore, up)
        if entry is None:
            return None  # a bore fed from inside a cavity has no open entry
        entry_face, entry_height = entry

        origin = bore.cyl_cone_axis.Location()
        probe_length = max(_HOLDER_PROBE_DIAMETERS * nominal, _HOLDER_PROBE_MIN_MM)
        start = origin.Translated(up.Multiplied(entry_height))
        probe = BRepBuilderAPI_MakeEdge(
            start, start.Translated(up.Multiplied(probe_length))
        ).Shape()

        minimum_height = context.config.thresholds.thread_shoulder_min_height_mm
        own_faces = set(hole.faces) | {entry_face.face_id}

        nearest: Optional[float] = None
        nearest_face = 0
        for node in sorted(context.graph.nodes, key=lambda n: n.face_id):
            if node.face_id in own_faces or node.bbox.IsVoid():
                continue
            # A torus beside the hole is the blend on the rim, an edge
            # treatment rather than something the holder runs into.
            if node.surface_type is SurfaceType.TORUS:
                continue

            corners = _bbox_corners(node)
            highest = max(gp_Vec(origin, corner).Dot(up) for corner in corners)
            # It has to rise above the entry plane by enough to matter. A
            # face flush with the entry is the surface the tap works from.
            if highest - entry_height < minimum_height:
                continue

            # Cheap lower bound on how close the face can come to the axis,
            # so the solver only ever sees plausible candidates.
            xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
            middle = gp_Pnt(
                0.5 * (xmin + xmax), 0.5 * (ymin + ymax), 0.5 * (zmin + zmax)
            )
            centre = gp_Vec(origin, middle)
            radial = centre.Subtracted(up.Multiplied(centre.Dot(up))).Magnitude()
            half_diagonal = 0.5 * math.sqrt(
                (xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2
            )
            if radial - half_diagonal > bore.cyl_radius + limit + _PREFILTER_SLACK_MM:
                continue

            gap = self._distance_to_probe(context, probe, node, bore.cyl_radius)
            if gap is None:
                continue
            if nearest is None or gap < nearest:
                nearest = gap
                nearest_face = node.face_id

        return None if nearest is None else (nearest, nearest_face)

    @staticmethod
    def _distance_to_probe(
        context: MachiningContext, probe, node: AagNode, bore_radius: float
    ) -> Optional[float]:
        """Clearance from the bore wall to a face, out along the probe."""
        try:
            solver = BRepExtrema_DistShapeShape(
                probe, context.face_index.face_at(node.face_id)
            )
            if not solver.IsDone():
                return None
            return solver.Value() - bore_radius
        except Exception:
            return None


@register_check(Rulebook.THREAD_WALL_THICKNESS)
class ThreadWallThicknessCheck(MachiningCheck):
    """Material left around a tapped hole once the thread is cut.

    The thread root sits outside the drilled bore, so tapping takes another
    slice off whatever wall is there. A fastener pulled up in a wall too thin
    to carry it splits the part along the hole, and it does so in service
    rather than in the shop.
    """

    @property
    def name(self) -> str:
        return "Thread Wall Thickness Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None:
            target = thresholds.thin_wall_warn_mm
        if limit is None:
            limit = thresholds.thin_wall_error_mm

        bounds = context.plane_bbox_bounds()
        results: list[CheckResult] = []

        for hole in _threaded_features(context):
            diameter = hole.number("diameter_mm") or 0.0
            if diameter <= 0.0:
                continue

            wall = self._nearest_wall(context, hole, diameter)
            if wall is None and bounds is not None:
                # Nothing planar beside the bore, so fall back to the outside
                # of the part -- the same measurement the hole edge-distance
                # rule makes, and worth reusing rather than approximating
                # again from the bounding box.
                outside = HoleEdgeDistanceCheck._distance_to_outside(
                    context, hole, bounds
                )
                if outside is not None and outside > 0.0:
                    wall = outside
            if wall is None:
                continue

            # How far the thread root reaches past the drilled bore. A rough
            # figure by design: the minor diameter of a standard thread runs
            # about four fifths of nominal, so a tenth of the bore either
            # side is the right order without needing the thread form.
            cut_depth = diameter * thresholds.thread_cut_depth_ratio
            remaining = wall - cut_depth

            graded = self.graded(remaining, target, limit, "min")
            if graded is None:
                continue
            severity, threshold = graded

            designation = hole.param("thread_designation")
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{remaining:.2f} mm of wall",
                    self.render(
                        feedback,
                        severity,
                        remaining,
                        target,
                        limit,
                        "mm",
                        f"Tapping this {designation} hole leaves about "
                        f"{remaining:.2f} mm of wall beside it, against a "
                        f"{threshold:.2f} mm minimum: there is {wall:.2f} mm of "
                        f"material there now and the thread root takes another "
                        f"{cut_depth:.2f} mm of it. A wall that thin splits along "
                        "the hole when the fastener is pulled up, and it does it "
                        "in service rather than on the bench. Move the hole "
                        "inboard, or leave more stock around it.",
                    ),
                    faces=hole.faces,
                    value=remaining,
                    limit=threshold,
                    comparison="<",
                    unit="mm",
                )
            )
        return results

    @staticmethod
    def _nearest_wall(
        context: MachiningContext, hole: FeatureInstance, diameter: float
    ) -> Optional[float]:
        """Distance from the bore to the nearest planar surface beside it.

        Measured against every plane on the part, not just the outside of it,
        so a tapped hole sitting close to a pocket wall is caught as readily
        as one close to the edge. Planes square to the axis are skipped: they
        are the hole's own entry and floor, not walls.
        """
        bore = _bore_wall(context, hole)
        if bore is None:
            return None
        axis = bore.cyl_cone_axis.Direction()
        radius = diameter / 2.0
        cross_section = math.pi * bore.cyl_radius * bore.cyl_radius
        bore_face = context.face_index.face_at(bore.face_id)

        nearest: Optional[float] = None
        for node in sorted(
            context.graph.nodes_by_surface_type(SurfaceType.PLANE),
            key=lambda n: n.face_id,
        ):
            if hole.has_face(node.face_id):
                continue
            # A face smaller than the bore's own cross-section is not a wall.
            # It is a scrap of geometry -- the end cap of a modelled thread
            # groove is a square millimetre of plane sitting right beside the
            # bore -- and measuring to one invents a wall out of nothing.
            if node.area < cross_section:
                continue
            normal = node.outward_normal
            if normal is None or abs(axis.Dot(normal)) > _CAP_PLANE_ALIGNMENT:
                continue

            try:
                solver = BRepExtrema_DistShapeShape(
                    bore_face, context.face_index.face_at(node.face_id)
                )
                if not solver.IsDone() or solver.NbSolution() == 0:
                    continue
                distance = solver.Value()
            except Exception:
                continue

            if distance < _TANGENT_DISTANCE_MM:
                # The plane is touching the bore, so the face-to-face
                # distance says nothing. The wall is what is left between the
                # axis and the plane once the radius is taken off.
                offset = abs(
                    gp_Vec(bore.cyl_cone_axis.Location(), node.centroid).Dot(
                        gp_Vec(normal)
                    )
                )
                distance = offset - radius
                if distance <= 0.0:
                    continue

            if nearest is None or distance < nearest:
                nearest = distance
        return nearest


def _bbox_corners(node: AagNode) -> tuple[gp_Pnt, ...]:
    """The eight corners of a face's bounding box."""
    if node.bbox.IsVoid():
        return ()
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    return tuple(
        gp_Pnt(x, y, z)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
        for z in (zmin, zmax)
    )


def _within_bbox(node: AagNode, point: gp_Pnt) -> bool:
    """Whether a point falls inside a face's bounding box in plan.

    Used to ask whether the bore axis passes through a candidate entry face.
    The box of a face with a hole in it covers the hole, which is exactly the
    part of it the axis goes through.
    """
    if node.bbox.IsVoid():
        return False
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    return (
        xmin <= point.X() <= xmax
        and ymin <= point.Y() <= ymax
        and zmin <= point.Z() <= zmax
    )
