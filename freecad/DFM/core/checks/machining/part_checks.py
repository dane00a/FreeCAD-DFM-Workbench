# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Whole-part rules: proportion, stock waste, and enclosed voids."""

from __future__ import annotations

import math
from typing import Optional

from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.gp import gp_Pnt, gp_Vec
from OCP.TopAbs import TopAbs_SHELL
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape

from ...machining.aag import SurfaceType
from ...machining.context import MachiningContext
from ...machining.process_classifier import PartProcessType, axes_colinear, face_axis
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


# A void smaller than this is modelling noise, not a trapped cavity.
_MIN_VOID_EXTENT_MM = 0.5


@register_check(Rulebook.PART_ASPECT_RATIO)
class PartAspectRatioCheck(MachiningCheck):
    """Slender parts deflect. What "slender" means depends on the process.

    On a lathe the question is length over diameter -- a shaft past about 4:1
    wants a steady rest and past 8:1 is Swiss territory. On a mill it is the
    bounding box's longest over shortest, and the advice then splits again:
    a bar deflects and chatters, a plate warps and drums. Those are different
    conversations, so the finding says which one it is.
    """

    @property
    def name(self) -> str:
        return "Part Slenderness Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        if context.is_turning_family:
            return self._turned(context, rule_config, rule, feedback)
        return self._milled(context, rule_config, rule, feedback)

    # -- turning ------------------------------------------------------------

    def _turned(
        self, context: MachiningContext, rule_config, rule, feedback
    ) -> list[CheckResult]:
        axis = context.part_process.axis_of_revolution
        if axis is None:
            return []

        length, diameter = self._axial_extent_and_diameter(context, axis)
        if diameter <= 1e-6 or length <= diameter:
            return []  # a disc is not slender, however wide

        # The material's own limits win when the process defines them: a
        # stainless bar overhangs less happily than an aluminium one, and that
        # is exactly the kind of thing a shop tunes per material.
        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None and limit is None:
            target = thresholds.turn_slender_warn_ratio
            limit = thresholds.turn_slender_error_ratio

        ratio = length / diameter
        graded = self.graded(ratio, target, limit, "max")
        if graded is None:
            return []

        severity, threshold = graded
        detail = (
            "past the point where a steady rest or Swiss-style guide bushing "
            "is needed"
            if severity is Severity.ERROR
            else "slender enough that it will deflect away from the tool "
            "without a steady rest"
        )
        message = self.render(
            feedback,
            severity,
            ratio,
            target if target is not None else 0.0,
            limit if limit is not None else 0.0,
            "",
            f"The turned profile is {length:.1f} mm long on a {diameter:.1f} mm "
            f"diameter, a {ratio:.1f}:1 ratio -- {detail}. Expect taper and "
            "chatter on the unsupported end.",
        )
        return [
            self.finding(
                rule,
                severity,
                f"{ratio:.1f}:1 length to diameter",
                message,
                value=ratio,
                limit=threshold,
                comparison=">",
            )
        ]

    @staticmethod
    def _axial_extent_and_diameter(context: MachiningContext, axis) -> tuple[float, float]:
        """Length along the axis and the turned diameter about it.

        The diameter is taken from the coaxial cylinder and cone radii, not
        from bounding boxes: a box drawn round a cylinder touches at the
        corners, which overstates the diameter by a factor of root two and
        would make every shaft look stubbier than it is.
        """
        direction = axis.Direction()
        origin = axis.Location()
        low, high = math.inf, -math.inf

        for node in context.graph.nodes:
            if node.bbox.IsVoid():
                continue
            xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
            for x in (xmin, xmax):
                for y in (ymin, ymax):
                    for z in (zmin, zmax):
                        along = gp_Vec(origin, gp_Pnt(x, y, z)).Dot(gp_Vec(direction))
                        low = min(low, along)
                        high = max(high, along)

        if low is math.inf:
            return (0.0, 0.0)

        radius = 0.0
        for node in context.graph.nodes:
            node_axis = face_axis(node)
            if node_axis is None or not axes_colinear(node_axis, axis):
                continue
            if node.surface_type is SurfaceType.CYLINDER:
                radius = max(radius, node.cyl_radius)
            elif node.surface_type is SurfaceType.CONE:
                radius = max(radius, node.cone_r0, node.cone_r1)

        if radius <= 0.0:
            # No analytic turned surface on the axis: fall back to the widest
            # radial reach, accepting that it is an over-estimate.
            for node in context.graph.nodes:
                if node.bbox.IsVoid():
                    continue
                xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
                for x in (xmin, xmax):
                    for y in (ymin, ymax):
                        for z in (zmin, zmax):
                            radius = max(
                                radius,
                                gp_Vec(origin, gp_Pnt(x, y, z)).CrossMagnitude(gp_Vec(direction)),
                            )

        return (high - low, radius * 2.0)

    # -- milling ------------------------------------------------------------

    def _milled(self, context: MachiningContext, rule_config, rule, feedback) -> list[CheckResult]:
        shortest, middle, longest = context.sorted_bbox_dims()
        if shortest <= 1e-6:
            return []

        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None and limit is None:
            target = context.config.thresholds.part_aspect_warn_ratio

        ratio = longest / shortest
        graded = self.graded(ratio, target, limit, "max")
        if graded is None:
            return []

        severity, threshold = graded
        is_plate = middle / shortest >= context.config.thresholds.plate_mid_min_ratio
        if is_plate:
            advice = (
                f"This is a plate: {longest:.0f} x {middle:.0f} mm across and only "
                f"{shortest:.1f} mm thick. Thin plates relieve residual stress as "
                "material comes off and warp away from the table, and unsupported "
                "spans drum under the cutter. Consider stress-relieved stock, "
                "vacuum or tab fixturing, and machining both faces to balance the cut."
            )
        else:
            advice = (
                f"This is a slender bar: {longest:.0f} mm long on a "
                f"{shortest:.1f} mm section. It will deflect away from the cutter "
                "and chatter on unsupported spans. Consider steady support along "
                "its length, lighter radial engagement, or standing it up in a "
                "different orientation."
            )

        message = self.render(
            feedback, severity, ratio, target or 0.0, limit or 0.0, "", advice
        )
        return [
            self.finding(
                rule,
                severity,
                f"{ratio:.1f}:1 {'plate' if is_plate else 'bar'}",
                message,
                value=ratio,
                limit=threshold,
                comparison=">",
            )
        ]


