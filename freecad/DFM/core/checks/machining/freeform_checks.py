# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about shaped surfaces: sculpture, turned profiles and ball pockets.

A sculpted surface has no dimension a machinist can argue with. What it has is
a tightest concave radius, and that one number decides everything: it caps the
cutter, the cutter caps the stepover, and the stepover multiplied by the area
is the hours. So the rules here all ask the same question in different words
-- what is the biggest tool that still reaches the bottom of the tightest
hollow, and what does using it cost.

The tightest hollow is also where a rule stops being about cost and starts
being about possibility. Below the smallest ball nose in the shop the surface
cannot be finished at all, however long anyone is willing to run the machine;
and a bowl sunk past its own equator cannot be reached by a straight tool at
any radius, because the rim is in the way.

Which faces count as sculpture is read off the adjacency graph, as the
reference reads it, rather than off the FREEFORM_SURFACE features: a shaped
face no recognizer kept still has to be cut. `_sculpted_faces` says which.
"""

from __future__ import annotations

import math
from typing import Optional

from ...machining.aag import SurfaceType
from ...machining.config import MachiningConfig
from ...machining.process_classifier import PartProcessType
from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# The tool types that can finish a sculpted concavity. For a hollow the
# limiting dimension is the tool radius whatever the tip form, so a flat end
# mill counts alongside a ball nose.
_MILLING_TOOL_TYPES = ("end_mill", "ball_nose")

# Scallop height is quoted in microns and every length here is in millimetres.
_MICRONS_PER_MM = 1000.0

# Toolpath length is reported in metres, because a finishing pass on a
# sculpted part runs to tens of them.
_MM_PER_M = 1000.0

# Where a finishing pass stops being incidental and starts being the job.
# Roughly: an hour of ball-nose work at a typical finishing feed sits near
# the upper figure.
_BURDEN_MODERATE_PATH_M = 10.0
_BURDEN_HEAVY_PATH_M = 50.0

# A face is sculpted when it curves tighter than this, convexly (a 500 mm
# radius). A flat ruled spline carries near-zero curvature and machines
# like a plane.
_MIN_SCULPT_CURVATURE = 1.0 / 500.0


def _smallest_milling_radius(config: MachiningConfig) -> Optional[float]:
    """Radius of the smallest cutter that could work a hollow, or None."""
    radii = [
        tool.min_diameter_mm * 0.5
        for tool in config.tool_library
        if tool.type in _MILLING_TOOL_TYPES and tool.min_diameter_mm > 0.0
    ]
    return min(radii) if radii else None


def _ball_nose_radii(config: MachiningConfig) -> list[float]:
    """Every ball-nose radius in the library, ascending."""
    return sorted(
        tool.min_diameter_mm * 0.5
        for tool in config.tools_of_type("ball_nose")
        if tool.min_diameter_mm > 0.0
    )


def _sculpted_faces(context, skip_blend_bands: bool = False) -> list:
    """Every sculpted face of the part, read off the graph, not off features.

    The reference scans its adjacency graph for these two rules, so a
    sculpted face counts whether or not a FREEFORM_SURFACE feature claimed
    it: a concave B-spline band that no recognizer kept still has a radius
    a cutter has to fit.

    A face counts when its sampled curvature says it is shaped: any concave
    region, or convex curvature tighter than a 500 mm radius (a flat ruled
    spline machines like a plane). Left out:

    - a revolved face on a turned or mill-turn part, which the lathe forms;
    - ENGRAVING IS NOT SCULPTING: a face a MARKING_TEXT owns. A letter has
      tight concave corners by design and is cut by a V-bit, a laser or a
      die. The scan is filtered, not the finding, so a part with a mark and
      a real sculpted region still reports the region;
    - for the finishing estimate only, a blend band (see `_blend_band`).
    """
    marking: set[int] = set()
    for feature in context.recognition.of_type(FeatureType.MARKING_TEXT):
        marking.update(feature.faces)
    lathe = context.process_type in (PartProcessType.TURNED, PartProcessType.MILL_TURN)
    blend_max = context.config.thresholds.freeform_blend_band_max_radius_mm
    faces = []
    for node in context.graph.nodes:
        if not _is_sculpted(node):
            continue
        if lathe and node.surface_type is SurfaceType.REVOLVED:
            continue
        if node.face_id in marking:
            continue
        if skip_blend_bands and _blend_band(context, node, blend_max):
            continue
        faces.append(node)
    return faces


def _is_sculpted(node) -> bool:
    if not node.has_freeform_curvature:
        return False
    if node.freeform_min_concave_radius_mm > 0.0:
        return True
    return node.freeform_max_convex_curvature > _MIN_SCULPT_CURVATURE


def _blend_band(context, node, blend_max: float) -> bool:
    """A fillet in B-spline clothing, which no ball nose has to finish.

    Convex-only: a tight convex radius (an edge round along a curved edge,
    which OCCT emits as a spline). Concave: a blend-scale radius and tangent
    to its neighbours on at least two sides, which is what a rolling-ball
    blend is by construction; a sculpted wall meets its surroundings sharp.
    """
    concave = node.freeform_min_concave_radius_mm
    if concave <= 0.0:
        convex = node.freeform_max_convex_curvature
        if convex <= 0.0:
            return False
        return 1.0 / convex <= blend_max
    if concave > blend_max:
        return False
    tangent = sum(1 for edge in context.graph.edges_of(node.face_id) if edge.is_tangent)
    return tangent >= 2


@register_check(Rulebook.FREEFORM_INTERNAL_RADIUS)
class FreeformInternalRadiusCheck(MachiningCheck):
    """A hollow has to be wider than the tool that finishes it.

    A cutter working a concave region at close to its own radius is in
    full-width contact: it stops cutting and starts rubbing, and that is
    where chatter and burn come from. Two tiers, because they are two
    different conversations. Tighter than the smallest cutter can manage is
    a design problem. Merely tight enough to force the bottom of the tool
    library is a price.

    Reported once for the whole part rather than once per region: the answer
    is a single decision about tooling, and repeating it for every sculpted
    patch would bury it.
    """

    @property
    def name(self) -> str:
        return "Freeform Internal Radius Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        tool_radius = _smallest_milling_radius(context.config)
        if tool_radius is None:
            return []  # no milling tools configured; nothing to judge against

        thresholds = context.config.thresholds
        # A configured floor of zero is the absence of one, not a
        # requirement no radius can fail, so it falls through to the figure
        # the tool library gives.
        required = self.safe_float(rule_config.limit) or 0.0
        if required <= 0.0:
            required = tool_radius * thresholds.freeform_radius_safety
        info_tier = self.safe_float(rule_config.target) or 0.0
        if info_tier <= 0.0:
            info_tier = thresholds.freeform_radius_info_tier_mm

        tight: list = []
        mild: list = []
        for node in _sculpted_faces(context):
            radius = node.freeform_min_concave_radius_mm
            # A convex-only face has no internal radius: any tool rolls
            # over the outside of it.
            if radius <= 0.0:
                continue
            if radius < required:
                tight.append(node)
            elif radius < info_tier:
                mild.append(node)

        results: list[CheckResult] = []
        if tight:
            worst = min(n.freeform_min_concave_radius_mm for n in tight)
            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{worst:.2f} mm concave radius",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        worst,
                        info_tier,
                        required,
                        "mm",
                        f"The sculpted surfaces on this part come down to a "
                        f"{worst:.2f} mm concave radius. The smallest cutter in "
                        f"the library is {tool_radius * 2.0:.1f} mm diameter, and "
                        f"it needs {required:.2f} mm of concave radius before it "
                        "is finishing rather than rubbing -- run it into a "
                        "tighter hollow than that and the whole width of the "
                        "cutter is in contact, which is where chatter and burnt "
                        "surfaces come from. Opening the radius out is the cheap "
                        "answer; a special ground cutter is the other one.",
                    ),
                    faces=_faces_of(tight),
                    value=worst,
                    limit=required,
                    comparison="<",
                    unit="mm",
                )
            )

        if mild:
            worst = min(n.freeform_min_concave_radius_mm for n in mild)
            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    f"{worst:.2f} mm concave radius",
                    self.render(
                        feedback,
                        Severity.INFO,
                        worst,
                        info_tier,
                        required,
                        "mm",
                        f"The sculpted surfaces here come down to a {worst:.2f} mm "
                        f"concave radius, inside the {info_tier:.1f} mm mark. That "
                        "is machinable, but it forces the bottom of the tool "
                        "library: small cutters, light stepovers, and a good deal "
                        "more time in the machine than the shape suggests. Worth "
                        "knowing before the price is set.",
                    ),
                    faces=_faces_of(mild),
                    value=worst,
                    limit=info_tier,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.FREEFORM_FINISHING)
class FreeformFinishingCheck(MachiningCheck):
    """What it costs to finish the sculpture, and when it cannot be done.

    A ball nose leaves a scallop between passes, and the height of that
    scallop is what the stepover is chosen for. So the ball that fits the
    tightest hollow sets the stepover, the stepover divides into the area,
    and the answer is metres of toolpath -- a number that belongs in the
    quote rather than in a defect list.

    The exception is a hollow tighter than the smallest ball in the shop.
    There is no stepover that helps: the tool cannot reach the bottom of it
    at all, so estimating the burden would be answering the wrong question.
    """

    @property
    def name(self) -> str:
        return "Freeform Finishing Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        ball_radii = _ball_nose_radii(context.config)
        if not ball_radii:
            return []  # no finishing tools configured
        smallest_ball = ball_radii[0]

        thresholds = context.config.thresholds
        regions = _sculpted_faces(context, skip_blend_bands=True)
        if not regions:
            return []

        total_area = 0.0
        tightest = 0.0
        unreachable: list = []
        for node in regions:
            total_area += node.area
            radius = node.freeform_min_concave_radius_mm
            if radius <= 0.0:
                continue
            if tightest == 0.0 or radius < tightest:
                tightest = radius
            if radius < smallest_ball:
                unreachable.append(node)

        scallop_um = thresholds.freeform_scallop_target_um
        scallop_mm = scallop_um / _MICRONS_PER_MM

        if unreachable:
            worst = min(n.freeform_min_concave_radius_mm for n in unreachable)
            # A warning, as the reference grades it, whatever the rule's
            # configured severity.
            severity = Severity.WARNING
            return [
                self.finding(
                    rule,
                    severity,
                    f"{worst:.2f} mm below the smallest ball",
                    self.render(
                        feedback,
                        severity,
                        worst,
                        smallest_ball,
                        smallest_ball,
                        "mm",
                        f"A sculpted hollow on this part closes to {worst:.2f} mm "
                        f"radius, tighter than the smallest ball nose in the "
                        f"library at R{smallest_ball:.1f}. The ball never touches "
                        f"the bottom of it, so the {scallop_um:.0f} micron finish "
                        "asked for cannot be held down there at any stepover -- "
                        "the corner comes out as the tool left it, and it will "
                        "need hand work or EDM. Opening the radius to clear the "
                        "smallest ball is what fixes it.",
                    ),
                    faces=_faces_of(unreachable),
                    value=worst,
                    limit=smallest_ball,
                    comparison="<",
                    unit="mm",
                )
            ]

        # Below this much sculpted area the finishing pass is a detail of the
        # job rather than a driver of its price, and saying so is noise.
        if total_area < thresholds.freeform_finishing_min_area_mm2:
            return []

        # The largest ball that still fits the tightest hollow. A part with no
        # concave region anywhere takes the largest ball in the library, since
        # nothing constrains it.
        fitting = ball_radii[-1]
        if tightest > 0.0:
            fitting = smallest_ball
            for radius in ball_radii:
                if radius <= tightest:
                    fitting = radius

        # Scallop geometry on a locally flat surface: a ball of radius r
        # stepping over s leaves a cusp of height h where s = 2*sqrt(2rh-h^2).
        # A concave region tolerates slightly more and a convex one slightly
        # less; the flat figure is the number CAM planning works from.
        stepover = 2.0 * math.sqrt(
            max(0.0, 2.0 * fitting * scallop_mm - scallop_mm * scallop_mm)
        )
        if stepover <= 0.0:
            return []

        path_length_m = total_area / stepover / _MM_PER_M
        if path_length_m < _BURDEN_MODERATE_PATH_M:
            burden = "light"
        elif path_length_m < _BURDEN_HEAVY_PATH_M:
            burden = "moderate"
        else:
            burden = "heavy"

        minimum = thresholds.freeform_finishing_min_area_mm2
        return [
            self.finding(
                rule,
                Severity.INFO,
                f"{total_area:.0f} mm2 to finish",
                self.render(
                    feedback,
                    Severity.INFO,
                    total_area,
                    minimum,
                    minimum,
                    "",
                    f"There are {total_area:.0f} square millimetres of sculpted "
                    f"surface here to finish with a ball nose. The tightest "
                    f"hollow admits a {fitting * 2.0:.1f} mm ball, which at a "
                    f"{scallop_um:.0f} micron scallop means a "
                    f"{stepover:.2f} mm stepover -- about "
                    f"{path_length_m:.1f} metres of finishing path, a {burden} "
                    "burden. Nothing is wrong with the part; this is time on the "
                    "machine and it wants to be in the price.",
                ),
                faces=_faces_of(regions),
                value=total_area,
                limit=minimum,
                comparison=">",
            )
        ]


@register_check(Rulebook.TURNED_PROFILE_RADIUS)
class TurnedProfileRadiusCheck(MachiningCheck):
    """The lathe-side version of the same question.

    A valley in a turned profile is traced by the nose of an insert, so the
    nose radius is what has to fit. Tighter than the smallest nose in the
    library and the whole nose sits in the cut at once, which on a lathe
    means the same dwell and the same chatter it means on a mill.
    """

    @property
    def name(self) -> str:
        return "Turned Profile Radius Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        nose = context.config.smallest_turning_nose_radius()
        if nose is None or nose <= 0.0:
            return []  # no inserts configured

        # As above: zero is not a requirement, so the insert nose decides.
        required = self.safe_float(rule_config.limit) or 0.0
        if required <= 0.0:
            required = nose * context.config.thresholds.freeform_radius_safety

        results: list[CheckResult] = []
        for profile in context.recognition.of_type(FeatureType.TURNED_PROFILE):
            radius = profile.number("min_concave_radius_mm") or 0.0
            # A profile that is convex the whole way along is traced by any
            # nose at all.
            if radius <= 0.0 or radius >= required:
                continue

            results.append(
                self.finding(
                    rule,
                    Severity.WARNING,
                    f"{radius:.2f} mm valley",
                    self.render(
                        feedback,
                        Severity.WARNING,
                        radius,
                        required,
                        required,
                        "mm",
                        f"This turned profile drops into a {radius:.2f} mm concave "
                        f"valley. The smallest insert nose in the library is "
                        f"R{nose:.1f}, and it wants {required:.2f} mm of radius to "
                        "trace a valley without burying the whole nose in the cut. "
                        "As drawn it needs a grooving pass with a form tool, a "
                        "special insert, or a wider radius on the print.",
                    ),
                    faces=profile.faces,
                    value=radius,
                    limit=required,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.SPHERICAL_POCKET_UNDERCUT)
class SphericalPocketUndercutCheck(MachiningCheck):
    """A bowl sunk past its equator cannot be milled.

    The rim is then narrower than the cavity behind it, and no ball mill --
    however small -- gets back under the lip once it has gone down through
    the neck. A little of this is rounding on the model rather than a
    designed undercut, so the rim has to hang over by more than a tolerance
    before it is worth saying anything.
    """

    @property
    def name(self) -> str:
        return "Spherical Pocket Undercut Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.spherical_overhang_warn_mm
        severity = self.severity_from_rule_config(rule_config)

        results: list[CheckResult] = []
        for bowl in context.recognition.of_type(FeatureType.SPHERICAL_POCKET):
            if not bowl.param("is_super_hemispherical"):
                continue
            overhang = bowl.number("overhang_mm") or 0.0
            if overhang <= limit:
                continue

            radius = bowl.number("radius_mm") or 0.0
            share = (overhang / radius * 100.0) if radius > 0.0 else 0.0
            opening = bowl.number("opening_diameter_mm") or 0.0

            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{overhang:.2f} mm overhang",
                    self.render(
                        feedback,
                        severity,
                        overhang,
                        limit,
                        limit,
                        "mm",
                        f"This ball-ended pocket is sunk past its own equator. The "
                        f"rim hangs {overhang:.2f} mm over the cavity behind it, "
                        f"{share:.0f} per cent of the R{radius:.1f} ball, so the "
                        f"{opening:.1f} mm opening is narrower than the bowl it "
                        "leads into. A cutter that goes in straight cannot get "
                        "back under the lip whatever its size -- a small ball mill "
                        "will scrub most of the surface and leave the overhang "
                        "behind. As drawn this is sinker EDM, or a flip and a "
                        "second setup. Sinking the ball only to its equator would "
                        "make it a milling job.",
                    ),
                    faces=bowl.faces,
                    value=overhang,
                    limit=limit,
                    comparison=">",
                    unit="mm",
                )
            )
        return results


def _faces_of(regions) -> list[int]:
    """Every face of a group of graph faces or regions, in a stable order."""
    faces: set[int] = set()
    for region in regions:
        if hasattr(region, "face_id"):
            faces.add(region.face_id)
        else:
            faces.update(region.faces)
    return sorted(faces)
