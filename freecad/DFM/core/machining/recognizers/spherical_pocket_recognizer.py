# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes ball-ended pockets.

A spherical pocket is a bowl sunk into the part: a concave patch of sphere,
reached by a ball endmill or a form tool. What matters is not the bowl but its
rim. Sink the ball past the equator and the opening is narrower than the
cavity behind it -- a super-hemispherical bowl is an undercut, and nothing
that goes in straight comes back out.

Two things make the reading harder than it sounds. A kernel is free to split
one sphere across several patches divided by great-circle seams, and then no
single face owns the rim; the patches of one sphere are therefore clustered
first and the opening read off the cluster boundary. And a spherical patch is
also what appears where several constant-radius fillets meet at a corner. That
blend carries the same concave sphere geometry as a bowl, so it is told apart
by its rim: a corner blend is hemmed in entirely by fillets of its own radius,
while a real bowl opens onto the surface it was sunk into.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional, Sequence

from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from ..aag import AagEdge, AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer


# Two patches belong to the same sphere when centre and radius agree this
# closely. Tight on purpose: concentric bowls of nearly equal radius are
# separate features.
_SAME_SPHERE_TOL_MM = 1e-4

# A rim fillet counts as the same radius as the bowl within this share of it.
# A ball-endmill corner blend has fillet radius equal to sphere radius by
# construction, so the match is what marks the blend.
_FILLET_RADIUS_TOL_SHARE = 0.05

# The equator has to be buried by more than rounding noise before the bowl is
# called an undercut: an exact hemisphere sits at zero and overhangs nothing.
_SUPER_HEMISPHERICAL_TOL_MM = 1e-6


