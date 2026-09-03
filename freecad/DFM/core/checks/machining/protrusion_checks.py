# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rules about protrusions: bosses and ribs.

A cavity is cut with the tool inside it and the material all around holding
still. A protrusion is the opposite: the tool walks round the outside of
something that is standing on its own, getting thinner and less supported with
every pass. So the questions here are all about stiffness -- how tall a post
is against its own base, how thin a web is against its height -- and about
whether the spindle can see round the thing at all from where the part is
clamped.

The one rule that is not about stiffness is rib draft, which is a casting
question rather than a milling one. Vertical rib walls are the normal case on
a milled part and there is nothing to say about them; on an as-cast blank the
same walls drag on the pattern as it is pulled.
"""

from __future__ import annotations

import math

from ...machining.aag import SurfaceType
from ...machining.features import FeatureType
from ...machining.process_classifier import PartProcessType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# The three directions a three-axis spindle can be pointed at the part once
# it is clamped. A boss on any of them is reachable in one setup; a boss
# between them is not.
_CARDINALS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

# Stock forms that are poured or pressed rather than sawn, where a wall
# without draft has to come out of a mould.
_MOULDED_BLANKS = ("as_cast",)

# Processes that cut every wall with a tool, so nothing is ever pulled from a
# pattern and draft is neither expected nor useful.
_CUT_FROM_SOLID = (
    PartProcessType.MILLED,
    PartProcessType.TURNED,
    PartProcessType.MILL_TURN,
)


@register_check(Rulebook.BOSS_HEIGHT_RATIO)
class BossHeightRatioCheck(MachiningCheck):
    """A tall boss is a cantilever with the cutter pushing on its top.

    Judged against the base rather than the height alone, because stiffness
    falls off with the cube of the ratio between them: a 40 mm post on a 40 mm
    pad is solid, and the same post on a 5 mm pad is a whip.
    """

    @property
    def name(self) -> str:
        return "Boss Height Ratio Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None:
            target = thresholds.boss_height_warn_ratio
        if limit is None:
            limit = thresholds.boss_height_error_ratio

        results: list[CheckResult] = []
        for boss in context.recognition.of_type(FeatureType.BOSS):
            height = boss.number("height_mm") or 0.0
            if height <= 0.0:
                continue
            base = self._base_dimension(boss)
            if base <= 0.0:
                continue

            ratio = height / base
            graded = self.graded(ratio, target, limit, "max")
            if graded is None:
                continue

            severity, threshold = graded
            outlook = (
                "Past this ratio it does not just chatter, it breaks off: expect "
                "to lose the part rather than the finish."
                if severity is Severity.ERROR
                else "Expect it to push away from the cutter and chatter, so the "
                "post finishes tapered and marked."
            )
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{ratio:.1f}x its base",
                    self.render(
                        feedback,
                        severity,
                        ratio,
                        target,
                        limit,
                        "",
                        f"This boss stands {height:.1f} mm off a {base:.1f} mm base, "
                        f"{ratio:.1f} times its own width, with the cutter walking "
                        f"right round it and nothing holding the top. {outlook} "
                        "Light radial cuts stepping down in Z will get it made; a "
                        "shorter or fatter boss, or a separate pressed-in pin, "
                        "would be cheaper.",
                    ),
                    faces=boss.faces,
                    value=ratio,
                    limit=threshold,
                    comparison=">",
                )
            )
        return results

    @staticmethod
    def _base_dimension(boss) -> float:
        """What the boss stands on, in the direction it is weakest.

        A round boss has one answer. A rectangular pad bends first about its
        narrow side, so that is the one that decides.
        """
        diameter = boss.number("diameter_mm") or 0.0
        if diameter > 0.0:
            return diameter

        width = boss.number("width_mm") or 0.0
        length = boss.number("length_mm") or 0.0
        if width > 0.0 and length > 0.0:
            return min(width, length)
        return max(width, length)


@register_check(Rulebook.BOSS_WALL_THICKNESS)
class BossWallThicknessCheck(MachiningCheck):
    """A round boss below the diameter the shop will stand behind.

    Only cylindrical bosses are judged. A rectangular pad's stiffness is
    already covered by the height ratio, but a slender round post has a
    minimum size of its own below which it will not survive the cut whatever
    its height.
    """

    @property
    def name(self) -> str:
        return "Boss Wall Thickness Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None and limit is None:
            target = context.config.thresholds.boss_min_diameter_mm

        results: list[CheckResult] = []
        for boss in context.recognition.of_type(FeatureType.BOSS):
            diameter = boss.number("diameter_mm") or 0.0
            # Absent or zero means the recognizer found no cylindrical wall,
            # so this is a rectangular pad and the rule has nothing to say.
            if diameter <= 0.0:
                continue

            graded = self.graded(diameter, target, limit, "min")
            if graded is None:
                continue

            severity, threshold = graded
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{diameter:.2f} mm boss",
                    self.render(
                        feedback,
                        severity,
                        diameter,
                        target if target is not None else threshold,
                        limit if limit is not None else threshold,
                        "mm",
                        f"This boss is only {diameter:.2f} mm across, under the "
                        f"{threshold:.2f} mm the shop will stand behind. A post that "
                        "thin flexes away from the cutter as the material round it "
                        "comes off, so it finishes tapered and torn and there is a "
                        "real chance of snapping it. Growing the diameter, or making "
                        "it a separate pressed-in dowel, is the usual answer.",
                    ),
                    faces=boss.faces,
                    value=diameter,
                    limit=threshold,
                    comparison="<",
                    unit="mm",
                )
            )
        return results


@register_check(Rulebook.BOSS_UNDERCUT)
class BossUndercutCheck(MachiningCheck):
    """A boss leaning off every direction the spindle can approach from.

    A boss standing on a side face is not the problem -- the part gets turned
    over and it is a top face again. What costs money is a boss pointing
    between the cardinals, where no single clamping gets the spindle round the
    whole of it and the leeward side of the lean stays shadowed.
    """

    @property
    def name(self) -> str:
        return "Boss Needs Special Fixturing Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        threshold = context.config.thresholds.boss_cardinal_alignment_min_dot
        severity = self.severity_from_rule_config(rule_config)

        results: list[CheckResult] = []
        for boss in context.recognition.of_type(FeatureType.BOSS):
            axis = boss.direction("axis")
            # Rectangular pads carry no axis, and a cylindrical boss only gets
            # one when the recognizer found a cylindrical wall to read it off.
            if axis is None:
                continue

            alignment = max(
                abs(axis.X() * x + axis.Y() * y + axis.Z() * z)
                for x, y, z in _CARDINALS
            )
            if alignment >= threshold:
                continue

            # Reported as an angle rather than as the dot product it is
            # measured with: a machinist reads "22 degrees off" and knows
            # immediately what has to happen to the fixture.
            tilt = math.degrees(math.acos(min(1.0, alignment)))
            allowed = math.degrees(math.acos(min(1.0, threshold)))
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{tilt:.0f} degrees off axis",
                    self.render(
                        feedback,
                        severity,
                        tilt,
                        allowed,
                        allowed,
                        "deg",
                        f"This boss leans {tilt:.0f} degrees off every machine axis. "
                        "A three-axis spindle cannot get round the whole of it from "
                        "one direction -- the leeward side of the lean is shadowed "
                        "by the boss itself -- so it needs an indexed or fourth-axis "
                        "setup, or a fixture that stands the part over on that "
                        "angle. Either way that is another setup and another "
                        "fixture to make.",
                    ),
                    faces=boss.faces,
                    value=tilt,
                    limit=allowed,
                    comparison=">",
                    unit="deg",
                )
            )
        return results


@register_check(Rulebook.RIB_HEIGHT_ASPECT)
class RibHeightAspectCheck(MachiningCheck):
    """A web too tall for its own thickness.

    The rib rings under the cutter and bends away from it, which is a problem
    twice over: the finished thickness wanders while it is being made, and the
    same slenderness is still there in service.
    """

    @property
    def name(self) -> str:
        return "Rib Height Aspect Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None:
            target = context.config.thresholds.rib_height_aspect_warn

        results: list[CheckResult] = []
        for rib in context.recognition.of_type(FeatureType.RIB):
            height = rib.number("height_mm") or 0.0
            thickness = rib.number("thickness_mm") or 0.0
            if height <= 0.0 or thickness <= 0.0:
                continue

            ratio = height / thickness
            graded = self.graded(ratio, target, limit, "max")
            if graded is None:
                continue

            severity, threshold = graded
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{ratio:.1f}x its thickness",
                    self.render(
                        feedback,
                        severity,
                        ratio,
                        target if target is not None else threshold,
                        limit if limit is not None else threshold,
                        "",
                        f"This rib stands {height:.1f} mm tall on a {thickness:.1f} mm "
                        f"section, {ratio:.1f} times its own thickness. A web that "
                        "slender rings under the cutter and bends away from it, so "
                        "the thickness wanders along its length and the faces come "
                        "out marked -- and it is just as flexible in service. "
                        "Thickening it, shortening it, or roughing oversize and "
                        "finishing with light spring passes are the ways round it.",
                    ),
                    faces=rib.faces,
                    value=ratio,
                    limit=threshold,
                    comparison=">",
                )
            )
        return results


@register_check(Rulebook.RIB_DRAFT_ANGLE)
class RibDraftAngleCheck(MachiningCheck):
    """Rib walls square enough to drag on the pattern.

    Only asked of parts that come out of a mould. On anything cut from solid
    a vertical rib wall is the normal case: the end mill cuts it square and no
    draft is wanted, so flagging one would be noise on every milled bracket
    that has a web on it.

    The draft is measured rather than assumed. The two webs of a rib face
    exactly opposite each other when the walls are vertical and open up as the
    rib tapers, so half the shortfall from a straight angle is the draft per
    side.
    """

    @property
    def name(self) -> str:
        return "Rib Draft Angle Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        # A declared as-cast blank makes the casting real whatever the
        # geometry classifies as. Failing that, anything definitively cut from
        # solid stands down, and an unclassified part is let through as the
        # conservative default.
        if context.config.blank_form not in _MOULDED_BLANKS:
            if context.process_type in _CUT_FROM_SOLID:
                return []

        limit = self.safe_float(rule_config.limit)
        if limit is None:
            limit = context.config.thresholds.rib_min_draft_angle_deg

        results: list[CheckResult] = []
        for rib in context.recognition.of_type(FeatureType.RIB):
            draft = self._draft_per_side(context, rib)
            # No pair of walls to measure: say nothing rather than accuse the
            # rib of something that was never established.
            if draft is None or draft >= limit:
                continue

            severity = Severity.INFO
            thickness = rib.number("thickness_mm") or 0.0
            section = f" on a {thickness:.1f} mm section" if thickness > 0.0 else ""
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"{draft:.1f} deg draft",
                    self.render(
                        feedback,
                        severity,
                        draft,
                        limit,
                        limit,
                        "deg",
                        f"This rib{section} has about {draft:.1f} degrees of draft "
                        f"per side against the {limit:.1f} a pattern needs to pull "
                        "cleanly. Walls this square drag as the pattern comes out, "
                        "which tears sand moulds and scores die faces, and the same "
                        "walls have to be cut square afterwards rather than swept. "
                        "Leaning them out a degree or two costs nothing in the "
                        "casting and nothing on the machine.",
                    ),
                    faces=rib.faces,
                    value=draft,
                    limit=limit,
                    comparison="<",
                    unit="deg",
                )
            )
        return results

    @staticmethod
    def _draft_per_side(context, rib):
        """Half the angle the rib's two dominant walls open by.

        The two biggest planar faces of a rib are its webs -- the top strip and
        the end caps are much smaller -- and their outward normals are exactly
        opposed on an undrafted rib.
        """
        walls = []
        for face_id in sorted(rib.faces):
            if not context.graph.has_node(face_id):
                continue
            node = context.graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            if node.outward_normal is None:
                continue
            walls.append(node)
        if len(walls) < 2:
            return None

        walls.sort(key=lambda node: (-node.area, node.face_id))
        first, second = walls[0].outward_normal, walls[1].outward_normal
        opening = math.degrees(first.Angle(second))
        return max(0.0, (180.0 - opening) / 2.0)
