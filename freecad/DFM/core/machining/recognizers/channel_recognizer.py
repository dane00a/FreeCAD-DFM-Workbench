# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes the canyons between things standing up.

A slot is cut into the part and the pocket vocabulary describes it. A channel
is what is left between two walls that were never cut at all -- the gap
between two ribs on a casting, between two fins on a heatsink, between a boss
and the wall beside it. Nobody removed material to make it; it is the space
the raised features leave, and the tool has to reach down into it all the
same.

That is why it is found from the walls rather than from a floor. A slot's
floor is bounded by the slot -- it starts at one wall and ends at the other --
and a channel's floor is the plate everything stands on, running away in both
directions past the gap. Asking whether the shared floor is much wider than
the gap is what separates the two, and it is the test that keeps this
recognizer out of the slot family's business.

Drafted walls count. A cast rib leans a couple of degrees and is modelled as
a loft rather than a plane, so a face that is nearly flat -- sampled over its
surface, with every normal within eight degrees of every other -- is admitted
as a wall with its average direction. Genuinely curved faces are not, which
also stops the two halves of a bore from being read as walls facing each
other across it.
"""

from __future__ import annotations

from typing import Optional, Sequence

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec, gp_XYZ
from OCP.GeomLProp import GeomLProp_SLProps
from OCP.TopAbs import TopAbs_REVERSED

from ...utils.geometry import FaceIndex
from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..face_extent import band_along
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer


# Wider than this and the gap is open floor with things standing on it rather
# than a channel anything has to reach into.
_MAX_GAP_MM = 20.0

# Below this the two walls are a crack, not a channel.
_MIN_GAP_MM = 0.5

# The walls have to stand at least as tall as the gap is wide. Shallower than
# that and there is nothing to reach down into.
_MIN_DEPTH_RATIO = 1.0

# How far past the gap the shared floor has to run before it is the plate the
# walls stand on rather than the bottom of a slot between them.
_HOST_FLOOR_SPAN = 1.5

# Two walls face each other when their outward normals oppose.
_FACING_MAX_DOT = -0.9

# The floor stands square to the walls.
_FLOOR_PERPENDICULAR_MAX_DOT = 0.3

# How far apart two sampled normals may point and still be one flat wall.
# Eight degrees admits a drafted casting wall and rejects a bore flank.
_NEAR_PLANAR_MIN_DOT = 0.99026

# The walls have to front each other by at least this much across.
_MIN_PROJECTED_OVERLAP_MM = 0.1

# Sampling grid for the near-planar test.
_SAMPLES_PER_AXIS = 3


class ChannelRecognizer(FeatureRecognizer):
    """Recognizes open-ended channels."""

    prefix = "ch"

    @property
    def name(self) -> str:
        return "Channel Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        self._shape = shape
        walls = self._walls(graph, shape)
        if len(walls) < 2:
            return []

        candidates = []
        for index, (first, normal_a) in enumerate(walls):
            for second, normal_b in walls[index + 1 :]:
                found = self._as_channel(graph, first, normal_a, second, normal_b)
                if found is not None:
                    candidates.append(found)

        # Narrowest first, one channel per wall. A rib field's walls would
        # otherwise pair across the ribs between them: the first rib's right
        # wall genuinely does face the third rib's left wall, with a rib in
        # the way.
        candidates.sort(key=lambda entry: (entry["gap"], entry["faces"]))
        used: set[int] = set()
        found_features: list[FeatureInstance] = []
        for entry in candidates:
            if any(face_id in used for face_id in entry["faces"]):
                continue
            used.update(entry["faces"])
            found_features.append(
                FeatureInstance(
                    instance_id=self.instance_id(len(found_features)),
                    # Walls only. The floor is the plate the whole part
                    # stands on, and listing it would light up the part in
                    # the viewport for the sake of a gap between two ribs.
                    type=FeatureType.CHANNEL,
                    faces=list(entry["faces"]),
                    parameters={
                        "width_mm": round(entry["gap"], 6),
                        "depth_mm": round(entry["depth"], 6),
                        "length_mm": round(entry["length"], 6),
                        "floor_face": entry["floor"],
                        "axis": entry["axis"],
                    },
                )
            )
        return found_features

    # -- walls --------------------------------------------------------------

    def _walls(
        self, graph: AttributedAdjacencyGraph, shape
    ) -> list[tuple[AagNode, gp_Dir]]:
        """Faces flat enough to be the side of something standing up.

        Analytic curved surfaces are never walls and are not sampled: it is
        cheaper to exclude a cylinder than to measure it.
        """
        walls: list[tuple[AagNode, gp_Dir]] = []
        faces: Optional[FaceIndex] = None

        for node in graph.nodes:
            if node.surface_type is SurfaceType.PLANE:
                normal = node.outward_normal
                if normal is not None:
                    walls.append((node, normal))
            elif node.surface_type in (SurfaceType.BSPLINE, SurfaceType.OTHER):
                if shape is None:
                    continue
                if faces is None:
                    faces = FaceIndex(shape)
                normal = _average_normal_if_flat(faces, node)
                if normal is not None:
                    walls.append((node, normal))
        return walls

    # -- pairing ------------------------------------------------------------

    def _as_channel(
        self,
        graph: AttributedAdjacencyGraph,
        first: AagNode,
        normal_a: gp_Dir,
        second: AagNode,
        normal_b: gp_Dir,
    ) -> Optional[dict]:
        """Whether these two walls have a channel between them."""
        if normal_a.Dot(normal_b) > _FACING_MAX_DOT:
            return None
        # Facing, not back to back: the outward normals have to point into
        # the space between them. Two ribs' outer skins are anti-parallel
        # too, and the material rather than the air is what lies between.
        gap = gp_Vec(first.centroid, second.centroid).Dot(gp_Vec(normal_a))
        if not _MIN_GAP_MM <= gap <= _MAX_GAP_MM:
            return None

        floor = self._shared_floor(graph, first, second, normal_a)
        if floor is None:
            return None

        # Every measurement below is in the channel's own frame: gap = the
        # first wall's normal, up = the floor's outward normal, run = gap x
        # up, each measured on the faces (band_along). It was the dominant
        # cardinal axis of each normal read off the faces' boxes, and on a
        # slot sunk at 45 degrees (optical_periscope_housing's mirror seats)
        # that took an end wall's box shadow for a host floor and reported a
        # channel as deep as the part was wide.
        up = floor.outward_normal
        if up is None:
            return None
        run_vec = gp_Vec(normal_a).Crossed(gp_Vec(up))
        if run_vec.Magnitude() < 0.5:
            return None  # degenerate
        run = gp_Dir(run_vec)
        shape = getattr(self, "_shape", None)

        def band(node: AagNode, direction: gp_Dir):
            ok, low, high = band_along(
                shape, node.face_id, node.bbox, gp_Pnt(0.0, 0.0, 0.0), direction,
                1.0e18, -1.0e18,
            )
            if not ok or not high > low:
                return None
            return low, high

        # A slot bottom spans about the gap; the plate a rib stands on spans
        # far more, measured along the gap direction.
        floor_band = band(floor, normal_a)
        if floor_band is None:
            return None
        if floor_band[1] - floor_band[0] < gap * _HOST_FLOOR_SPAN:
            return None  # bounded by the walls: a slot bottom, not a plate

        # The walls genuinely front each other along up and along run.
        a_up, b_up = band(first, up), band(second, up)
        a_run, b_run = band(first, run), band(second, run)
        if a_up is None or b_up is None or a_run is None or b_run is None:
            return None
        overlap_up = min(a_up[1], b_up[1]) - max(a_up[0], b_up[0])
        overlap_run = min(a_run[1], b_run[1]) - max(a_run[0], b_run[0])
        if overlap_up < _MIN_PROJECTED_OVERLAP_MM or overlap_run < _MIN_PROJECTED_OVERLAP_MM:
            return None  # they are anti-parallel but not opposite

        depth = min(a_up[1] - a_up[0], b_up[1] - b_up[0])
        if depth < gap * _MIN_DEPTH_RATIO:
            return None
        length = overlap_run

        return {
            "faces": tuple(sorted((first.face_id, second.face_id))),
            "gap": gap,
            "depth": depth,
            "length": length,
            "floor": floor.face_id,
            "axis": (
                round(normal_a.X(), 6),
                round(normal_a.Y(), 6),
                round(normal_a.Z(), 6),
            ),
        }

    @staticmethod
    def _shared_floor(
        graph: AttributedAdjacencyGraph,
        first: AagNode,
        second: AagNode,
        normal_a: gp_Dir,
    ) -> Optional[AagNode]:
        """The face both walls stand on, if they stand on the same one."""
        against_b = {
            edge.other_face(second.face_id)
            for edge in graph.concave_edges_of(second.face_id)
        }
        for edge in graph.concave_edges_of(first.face_id):
            candidate = graph.node(edge.other_face(first.face_id))
            if candidate.surface_type is not SurfaceType.PLANE:
                continue
            normal = candidate.outward_normal
            if normal is None:
                continue
            if abs(normal.Dot(normal_a)) > _FLOOR_PERPENDICULAR_MAX_DOT:
                continue
            if candidate.face_id in against_b:
                return candidate
        return None


def _average_normal_if_flat(faces: FaceIndex, node: AagNode) -> Optional[gp_Dir]:
    """The direction a nearly flat sculpted face looks in, if it is one.

    A cast wall drafted two degrees comes through as a loft rather than a
    plane and is every bit a channel wall. Sampled across the surface, its
    normals all agree to within a few degrees; a bore flank's do not, which
    is what keeps the two halves of a hole from reading as walls facing each
    other across it.
    """
    try:
        face = faces.face_at(node.face_id)
    except Exception:
        return None
    if face is None or face.IsNull():
        return None

    surface = BRepAdaptor_Surface(face, True)
    u0, u1 = surface.FirstUParameter(), surface.LastUParameter()
    v0, v1 = surface.FirstVParameter(), surface.LastVParameter()
    reversed_face = face.Orientation() == TopAbs_REVERSED

    normals: list[gp_Dir] = []
    for iu in range(_SAMPLES_PER_AXIS):
        for iv in range(_SAMPLES_PER_AXIS):
            u = u0 + (u1 - u0) * (iu + 0.5) / _SAMPLES_PER_AXIS
            v = v0 + (v1 - v0) * (iv + 0.5) / _SAMPLES_PER_AXIS
            try:
                props = GeomLProp_SLProps(surface.Surface().Surface(), u, v, 1, 1e-9)
                if not props.IsNormalDefined():
                    return None
                normal = props.Normal()
            except Exception:
                return None
            if reversed_face:
                normal.Reverse()
            normals.append(normal)

    for index, first in enumerate(normals):
        for second in normals[index + 1 :]:
            if first.Dot(second) < _NEAR_PLANAR_MIN_DOT:
                return None

    total = gp_XYZ(0.0, 0.0, 0.0)
    for normal in normals:
        total.Add(normal.XYZ())
    if total.Modulus() < 1e-9:
        return None
    return gp_Dir(total)
