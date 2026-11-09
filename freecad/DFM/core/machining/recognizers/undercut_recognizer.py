# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes surfaces no three-axis approach can reach.

An undercut is geometry that hides behind something else: the underside of a
T-slot lip, the far wall of a dovetail, the back of a re-entrant boss. None of
it can be cut with the part fixtured one way and the tool coming straight
down, so it means a second setup, a shaped cutter, or another process.

Bores are excluded deliberately. A hole is not reachable from the side either,
and saying so about every hole on the part would bury the findings that
matter. Holes carry their own access rules.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Dir

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import HOLE_TYPES, FeatureInstance, FeatureType
from .base import FeatureRecognizer
from .reachability import Reachability


# A curved face is reachable when some external surface faces within this of
# its axis. Generous, because the test is a screen rather than a simulation.
_CURVED_ACCESS_RAD = math.radians(30.0)

# An outer cylinder's axis component below this counts as square to that
# cardinal, so the cardinal pair along it is an approach.
_CARDINAL_AXIS_TOL = 0.05


class UndercutRecognizer(FeatureRecognizer):
    """Finds faces a three-axis machine cannot reach."""

    prefix = "uc"

    @property
    def name(self) -> str:
        return "Undercut Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        if shape is None:
            return []

        # A bore is unreachable from the side by nature. Reporting that would
        # drown the real undercuts, and the hole rules cover access anyway.
        # Only the six drilled-hole types: a PARTIAL_BORE, pocket, slot or
        # boss face is still tested -- a slot's walls can be genuine
        # undercut shoulders (T-slot, dovetail) nothing else reports.
        excluded = {
            face_id
            for feature in (prior or ())
            if feature.type in HOLE_TYPES
            for face_id in feature.faces
        }

        reach = Reachability(shape)
        found = self._planar_undercuts(graph, reach, excluded)
        found.extend(self._curved_undercuts(graph, excluded))

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- planar -------------------------------------------------------------

    def _planar_undercuts(
        self,
        graph: AttributedAdjacencyGraph,
        reach: Reachability,
        excluded: set[int],
    ) -> list[FeatureInstance]:
        """Flat faces with no clear approach, grouped by which way they face.

        Grouped because the two shoulders of a T-slot are one problem, not
        two: they are both reached, or not, from the same direction.
        """
        blocked: dict[tuple[int, int, int], list[AagNode]] = {}

        for node in graph.nodes:
            if node.surface_type is not SurfaceType.PLANE or node.face_id in excluded:
                continue
            # A face with no concave neighbour sits on the outside of the
            # part and is trivially reachable; testing it wastes rays.
            if node.concave_neighbor_count == 0 or node.inner_loop_count > 0:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            if reach.reachable_from_any_cardinal(node.centroid, normal):
                continue

            direction = reach.blocked_direction(node.centroid, normal)
            if direction is None:
                continue
            key = (
                int(round(direction.X())),
                int(round(direction.Y())),
                int(round(direction.Z())),
            )
            blocked.setdefault(key, []).append(node)

        features = []
        for key in sorted(blocked):
            bucket = blocked[key]
            normal = bucket[0].outward_normal
            for nodes in self._connected_groups(graph, bucket):
                features.append(self._planar_feature(nodes, key, normal))
        return features

    @staticmethod
    def _connected_groups(
        graph: AttributedAdjacencyGraph, bucket: list[AagNode]
    ) -> list[list[AagNode]]:
        """Split one direction's faces into the separate undercuts they are.

        Facing the same way does not make two faces one undercut: a step at
        an angled hole's entry and a tilted counterbore a long way off came
        back as one. Two faces are one undercut when they touch, or share at
        least TWO neighbouring faces -- a T-slot's shoulders do not touch
        (the neck is between them) but both border the slot's walls. Faces
        that share only the one host face they are cut into are separate.
        """
        count = len(bucket)
        parent = list(range(count))

        def root(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        neighbours = [
            {edge.other_face(node.face_id) for edge in graph.edges_of(node.face_id)}
            for node in bucket
        ]
        for a in range(count):
            for c in range(a + 1, count):
                join = bucket[c].face_id in neighbours[a]
                if not join:
                    join = len(neighbours[a] & neighbours[c]) >= 2
                if join:
                    parent[root(a)] = root(c)

        # Components in order of their root, members in bucket order, as
        # the reference's ordered map lays them out.
        components: dict[int, list[AagNode]] = {}
        for k in range(count):
            components.setdefault(root(k), []).append(bucket[k])
        return [components[r] for r in sorted(components)]

    def _planar_feature(self, nodes: list[AagNode], key, normal) -> FeatureInstance:
        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.UNDERCUT,
            faces=sorted(n.face_id for n in nodes),
            parameters={
                "surface_type": "PLANAR",
                "face_count": len(nodes),
                "blocked_tad": key,
                "face_normal": (
                    round(normal.X(), 6),
                    round(normal.Y(), 6),
                    round(normal.Z(), 6),
                )
                if normal
                else (0.0, 0.0, 0.0),
            },
        )

    # -- curved -------------------------------------------------------------

    def _curved_undercuts(
        self, graph: AttributedAdjacencyGraph, excluded: set[int]
    ) -> list[FeatureInstance]:
        """Cylinders and tori with no approach along their axis.

        No rays here. A curved surface in a recess is cut by a tool coming in
        along its axis, so the question is whether any external face of the
        part looks that way -- which is answered from the graph.

        A candidate is any cylinder or torus with a concave neighbour, as the
        reference selects them. Its axis is read from `cyl_cone_axis` for
        both kinds: the reference never fills that field on a torus, so every
        torus is tested along the default +Z axis. That looks like a
        reference slip; it is ported as it stands.
        """
        external = self._external_directions(graph)
        if not external:
            return []
        features = []

        for node in graph.nodes:
            if node.surface_type not in (SurfaceType.CYLINDER, SurfaceType.TORUS):
                continue
            if node.concave_neighbor_count == 0:
                continue
            if node.face_id in excluded:
                continue
            if node.surface_type is SurfaceType.CYLINDER and node.cyl_cone_axis is not None:
                axis = node.cyl_cone_axis.Direction()
            else:
                axis = gp_Dir(0, 0, 1)

            # Reachable when some external direction lies strictly within
            # thirty degrees of the axis, either way along it.
            if any(
                math.acos(min(1.0, abs(axis.Dot(direction)))) < _CURVED_ACCESS_RAD
                for direction in external
            ):
                continue

            dims = node.bbox_dims()
            features.append(
                FeatureInstance(
                    instance_id=self.instance_id(0),
                    type=FeatureType.UNDERCUT,
                    faces=[node.face_id],
                    parameters={
                        "surface_type": node.surface_type.name,
                        "face_count": 1,
                        "axis": (round(axis.X(), 6), round(axis.Y(), 6), round(axis.Z(), 6)),
                        "depth_estimate_mm": round(min(dims) if dims else 0.0, 6),
                    },
                )
            )
        return features

    @staticmethod
    def _external_directions(graph: AttributedAdjacencyGraph) -> list[gp_Dir]:
        """Directions a tool can come from, read off the outside of the part.

        A flat face with a convex neighbour contributes its outward normal.
        A cylinder with a convex neighbour is approachable from anywhere
        around it, so it contributes the cardinals square to its axis -- but
        only for an axis that is itself near-cardinal: each axis component
        under 0.05 adds that axis's pair of cardinals.
        """
        directions: list[gp_Dir] = []
        for node in graph.nodes:
            if node.convex_neighbor_count == 0:
                continue
            if node.surface_type is SurfaceType.PLANE:
                normal = node.outward_normal
                if normal is not None:
                    directions.append(normal)
            elif node.surface_type is SurfaceType.CYLINDER and node.cyl_cone_axis is not None:
                axis = node.cyl_cone_axis.Direction()
                if abs(axis.X()) < _CARDINAL_AXIS_TOL:
                    directions.extend((gp_Dir(1, 0, 0), gp_Dir(-1, 0, 0)))
                if abs(axis.Y()) < _CARDINAL_AXIS_TOL:
                    directions.extend((gp_Dir(0, 1, 0), gp_Dir(0, -1, 0)))
                if abs(axis.Z()) < _CARDINAL_AXIS_TOL:
                    directions.extend((gp_Dir(0, 0, 1), gp_Dir(0, 0, -1)))
        return directions
