# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes press-brake bends.

A bend is where the brake folded the blank. In the B-rep it arrives as a
matched pair of coaxial cylinders -- the concave inside of the fold at radius
r, and the convex outside at r plus the gauge -- bridging two flat panels
through tangent edges.

The radial offset between the two cylinders *is* the material thickness, and
that is the whole discriminator. A machined fillet has an inside radius and
nothing behind it; only metal that was folded carries its own outside radius
one gauge out on the same axis. It is the same signature the sheet-metal
classifier keys on, which is why a part that has no bends is not sheet in the
first place.

Bends run on sheet parts only. On a milled part the same pair of coaxial
cylinders is a bore inside a boss, so the recognizer stands down entirely
unless the analyzer classified the part as sheet metal.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Ax1, gp_Pnt, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import FeatureInstance, FeatureType
from ..process_classifier import PartProcessType
from .base import FeatureRecognizer, axes_are_coaxial, cylinder_length, neighbours


# The feature type this recognizer emits. Spelled out here until
# `features.FeatureType` carries it: the string is the contract, because it is
# what rules match on and what a saved analysis stores.
BEND = FeatureType.BEND


# The two cylinders of one bend are the same fold seen from both sides, so
# their axes are the same line to well inside modelling noise -- much tighter
# than the general coaxial test, which has to tolerate separately modelled
# bores.
_AXIS_DOT_TOL = 0.999
_LINE_DISTANCE_TOL_MM = 0.2

# How far the measured radial offset may sit from the classified gauge before
# the pair is something other than a fold.
_GAUGE_TOL_MM = 0.2

# Several bends can share one fold axis line: think two parallel flanges
# brought up off one base, whose bend lines happen to be colinear. Without an
# axial overlap test the first band's inside skin pairs with the second band's
# outside skin and the result is a chimera bend spanning two real ones. The
# margin keeps two bands that merely abut from counting as overlapping.
_AXIAL_OVERLAP_MARGIN_MM = 0.5

# Two planar fragments are the same panel when their outward normals agree
# and they lie in one plane. Boolean seams split a physical flat into several
# coplanar faces, all of them tangent to the bend cylinder, and each one would
# otherwise count as a panel of its own.
_COPLANAR_MIN_DOT = 0.999
_COPLANAR_OFFSET_MM = 0.1

# A bend joins sheet FLATS. The little tangent walls at the rounded end of an
# emboss or a cutout are also a coaxial gauge-offset pair with tangent
# neighbours -- a perfect counterfeit hem -- and this is what refuses them:
# a real panel is many times the gauge across, so its area dwarfs the gauge
# squared. Expressed in gauges so it scales with the material.
_PANEL_AREA_FLOOR_GAUGES = 20.0

# Folded back on itself. At this angle the two panels are face to face and the
# brake made a hem, not a corner.
_HEM_MIN_ANGLE_DEG = 175.0


