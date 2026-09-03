# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes shaped surfaces: turned profiles and milled sculpture.

Two families, told apart by how the part is made rather than by what the
surface is.

On a turned part, a surface of revolution is the profile a form tool or a
contouring pass traces down the workpiece -- a crowned roller, a radiused
shoulder, a tapered nose. Consecutive bands of one profile are one pass, so
adjacent coaxial faces merge into a single feature.

Everything else genuinely sculpted is milled sculpture: a B-spline sheet, a
moulded grip, a revolved patch on a part that is not turned. These are cut
with a ball nose stepping over, so the cost driver is the tightest concave
radius anywhere in the surface -- that is what caps the cutter diameter, and
with it the step-over and the cycle time.

The hard part is neither of those. It is refusing to count fillets. A blend
modelled as a spline is a spline by every local test, and a part with a
hundred filleted edges would otherwise report a hundred sculpted surfaces and
bury the one face that is actually shaped.
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from ..process_classifier import PartProcessType
from .base import FeatureRecognizer


# Flatter than this and the surface is not shaped at all -- it is a plane or
# a ruled band that happened to be modelled as a spline.
_MIN_SCULPT_CURVATURE = 1.0 / 500.0

# A blend is a transition, not a surface anyone set out to make. This mirrors
# the default the freeform rules use, so the feature census shows the same
# faces that drive the findings.
_BLEND_BAND_MAX_RADIUS_MM = 10.0

# Two faces belong to one profile when their axes agree this closely.
_AXIS_ALIGNMENT = 0.99


class TurnedProfileRecognizer(FeatureRecognizer):
    """Groups shaped faces into turning passes and sculpted regions."""

    prefix = "tp"

    @property
    def name(self) -> str:
        return "Turned Profile Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        turned = self._part_is_turned()

        profile_faces: list[AagNode] = []
        sculpted_faces: list[AagNode] = []
        for node in graph.nodes:
            if not node.has_freeform_curvature:
                continue
            if turned and node.surface_type is SurfaceType.REVOLVED:
                profile_faces.append(node)
            elif self._is_sculpted(graph, node):
                sculpted_faces.append(node)

        found: list[FeatureInstance] = []
        found.extend(
            self._group(
                graph,
                profile_faces,
                FeatureType.TURNED_PROFILE,
                coaxial_only=True,
            )
        )
        found.extend(
            self._group(
                graph,
                sculpted_faces,
                FeatureType.FREEFORM_SURFACE,
                coaxial_only=False,
            )
        )

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    def _part_is_turned(self) -> bool:
        """Whether the analyzer classified this part as turned.

        The recognizer is handed the part classification rather than working
        it out again: the classification is expensive and the analyzer has
        already paid for it.
        """
        process = getattr(self, "part_process", None)
        if process is None:
            return False
        return process.type in (PartProcessType.TURNED, PartProcessType.MILL_TURN)

    # -- qualification ------------------------------------------------------

    @staticmethod
    def _is_sculpted(graph: AttributedAdjacencyGraph, node: AagNode) -> bool:
        """Whether a curved face is really shaped, or just a blend.

        A fillet modelled as a spline reads as curved by every local test, so
        the distinction has to come from what the curvature is doing. A gentle
        curve is not shaped at all. A tight concave one that runs tangentially
        into its neighbours at both ends is a blend running along an edge --
        which is a transition between two surfaces, not a surface.
        """
        concave = node.freeform_min_concave_radius_mm

        if concave <= 0.0:
            # Convex only. Too flat to be shaped, or tight enough to be an
            # edge break rather than a form.
            if node.freeform_max_convex_curvature <= _MIN_SCULPT_CURVATURE:
                return False
            return 1.0 / node.freeform_max_convex_curvature > _BLEND_BAND_MAX_RADIUS_MM

        if concave <= _BLEND_BAND_MAX_RADIUS_MM:
            # Tangent at both ends means it is bridging two surfaces rather
            # than being one.
            tangent = sum(1 for edge in graph.edges_of(node.face_id) if edge.is_tangent)
            if tangent >= 2:
                return False

        return True

    # -- grouping -----------------------------------------------------------

    def _group(
        self,
        graph: AttributedAdjacencyGraph,
        faces: list[AagNode],
        feature_type: str,
        coaxial_only: bool,
    ) -> list[FeatureInstance]:
        """Merge adjacent qualifying faces into one feature each.

        Consecutive bands of one profile are one pass of the tool, and one
        sculpted region is one surfacing operation, so reporting them face by
        face would multiply a single decision into a dozen findings.
        """
        if not faces:
            return []

        position = {node.face_id: index for index, node in enumerate(faces)}
        parent = list(range(len(faces)))

        def root(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for index, node in enumerate(faces):
            for edge in graph.edges_of(node.face_id):
                neighbour = position.get(edge.other_face(node.face_id))
                if neighbour is None:
                    continue
                if coaxial_only and not self._axes_agree(node, faces[neighbour]):
                    continue
                parent[root(index)] = root(neighbour)

        groups: dict[int, list[AagNode]] = {}
        for index, node in enumerate(faces):
            groups.setdefault(root(index), []).append(node)

        found: list[FeatureInstance] = []
        for key in sorted(groups):
            members = groups[key]
            parameters = {
                "min_concave_radius_mm": round(_tightest_concave(members), 6),
                "area_mm2": round(sum(node.area for node in members), 6),
                "face_count": len(members),
            }
            axis = members[0].revolved_axis
            if feature_type == FeatureType.TURNED_PROFILE and axis is not None:
                direction = axis.Direction()
                parameters["axis"] = (
                    round(direction.X(), 6),
                    round(direction.Y(), 6),
                    round(direction.Z(), 6),
                )
            found.append(
                FeatureInstance(
                    instance_id=self.instance_id(0),
                    type=feature_type,
                    faces=sorted(node.face_id for node in members),
                    parameters=parameters,
                )
            )
        return found

    @staticmethod
    def _axes_agree(a: AagNode, b: AagNode) -> bool:
        if a.revolved_axis is None or b.revolved_axis is None:
            return False
        return (
            abs(a.revolved_axis.Direction().Dot(b.revolved_axis.Direction()))
            >= _AXIS_ALIGNMENT
        )


def _tightest_concave(members: Sequence[AagNode]) -> float:
    """The smallest concave radius anywhere in a group.

    This is the number that matters: it caps the ball nose that can finish
    the region, and the cutter diameter sets the step-over and the cycle
    time. Zero means the region has no concave part to constrain the tool.
    """
    tightest = 0.0
    for node in members:
        radius = node.freeform_min_concave_radius_mm
        if radius > 0.0 and (tightest == 0.0 or radius < tightest):
            tightest = radius
    return tightest
