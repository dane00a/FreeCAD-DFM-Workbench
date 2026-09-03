# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes stiffening ribs.

A rib is a thin web left standing to stiffen something: a fin on a heat sink, a
gusset under a bracket, the web between two lightening pockets. It is two flat
faces back to back with a little material between them, growing out of a bigger
surface underneath.

The seed is therefore a *pair*, not a face, and the pair has to pass the same
sign test the thin-wall rule uses. Two opposed faces with material between them
bound a wall; two opposed faces with a void between them are the sides of a
slot, and the projection of the offset onto one of the outward normals says
which. Get that backwards and every slot in the part becomes a rib.

Three further guards do the rest of the work. Both faces must be small
relative to the part, or the outside of a shell reads as a rib. They must
actually shadow each other, or two unrelated faces on opposite ends of a plate
pair up through the plate itself and the "rib" spans the whole part. And there
must be a base: a face at the *root* of the wall, perpendicular to it, that the
rib grows out of. A face at the wall's far end is the rib's own top, and an end
cap is not a base at all.

Rib membership is worth getting right beyond the feature list. A field of ribs
is thin everywhere by design, so a thin-feature finding on each web buries the
findings that matter.
"""

from __future__ import annotations

from typing import Optional, Sequence

from OCP.gp import gp_Dir, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer


# A face owning more than this share of the part is bulk geometry -- a shell
# wall, a plate face -- not a web standing on it.
_MAX_WALL_AREA_SHARE = 0.10

# The two webs of a rib face away from each other.
_ANTIPARALLEL_MAX_DOT = -0.9

# They are mirror images of the same protrusion, so their areas are
# comparable. A big outer face paired with a small feature wall is a lip on
# the part's bulk, not a rib.
_MAX_AREA_RATIO = 2.0

# Real machined ribs are thin. Past 5 mm a wall is structure, and it is stiff
# enough that none of the rib rules have anything to say about it.
_MIN_THICKNESS_MM = 0.1
_MAX_THICKNESS_MM = 5.0

# Below this a wall is just a shoulder. Ribs are what stand up.
_MIN_HEIGHT_ASPECT = 3.0

# A base stands square to the web it carries.
_BASE_PERPENDICULAR_MAX_DOT = 0.3

# ...and its own normal runs along the web's height axis this strongly.
_BASE_NORMAL_MIN_DOT = 0.9

# The base sits at the web's root, within this of the wall's lower extent.
_BASE_AT_ROOT_TOLERANCE_MM = 0.5

# A bounding-box extent below this is float noise on a flat face. Deliberately
# far under a millimetre: a sub-millimetre shelf is still a real protrusion,
# and a coarser floor makes the height axis come out as the length axis, which
# reports a rib's run as its standing height.
_MIN_REAL_DIM_MM = 1.0e-3

# A curved body the rib grows out of is clearly larger than the web itself.
_CURVED_BASE_MIN_AREA_RATIO = 2.0

# The root of a web on a curved body wanders with the surface, so the band it
# is looked for in scales with the wall -- its lower third -- with a floor for
# small walls.
_CURVED_ROOT_BAND_FRACTION = 3.0
_CURVED_ROOT_BAND_MIN_MM = 2.0

# Bounding boxes must genuinely overlap, not merely touch at a corner. The
# margin shrugs off the padding a boolean leaves on a box.
_OVERLAP_MARGIN_MM = 0.1

# A normal is taken to run along a world axis past this.
_AXIS_DOMINANT_MIN_DOT = 0.7


class RibRecognizer(FeatureRecognizer):
    """Recognizes stiffening ribs."""

    prefix = "ri"

    @property
    def name(self) -> str:
        return "Rib Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored. The step and boss passes run first
        # and legitimately claim the plate a rib field stands on, and on a part
        # that is mostly ribs a web is often also a wall of the pocket beside
        # it. Honouring the claim set would lose exactly the ribs that matter.
        total_area = graph.total_area()
        if total_area <= 1e-9:
            return []
        ceiling = total_area * _MAX_WALL_AREA_SHARE

        planes = [
            node
            for node in graph.nodes_by_surface_type(SurfaceType.PLANE)
            if node.area <= ceiling and node.outward_normal is not None
        ]

        found: list[FeatureInstance] = []
        taken: set[int] = set()

        for index, first in enumerate(planes):
            if first.face_id in taken:
                continue
            for second in planes[index + 1 :]:
                if second.face_id in taken:
                    continue
                feature = self._pair(graph, first, second, ceiling)
                if feature is None:
                    continue
                found.append(feature)
                taken.add(first.face_id)
                taken.add(second.face_id)
                break  # this web is spoken for; move to the next face

        for order, feature in enumerate(found):
            feature.instance_id = self.instance_id(order)
        return found

    # -- pairing ------------------------------------------------------------

    def _pair(
        self,
        graph: AttributedAdjacencyGraph,
        first: AagNode,
        second: AagNode,
        ceiling: float,
    ) -> Optional[FeatureInstance]:
        normal = first.outward_normal
        other_normal = second.outward_normal
        if normal is None or other_normal is None:
            return None
        if normal.Dot(other_normal) > _ANTIPARALLEL_MAX_DOT:
            return None

        larger = max(first.area, second.area)
        smaller = min(first.area, second.area)
        if smaller < 1e-6 or larger / smaller > _MAX_AREA_RATIO:
            return None

        # The sign test. A rib web's outward normal points away from the rib's
        # material, so its partner sits *behind* that normal and the projection
        # comes out negative. A slot wall's outward normal points into the
        # void, so its partner sits in front of it and the projection is
        # positive -- that is a cavity, not a wall.
        offset = gp_Vec(first.centroid, second.centroid)
        along = offset.Dot(gp_Vec(normal))
        if along > 0.0:
            return None
        thickness = abs(along)
        if thickness < _MIN_THICKNESS_MM or thickness > _MAX_THICKNESS_MM:
            return None

        if not self._connected_within_two_hops(graph, first, second):
            return None

        base, curved_height_axis = self._find_base(graph, first, second)
        if base is None:
            return None

        if not self._shadow_each_other(first, second, normal):
            return None

        if curved_height_axis is None:
            base_normal = base.outward_normal
            if base_normal is None:
                return None
        else:
            # No plane to take a direction from, so the wall's own height axis
            # stands in. Only magnitudes are read downstream, so the sign of
            # this direction does not matter.
            base_normal = _world_axis(curved_height_axis)

        height, length = self._span(first, normal, base_normal)
        if height / thickness < _MIN_HEIGHT_ASPECT:
            return None

        faces = [first.face_id, second.face_id]
        faces.extend(self._caps(graph, first, second, base, ceiling))

        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.RIB,
            faces=faces,
            parameters={
                "height_mm": round(height, 6),
                "thickness_mm": round(thickness, 6),
                "length_mm": round(length, 6),
            },
        )

    # -- guards -------------------------------------------------------------

    @staticmethod
    def _connected_within_two_hops(
        graph: AttributedAdjacencyGraph, first: AagNode, second: AagNode
    ) -> bool:
        """Whether the two webs belong to the same lump of material.

        Two hops rather than one: a rib whose top is broken by a chamfer or a
        fillet reaches its other side through that blend rather than directly.
        """
        adjacent = set(graph.neighbors_of(first.face_id))
        if second.face_id in adjacent:
            return True
        reachable = set(adjacent)
        for face_id in sorted(adjacent):
            reachable.update(graph.neighbors_of(face_id))
        return second.face_id in reachable

    @staticmethod
    def _shadow_each_other(
        first: AagNode, second: AagNode, normal: gp_Dir
    ) -> bool:
        """Whether the two webs actually front each other.

        Two-hop connectivity is satisfied by any two opposed faces linked
        through a shared bulk face, however far apart they sit -- two edges of
        the same plate, sixty millimetres away from each other, join up that
        way and produce a phantom rib that highlights the whole plate. A real
        web bounds a continuous slab, so the two faces overlap when projected
        onto the wall plane.
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
            if a[axis + 3] <= b[axis] + _OVERLAP_MARGIN_MM:
                return False
            if b[axis + 3] <= a[axis] + _OVERLAP_MARGIN_MM:
                return False
        return True

    # -- the base -----------------------------------------------------------

    def _find_base(
        self, graph: AttributedAdjacencyGraph, first: AagNode, second: AagNode
    ) -> tuple[Optional[AagNode], Optional[int]]:
        """The face the rib grows out of, and the height axis if it is curved.

        Tried on both webs, planar path first. The height axis comes back only
        for the curved path, where there is no base normal to measure against.
        """
        for wall in (first, second):
            base = self._planar_base(graph, wall)
            if base is not None:
                return base, None
        for wall in (first, second):
            base = self._curved_base(graph, wall)
            if base is not None:
                return base, _height_axis(wall)
        return None, None

    @staticmethod
    def _planar_base(
        graph: AttributedAdjacencyGraph, wall: AagNode
    ) -> Optional[AagNode]:
        """The plate a web stands on, found by where it sits rather than by size.

        The structural signature of a base is that the wall grows *up out of*
        it: perpendicular to the web, normal along the web's height axis, and
        sitting at the web's lower extent along that axis. A face at the upper
        extent is the rib's own top, and a perpendicular face along the length
        axis is an end cap. The largest qualifying face wins.
        """
        axis = _height_axis(wall)
        if axis is None:
            return None
        wall_normal = wall.outward_normal
        if wall_normal is None or wall.bbox.IsVoid():
            return None
        root = wall.bbox.Get()[axis]

        best: Optional[AagNode] = None
        best_area = 0.0
        for face_id in graph.neighbors_of(wall.face_id):
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            if abs(normal.Dot(wall_normal)) > _BASE_PERPENDICULAR_MAX_DOT:
                continue
            if abs(_component(normal, axis)) < _BASE_NORMAL_MIN_DOT:
                continue
            centre = (node.centroid.X(), node.centroid.Y(), node.centroid.Z())
            if abs(centre[axis] - root) > _BASE_AT_ROOT_TOLERANCE_MM:
                continue
            if node.area > best_area:
                best = node
                best_area = node.area
        return best

    @staticmethod
    def _curved_base(
        graph: AttributedAdjacencyGraph, wall: AagNode
    ) -> Optional[AagNode]:
        """The same thing on a body that is not flat.

        A web bridging a revolved or freeform flank grows out of a curved bulk
        surface, where "perpendicular normal at the lower extent" means
        nothing. The curved equivalent is a concave edge from the web to a much
        larger non-planar face, with the junction low on the web. Without this
        such webs are featureless, and their roots then fire as anonymous
        sharp-corner noise instead of one rib finding.
        """
        axis = _height_axis(wall)
        if axis is None or wall.bbox.IsVoid():
            return None
        bounds = wall.bbox.Get()
        root = bounds[axis]
        span = bounds[axis + 3] - bounds[axis]
        band = max(_CURVED_ROOT_BAND_MIN_MM, span / _CURVED_ROOT_BAND_FRACTION)

        best: Optional[AagNode] = None
        best_area = 0.0
        for edge in sorted(
            graph.concave_edges_of(wall.face_id),
            key=lambda e: e.other_face(wall.face_id),
        ):
            face_id = edge.other_face(wall.face_id)
            if not graph.has_node(face_id) or edge.midpoint is None:
                continue
            node = graph.node(face_id)
            if node.surface_type is SurfaceType.PLANE:
                continue
            if node.area < wall.area * _CURVED_BASE_MIN_AREA_RATIO:
                continue
            midpoint = (
                edge.midpoint.X(),
                edge.midpoint.Y(),
                edge.midpoint.Z(),
            )
            if abs(midpoint[axis] - root) > band:
                continue
            if node.area > best_area:
                best = node
                best_area = node.area
        return best

    # -- measurement --------------------------------------------------------

    @staticmethod
    def _span(
        wall: AagNode, normal: gp_Dir, base_normal: gp_Dir
    ) -> tuple[float, float]:
        """How tall the rib stands and how far it runs.

        Measured along named axes rather than by sorting the bounding box: a
        long low rib has its run as the largest dimension, and sorting reports
        that as the height, which is the one number the rib rules read.
        """
        dx, dy, dz = wall.bbox_dims()
        along = gp_Vec(normal).Crossed(gp_Vec(base_normal))
        if along.Magnitude() <= 1e-6:
            # Degenerate: the web is coplanar with its base. Nothing sensible
            # to project onto, so fall back to the sorted extents.
            ordered = sorted((dx, dy, dz))
            return ordered[2], ordered[1]
        along.Normalize()
        height = (
            abs(dx * base_normal.X())
            + abs(dy * base_normal.Y())
            + abs(dz * base_normal.Z())
        )
        length = abs(dx * along.X()) + abs(dy * along.Y()) + abs(dz * along.Z())
        return height, length

    @staticmethod
    def _caps(
        graph: AttributedAdjacencyGraph,
        first: AagNode,
        second: AagNode,
        base: AagNode,
        ceiling: float,
    ) -> list[int]:
        """The rib's top and end walls: faces convex to both webs.

        The base is convex to both webs on some parts and would be pulled in
        too, so it is excluded by name. Every other bulk face is excluded by
        size. A rib bridging two plates has the far plate as a shared convex
        neighbour, and letting it in makes the finding highlight the plate
        rather than the web -- the rib itself is still real, so it stays; only
        the plate is kept out of its face list.
        """
        shared = {
            edge.other_face(first.face_id)
            for edge in graph.convex_edges_of(first.face_id)
        }
        caps: list[int] = []
        for face_id in sorted(
            {
                edge.other_face(second.face_id)
                for edge in graph.convex_edges_of(second.face_id)
            }
        ):
            if face_id == base.face_id or face_id not in shared:
                continue
            if not graph.has_node(face_id):
                continue
            if graph.node(face_id).area > ceiling:
                continue
            caps.append(face_id)
        return caps


