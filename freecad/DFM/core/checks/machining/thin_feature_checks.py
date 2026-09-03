# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Thin-wall detection.

The single highest-value geometric machining rule, and the one most prone to
false positives. Two faces being close together is not enough -- two walls of
a pocket are close and there is nothing between them. What matters is
material between the faces, and that is what the sign test below establishes.

Two passes. The first is planar: material between two opposed flat faces,
which is most of what a machinist means by a wall. The second is the ligament
a drill leaves between its bore and whatever it ran alongside -- the edge of
the part, a pocket wall, the flat of a boss. That one cannot be found by
looking at pairs of planes, because one side of it is round, and it is where
a part actually breaks: a hole half a millimetre from a wall tears out under
the drill long before a flat section of the same thickness gives trouble.
"""

from __future__ import annotations

import math

from typing import Iterator, Optional

from OCP.Bnd import Bnd_Box
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.TopAbs import TopAbs_OUT
from OCP.gp import gp_Pnt, gp_Vec

from ...machining.aag import AagNode, SurfaceType
from ...machining.context import MachiningContext
from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# The bores whose ligament to a wall is worth measuring. A tapped hole is
# left to the thread rule, which knows the root eats into the wall.
_BORE_WALL_TYPES = frozenset(
    {
        FeatureType.THROUGH_HOLE,
        FeatureType.BLIND_HOLE,
        FeatureType.COUNTERBORE,
        FeatureType.COUNTERSINK,
        # A half-open channel three quarters of a millimetre from a pocket
        # wall is as thin as a full bore would be. Wall physics is not drill
        # semantics -- only the drilling rules care that this is a part bore.
        FeatureType.PARTIAL_BORE,
        # A gasket groove running round a part has corner radii that come as
        # close to the edge as any hole, and the tube sheet has a quarter of
        # a millimetre left at them.
        FeatureType.GROOVE,
        FeatureType.O_RING_GLAND,
        FeatureType.RETAINING_RING_GROOVE,
    }
)

_GLAND_TYPES = frozenset(
    {
        FeatureType.GROOVE,
        FeatureType.O_RING_GLAND,
        FeatureType.RETAINING_RING_GROOVE,
    }
)

# How far from a bore's axis to bother looking for a wall, in bore radii and
# in multiples of the wall a shop cares about.
_BORE_SEARCH_RADII = 8.0
_BORE_SEARCH_WALLS = 4.0

# A face square to the bore's axis is its entry or exit, not a side wall.
_BORE_CAP_ALIGNMENT = 0.9

# A flat smaller than this many bore cross-sections, sitting against the
# bore, is a boolean sliver rather than a wall.
_BORE_SHOULDER_AREA_RATIO = 3.0

# Below this the surface-to-surface measurement has collapsed and the axis
# has to answer instead.
_BORE_DEGENERATE_MM = 0.01

# How closely an outer face lines up with an axis, and how much slack to
# allow when asking whether it sits on the part's envelope.
_CARDINAL_DOT = 0.99
_ENVELOPE_SLACK_MM = 0.1

# Past this share of the part's whole surface a face is the outside of the
# part rather than one side of a wall.
_EXTERIOR_AREA_FRACTION = 0.25

# The bores whose web against another bore is worth measuring. A threaded
# hole belongs here, unlike the wall pass: the thread rule speaks about the
# wall around one tapped hole, not about the web between two.
_WEB_BORE_TYPES = frozenset(
    {
        FeatureType.THROUGH_HOLE,
        FeatureType.BLIND_HOLE,
        FeatureType.COUNTERBORE,
        FeatureType.COUNTERSINK,
        FeatureType.THREADED_HOLE,
    }
)

# How closely two bores' axes have to agree to be read as parallel.
_WEB_PARALLEL_DOT = 0.95

# Axes nearer than this are the same axis: a counterbore and its pilot.
_WEB_COAXIAL_MM = 0.01

# Bores whose axes come this close, relative to their radii, have merged.
_WEB_MERGE_SLACK = 1.05

# How finely to walk two axis segments looking for their closest approach.
_WEB_AXIS_SAMPLES = 12

# What kind of wall a pass found. The grader reads it rather than guessing
# from the surface types, because a bore measured against a plane and two
# planes leaning together are different sentences to a machinist.
_OPPOSED = "opposed"
_BORE_WALL = "bore-wall"
_BORE_WEB = "bore-web"
_CONVERGING = "converging"

# Past this the two faces lean the same way and enclose nothing between them.
_CONVERGING_DOT_MAX = 0.3

# How far to step off each face towards its own centroid before walking the
# line between them, so the endpoints do not classify as on the surface.
_PATH_NUDGE = 0.3

# How squarely the line between two faces must run out of one and into the
# other before there is a wall between them rather than a corner beside them.
_WALL_ALIGNMENT_MIN = 0.3
_PATH_SAMPLES = 8

# Faces must be substantially opposed before they can bound a wall.
_ANTIPARALLEL_DOT_MAX = -0.8

# Bounding boxes must genuinely overlap in the two in-plane directions;
# a shared corner is not a wall.
_OVERLAP_MARGIN_MM = 0.1


def _distance(a: gp_Pnt, b: gp_Pnt) -> float:
    return a.Distance(b)


def _towards(point: gp_Pnt, anchor: gp_Pnt, fraction: float) -> gp_Pnt:
    """A point moved part of the way from where it is towards somewhere else."""
    return gp_Pnt(
        point.X() + fraction * (anchor.X() - point.X()),
        point.Y() + fraction * (anchor.Y() - point.Y()),
        point.Z() + fraction * (anchor.Z() - point.Z()),
    )


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
        muted = self._muted_faces(context)
        rib_pairs = self._rib_webs(context)

        # The planar pass, then the bores. Two different shapes of wall and
        # two different measurements, but one rule: what a machinist wants
        # to know is how much material is left, not which of our passes
        # happened to find it.
        walls = [
            (a, b, t, _OPPOSED) for a, b, t in self._opposed_planar_pairs(context)
        ]
        walls += [
            (a, b, t, _BORE_WALL)
            for a, b, t in self._bore_walls(context, target, limit)
        ]
        walls += [
            (a, b, t, _BORE_WEB) for a, b, t in self._bore_webs(context, target)
        ]
        walls += [
            (a, b, t, _CONVERGING)
            for a, b, t in self._converging_pairs(context, target)
        ]

        for first, second, thickness, kind in walls:
            pair = (min(first.face_id, second.face_id), max(first.face_id, second.face_id))
            if pair in reported:
                continue
            if first.face_id in muted or second.face_id in muted:
                continue
            if pair in rib_pairs:
                continue

            if kind is _BORE_WEB:
                verdict = self._grade_bore_web(thickness, target, limit)
            elif kind is _BORE_WALL:
                verdict = self._grade_bore_wall(thickness, target, limit)
            elif kind is _CONVERGING:
                verdict = self._grade_converging(thickness, target, limit)
            else:
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

    # -- suppression --------------------------------------------------------

    @staticmethod
    def _muted_faces(context) -> set[int]:
        """Faces whose thinness is a consequence rather than a choice.

        A tapped hole's wall thickness follows from the thread size, which
        the designer picked for the fastener, not for the wall. An engraved
        character is a few tenths of material between adjacent strokes, and a
        marked part carries hundreds of them -- reporting each one buries
        every finding worth reading.
        """
        muted: set[int] = set()
        for feature in context.recognition.of_type(
            FeatureType.THREADED_HOLE, FeatureType.MARKING_TEXT
        ):
            muted.update(feature.faces)
        return muted

    @staticmethod
    def _rib_webs(context) -> set[tuple[int, int]]:
        """The two web faces of each rib, as a pair.

        A rib is thin by definition -- that is what makes it a rib rather
        than a wall -- so its own two faces are not a finding. Only that pair
        is muted: a rib standing too close to something else still is.
        """
        pairs: set[tuple[int, int]] = set()
        for feature in context.recognition.of_type(FeatureType.RIB):
            webs = sorted(feature.faces[:2])
            if len(webs) == 2:
                pairs.add((webs[0], webs[1]))
        return pairs

    @staticmethod
    def _grade_bore_wall(
        thickness: float, target: float, limit: float
    ) -> tuple[Severity, float, str]:
        """A bore's ligament is thin or it is not; there is no aspect path.

        The broad-and-thin reading does not apply to a hole. A ligament is
        short by construction -- it is the gap between a bore and one face --
        so its length says nothing, and what matters is only how little is
        left.
        """
        if thickness <= limit:
            return (
                Severity.ERROR,
                limit,
                f"Only {thickness:.2f} mm of material is left between this bore "
                f"and the face beside it, under the {limit:.2f} mm floor. A "
                "ligament this thin tears out under the drill, and if it "
                "survives machining it will not survive the fastener.",
            )
        return (
            Severity.WARNING,
            target,
            f"There is {thickness:.2f} mm between this bore and the face beside "
            f"it, below the {target:.2f} mm target. Expect the wall to bulge as "
            "the drill passes and the hole to lose its position; consider "
            "moving the hole or drilling before the adjacent face is finished.",
        )

    @staticmethod
    def _grade_bore_web(
        thickness: float, target: float, limit: float
    ) -> tuple[Severity, float, str]:
        """How little is left between two holes."""
        if thickness <= limit:
            return (
                Severity.ERROR,
                limit,
                f"Only {thickness:.2f} mm of material separates these two bores, "
                f"under the {limit:.2f} mm floor. The second drill will push "
                "into the web the first one left, wander off position, and is "
                "likely to break through into the other hole.",
            )
        return (
            Severity.WARNING,
            target,
            f"There is {thickness:.2f} mm of web between these two bores, below "
            f"the {target:.2f} mm target. Drill both before finishing anything "
            "that depends on their position, and expect the second to pull "
            "towards the first.",
        )

    @staticmethod
    def _grade_converging(
        thickness: float, target: float, limit: float
    ) -> tuple[Severity, float, str]:
        """A wedge of material, measured where it is thinnest."""
        if thickness <= limit:
            return (
                Severity.ERROR,
                limit,
                f"These two faces close to {thickness:.2f} mm apart at their "
                f"nearest, under the {limit:.2f} mm floor. The wall between "
                "them tapers to nothing and will break out as the second face "
                "is cut.",
            )
        return (
            Severity.WARNING,
            target,
            f"These two faces converge to {thickness:.2f} mm apart, below the "
            f"{target:.2f} mm target. The wall is thinnest where they meet and "
            "will deflect there first; cut it last and lightly.",
        )

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

    # -- the wall a bore leaves ---------------------------------------------

    def _bore_walls(
        self, context: MachiningContext, target: float, limit: float
    ) -> Iterator[tuple[AagNode, AagNode, float]]:
        """Yield bore-and-plane pairs with a thin ligament between them.

        Measured surface to surface rather than centre to centre, because a
        bore drilled at an angle is closest to the face somewhere along its
        length and nowhere near where its axis happens to be anchored.

        Iterated per recognized hole rather than per cylinder: OpenCascade
        splits a bore that runs tangent to a face into several cylindrical
        fragments, and a machinist who is told three times about one hole
        stops reading.
        """
        planes = context.graph.nodes_by_surface_type(SurfaceType.PLANE)
        if not planes:
            return

        fragments = self._bore_shoulders(context)
        seen: set[tuple[int, int]] = set()

        for hole in context.recognition.of_type(*sorted(_BORE_WALL_TYPES)):
            # A tapped hole's wall is the thread rule's to speak about: the
            # root of the thread eats into it, so the raw geometric distance
            # overstates what is left.
            if hole.type == FeatureType.THREADED_HOLE:
                continue
            if hole.type in _GLAND_TYPES and hole.param("gland_shape") != "loop":
                continue

            bores = [
                context.graph.node(face_id)
                for face_id in hole.faces
                if context.graph.has_node(face_id)
                and context.graph.node(face_id).surface_type is SurfaceType.CYLINDER
            ]
            if not bores:
                continue

            primary = bores[0]
            axis = primary.cyl_cone_axis
            if axis is None or primary.cyl_radius <= 0.0:
                continue
            radius = primary.cyl_radius
            reach = radius * _BORE_SEARCH_RADII + target * _BORE_SEARCH_WALLS

            shoulders = self._bore_shoulders(context, bores)

            for plane in planes:
                if plane.face_id in fragments or plane.face_id in shoulders:
                    continue
                normal = plane.outward_normal
                if normal is None:
                    continue
                # A face square to the axis is the hole's own entry or exit,
                # not a wall running alongside it.
                if abs(axis.Direction().Dot(normal)) > _BORE_CAP_ALIGNMENT:
                    continue
                if plane.centroid.Distance(axis.Location()) > reach:
                    continue
                if plane.bbox.Distance(primary.bbox) >= target:
                    continue

                pair = (min(primary.face_id, plane.face_id),
                        max(primary.face_id, plane.face_id))
                if pair in seen:
                    continue

                thickness = self._bore_to_plane(context, bores, plane, axis, radius)
                if thickness is None or thickness >= target:
                    continue
                if self._on_the_outside(context, plane, normal) and thickness >= limit:
                    # A hole this close to the outside of the part is what
                    # the edge-distance rule measures, and it says it better.
                    # Below the error floor this one keeps it, because that
                    # rule has no way to call anything critical.
                    continue

                seen.add(pair)
                yield primary, plane, thickness

    def _bore_shoulders(
        self, context: MachiningContext, bores=None
    ) -> set[int]:
        """Small flats sitting against a bore, which are not walls.

        A boolean cut leaves slivers where a bore breaks a surface or crosses
        another bore. They are adjacent to the cylinder and a fraction of a
        millimetre from it, so every one of them reads as a critically thin
        wall, and none of them is anything at all.

        Collected across every hole on the part when asked for the whole set:
        a sliver left by one bore is just as meaningless measured against the
        next one along.
        """
        shoulders: set[int] = set()
        if bores is None:
            candidates = []
            for hole in context.recognition.of_type(*sorted(_BORE_WALL_TYPES)):
                if hole.type == FeatureType.THREADED_HOLE:
                    continue
                candidates.extend(
                    context.graph.node(face_id)
                    for face_id in hole.faces
                    if context.graph.has_node(face_id)
                    and context.graph.node(face_id).surface_type
                    is SurfaceType.CYLINDER
                )
        else:
            candidates = list(bores)

        for bore in candidates:
            if bore.cyl_radius <= 0.0:
                continue
            section = math.pi * bore.cyl_radius * bore.cyl_radius
            for edge in context.graph.concave_edges_of(bore.face_id):
                other = edge.other_face(bore.face_id)
                if not context.graph.has_node(other):
                    continue
                neighbour = context.graph.node(other)
                if (
                    neighbour.surface_type is SurfaceType.PLANE
                    and neighbour.area < section * _BORE_SHOULDER_AREA_RATIO
                ):
                    shoulders.add(other)
        return shoulders

    def _bore_to_plane(
        self, context: MachiningContext, bores, plane, axis, radius: float
    ) -> Optional[float]:
        """How much material lies between a bore and a face."""
        face = self._face_of(context, plane.face_id)
        if face is None:
            return None

        closest = None
        for bore in bores:
            wall = self._face_of(context, bore.face_id)
            if wall is None:
                continue
            try:
                solver = BRepExtrema_DistShapeShape(wall, face)
                if not solver.IsDone() or solver.NbSolution() < 1:
                    continue
                measured = solver.Value()
            except Exception:
                continue
            if closest is None or measured < closest:
                closest = measured

        if closest is None:
            return None
        if closest >= _BORE_DEGENERATE_MM:
            return closest

        # Surface to surface came back as zero, which happens when the bore
        # runs tangent to the face and when a boolean has left the two
        # touching. Fall back to the axis: the distance from the axis to the
        # plane, less the radius, is the ligament. Sampled at both ends of
        # the bore because a hole drilled at an angle is nearer the face at
        # one end than the other.
        normal = plane.outward_normal
        if normal is None:
            return None
        span = self._axis_to_plane(bores[0], axis, plane, normal)
        if span is None:
            return None
        thickness = span - radius
        return thickness if thickness > _BORE_DEGENERATE_MM else None

    @staticmethod
    def _axis_to_plane(bore, axis, plane, normal) -> Optional[float]:
        """The nearest approach of a bore's axis segment to a plane."""
        if bore.bbox.IsVoid():
            return None
        xmin, ymin, zmin, xmax, ymax, zmax = bore.bbox.Get()
        origin = axis.Location()
        direction = gp_Vec(axis.Direction())
        low, high = math.inf, -math.inf
        for x in (xmin, xmax):
            for y in (ymin, ymax):
                for z in (zmin, zmax):
                    along = gp_Vec(origin, gp_Pnt(x, y, z)).Dot(direction)
                    low = min(low, along)
                    high = max(high, along)
        if low is math.inf:
            return None

        offset = gp_Vec(normal)
        anchor = gp_Vec(plane.centroid.XYZ())
        nearest = None
        for along in (low, high):
            point = gp_Vec(origin.XYZ()) + direction.Multiplied(along)
            distance = abs((point - anchor).Dot(offset))
            if nearest is None or distance < nearest:
                nearest = distance
        return nearest

    @staticmethod
    def _faces_of(context: MachiningContext, *types: str) -> set[int]:
        """Every face claimed by any feature of the given types."""
        faces: set[int] = set()
        for feature in context.recognition.of_type(*types):
            faces.update(feature.faces)
        return faces

    @staticmethod
    def _face_of(context: MachiningContext, face_id: int):
        try:
            return context.face_index.face_at(face_id)
        except Exception:
            return None

    @staticmethod
    def _on_the_outside(context: MachiningContext, plane, normal) -> bool:
        """Whether a face sits on the outside of the part, square to an axis.

        Only such a face is one the edge-distance rule can see: it measures
        against the part's bounding box, so a tilted outer face or an
        interior pocket wall is invisible to it and this rule keeps them.
        """
        largest = max(abs(normal.X()), abs(normal.Y()), abs(normal.Z()))
        if largest <= _CARDINAL_DOT:
            return False
        box = Bnd_Box()
        for node in context.graph.nodes:
            if not node.bbox.IsVoid():
                box.Add(node.bbox)
        if box.IsVoid():
            return False
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        position = (
            plane.centroid.X() * normal.X()
            + plane.centroid.Y() * normal.Y()
            + plane.centroid.Z() * normal.Z()
        )
        envelope = (
            max(xmin * normal.X(), xmax * normal.X())
            + max(ymin * normal.Y(), ymax * normal.Y())
            + max(zmin * normal.Z(), zmax * normal.Z())
        )
        return position >= envelope - _ENVELOPE_SLACK_MM


    # -- the web between two bores ------------------------------------------

    def _bore_webs(
        self, context: MachiningContext, target: float
    ) -> Iterator[tuple[AagNode, AagNode, float]]:
        """Yield bore pairs with a thin web of material between them.

        Two holes drilled close together leave a web, and it is the first
        thing to break: the second drill pushes into the wall the first one
        left, wanders off position, and often breaks through. A machinist
        would rather be told about it than find out on the second op.

        Neither side of this is planar, so the planar pass cannot see it at
        all, and it is what the shop's own calibration parts are cut to test.
        """
        cylinders = context.graph.nodes_by_surface_type(SurfaceType.CYLINDER)
        if len(cylinders) < 2:
            return

        marking = self._faces_of(context, FeatureType.MARKING_TEXT)
        fillets = self._faces_of(context, FeatureType.FILLET)
        bores = self._faces_of(context, *sorted(_WEB_BORE_TYPES))

        for index, first in enumerate(cylinders):
            if first.face_id in marking or first.cyl_cone_axis is None:
                continue
            for second in cylinders[index + 1:]:
                if second.face_id in marking or second.cyl_cone_axis is None:
                    continue
                # Two rounded edges are not two holes, and the gap between
                # them is not a wall anybody has to make.
                if first.face_id in fillets and second.face_id in fillets:
                    continue

                span = first.cyl_radius + second.cyl_radius + target * 2.0
                if first.centroid.Distance(second.centroid) > span:
                    continue
                if first.bbox.Distance(second.bbox) >= target:
                    continue

                axis_a = first.cyl_cone_axis.Direction()
                axis_b = second.cyl_cone_axis.Direction()
                parallel = abs(axis_a.Dot(axis_b)) > _WEB_PARALLEL_DOT

                # A fillet running across a bore rather than alongside it is
                # an edge treatment the bore happens to meet, not a web.
                a_fillet = first.face_id in fillets
                b_fillet = second.face_id in fillets
                if a_fillet != b_fillet and not parallel:
                    continue

                if parallel and self._nested_bores(first, second, target):
                    continue

                a_bore = first.face_id in bores
                b_bore = second.face_id in bores
                # Two bores whose axes come within the sum of their radii
                # have merged into one cavity. The intersecting-hole rule
                # reports that, and whatever distance the solver returns
                # across the tangent region is arithmetic, not material.
                if a_bore and b_bore and self._axes_merge(first, second):
                    continue

                thickness = self._surface_gap(context, first, second)
                if thickness is None or thickness >= target:
                    continue
                if (
                    a_bore != b_bore
                    and parallel
                    and self._bore_inside_a_boss(first, second, a_bore)
                ):
                    continue

                yield first, second, thickness

    @staticmethod
    def _nested_bores(first, second, target: float) -> bool:
        """Whether two cylinders are one counterbored hole seen twice.

        A counterbore and its pilot share an axis, and the step between them
        is a shoulder rather than a wall. Only when they are truly coaxial:
        two parallel bores side by side are a web and do count.
        """
        offset = gp_Vec(
            first.cyl_cone_axis.Location(), second.cyl_cone_axis.Location()
        )
        separation = offset.Crossed(gp_Vec(first.cyl_cone_axis.Direction())).Magnitude()
        if separation >= _WEB_COAXIAL_MM:
            return False
        return abs(first.cyl_radius - second.cyl_radius) <= target

    def _axes_merge(self, first, second) -> bool:
        """Whether two bores have run into each other."""
        distance = self._axis_gap(first, second)
        if distance is None:
            return False
        return distance <= (first.cyl_radius + second.cyl_radius) * _WEB_MERGE_SLACK

    @staticmethod
    def _bore_inside_a_boss(first, second, first_is_bore: bool) -> bool:
        """Whether a bore lies wholly within the round stock around it.

        A hole up the middle of a boss is not close to the boss's outside --
        it is inside it, and the wall is the whole annulus. Only asked of an
        outward-facing partner, because an inward-facing one is another bore.
        """
        bore, partner = (first, second) if first_is_bore else (second, first)
        if partner.is_reversed:
            return False
        offset = gp_Vec(
            partner.cyl_cone_axis.Location(), bore.cyl_cone_axis.Location()
        )
        separation = offset.Crossed(
            gp_Vec(partner.cyl_cone_axis.Direction())
        ).Magnitude()
        return separation + bore.cyl_radius < partner.cyl_radius

    def _surface_gap(self, context, first, second) -> Optional[float]:
        """The nearest approach of two faces, measured surface to surface."""
        a = self._face_of(context, first.face_id)
        b = self._face_of(context, second.face_id)
        if a is None or b is None:
            return None
        try:
            solver = BRepExtrema_DistShapeShape(a, b)
            if not solver.IsDone() or solver.NbSolution() < 1:
                return None
            measured = solver.Value()
        except Exception:
            return None
        return measured if measured >= _BORE_DEGENERATE_MM else None

    @staticmethod
    def _axis_gap(first, second) -> Optional[float]:
        """The closest approach of two bores' axis segments.

        Segment to segment rather than line to line: two holes drilled from
        opposite faces can have axes that would cross if extended and never
        come near each other inside the part.
        """
        segments = []
        for node in (first, second):
            if node.bbox.IsVoid() or node.cyl_cone_axis is None:
                return None
            xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
            anchor = node.cyl_cone_axis.Location()
            direction = gp_Vec(node.cyl_cone_axis.Direction())
            low, high = math.inf, -math.inf
            for x in (xmin, xmax):
                for y in (ymin, ymax):
                    for z in (zmin, zmax):
                        along = gp_Vec(anchor, gp_Pnt(x, y, z)).Dot(direction)
                        low = min(low, along)
                        high = max(high, along)
            segments.append((gp_Vec(anchor.XYZ()), direction, low, high))

        (origin_a, dir_a, low_a, high_a) = segments[0]
        (origin_b, dir_b, low_b, high_b) = segments[1]
        # Sampled rather than solved. The exact segment-to-segment minimum is
        # a short piece of algebra with several degenerate cases, and this is
        # a gate on a measurement the solver makes properly afterwards -- so
        # a coarse walk along both segments is accurate enough and has no
        # special cases to get wrong.
        best = math.inf
        for i in range(_WEB_AXIS_SAMPLES + 1):
            pa = origin_a + dir_a.Multiplied(
                low_a + (high_a - low_a) * i / _WEB_AXIS_SAMPLES
            )
            for j in range(_WEB_AXIS_SAMPLES + 1):
                pb = origin_b + dir_b.Multiplied(
                    low_b + (high_b - low_b) * j / _WEB_AXIS_SAMPLES
                )
                best = min(best, (pa - pb).Magnitude())
        return None if best is math.inf else best



    # -- walls between faces that lean together -----------------------------

    def _converging_pairs(
        self, context: MachiningContext, target: float
    ) -> Iterator[tuple[AagNode, AagNode, float]]:
        """Yield faces that are not opposed but still pinch a wall between them.

        The planar pass looks for faces that front each other squarely. Two
        faces that lean towards each other bound a wall too -- it is just a
        wedge rather than a slab, and it is thinnest where they come closest.
        An angled pocket beside a straight one, a V-groove flank against the
        next flank along, a chamfer running into a wall.

        Measured surface to surface, because the closest approach of two
        tilted faces is not at either centroid and there is no useful
        formula for where it is.
        """
        planes = self._wall_candidates(context)
        if len(planes) < 2:
            return

        muted = self._muted_faces(context)
        blends = self._faces_of(context, FeatureType.FILLET, FeatureType.CHAMFER)
        classifier = BRepClass3d_SolidClassifier(context.shape)

        for index, first in enumerate(planes):
            normal_a = first.outward_normal
            if normal_a is None or first.face_id in muted:
                continue
            for second in planes[index + 1:]:
                normal_b = second.outward_normal
                if normal_b is None or second.face_id in muted:
                    continue

                alignment = normal_a.Dot(normal_b)
                # Below this the faces are opposed, which the planar pass
                # already measured properly; above it they lean the same way
                # and enclose nothing.
                if alignment < _ANTIPARALLEL_DOT_MAX or alignment > _CONVERGING_DOT_MAX:
                    continue
                # Cheap first. Two bounding boxes further apart than the wall
                # we care about cannot have a thin wall between them, and
                # asking the kernel for an exact surface distance is by far
                # the most expensive thing in this rule.
                if first.bbox.Distance(second.bbox) >= target:
                    continue
                if self._share_an_edge(context, first, second):
                    continue
                # Two faces meeting through a fillet are not converging in
                # any sense a machinist would recognize -- their closest
                # approach is just the blend's own radius.
                if self._meet_through_a_blend(context, first, second, blends):
                    continue

                closest = self._closest_points(context, first, second)
                if closest is None:
                    continue
                start, end = closest
                thickness = _distance(start, end)
                if thickness < _BORE_DEGENERATE_MM or thickness >= target:
                    continue
                # The two faces have to be on opposite sides of the same
                # material. Walking off the first face into what it fronts
                # must arrive at the second, and arrive at its front. A pair
                # meeting round an L-corner fails this: the line between them
                # runs sideways, along neither face's normal, and there is no
                # continuous wall there to be thin.
                if not self._face_each_other(start, end, normal_a, normal_b):
                    continue
                if self._path_leaves_the_solid(
                    classifier, start, end, first.centroid, second.centroid
                ):
                    continue

                yield first, second, thickness

    @staticmethod
    def _share_an_edge(context, first, second) -> bool:
        return any(
            edge.other_face(first.face_id) == second.face_id
            for edge in context.graph.edges_of(first.face_id)
        )

    @staticmethod
    def _meet_through_a_blend(context, first, second, blends: set[int]) -> bool:
        """Whether the two faces are joined by a fillet or chamfer between them."""
        if not blends:
            return False
        near_first = {
            edge.other_face(first.face_id)
            for edge in context.graph.edges_of(first.face_id)
        } & blends
        if not near_first:
            return False
        near_second = {
            edge.other_face(second.face_id)
            for edge in context.graph.edges_of(second.face_id)
        } & blends
        return bool(near_first & near_second)

    @staticmethod
    def _face_each_other(start: gp_Pnt, end: gp_Pnt, normal_a, normal_b) -> bool:
        """Whether the line between two faces runs from the back of one to
        the front of the other, which is what having a wall between them
        means."""
        along = gp_Vec(start, end)
        if along.Magnitude() < 1.0e-9:
            return False
        along.Normalize()
        into_a = -along.Dot(gp_Vec(normal_a))
        onto_b = along.Dot(gp_Vec(normal_b))
        return into_a >= _WALL_ALIGNMENT_MIN and onto_b >= _WALL_ALIGNMENT_MIN

    def _path_leaves_the_solid(self, classifier, start, end, anchor_a, anchor_b) -> bool:
        """Whether the line between two faces runs outside the part.

        The solver measures a straight line and does not care what it passes
        through. On an array of V-grooves the flanks of the first and third
        groove are a pitch and a half apart, and the line between them runs
        through the void of the second -- a wall that does not exist. Walked
        with the solid classifier, nudged off both surfaces first so the
        endpoints do not read as ON.
        """
        start = _towards(start, anchor_a, _PATH_NUDGE)
        end = _towards(end, anchor_b, _PATH_NUDGE)

        for step in range(1, _PATH_SAMPLES):
            fraction = step / _PATH_SAMPLES
            sample = gp_Pnt(
                start.X() + (end.X() - start.X()) * fraction,
                start.Y() + (end.Y() - start.Y()) * fraction,
                start.Z() + (end.Z() - start.Z()) * fraction,
            )
            try:
                classifier.Perform(sample, 1.0e-3)
                if classifier.State() == TopAbs_OUT:
                    return True
            except Exception:
                return False
        return False

    def _closest_points(self, context, first, second):
        """Where two faces come nearest each other."""
        a = self._face_of(context, first.face_id)
        b = self._face_of(context, second.face_id)
        if a is None or b is None:
            return None
        try:
            solver = BRepExtrema_DistShapeShape(a, b)
            if not solver.IsDone() or solver.NbSolution() < 1:
                return None
            return solver.PointOnShape1(1), solver.PointOnShape2(1)
        except Exception:
            return None


    def _wall_candidates(self, context: MachiningContext) -> list[AagNode]:
        """The planar faces that could be the side of a wall.

        A face that is a large fraction of the whole part is the outside of
        it, not the side of a wall. Left in, the back of a lightweight panel
        pairs with every cell floor above it and reports the one skin once
        per cell -- twelve findings that are all the same millimetre.
        """
        planes = [
            node
            for node in context.graph.nodes
            if node.surface_type is SurfaceType.PLANE
        ]
        total_area = sum(node.area for node in context.graph.nodes)
        if total_area <= 0.0:
            return planes
        return [
            node for node in planes if node.area <= total_area * _EXTERIOR_AREA_FRACTION
        ]

    # -- geometry -----------------------------------------------------------

    def _opposed_planar_pairs(
        self, context: MachiningContext
    ) -> Iterator[tuple[AagNode, AagNode, float]]:
        """Yield planar face pairs with material between them, and how much."""
        planes = self._wall_candidates(context)
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
