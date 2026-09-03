# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Base class and shared geometry helpers for feature recognizers.

Recognizers work almost entirely on the adjacency graph rather than on the
shape, which is what keeps them fast: the kernel cost was paid once when the
graph was built. They are stateless between parts -- everything they know
arrives as arguments.

Order matters. Recognizers run in a fixed sequence and later ones are told
what earlier ones already claimed, so a groove does not re-recognize the bore
it sits in.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance


class FeatureRecognizer:
    """Finds one kind of feature in the adjacency graph."""

    #: Short prefix for generated instance ids, e.g. "h" for holes.
    prefix = "f"

    #: The shop's configuration, set by the analyzer before each run. Only a
    #: few recognizers care what tools the shop owns; the rest ignore it.
    config = None

    #: How the analyzer classified the part. A recognizer that reads it is
    #: asking "is this turned", which changes what a surface of revolution
    #: means -- a profile on a lathe, sculpture anywhere else.
    part_process = None

    @property
    def name(self) -> str:
        raise NotImplementedError

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        """Return the features found. Must not mutate the graph."""
        raise NotImplementedError

    def instance_id(self, index: int) -> str:
        return f"{self.prefix}_{index}"


# =============================================================================
# Geometry helpers
# =============================================================================


def axis_direction(node: AagNode) -> Optional[gp_Dir]:
    """The revolution axis direction a face carries, if any."""
    if node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
        return node.cyl_cone_axis.Direction() if node.cyl_cone_axis else None
    if node.surface_type is SurfaceType.REVOLVED:
        return node.revolved_axis.Direction() if node.revolved_axis else None
    return None


def axes_are_coaxial(
    a: gp_Ax1, b: gp_Ax1, direction_dot: float = 0.98, line_distance: float = 0.5
) -> bool:
    """Whether two axes describe the same line, within tolerance."""
    if abs(a.Direction().Dot(b.Direction())) < direction_dot:
        return False
    offset = gp_Vec(a.Location(), b.Location())
    return offset.Crossed(gp_Vec(a.Direction())).Magnitude() < line_distance


def axial_coordinate(point: gp_Pnt, origin: gp_Pnt, direction: gp_Dir) -> float:
    """Distance along an axis from its origin to the point's projection."""
    return gp_Vec(origin, point).Dot(gp_Vec(direction))


def radial_distance(point: gp_Pnt, origin: gp_Pnt, direction: gp_Dir) -> float:
    """Perpendicular distance from an axis to a point."""
    return gp_Vec(origin, point).CrossMagnitude(gp_Vec(direction))


def outward_normal(node: AagNode) -> Optional[gp_Dir]:
    return node.outward_normal


def cylinder_wrap(node: AagNode) -> float:
    """How much of a full revolution a cylindrical face covers, 0 to 1.

    A drilled bore wraps fully. A fillet band running along an edge covers a
    quarter turn or less, which is how the two are told apart.
    """
    if node.surface_type is not SurfaceType.CYLINDER or node.cyl_radius <= 1e-9:
        return 1.0
    if node.cyl_p0 is None or node.cyl_p1 is None:
        return 1.0
    length = node.cyl_p0.Distance(node.cyl_p1)
    full_area = 2.0 * math.pi * node.cyl_radius * length
    return node.area / full_area if full_area > 1e-9 else 1.0


def cylinder_length(node: AagNode) -> float:
    """Axial extent of a cylindrical face."""
    if node.cyl_p0 is None or node.cyl_p1 is None:
        return 0.0
    return node.cyl_p0.Distance(node.cyl_p1)


def cross_section_area(node: AagNode) -> float:
    return math.pi * node.cyl_radius * node.cyl_radius


def neighbours(
    graph: AttributedAdjacencyGraph, face_id: int
) -> list[tuple[AagNode, "object"]]:
    """Adjacent nodes paired with the edge that reaches them.

    Sorted by face id so a recognizer's first-match-wins decisions are
    reproducible; dictionary or set ordering must never decide which of two
    equally valid candidates is chosen.
    """
    found = []
    for edge in graph.edges_of(face_id):
        other = edge.other_face(face_id)
        if graph.has_node(other):
            found.append((graph.node(other), edge))
    return sorted(found, key=lambda pair: pair[0].face_id)


def shares_inner_wire_with(
    graph: AttributedAdjacencyGraph, face_id: int, other_face_id: int
) -> bool:
    """Whether the two faces meet along an edge on an inner wire.

    For a plain bore this means the bore passes *through* the other face: the
    cylinder itself has no inner wires, so the inner wire must belong to the
    face it pierces.
    """
    for edge in graph.edges_of(face_id):
        if edge.is_inner_wire_edge and edge.other_face(face_id) == other_face_id:
            return True
    return False