# =============================================================================
# Helpers
# =============================================================================


def _component(direction: gp_Dir, axis: int) -> float:
    return (direction.X(), direction.Y(), direction.Z())[axis]


def _world_axis(axis: int) -> gp_Dir:
    return gp_Dir(*(1.0 if i == axis else 0.0 for i in range(3)))


def _height_axis(wall: AagNode) -> Optional[int]:
    """Which world axis a web stands up along.

    Of the two axes that are not the web's thickness direction, the shorter is
    its height and the longer its run. A rib taller than it is long is not
    machined geometry -- that shape gets read as a boss.
    """
    normal = wall.outward_normal
    if normal is None:
        return None
    thickness_axis = _thickness_axis(normal)
    dims = wall.bbox_dims()

    best: Optional[int] = None
    best_dim = float("inf")
    for axis in range(3):
        if axis == thickness_axis:
            continue
        if _MIN_REAL_DIM_MM < dims[axis] < best_dim:
            best_dim = dims[axis]
            best = axis
    return best


def _thickness_axis(normal: gp_Dir) -> int:
    """The world axis a planar web's normal runs along."""
    if abs(normal.X()) > _AXIS_DOMINANT_MIN_DOT:
        return 0
    if abs(normal.Y()) > _AXIS_DOMINANT_MIN_DOT:
        return 1
    return 2
