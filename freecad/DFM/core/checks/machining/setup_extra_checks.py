# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about how many times the part has to be picked up and put down.

Setups are the hidden cost. A shop quoting a part counts them before it
counts anything else, because each one is a fixture, an alignment, a probing
cycle and a fresh chance to get it wrong -- and none of that shows up in the
geometry as a defect. Every rule here is about that count rather than about
any feature being wrong.

The count is estimated by clustering approach directions. Every feature has
one -- the way the tool has to come at it -- and features sharing a direction
can be cut in the same setup. How many distinct directions are left is how
many times the part gets moved.
"""

from __future__ import annotations

import math

from OCP.gp import gp_Dir

from ...machining.aag import SurfaceType
from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


#: The six directions a three-axis machine can approach from.
_CARDINALS = (
    gp_Dir(1, 0, 0),
    gp_Dir(-1, 0, 0),
    gp_Dir(0, 1, 0),
    gp_Dir(0, -1, 0),
    gp_Dir(0, 0, 1),
    gp_Dir(0, 0, -1),
)

# Two faces count as perpendicular within about six degrees, which is as
# square as a sawn billet face is going to be.
_PERPENDICULAR_TOL = 0.1

# Feature types that carry a real approach direction. A fillet or a chamfer
# is cut by whatever tool made the feature it runs along, so it costs no
# setup of its own.
_COSTS_A_SETUP = frozenset(
    {
        FeatureType.THROUGH_HOLE,
        FeatureType.BLIND_HOLE,
        FeatureType.COUNTERBORE,
        FeatureType.COUNTERSINK,
        FeatureType.THREADED_HOLE,
        FeatureType.PARTIAL_BORE,
        FeatureType.POCKET,
        FeatureType.SLOT,
        FeatureType.CHANNEL,
        FeatureType.THROUGH_CAVITY,
        FeatureType.SPHERICAL_POCKET,
        FeatureType.BOSS,
        FeatureType.STEP,
        FeatureType.GROOVE,
        FeatureType.O_RING_GLAND,
        FeatureType.RETAINING_RING_GROOVE,
        FeatureType.THREAD_RELIEF_GROOVE,
        FeatureType.EXTERNAL_THREAD,
        FeatureType.FLEXURE_SLIT,
        FeatureType.BROACHED_SLOT,
    }
)


@register_check(Rulebook.SETUP_COUNT_HIGH)
class SetupCountCheck(MachiningCheck):
    """How many times the part has to be refixtured.

    Each setup is a fixture, an alignment and a probing cycle, and none of it
    appears in the geometry as a defect. Counted by clustering the approach
    directions of every feature that needs one: features sharing a direction
    come off in the same setup, and what is left is how often the part moves.
    """

    @property
    def name(self) -> str:
        return "Setup Count Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None:
            target = float(thresholds.setup_count_warn)
        if limit is None:
            limit = float(thresholds.setup_count_error)
        cluster_deg = thresholds.tad_angular_cluster_deg

        directions = self._approach_directions(context)
        if not directions:
            return []

        # A part always takes at least one setup, whatever the clustering
        # says: the stock has to be held to be cut at all.
        setups = max(1, _cluster(directions, cluster_deg))
        count = float(setups)

        graded = self.graded(count, target, limit, "max")
        if graded is not None:
            severity, threshold = graded
            advice = (
                "Bringing features onto shared faces, or accepting a fourth "
                "axis, is what reduces this."
            )
        elif setups >= thresholds.setup_count_info_min:
            # Two setups is routine -- almost every turned part needs a
            # sub-spindle hand-off and many milled parts need a flip for the
            # second face. It is still the single biggest thing the geometry
            # says about what the part costs, and an estimator who has to
            # count approach directions by eye will get it wrong. Said
            # plainly, without alarm.
            severity = Severity.INFO
            threshold = target
            advice = "That is ordinary for a part of this shape."
        else:
            return []

        return [
            self.finding(
                rule,
                severity,
                f"{setups} setups",
                self.render(
                    feedback,
                    severity,
                    count,
                    target,
                    limit,
                    "",
                    f"The features on this part are approached from {setups} "
                    "distinct directions, so it has to be refixtured that many "
                    "times. Each pickup is a fixture, an alignment and a "
                    "probing cycle, and every one of them puts the previous "
                    f"setup's tolerances at risk. {advice}",
                ),
                faces=[],
                value=count,
                limit=threshold,
                comparison=">",
            )
        ]

    @staticmethod
    def _approach_directions(context) -> list[tuple[gp_Dir, bool]]:
        """Where the tool comes from, and whether it could come from the other side.

        Each direction carries whether it is reversible. A through hole is
        not: it is drilled from whichever end is convenient, and which end is
        a fixturing decision rather than a fact about the hole. A blind hole
        is -- there is one end you can drill it from, and no amount of
        fixturing changes that. So is a cavity, which is reached from the
        side its floor faces, and so is anything applied onto a face.

        Getting this wrong in the permissive direction is what made a
        compressor disk with work on both faces read as a single setup.
        """
        directions: list[tuple[gp_Dir, bool]] = []
        for feature in context.recognition.features:
            if feature.type not in _COSTS_A_SETUP:
                continue

            # A cavity is reached from the side its floor points to, and that
            # side only.
            floor = feature.direction("floor_normal")
            if floor is not None:
                directions.append((floor, True))
                continue

            # A feature applied onto a face -- engraving, marking -- can only
            # be approached from the outward side of the face carrying it.
            host = SetupCountCheck._host_face_normal(context, feature)
            if host is not None:
                directions.append((host, True))
                continue

            axis = feature.direction("axis")
            if axis is None:
                axis = feature.direction("normal")
            if axis is not None:
                # A bore is reversible unless something closes one end.
                directions.append((axis, bool(feature.param("axis_signed"))))
        return directions

    @staticmethod
    def _host_face_normal(context, feature):
        """The outward normal of the face a feature is applied to."""
        host = feature.parameters.get("host_face")
        if host is None:
            return None
        try:
            face_id = int(host)
        except (TypeError, ValueError):
            return None
        if not context.graph.has_node(face_id):
            return None
        node = context.graph.node(face_id)
        if node.surface_type is not SurfaceType.PLANE:
            return None
        return node.outward_normal


@register_check(Rulebook.NO_ORTHOGONAL_DATUM_TRIO)
class NoOrthogonalDatumTrioCheck(MachiningCheck):
    """No three square faces to locate the part from.

    Three-two-one location wants three mutually perpendicular surfaces: one
    to sit on, one to push against, one to stop against. Without them the
    part has to be located off something else -- a sacrificial pad, a
    soft-jaw form, a fixture built for this job -- and that is a cost and a
    lead time nobody costed.

    Only asked in precision mode. On ordinary work a part that clamps
    adequately in a vise does not need a formal datum scheme, and demanding
    one would fire on almost every part.
    """

    @property
    def name(self) -> str:
        return "Orthogonal Datum Trio Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        if not context.config.precision_mode:
            return []

        minimum_area = context.config.thresholds.datum_face_min_area_mm2
        normals = [
            node.outward_normal
            for node in context.graph.nodes
            if node.surface_type is SurfaceType.PLANE
            and node.convex_neighbor_count > 0
            and node.area >= minimum_area
            and node.outward_normal is not None
        ]

        if _has_orthogonal_trio(normals):
            return []

        return [
            self.finding(
                rule,
                Severity.WARNING,
                f"{len(normals)} usable faces",
                self.render(
                    feedback,
                    Severity.WARNING,
                    float(len(normals)),
                    3.0,
                    3.0,
                    "",
                    "No three mutually square faces on this part are large "
                    f"enough ({minimum_area:.0f} mm2) to locate from. "
                    "Three-two-one location wants one face to sit on, one to "
                    "push against and one to stop against; without them the "
                    "part needs a sacrificial pad or a purpose-built fixture, "
                    "which is a cost and a lead time that will not have been "
                    "quoted.",
                ),
                faces=[],
                value=float(len(normals)),
                limit=3.0,
                comparison="<",
            )
        ]


@register_check(Rulebook.TOOL_ACCESS_SPECIAL_SETUP)
class ToolAccessSpecialSetupCheck(MachiningCheck):
    """A feature reachable only from an angle the machine cannot index to.

    Off-cardinal geometry is not unmachinable, it is inconvenient: the part
    has to be tipped in a fixture, or the shop needs a fourth axis. Saying so
    lets it be priced, which is different from the access rule that reports
    geometry nothing can reach at all.

    Only asked of a three-axis shop. A machine that can index has no trouble
    with any of this, which is exactly why shops buy them.
    """

    @property
    def name(self) -> str:
        return "Tool Access Special Setup Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        if context.config.machine_mode != "3axis":
            return []

        cluster_deg = context.config.thresholds.tad_angular_cluster_deg
        tolerance = math.cos(math.radians(cluster_deg))

        results: list[CheckResult] = []
        for feature in context.recognition.features:
            if feature.type not in _COSTS_A_SETUP:
                continue
            axis = feature.direction("axis")
            if axis is None or _is_cardinal(axis, tolerance):
                continue

            tilt = math.degrees(math.acos(min(1.0, _best_cardinal_dot(axis))))
            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{tilt:.0f} deg off axis",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        tilt,
                        0.0,
                        0.0,
                        "deg",
                        f"This {feature.type.replace('_', ' ').lower()} lies "
                        f"{tilt:.0f} degrees off every direction a three-axis "
                        "machine can approach from. It is not unmachinable -- "
                        "the part gets tipped in a fixture, or it goes on a "
                        "machine that can index -- but that is an extra setup "
                        "and a fixture to build, and neither will have been "
                        "in the quote.",
                    ),
                    faces=feature.faces,
                    value=tilt,
                    limit=0.0,
                    comparison=">",
                    unit="deg",
                )
            )
        return results


# =============================================================================
# Direction arithmetic
# =============================================================================


def _cluster(directions: list[tuple[gp_Dir, bool]], cluster_deg: float) -> int:
    """How many times the part has to be refixtured.

    Greedy: each direction joins the first cluster it can. Order-dependent in
    principle, but the graph hands back faces in a fixed order, so the answer
    is stable for a given part.

    What decides whether two directions are one setup is whether either can
    be reversed. Two features that can each only be reached from one side are
    the same setup only if that side is the same side -- top and bottom are
    two. A feature that can be reached from either end joins whichever
    cluster it is parallel to, in either sense, and once a one-sided feature
    joins it the cluster is one-sided too, pointing the way that feature
    needs.
    """
    tolerance = math.cos(math.radians(cluster_deg))
    clusters: list[list] = []  # [direction, fixed]

    for direction, fixed in directions:
        for cluster in clusters:
            existing, existing_fixed = cluster
            alignment = direction.Dot(existing)
            if not (fixed and existing_fixed):
                alignment = abs(alignment)
            if alignment > tolerance:
                if fixed and not existing_fixed:
                    # The cluster was free to be approached either way and
                    # is not any more.
                    if direction.Dot(existing) < 0.0:
                        existing.Reverse()
                    cluster[1] = True
                break
        else:
            clusters.append([gp_Dir(direction.XYZ()), fixed])
    return len(clusters)


def _best_cardinal_dot(axis: gp_Dir) -> float:
    return max(abs(axis.Dot(cardinal)) for cardinal in _CARDINALS)


def _is_cardinal(axis: gp_Dir, tolerance: float) -> bool:
    return _best_cardinal_dot(axis) > tolerance


def _has_orthogonal_trio(normals) -> bool:
    """Whether three of these faces are mutually square."""
    count = len(normals)
    if count < 3:
        return False
    for i in range(count):
        for j in range(i + 1, count):
            if abs(normals[i].Dot(normals[j])) > _PERPENDICULAR_TOL:
                continue
            for k in range(j + 1, count):
                if abs(normals[i].Dot(normals[k])) > _PERPENDICULAR_TOL:
                    continue
                if abs(normals[j].Dot(normals[k])) <= _PERPENDICULAR_TOL:
                    return True
    return False