class SphericalPocketRecognizer(FeatureRecognizer):
    """Recognizes ball-ended pockets."""

    prefix = "sph"

    @property
    def name(self) -> str:
        return "Spherical Pocket Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        found: list[FeatureInstance] = []

        for cluster in self._sphere_clusters(graph):
            seed = graph.node(cluster[0])

            # A patch rimmed entirely by fillets of its own radius is the
            # blend where several of them meet at a corner, not a bowl. Left
            # in, every rounded corner of a filleted pocket reads as a
            # super-hemispherical undercut -- on geometry a ball endmill of
            # the fillet radius rolls straight through.
            if self._is_fillet_corner_blend(graph, cluster, seed.sphere_radius):
                continue

            # A single patch that owns its own cap circle can be measured from
            # that circle alone. Everything else -- split bowls, and patches
            # whose rim is a great circle rather than a small cap -- has to be
            # read off the cluster boundary.
            if len(cluster) > 1 or not seed.sphere_has_clip:
                feature = self._from_cluster(graph, cluster)
            else:
                feature = self._from_face(seed)
            if feature is None:
                continue
            found.append(feature)

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- clustering ---------------------------------------------------------

    @staticmethod
    def _sphere_clusters(graph: AttributedAdjacencyGraph) -> list[list[int]]:
        """Connected groups of concave patches that share one sphere.

        Membership is decided on the material side, not on edge signs: a
        bowl has the material outside the sphere, a dome boss has it inside.
        Reading the rim instead would fail either way round, because a bowl's
        rim is physically convex -- it is the edge you deburr.
        """
        members = {
            node.face_id
            for node in graph.nodes
            if node.surface_type is SurfaceType.SPHERE
            and node.is_internal
            and node.sphere_radius > 0.0
            and node.sphere_center is not None
        }
        if not members:
            return []

        clusters: list[list[int]] = []
        seen: set[int] = set()
        for node in graph.nodes:  # ascending face id, so grouping is stable
            if node.face_id not in members or node.face_id in seen:
                continue
            cluster: list[int] = []
            queue: deque[int] = deque([node.face_id])
            seen.add(node.face_id)
            while queue:
                current = queue.popleft()
                cluster.append(current)
                here = graph.node(current)
                for other_id in graph.neighbors_of(current):
                    if other_id not in members or other_id in seen:
                        continue
                    other = graph.node(other_id)
                    if (
                        abs(here.sphere_radius - other.sphere_radius)
                        >= _SAME_SPHERE_TOL_MM
                    ):
                        continue
                    if (
                        here.sphere_center.Distance(other.sphere_center)
                        >= _SAME_SPHERE_TOL_MM
                    ):
                        continue
                    seen.add(other_id)
                    queue.append(other_id)
            clusters.append(sorted(cluster))
        return clusters

    @staticmethod
    def _boundary_edges(
        graph: AttributedAdjacencyGraph, cluster: list[int]
    ) -> list[AagEdge]:
        """Edges with exactly one end inside the cluster.

        The seams between a split bowl's own patches are interior and say
        nothing about where it opens; the rim is what is left.
        """
        inside = set(cluster)
        boundary: list[AagEdge] = []
        seen: set[tuple[int, int]] = set()
        for face_id in cluster:
            for edge in graph.edges_of(face_id):
                if edge.face_id_a in inside and edge.face_id_b in inside:
                    continue
                key = (edge.face_id_a, edge.face_id_b)
                if key in seen:
                    continue
                seen.add(key)
                boundary.append(edge)
        return sorted(boundary, key=lambda e: (e.face_id_a, e.face_id_b))

    @staticmethod
    def _is_fillet_corner_blend(
        graph: AttributedAdjacencyGraph, cluster: list[int], radius: float
    ) -> bool:
        """Whether the patch is a fillet corner rather than a bowl.

        A genuine bowl opens onto at least one face that is not a fillet of
        its own radius -- the surface it was sunk into, or a rim fillet ground
        to some other radius. Either leaves a non-matching neighbour.
        """
        if radius <= 0.0:
            return False
        inside = set(cluster)
        tolerance = _FILLET_RADIUS_TOL_SHARE * radius
        fillet_neighbours = 0
        for face_id in cluster:
            for other_id in graph.neighbors_of(face_id):
                if other_id in inside:
                    continue  # a seam within the sphere itself
                other = graph.node(other_id)
                matches = (
                    other.surface_type is SurfaceType.CYLINDER
                    and abs(other.cyl_radius - radius) < tolerance
                ) or (
                    other.surface_type is SurfaceType.TORUS
                    and abs(other.torus_minor_r - radius) < tolerance
                )
                if not matches:
                    return False
                fillet_neighbours += 1
        return fillet_neighbours > 0

    # -- measurement --------------------------------------------------------

    def _from_face(self, node: AagNode) -> Optional[FeatureInstance]:
        """Measure a bowl that owns its cap circle outright."""
        radius = node.sphere_radius
        offset = node.sphere_clip_offset
        normal = node.sphere_clip_normal
        if radius <= 0.0 or normal is None or node.sphere_center is None:
            return None

        # The graph stores the cap axis pointing from the sphere centre out to
        # the cap circle, with an unsigned offset. Which way is *out of the
        # part* is settled by where the patch itself sits: the opening is the
        # end of that axis the patch does not occupy.
        cap_center = node.sphere_center.Translated(gp_Vec(normal).Multiplied(offset))
        opening_normal = gp_Dir(normal.XYZ())
        if gp_Vec(node.centroid, cap_center).Dot(gp_Vec(opening_normal)) < 0.0:
            opening_normal.Reverse()

        # Signed distance from the sphere centre to the cap plane, measured
        # into the material. Negative when the centre lies below the rim: the
        # equator is buried and the opening is narrower than the bowl.
        buried = gp_Vec(normal).Dot(gp_Vec(opening_normal)) > 0.0
        signed_offset = -offset if buried else offset

        return self._build(
            radius=radius,
            center=node.sphere_center,
            signed_offset=signed_offset,
            opening_normal=opening_normal,
            faces=[node.face_id],
        )

    def _from_cluster(
        self, graph: AttributedAdjacencyGraph, cluster: list[int]
    ) -> Optional[FeatureInstance]:
        """Measure a bowl from the outer boundary of the patches making it up."""
        first = graph.node(cluster[0])
        radius = first.sphere_radius
        if radius <= 0.0 or first.sphere_center is None:
            return None

        boundary = self._boundary_edges(graph, cluster)
        if not boundary:
            return None  # a sealed sphere is a void, not a pocket

        # Area-weighted centroid of the patches: the side the material is on.
        total = [0.0, 0.0, 0.0]
        area = 0.0
        for face_id in cluster:
            node = graph.node(face_id)
            total[0] += node.centroid.X() * node.area
            total[1] += node.centroid.Y() * node.area
            total[2] += node.centroid.Z() * node.area
            area += node.area
        if area <= 0.0:
            return None
        patch_centroid = gp_Pnt(total[0] / area, total[1] / area, total[2] / area)

        # The opening comes from the planar faces the bowl runs out onto, not
        # from the boundary edge midpoints. An exact hemisphere's rim is one
        # closed circle whose single midpoint sits *on* the rim, so averaging
        # midpoints puts the cap centre a full radius off and reads a plain
        # hemisphere as a total undercut. A cap plane gives the circle
        # exactly. Where there are several candidate openings -- a top face
        # and a second plane slicing the bowl -- the access that governs is
        # the one whose cap circle is largest.
        inside = set(cluster)
        best_cap_radius = -1.0
        best_offset = 0.0
        best_clip_normal: Optional[gp_Dir] = None
        seen_caps: set[int] = set()
        for edge in boundary:
            other_id = edge.face_id_b if edge.face_id_a in inside else edge.face_id_a
            if not graph.has_node(other_id) or other_id in seen_caps:
                continue
            seen_caps.add(other_id)
            cap = graph.node(other_id)
            if cap.surface_type is not SurfaceType.PLANE or cap.plane_normal is None:
                continue

            # Point the cap plane's normal into the half-space the bowl lies
            # in. The stored sense does not matter because it is established
            # here from the material side.
            clip_normal = gp_Dir(cap.plane_normal.XYZ())
            if gp_Vec(cap.centroid, patch_centroid).Dot(gp_Vec(clip_normal)) < 0.0:
                clip_normal.Reverse()
            signed_offset = gp_Vec(first.sphere_center, cap.centroid).Dot(
                gp_Vec(clip_normal)
            )
            if abs(signed_offset) >= radius:
                continue  # the plane misses the sphere
            cap_radius = math.sqrt(
                max(0.0, radius * radius - signed_offset * signed_offset)
            )
            if cap_radius > best_cap_radius:
                best_cap_radius = cap_radius
                best_offset = signed_offset
                best_clip_normal = clip_normal

        if best_clip_normal is None:
            # The bowl runs out into a bore or a freeform wall, so the opening
            # cannot be measured from here. Report the pocket without
            # accusing it of an undercut: a fabricated total overhang is worse
            # than an unmeasured opening, which a rule can at least skip.
            return FeatureInstance(
                instance_id=self.instance_id(0),
                type=FeatureType.SPHERICAL_POCKET,
                faces=list(cluster),
                parameters={
                    "radius_mm": round(radius, 6),
                    "center": _triple(first.sphere_center),
                    "opening_diameter_mm": 0.0,
                    "embedment_depth_mm": 0.0,
                    "overhang_mm": 0.0,
                    "is_super_hemispherical": False,
                    "opening_unmeasured": True,
                    "face_count": len(cluster),
                },
            )

        opening_normal = gp_Dir(best_clip_normal.XYZ())
        opening_normal.Reverse()
        return self._build(
            radius=radius,
            center=first.sphere_center,
            signed_offset=best_offset,
            opening_normal=opening_normal,
            faces=list(cluster),
        )

    def _build(
        self,
        radius: float,
        center: gp_Pnt,
        signed_offset: float,
        opening_normal: gp_Dir,
        faces: list[int],
    ) -> FeatureInstance:
        cap_radius = math.sqrt(max(0.0, radius * radius - signed_offset * signed_offset))
        is_super_hemispherical = signed_offset < -_SUPER_HEMISPHERICAL_TOL_MM
        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.SPHERICAL_POCKET,
            faces=faces,
            parameters={
                "radius_mm": round(radius, 6),
                "center": _triple(center),
                "opening_normal": (
                    round(opening_normal.X(), 6),
                    round(opening_normal.Y(), 6),
                    round(opening_normal.Z(), 6),
                ),
                "opening_diameter_mm": round(2.0 * cap_radius, 6),
                "embedment_depth_mm": round(abs(signed_offset), 6),
                # How far the bowl reaches back under its own rim. Zero unless
                # the equator is buried, because only then is there anything
                # for a straight tool to foul on.
                "overhang_mm": (
                    round(radius - cap_radius, 6) if is_super_hemispherical else 0.0
                ),
                "is_super_hemispherical": is_super_hemispherical,
                "face_count": len(faces),
            },
        )


def _triple(point: gp_Pnt) -> tuple[float, float, float]:
    return (round(point.X(), 6), round(point.Y(), 6), round(point.Z(), 6))
