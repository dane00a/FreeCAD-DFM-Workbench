# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about the part as a whole rather than about one feature.

Three quite different concerns share this module because they share a
question: is the shop equipped to make this at all.

The smallest detail on the part has to be bigger than the smallest tool in
the shop. A square inside corner has to be cut by something that is not
round, which means a second process. And the number of distinct features is
what actually sets the programming time, whatever the individual limits say.

Most of the code here is suppression, and that is the point. `sharp_internal
edge` in particular will fire on the rim of every cavity on the part if left
alone -- a pocket wall meets its host face at a sharp concave corner by
construction, and so does a bore, and so does the base of every boss. All of
those are already reported by the rule that owns the feature. What is left
after the suppressions is the corner nobody else is speaking for.
"""

from __future__ import annotations

import math

from ...machining.aag import Concavity, SurfaceType
from .corner_access import (
    ShapeProbe,
    is_cutter_formed,
    is_reachable,
    second_smallest_extent,
)
from ...machining.features import BORE_TYPES, FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# Dimensions that describe how big a feature is. Height is deliberately
# absent: a plateau a tenth of a millimetre tall is trivially machinable by
# facing the surround down. What is not machinable is forming its base
# corners at that scale, and the sharp-edge rule reports those.
_SIZE_PARAMETERS = (
    "diameter_mm",
    "width_mm",
    "min_width_mm",
    "depth_mm",
    "radius_mm",
)

# Feature types whose characteristic size is set by their process rather than
# by an end mill, so measuring them against a milling floor says nothing.
# Engraving is a few tenths by definition; a flexure slit is a saw kerf or an
# EDM wire; a V-groove is a dressing wheel. Fillets and chamfers are edge
# treatments, and their lower bound is the cutter-radius rules' business.
_SIZE_EXEMPT = frozenset(
    {
        FeatureType.MARKING_TEXT,
        FeatureType.FLEXURE_SLIT,
        FeatureType.V_GROOVE,
        FeatureType.FILLET,
        FeatureType.CHAMFER,
    }
)

# Features whose own rules already speak for the corners inside them.
#
# The slit family and V-grooves belong here even though their corners really
# are square: those corners are the *shape of the process* -- a broach
# profile, a saw kerf, a dressing wheel -- and the rule that reports the
# process speaks for them. Without this a part with forty-eight broached
# dovetails reports ninety-six square corners, every one of them correct and
# every one of them useless.
_OWNS_ITS_CORNERS = frozenset(
    {
        FeatureType.POCKET,
        FeatureType.SLOT,
        FeatureType.CHANNEL,
        FeatureType.THROUGH_CAVITY,
        FeatureType.UNDERCUT,
        FeatureType.SPHERICAL_POCKET,
        FeatureType.FLEXURE_SLIT,
        FeatureType.BROACHED_SLOT,
        FeatureType.V_GROOVE,
        FeatureType.STEP,
        FeatureType.RIB,
        FeatureType.BOSS,
        FeatureType.GROOVE,
        FeatureType.THREAD_RELIEF_GROOVE,
        FeatureType.O_RING_GLAND,
        FeatureType.RETAINING_RING_GROOVE,
        FeatureType.EXTERNAL_THREAD,
        FeatureType.MARKING_TEXT,
    }
    | BORE_TYPES
)

# A step is an external terrace, not a cavity. Its edge down into whatever
# lies below is that thing's opening rim rather than the step's own corner,
# so a step counts as bulk material for the rim test even though its own
# rule speaks for its corners.
_CARRIES_ITS_RIM = _OWNS_ITS_CORNERS - {FeatureType.STEP}

# Cavities whose walls the recognizer may not have fully absorbed. One hop
# out along a concave edge finds the walls it missed.
_POCKET_LIKE = frozenset(
    {
        FeatureType.POCKET,
        FeatureType.SLOT,
        FeatureType.CHANNEL,
        FeatureType.THROUGH_CAVITY,
        FeatureType.UNDERCUT,
        FeatureType.SPHERICAL_POCKET,
        FeatureType.FLEXURE_SLIT,
        FeatureType.BROACHED_SLOT,
        FeatureType.V_GROOVE,
    }
)

# A face counts as turned when it lines up with the part's rotation axis.
_TURNED_ALIGNMENT = 0.95

# The band a right angle falls in, give or take, in radians of deviation
# from flat. Used to spot the arc where a corner radius meets its floor.
_RIGHT_ANGLE_LOW = 1.3090
_RIGHT_ANGLE_HIGH = 1.8326


@register_check(Rulebook.MINIMUM_FEATURE_SIZE)
class MinimumFeatureSizeCheck(MachiningCheck):
    """Detail smaller than the shop's smallest tool can cut.

    Below the floor the feature is not expensive, it is impossible on a mill:
    it needs EDM, or a micro-machining shop, or a change to the drawing.
    """

    @property
    def name(self) -> str:
        return "Minimum Feature Size Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.minimum_feature_size_mm
        if limit <= 0.0:
            return []

        results: list[CheckResult] = []
        for feature in context.recognition.features:
            if feature.type in _SIZE_EXEMPT:
                continue
            smallest = self._smallest_dimension(feature, limit)
            if smallest is None:
                continue
            _, value = smallest
            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{value:.2f} mm",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        value,
                        limit,
                        limit,
                        "mm",
                        f"This {_readable(feature.type)} measures {value:.2f} mm "
                        f"across, below the {limit:.2f} mm floor the tool "
                        "library can reach. No cutter that small is in the "
                        "library, so this is not a matter of cost -- it needs "
                        "EDM, a micro-machining house, or a larger feature.",
                    ),
                    faces=feature.faces,
                    value=value,
                    limit=limit,
                    comparison="<",
                    unit="mm",
                )
            )
        return results

    @staticmethod
    def _smallest_dimension(feature, limit: float):
        smallest = None
        for name in _SIZE_PARAMETERS:
            value = feature.number(name)
            if value is None or value <= 0.0 or value >= limit:
                continue
            if smallest is None or value < smallest[1]:
                smallest = (name, value)
        return smallest


@register_check(Rulebook.SHARP_INTERNAL_EDGE)
class SharpInternalEdgeCheck(MachiningCheck):
    """A square inside corner that no rotating tool can produce.

    Almost all of this check is deciding what *not* to report. Every cavity
    on the part meets its host face at a sharp concave corner, every bore
    meets the surface it enters, every boss meets its base. Those are already
    reported by the rules that own the features, and repeating them per edge
    would bury the corner that nobody else is speaking for.
    """

    @property
    def name(self) -> str:
        return "Sharp Internal Edge Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.pocket_corner_radius_min_mm
        thresholds = context.config.thresholds
        sharp_min = math.radians(thresholds.sharp_edge_min_deviation_deg)
        minimum_size = thresholds.minimum_feature_size_mm

        graph = context.graph
        blends = self._faces_of(context, FeatureType.FILLET, FeatureType.CHAMFER)
        owners = self._face_owners(context, minimum_size)
        step_only = self._step_only_faces(context, owners)
        covered = self._covered_faces(context, graph, minimum_size)
        axis = self._rotation_axis(context)
        diagonal = self._part_diagonal(context)
        # Ray casting needs the solid itself. Built once and reused, because
        # the face walk it does at construction would otherwise happen for
        # every edge on the part.
        probe = ShapeProbe(context.shape, graph)

        results: list[CheckResult] = []
        for edge in graph.edges:
            if edge.concavity is not Concavity.CONCAVE:
                continue
            deviation = abs(edge.dihedral_angle - math.pi)
            if deviation < sharp_min:
                continue
            # A single corner cannot be longer than the part. Anything that
            # claims to be is a merged compound edge, and the angle sampled
            # at its midpoint does not describe the whole of it.
            if diagonal > 0.0 and edge.shared_edge_length > diagonal:
                continue
            if edge.face_id_a in blends or edge.face_id_b in blends:
                continue
            if not graph.has_node(edge.face_id_a) or not graph.has_node(edge.face_id_b):
                continue

            first = graph.node(edge.face_id_a)
            second = graph.node(edge.face_id_b)

            if self._corner_radius_arc(first, second, deviation, minimum_size):
                continue
            if axis is not None and self._both_turned(first, second, axis):
                continue
            if self._same_feature(owners, edge.face_id_a, edge.face_id_b):
                continue
            if self._curved_opening(first, second, owners):
                continue

            # The rim where a feature meets the bulk material around it.
            # Every cavity has one by construction, and so does every boss
            # and rib at its base -- it is the outline of the feature, not a
            # corner anyone chose to leave square, and the rule that owns
            # the feature is the one that should speak about it.
            a_rim = edge.face_id_a in covered
            b_rim = edge.face_id_b in covered
            a_bulk = edge.face_id_a not in owners or edge.face_id_a in step_only
            b_bulk = edge.face_id_b not in owners or edge.face_id_b in step_only
            if (a_rim and b_bulk) or (b_rim and a_bulk):
                continue
            # Two cavities meeting each other -- an undercut shoulder against
            # the slot it overhangs. Different features, but both have rules.
            if a_rim and b_rim:
                continue

            # Everything above is bookkeeping: whose feature is this, and
            # has something else already spoken for it. What is left is a
            # corner nobody owns, and the question becomes a physical one --
            # can a tool get to it, and did one already make it in passing.
            # Both cast rays, so they are asked last, of the few edges that
            # get this far.
            if is_cutter_formed(probe, edge, first, second, deviation, minimum_size):
                continue

            # A corner at a scale no cutter can work is reported whatever the
            # access, and reported generically: an array of micro-features
            # produces one finding per edge otherwise, all saying the same
            # thing about a part that needs EDM rather than a smaller mill.
            sub_pitch = (
                second_smallest_extent(first) < minimum_size
                or second_smallest_extent(second) < minimum_size
            )
            if not sub_pitch and is_reachable(probe, edge, first, second):
                continue

            angle = math.degrees(edge.dihedral_angle)
            # Reported as information rather than a warning, deliberately.
            # These corners are real -- square, unclaimed by any feature --
            # but the rule fires on the residue every recognizer left, so its
            # count is a reading of recognition coverage as much as of the
            # part. Until that coverage closes it should not be outranking
            # findings that name a specific feature and a specific fix.
            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    f"{angle:.0f} deg corner",
                    self.render(
                        feedback,
                        Severity.INFO,
                        0.0,
                        limit,
                        limit,
                        "mm",
                        "This inside corner is square. A rotating cutter "
                        "leaves its own radius in every corner it clears, so "
                        "a sharp one has to come from somewhere else -- a "
                        "corner radius on the drawing, a relief cut, or "
                        "sinker EDM if the sharp corner is functional. It is "
                        "also where the part will crack first in service.",
                    ),
                    faces=[edge.face_id_a, edge.face_id_b],
                    value=0.0,
                    limit=limit,
                    comparison="<",
                    unit="mm",
                )
            )
        return results

    # -- suppression --------------------------------------------------------

    @staticmethod
    def _faces_of(context, *types: str) -> set[int]:
        faces: set[int] = set()
        for feature in context.recognition.of_type(*types):
            faces.update(feature.faces)
        return faces

    @staticmethod
    def _unspoken_for(feature, minimum_size: float) -> bool:
        """Whether the feature's own rule declines to speak about its corners.

        This rule stands down on a recognized feature because the rule that
        owns the feature is expected to report its corners instead. Where
        that expectation is false the corner is reported by nobody at all,
        which is worse than reporting it twice -- and it is silent, so it
        looks like a clean part.

        Two cases, and both are the owning rule explicitly declining:

        A boss or rib below the tool floor gets no boss or rib finding,
        because its height is trivially machinable -- face the surround
        down. What cannot be made is its base corners at that scale.

        A slot open at both ends gets no corner finding, because the corner
        rule is right that a cutter entering one end and leaving the other
        never has to round anything. But when the recognizer reads a closed
        pocket as a pair of overlapping open slots -- which it does where a
        bore crosses the cavity -- the corners are real and nothing is left
        to say so.
        """
        if feature.type in (FeatureType.BOSS, FeatureType.RIB):
            height = feature.number("height_mm")
            width = feature.number("width_mm")
            return (height is not None and height < minimum_size) or (
                width is not None and width < minimum_size
            )
        if feature.type == FeatureType.SLOT:
            return bool(feature.param("is_open"))
        return False

    def _face_owners(self, context, minimum_size: float) -> dict[int, set[str]]:
        """Which features each face belongs to, for the same-feature test."""
        owners: dict[int, set[str]] = {}
        for feature in context.recognition.features:
            if feature.type not in _OWNS_ITS_CORNERS:
                continue
            if self._unspoken_for(feature, minimum_size):
                continue
            for face_id in feature.faces:
                owners.setdefault(face_id, set()).add(feature.instance_id)
        return owners

    def _covered_faces(self, context, graph, minimum_size: float) -> set[int]:
        """Faces belonging to a feature whose own rule reports its corners.

        Extended one hop out from cavities along concave edges. When the
        pocket recognizer fails to absorb a wall -- which happens when
        something on the host face interrupts the cavity rim -- that wall
        falls outside the feature and its corners fire redundantly against
        the pocket rule. A planar face sharing a concave edge with a pocket
        floor is a wall of it whether or not the recognizer noticed.

        One hop only. Two would reach unrelated features across the part and
        silence corners that genuinely need reporting.
        """
        # A bore keeps its claim on every face it takes, including a planar
        # one. An off-cardinal hole leaves a small flat lip that is
        # genuinely part of the hole, and the curved-opening test cannot
        # see it because that test gates on surface type. Letting a bore
        # stand down wherever a cavity also claims the face was tried and
        # measured: it recovers four corners on one fixture and loses
        # sixteen to a manifold full of cross-drilled channels.
        covered: set[int] = set()
        for feature in context.recognition.features:
            if feature.type not in _CARRIES_ITS_RIM:
                continue
            if self._unspoken_for(feature, minimum_size):
                continue
            covered.update(feature.faces)

        # The expansion reaches walls the recognizer missed, so it has to
        # obey the same rule as the direct set: a cavity whose own rule
        # declines to speak about it cannot lend that silence to a
        # neighbouring face either.
        pocket_like: set[int] = set()
        for feature in context.recognition.of_type(*sorted(_POCKET_LIKE)):
            if self._unspoken_for(feature, minimum_size):
                continue
            pocket_like.update(feature.faces)

        neighbours: set[int] = set()
        for face_id in sorted(pocket_like):
            for edge in graph.edges_of(face_id):
                if edge.concavity is not Concavity.CONCAVE:
                    continue
                other = edge.other_face(face_id)
                if other not in covered:
                    neighbours.add(other)
        return covered | neighbours

    @staticmethod
    def _step_only_faces(context, owners) -> set[int]:
        """Faces whose only claim on them is a step.

        A step recognizer routinely absorbs the top of a box that has a
        cavity cut into it. Counting that face as "in a feature" would stop
        the cavity's own opening rim from being recognized as a rim, and the
        pocket's corners would be reported twice over.
        """
        steps: set[int] = set()
        others: set[int] = set()
        for feature in context.recognition.features:
            target = steps if feature.type == FeatureType.STEP else others
            target.update(feature.faces)
        return (steps - others) & set(owners)

    @staticmethod
    def _same_feature(owners, first_id: int, second_id: int) -> bool:
        return bool(owners.get(first_id, set()) & owners.get(second_id, set()))

    @staticmethod
    def _curved_opening(first, second, owners) -> bool:
        """Whether this is a curved cavity meeting the face it opens onto.

        A bore or a ball-ended pocket necessarily makes a concave rim where
        it breaks the surface. That rim is not a corner anyone has to cut
        into -- it is the shape of the hole.
        """
        if first.face_id not in owners and second.face_id not in owners:
            return False
        curved = (SurfaceType.CYLINDER, SurfaceType.CONE, SurfaceType.SPHERE)
        return first.surface_type in curved or second.surface_type in curved

    @staticmethod
    def _corner_radius_arc(first, second, deviation: float, minimum_size: float) -> bool:
        """The arc where a corner radius meets the floor it was cut into.

        The tool that swept the corner radius formed this junction in the
        same pass, so it is not a corner anyone has to make separately. A
        radius below the tool floor is exempt from the exemption: no tool
        that size exists, so nothing formed it in passing.
        """
        if not _RIGHT_ANGLE_LOW < deviation < _RIGHT_ANGLE_HIGH:
            return False
        if first.surface_type is SurfaceType.CYLINDER and second.surface_type is SurfaceType.PLANE:
            cylinder, plane = first, second
        elif second.surface_type is SurfaceType.CYLINDER and first.surface_type is SurfaceType.PLANE:
            cylinder, plane = second, first
        else:
            return False
        if not cylinder.is_internal or cylinder.cyl_radius < minimum_size:
            return False
        if cylinder.cyl_cone_axis is None or plane.plane_normal is None:
            return False
        return (
            abs(cylinder.cyl_cone_axis.Direction().Dot(plane.plane_normal)) > 0.95
        )

    @staticmethod
    def _rotation_axis(context):
        process = context.part_process
        return process.axis_of_revolution if process.has_axis else None

    @staticmethod
    def _both_turned(first, second, axis) -> bool:
        """Whether both faces came off a lathe.

        A step between two turned diameters is sharp because the insert is,
        and it is cut in one pass. The machinability worry this rule carries
        is a milled one; on a mill-turn part the test is per-pair, so a
        cross-drilling or an end-milled flat still fires.
        """

        def turned(node) -> bool:
            direction = axis.Direction()
            if node.surface_type is SurfaceType.PLANE and node.plane_normal is not None:
                return abs(node.plane_normal.Dot(direction)) > _TURNED_ALIGNMENT
            if (
                node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE)
                and node.cyl_cone_axis is not None
            ):
                return (
                    abs(node.cyl_cone_axis.Direction().Dot(direction))
                    > _TURNED_ALIGNMENT
                )
            return False

        return turned(first) and turned(second)

    @staticmethod
    def _part_diagonal(context) -> float:
        dims = context.bbox_dims()
        if not dims:
            return 0.0
        return math.sqrt(sum(value * value for value in dims))


@register_check(Rulebook.PART_MARKING)
class PartMarkingCheck(MachiningCheck):
    """Engraved or embossed text on the part.

    Not a defect. It is a separate operation with its own tooling and its own
    setup, and it is routinely left off a quote because it does not look like
    machining. Saying what is there, and how deep, lets it be priced.
    """

    @property
    def name(self) -> str:
        return "Part Marking Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        markings = context.recognition.of_type(FeatureType.MARKING_TEXT)
        if not markings:
            return []

        results: list[CheckResult] = []
        for marking in markings:
            depth = marking.number("depth_mm") or 0.0
            stroke = marking.number("stroke_width_mm") or 0.0
            glyphs = int(marking.number("glyph_count") or 0)
            kind = marking.param("marking_type") or "engraved"

            described = f"{glyphs} character{'' if glyphs == 1 else 's'}" if glyphs else "text"
            detail = f"{described}, {depth:.2f} mm deep"
            if stroke > 0.0:
                detail += f" on a {stroke:.2f} mm stroke"

            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    kind.replace("_", " "),
                    self.render(
                        feedback,
                        Severity.INFO,
                        depth,
                        0.0,
                        0.0,
                        "mm",
                        f"This part carries {kind.replace('_', ' ')} marking -- "
                        f"{detail}. That is a separate operation with its own "
                        "tool and setup, whether it is cut with a V-bit, "
                        "laser-etched or dot-peened, and it is easy to leave "
                        "out of a quote because it does not look like "
                        "machining. Confirm the method and who supplies the "
                        "artwork.",
                    ),
                    faces=marking.faces,
                    value=depth,
                    limit=0.0,
                    comparison="",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.RAISED_TEXT_MACHINED_FACE)
class RaisedTextCheck(MachiningCheck):
    """Text left standing proud of a machined face.

    Far more expensive than engraving it, and the difference is not obvious
    from the model. Cutting text in removes the strokes; leaving it standing
    means clearing the whole field around it to depth, with a small tool,
    everywhere.
    """

    @property
    def name(self) -> str:
        return "Raised Text Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        results: list[CheckResult] = []
        for marking in context.recognition.of_type(FeatureType.MARKING_TEXT):
            if marking.param("marking_type") not in ("raised", "embossed", "relief"):
                continue
            stroke = marking.number("stroke_width_mm") or 0.0
            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    "raised text",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        stroke,
                        0.0,
                        0.0,
                        "mm",
                        "This text stands proud of the surface rather than "
                        "being cut into it, which is a different job "
                        "altogether. Engraving removes the strokes; leaving "
                        "them standing means clearing the entire field around "
                        "them to depth with a tool small enough to reach "
                        "between the letters. If the raised form is not "
                        "functional, engraving the same text costs a fraction "
                        "as much.",
                    ),
                    faces=marking.faces,
                    value=stroke,
                    limit=0.0,
                    comparison="",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.FEATURE_COMPLEXITY)
class FeatureComplexityCheck(MachiningCheck):
    """How many distinct features the part carries.

    Nothing here is individually wrong, which is exactly why it is worth
    saying. Programming time and setup count track the number of features
    far better than they track any single dimension, and a part that passes
    every other rule can still be the one that runs late.
    """

    @property
    def name(self) -> str:
        return "Feature Complexity Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None:
            target = thresholds.feature_complexity_warn
        if limit is None:
            limit = thresholds.feature_complexity_error

        census = self._census(context)
        count = float(census["total"])
        if count <= 0.0:
            return []

        graded = self.graded(count, target, limit, "max")
        if graded is not None:
            severity, threshold = graded
            closing = (
                "Nothing here is individually wrong, which is the point: "
                "programming time and setup count follow the number of "
                "features far more closely than any single dimension, so a "
                "part like this runs long even when every limit passes. "
                "Worth asking whether any of it can be simplified."
            )
        else:
            # Said on every part, not only complicated ones. This is the
            # closest thing the model produces to an operation list, and an
            # estimator wants it whether or not the part is unusual -- a
            # quote is built from how many things have to be cut, and that
            # number is only obvious once somebody has counted it.
            severity = Severity.INFO
            threshold = target
            closing = (
                "Not a fault, and not unusual for a part of this size. It is "
                "here because the operation count is what a quote is built "
                "from, and it is easier to read than to count."
            )

        return [
            self.finding(
                rule,
                severity,
                f"{int(count)} features, about {census['operations']} operations",
                self.render(
                    feedback,
                    severity,
                    count,
                    target,
                    limit,
                    "",
                    f"This part carries {int(count)} recognized features "
                    f"({census['summary']}), which is roughly "
                    f"{census['operations']} machining operations. {closing}",
                ),
                faces=[],
                value=count,
                limit=threshold,
                comparison=">",
            )
        ]

    @staticmethod
    def _census(context) -> dict:
        """What is on the part, and roughly how many operations it takes.

        The operation estimate is deliberately crude, because a precise one
        would need the toolpath. A hole is a drilling pass -- more than one
        when it crosses a void wide enough that the drill loses its guidance.
        A pocket is two, roughing and finishing. A slot and a blend are one
        each: a chamfer is its own tool and its own contour pass, however
        trivial it looks on the model.

        Members of a pattern are counted as features but not as operations.
        Twelve holes on one bolt circle are one drilling cycle, and pricing
        them as twelve is how a quote comes back double.
        """
        patterned: set[str] = set()
        for feature in context.recognition.of_type(FeatureType.PATTERN):
            children = feature.parameters.get("child_ids") or ()
            patterned.update(str(child) for child in children)

        counts = {"hole": 0, "pocket": 0, "slot": 0, "blend": 0,
                  "pattern": 0, "other": 0}
        operations = 0
        for feature in context.recognition.features:
            own_operation = feature.instance_id not in patterned
            if feature.type in BORE_TYPES:
                counts["hole"] += 1
                if own_operation:
                    passes = feature.number("drilling_passes")
                    operations += int(passes) if passes and passes > 0 else 1
            elif feature.type == FeatureType.POCKET:
                counts["pocket"] += 1
                if own_operation:
                    operations += 2
            elif feature.type == FeatureType.SLOT:
                counts["slot"] += 1
                if own_operation:
                    operations += 1
            elif feature.type in (FeatureType.FILLET, FeatureType.CHAMFER):
                counts["blend"] += 1
                operations += 1
            elif feature.type == FeatureType.PATTERN:
                counts["pattern"] += 1
                operations += 1
            else:
                counts["other"] += 1
                operations += 1

        spoken = [
            f"{number} {name}{'' if number == 1 else 's'}"
            for name, number in counts.items()
            if number
        ]
        return {
            "total": sum(counts.values()),
            "operations": operations,
            "summary": ", ".join(spoken) or "no recognized features",
        }


def _readable(feature_type: str) -> str:
    """A feature type as a machinist would say it."""
    return feature_type.replace("_", " ").lower()


@register_check(Rulebook.CASTING_DRAFT_ANGLE)
class CastingDraftAngleCheck(MachiningCheck):
    """A part declared as-cast whose walls carry no draft.

    Only meaningful when the shop has said the blank is a casting, because
    no analysis can recover that from geometry -- a machined part and a
    machined casting look identical once the flash is off. Given that
    declaration, walls with no draft on them are a contradiction: the part
    could not have come out of the mould.

    Read from the absence of draft features rather than from any marker on
    the walls themselves. Marking every vertical wall as undrafted would flag
    every pocket on every milled part and say nothing.
    """

    @property
    def name(self) -> str:
        return "Casting Draft Angle Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        if context.config.blank_form != "as_cast":
            return []
        if context.recognition.of_type(FeatureType.DRAFT_FACE):
            return []  # drafted casting: consistent

        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.rib_min_draft_angle_deg

        return [
            self.finding(
                rule,
                Severity.WARNING,
                "no draft",
                self.render(
                    feedback,
                    Severity.WARNING,
                    0.0,
                    limit,
                    limit,
                    "deg",
                    "The blank is declared as-cast, but no wall on this model "
                    "carries any draft. A casting has to release from its "
                    "mould, which normally means one to three degrees on "
                    "every wall running with the pull. Either the draft was "
                    "modelled away -- in which case the supplier's draft "
                    "allowances need checking against the machined envelope "
                    "-- or this part is not really cast.",
                ),
                faces=[],
                value=0.0,
                limit=limit,
                comparison="<",
                unit="deg",
            )
        ]


@register_check(Rulebook.SURFACE_FINISH_CONFLICT)
class SurfaceFinishConflictCheck(MachiningCheck):
    """A tapped hole breaking into a pocket floor.

    The two surfaces want different finishes -- a thread's flanks are cut to
    a form, a pocket floor is a milled surface -- so where they meet, one
    operation cannot serve both and the pocket has to be finished around the
    hole. It is a setup and a cost rather than a defect, so it is reported as
    information.

    Only a *tapped* hole counts. A plain drilled or reamed bore in a pocket
    floor takes the same finishing pass as the floor and there is nothing to
    reconcile, which is why every other hole type is left out of the pairing
    entirely rather than filtered later.
    """

    @property
    def name(self) -> str:
        return "Surface Finish Conflict Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        owner: dict[int, tuple[str, str]] = {}
        for feature in context.recognition.features:
            if feature.type not in (FeatureType.THREADED_HOLE, FeatureType.POCKET):
                continue
            for face_id in feature.faces:
                owner[face_id] = (feature.type, feature.instance_id)

        results: list[CheckResult] = []
        reported: set[tuple[str, str]] = set()
        for edge in context.graph.edges:
            first = owner.get(edge.face_id_a)
            second = owner.get(edge.face_id_b)
            if first is None or second is None:
                continue
            if {first[0], second[0]} != {FeatureType.THREADED_HOLE, FeatureType.POCKET}:
                continue

            pair = tuple(sorted((first[1], second[1])))
            if pair in reported:
                continue
            reported.add(pair)

            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    "finish transition",
                    self.render(
                        feedback,
                        Severity.INFO,
                        0.0,
                        0.0,
                        0.0,
                        "",
                        "A tapped hole opens into a pocket floor here, and the "
                        "two want different finishes -- the thread is cut to a "
                        "form, the floor is a milled surface. One pass cannot "
                        "serve both, so the floor has to be finished around "
                        "the hole. Not a defect, but it is an operation and a "
                        "setup that is easy to leave out of a quote.",
                    ),
                    faces=[edge.face_id_a, edge.face_id_b],
                    value=0.0,
                    limit=0.0,
                    comparison="",
                )
            )
        return results
