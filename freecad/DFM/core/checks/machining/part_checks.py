# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Whole-part rules: proportion, stock waste, and enclosed voids."""

from __future__ import annotations

import math
from typing import Optional

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_Plane
from OCP.GProp import GProp_GProps
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.gp import gp_Pnt, gp_Vec
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape

from ...machining.aag import SurfaceType
from ...machining.context import MachiningContext
from ...machining.process_classifier import PartProcessType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck
from ...utils.geometry import face_area


# A void smaller than this is modelling noise, not a trapped cavity.
_MIN_VOID_EXTENT_MM = 0.5

# How closely a cylinder's axis must run with the part's to be its OD.
_PARALLEL_AXIS_DOT = 0.99


def _optimal_part_box(context: MachiningContext) -> Optional[tuple]:
    """The part's true axis-aligned extremes, or None for an empty shape.

    Slenderness is measured, not accumulated. A box summed over every face
    over-reports the part, because OpenCascade pads a curved face's box to
    its control hull; on B-spline engraving that is millimetres, the same on
    every axis. Adding the same amount to the longest and the shortest side
    always shrinks their ratio, so a padded box can only hide a slender part,
    never invent one.

    A plane-only box is not the remedy here as it is in the sheet detectors:
    a general part need not be bounded by its planes (a dome's only plane is
    its base disc, which would make it zero thick). ``AddOptimal`` solves for
    the real extremes on any topology, without triangulation or tolerance.
    """
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(context.shape, box, False, False)
    if box.IsVoid():
        return None
    return box.Get()


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
        # A turned part with an axis and an OD on it is judged on length over
        # diameter and only on that. Without either it falls through to the
        # box, as the reference's rule does.
        if context.is_turning_family:
            turned = self._turned(context, rule_config, rule, feedback)
            if turned is not None:
                return turned
        return self._milled(context, rule_config, rule, feedback)

    # -- turning ------------------------------------------------------------

    def _turned(
        self, context: MachiningContext, rule_config, rule, feedback
    ) -> Optional[list[CheckResult]]:
        """Length over OD, or None when there is no axis or no OD to read.

        The limits are the turning thresholds unless the process states a
        turning pair. A turning process gives both a warning and an error
        ratio (and tunes them per material: stainless overhangs less happily
        than aluminium). A milling process gives only the box's 8:1, with no
        error tier because the box rule has none -- and reading that 8:1 here
        hid every shaft between 4:1 and 8:1. Both bounds are inclusive.
        """
        axis = context.part_process.axis_of_revolution
        if axis is None:
            return None

        length, diameter = self._axial_extent_and_diameter(context, axis)
        if diameter <= 0.0:
            return None
        if length <= diameter:
            return []  # a disc is not slender, however wide

        thresholds = context.config.thresholds
        target = thresholds.turn_slender_warn_ratio
        limit = thresholds.turn_slender_error_ratio
        configured_target = self.safe_float(rule_config.target)
        configured_limit = self.safe_float(rule_config.limit)
        if configured_target is not None and configured_limit is not None:
            target, limit = configured_target, configured_limit

        ratio = length / diameter
        if ratio >= limit:
            severity, threshold = Severity.ERROR, limit
        elif ratio >= target:
            severity, threshold = Severity.WARNING, target
        else:
            return []
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
            target,
            limit,
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

        The diameter is the largest cylinder whose axis runs parallel to the
        part's, as the reference reads it: parallel rather than colinear, and
        cylinders only (a cone carries no radius of its own there). Zero
        means there is no turned OD to measure.
        """
        direction = axis.Direction()
        origin = axis.Location()
        low, high = math.inf, -math.inf

        # The length is read off the corners of the measured part box, as the
        # reference does, not off every face's padded box.
        box = _optimal_part_box(context)
        if box is None:
            return (0.0, 0.0)
        xmin, ymin, zmin, xmax, ymax, zmax = box
        for x in (xmin, xmax):
            for y in (ymin, ymax):
                for z in (zmin, zmax):
                    along = gp_Vec(origin, gp_Pnt(x, y, z)).Dot(gp_Vec(direction))
                    low = min(low, along)
                    high = max(high, along)

        radius = 0.0
        for node in context.graph.nodes:
            if node.surface_type is not SurfaceType.CYLINDER:
                continue
            if node.cyl_cone_axis is None:
                continue
            if abs(node.cyl_cone_axis.Direction().Dot(direction)) > _PARALLEL_AXIS_DOT:
                radius = max(radius, node.cyl_radius)

        return (high - low, radius * 2.0)

    # -- milling ------------------------------------------------------------

    def _milled(self, context: MachiningContext, rule_config, rule, feedback) -> list[CheckResult]:
        box = _optimal_part_box(context)
        if box is None:
            return []
        xmin, ymin, zmin, xmax, ymax, zmax = box
        shortest, middle, longest = sorted((xmax - xmin, ymax - ymin, zmax - zmin))
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
        """The blank this part is cut from, as the reference prices it.

        A box first: the union of the faces' own boxes. The whole-shape box
        can be far looser (on nist_ctc_04 it stands 162 mm tall over faces
        that span 131 mm). A profile-extrusion blank is the drawn bar instead
        when that is smaller. A turned part is round bar and a mill-turn part
        the cheaper of bar and box, the bar measured on the part's vertices:
        box corners stand outside a cylinder and overstate its radius.
        """
        billet = _face_box_volume(context)
        stock, shape = billet, "billet"

        axis = context.part_process.axis_of_revolution
        if getattr(context.part_process, "blank", "") == "profile_extrusion" and axis is not None:
            bar = _profile_bar_volume(context, axis)
            if 1.0 < bar < billet:
                stock, shape = bar, "profile bar"

        if context.is_turning_family and axis is not None:
            cylinder = _vertex_cylinder_volume(context, axis)
            if cylinder > 1.0:
                if context.process_type is PartProcessType.TURNED:
                    stock, shape = cylinder, "round bar"
                elif cylinder < billet:
                    # A mill-turn part may start from either; price the
                    # cheaper. Compared with the box, as the reference does,
                    # whatever the profile branch chose.
                    stock, shape = cylinder, "bar"

        return (stock, shape)


def _vertex_cylinder_volume(context: MachiningContext, axis) -> float:
    """The smallest cylinder about the axis holding every vertex of the part."""
    origin = axis.Location()
    direction = gp_Vec(axis.Direction())
    low, high = math.inf, -math.inf
    reach = 0.0
    explorer = TopExp_Explorer(context.shape, TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        offset = gp_Vec(origin, point)
        along = offset.Dot(direction)
        low = min(low, along)
        high = max(high, along)
        radial = offset - direction.Multiplied(along)
        reach = max(reach, radial.SquareMagnitude())
        explorer.Next()
    if low is math.inf:
        return 0.0
    return math.pi * reach * (high - low)


def _profile_bar_volume(context: MachiningContext, axis) -> float:
    """Cross-section times cut length, for a part drawn or broached as a bar.

    The section is the outer wire of the largest planar end cap: inner wires
    are end-drilled bores, and the die's profile still holds that material.
    """
    origin = axis.Location()
    direction = axis.Direction()
    along_axis = gp_Vec(direction)
    low, high = math.inf, -math.inf
    explorer = TopExp_Explorer(context.shape, TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        along = gp_Vec(origin, point).Dot(along_axis)
        low = min(low, along)
        high = max(high, along)
        explorer.Next()
    if low is math.inf:
        return 0.0

    area = 0.0
    explorer = TopExp_Explorer(context.shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        explorer.Next()
        adaptor = BRepAdaptor_Surface(face)
        if adaptor.GetType() != GeomAbs_Plane:
            continue
        plane = adaptor.Plane()
        if abs(plane.Axis().Direction().Dot(direction)) < 0.99:
            continue
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        at = gp_Vec(origin, props.CentreOfMass()).Dot(along_axis)
        if abs(at - low) > 0.5 and abs(at - high) > 0.5:
            continue
        outer = BRepTools.OuterWire_s(face)
        if outer.IsNull():
            continue
        filled = BRepBuilderAPI_MakeFace(plane, outer)
        if not filled.IsDone():
            continue
        area = max(area, abs(face_area(filled.Face())))
    return area * (high - low)


def _face_box_volume(context: MachiningContext) -> float:
    """Volume of the box enclosing every face's bounding box."""
    low = [1e18, 1e18, 1e18]
    high = [-1e18, -1e18, -1e18]
    for node in context.graph.nodes:
        if node.bbox.IsVoid():
            continue
        corners = node.bbox.Get()
        for k in range(3):
            low[k] = min(low[k], corners[k])
            high[k] = max(high[k], corners[k + 3])
    if low[0] > high[0]:
        dx, dy, dz = context.bbox_dims()
        return dx * dy * dz
    return (high[0] - low[0]) * (high[1] - low[1]) * (high[2] - low[2])


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