@register_check(Rulebook.MATERIAL_REMOVAL)
class MaterialRemovalCheck(MachiningCheck):
    """How much of the stock ends up as chips.

    The stock model follows the process: a milled part comes from a
    rectangular billet, a turned part from round bar. Using a bounding box
    for a turned part would overstate the waste by the corners that were
    never there.
    """

    @property
    def name(self) -> str:
        return "Material Removal Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        part_volume = context.volume_mm3()
        if part_volume <= 1e-9:
            return []

        stock_volume, stock_shape = self._stock_volume(context)
        if stock_volume <= part_volume:
            return []

        thresholds = context.config.thresholds
        target = self.safe_float(rule_config.target)
        limit = self.safe_float(rule_config.limit)
        if target is None and limit is None:
            target = thresholds.material_removal_warn_pct
            limit = thresholds.material_removal_error_pct

        removed_pct = (1.0 - part_volume / stock_volume) * 100.0
        graded = self.graded(removed_pct, target, limit, "max")
        if graded is None:
            return []

        severity, threshold = graded
        message = self.render(
            feedback,
            severity,
            removed_pct,
            target or 0.0,
            limit or 0.0,
            "%",
            f"{removed_pct:.0f}% of the {stock_shape} stock is cut away to reach "
            f"this part ({stock_volume / 1000.0:.1f} cm3 down to "
            f"{part_volume / 1000.0:.1f} cm3). Most of the cycle time is roughing, "
            "and most of the material is paid for and thrown away. Worth asking "
            "whether a nearer-net blank -- a casting, a weldment, or a smaller "
            "section -- would be cheaper.",
        )
        return [
            self.finding(
                rule,
                severity,
                f"{removed_pct:.0f}% removed",
                message,
                value=removed_pct,
                limit=threshold,
                comparison=">",
                unit="%",
            )
        ]

    def _stock_volume(self, context: MachiningContext) -> tuple[float, str]:
        dx, dy, dz = context.bbox_dims()
        billet = dx * dy * dz

        axis = context.part_process.axis_of_revolution
        if context.is_turning_family and axis is not None:
            length, diameter = PartAspectRatioCheck._axial_extent_and_diameter(context, axis)
            if length > 0.0 and diameter > 0.0:
                bar = math.pi * (diameter / 2.0) ** 2 * length
                if context.process_type is PartProcessType.TURNED:
                    return (bar, "round bar")
                # A mill-turn part may start from either; price the cheaper.
                return (min(bar, billet), "bar" if bar <= billet else "billet")

        return (billet, "billet")


