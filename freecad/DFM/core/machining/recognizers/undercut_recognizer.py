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

from typing import Optional, Sequence

from OCP.gp import gp_Dir

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import BORE_TYPES, FeatureInstance, FeatureType
from .base import FeatureRecognizer
from .reachability import CARDINAL_DIRECTIONS, Reachability


# A curved face is reachable when some external surface faces within this of
# its axis. Generous, because the test is a screen rather than a simulation.
_CURVED_ACCESS_COS = 0.866  # 30 degrees


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
        excluded = {
            face_id
            for feature in (prior or ())
            if feature.type in BORE_TYPES
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
            nodes = blocked[key]
            normal = nodes[0].outward_normal
            features.append(
                FeatureInstance(
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
            )
        return features

    # -- curved -------------------------------------------------------------

    def _curved_undercuts(
        self, graph: AttributedAdjacencyGraph, excluded: set[int]
    ) -> list[FeatureInstance]:
        """Internal cylinders and tori with no approach along their axis.

        No rays here. A curved internal surface is cut by a tool coming in
        along its axis, so the question is whether any external face of the
        part looks that way -- which is answered from the graph.
        """
        external = self._external_directions(graph)
        features = []

        for node in graph.nodes:
            if node.face_id in excluded or not node.is_internal:
                continue
            if node.surface_type is SurfaceType.CYLINDER and node.cyl_cone_axis is not None:
                axis = node.cyl_cone_axis.Direction()
            elif node.surface_type is SurfaceType.TORUS and node.torus_axis is not None:
                axis = node.torus_axis.Direction()
            else:
                continue

            if any(abs(axis.Dot(direction)) >= _CURVED_ACCESS_COS for direction in external):
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
        """Directions the outside of the part faces.

        Flat outer faces contribute their own normal. An outer cylinder is
        approachable from anywhere around it, so it contributes the cardinals
        square to its axis.
        """
        directions: list[gp_Dir] = []

        def add(direction: gp_Dir) -> None:
            if not any(existing.Dot(direction) > 0.99 for existing in directions):
                directions.append(direction)

        for node in graph.nodes:
            if node.convex_neighbor_count == 0:
                continue
            if node.surface_type is SurfaceType.PLANE:
                normal = node.outward_normal
                if normal is not None:
                    add(normal)
            elif (
                node.surface_type is SurfaceType.CYLINDER
                and not node.is_internal
                and node.cyl_cone_axis is not None
            ):
                axis = node.cyl_cone_axis.Direction()
                for cardinal in CARDINAL_DIRECTIONS:
                    if abs(axis.Dot(cardinal)) < 0.3:
                        add(cardinal)
        return directions