class BendRecognizer(FeatureRecognizer):
    """Recognizes bends on sheet-metal parts."""

    prefix = "bd"

    @property
    def name(self) -> str:
        return "Bend Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        gauge = self._sheet_gauge()
        if gauge <= 0.0:
            return []  # no gauge, no sheet, no bends

        cylinders = graph.nodes_by_surface_type(SurfaceType.CYLINDER)
        taken: set[int] = set()
        found: list[FeatureInstance] = []

        # Seed on the INSIDE face of the pair: the concave cylinder, the one
        # that looks into the fold the way a bore looks into its hole. Its
        # convex partner sits one gauge further out on the same axis.
        for inner in cylinders:
            if not inner.is_internal or inner.cyl_cone_axis is None:
                continue
            if inner.face_id in taken:
                continue

            outer = self._outside_face(cylinders, inner, gauge, taken)
            if outer is None:
                continue

            panels = self._panel_clusters(graph, inner)
            if len(panels) != 2:
                continue

            floor = _PANEL_AREA_FLOOR_GAUGES * gauge * gauge
            if panels[0][1] < floor or panels[1][1] < floor:
                continue

            first = panels[0][0].outward_normal
            second = panels[1][0].outward_normal
            if first is None or second is None:
                continue
            # The bend angle read off the panels themselves: 90 degrees for a
            # right-angle corner, approaching 180 for a hem.
            angle_deg = math.degrees(
                math.acos(max(-1.0, min(1.0, first.Dot(second))))
            )

            axis = inner.cyl_cone_axis
            direction = axis.Direction()
            origin = axis.Location()
            taken.add(inner.face_id)
            taken.add(outer.face_id)
            found.append(
                FeatureInstance(
                    instance_id=self.instance_id(len(found)),
                    type=BEND,
                    faces=[inner.face_id, outer.face_id],
                    parameters={
                        "inner_radius_mm": round(inner.cyl_radius, 6),
                        "outer_radius_mm": round(outer.cyl_radius, 6),
                        "thickness_mm": round(
                            outer.cyl_radius - inner.cyl_radius, 6
                        ),
                        "angle_deg": round(angle_deg, 6),
                        "length_mm": round(cylinder_length(inner), 6),
                        "axis": [direction.X(), direction.Y(), direction.Z()],
                        "axis_origin": [origin.X(), origin.Y(), origin.Z()],
                        "panel_a": panels[0][0].face_id,
                        "panel_b": panels[1][0].face_id,
                        "is_hem": angle_deg >= _HEM_MIN_ANGLE_DEG,
                    },
                )
            )

        return found

    # -- gating ---------------------------------------------------------------

    def _sheet_gauge(self) -> float:
        """The classified sheet thickness, or zero when the part is not sheet.

        The recognizer is handed the classification rather than working it out
        again, and it is the gate as much as the measurement: on a milled part
        a coaxial pair of cylinders is a bore inside a boss, and calling that
        a bend would put brake rules on a part that never sees a brake.
        """
        process = getattr(self, "part_process", None)
        if process is None or process.type is not PartProcessType.SHEET_METAL:
            return 0.0
        return float(getattr(process, "sheet_thickness_mm", 0.0) or 0.0)

    # -- the pair -------------------------------------------------------------

    @staticmethod
    def _outside_face(
        cylinders: Sequence[AagNode],
        inner: AagNode,
        gauge: float,
        taken: set[int],
    ) -> Optional[AagNode]:
        """The convex cylinder one gauge out on the inside face's own axis."""
        axis = inner.cyl_cone_axis
        low, high = _axial_span(inner, axis)

        for candidate in cylinders:
            if candidate.is_internal or candidate.cyl_cone_axis is None:
                continue
            if candidate.face_id in taken:
                continue
            if abs((candidate.cyl_radius - inner.cyl_radius) - gauge) > _GAUGE_TOL_MM:
                continue
            if not axes_are_coaxial(
                candidate.cyl_cone_axis,
                axis,
                direction_dot=_AXIS_DOT_TOL,
                line_distance=_LINE_DISTANCE_TOL_MM,
            ):
                continue
            candidate_low, candidate_high = _axial_span(candidate, axis)
            if candidate_high < low + _AXIAL_OVERLAP_MARGIN_MM:
                continue
            if candidate_low > high - _AXIAL_OVERLAP_MARGIN_MM:
                continue
            return candidate
        return None

    # -- the panels -----------------------------------------------------------

    @staticmethod
    def _panel_clusters(
        graph: AttributedAdjacencyGraph, inner: AagNode
    ) -> list[tuple[AagNode, float]]:
        """The flats the fold blends into, one entry per physical panel.

        A bend has exactly two panels, but a Boolean fuse seam splits one
        physical flat into several coplanar fragments and every one of them is
        tangent to the bend cylinder. Clustering by plane is what keeps a
        seamed flange from reading as two panels -- and it is also what still
        refuses a filleted bore rim, which has only one flat to give.

        Each cluster is represented by its largest fragment and carries the
        summed area of the whole panel.
        """
        clusters: list[tuple[AagNode, float]] = []
        for other, edge in neighbours(graph, inner.face_id):
            if edge.concavity is not Concavity.TANGENT:
                continue
            if other.surface_type is not SurfaceType.PLANE:
                continue
            normal = other.outward_normal
            if normal is None:
                continue

            merged = False
            for index, (representative, area) in enumerate(clusters):
                existing = representative.outward_normal
                if existing is None or existing.Dot(normal) < _COPLANAR_MIN_DOT:
                    continue
                offset = gp_Vec(representative.centroid, other.centroid).Dot(
                    gp_Vec(existing)
                )
                if abs(offset) > _COPLANAR_OFFSET_MM:
                    continue
                keep = other if other.area > representative.area else representative
                clusters[index] = (keep, area + other.area)
                merged = True
                break

            if not merged:
                clusters.append((other, other.area))

        return clusters


# =============================================================================
# Geometry helpers
# =============================================================================


def _axial_span(node: AagNode, axis: gp_Ax1) -> tuple[float, float]:
    """How far a face reaches along an axis, from the axis origin.

    Taken from the bounding box rather than the trimmed parameter range so the
    two faces of a pair are measured on one common axis -- the inside face's
    -- and their overlap means the same thing for both.
    """
    if node.bbox.IsVoid():
        return (0.0, 0.0)
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    origin = axis.Location()
    direction = gp_Vec(axis.Direction())
    values = [
        gp_Vec(origin, gp_Pnt(x, y, z)).Dot(direction)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
        for z in (zmin, zmax)
    ]
    return (min(values), max(values))