@register_check(Rulebook.SEALED_VOID)
class SealedVoidCheck(MachiningCheck):
    """A cavity with no way in cannot be machined, only assembled around.

    This is the topological case: a closed inner shell, which means a void
    fully enclosed by material. It is an error rather than a warning because
    no amount of clever fixturing recovers it -- the part has to be split.
    """

    @property
    def name(self) -> str:
        return "Sealed Void Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        voids = self._enclosed_shells(context)
        if not voids:
            return []

        severity = self.severity_from_rule_config(rule_config)
        results: list[CheckResult] = []
        for extent, face_ids in voids:
            message = self.render(
                feedback,
                severity,
                extent,
                0.0,
                0.0,
                "mm",
                f"A fully enclosed cavity about {extent:.1f} mm across sits inside "
                "the part with no opening to the outside. No tool can reach it, so "
                "it cannot be machined from solid -- the part would have to be made "
                "in pieces and joined, or built additively.",
            )
            results.append(
                self.finding(
                    rule,
                    severity,
                    f"enclosed cavity {extent:.1f} mm",
                    message,
                    faces=face_ids,
                    value=extent,
                    comparison="=",
                    unit="mm",
                )
            )
        return results

    def _enclosed_shells(self, context: MachiningContext) -> list[tuple[float, list[int]]]:
        """Closed shells that are not the part's outer boundary."""
        shells = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(context.shape, TopAbs_SHELL, shells)
        if shells.Extent() < 2:
            return []  # a single shell is the outer boundary

        outer_volume = -1.0
        measured: list[tuple[float, float, list[int]]] = []
        for index in range(1, shells.Extent() + 1):
            shell = TopoDS.Shell_s(shells.FindKey(index))
            if not shell.Closed():
                continue
            box = Bnd_Box()
            BRepBndLib.Add_s(shell, box)
            if box.IsVoid():
                continue
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            dims = (xmax - xmin, ymax - ymin, zmax - zmin)
            span = max(dims)
            outer_volume = max(outer_volume, dims[0] * dims[1] * dims[2])
            if min(dims) < _MIN_VOID_EXTENT_MM:
                continue
            measured.append((dims[0] * dims[1] * dims[2], span, self._faces_of(context, shell)))

        # The largest closed shell is the part's own boundary, not a void.
        return [
            (span, faces)
            for volume, span, faces in measured
            if volume < outer_volume - 1e-9
        ]

    @staticmethod
    def _faces_of(context: MachiningContext, shell) -> list[int]:
        from OCP.TopAbs import TopAbs_FACE

        faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shell, TopAbs_FACE, faces)
        ids: list[int] = []
        for index in range(1, faces.Extent() + 1):
            face_id = context.face_index.index_of(TopoDS.Face_s(faces.FindKey(index)))
            if face_id:
                ids.append(face_id)
        return sorted(ids)
