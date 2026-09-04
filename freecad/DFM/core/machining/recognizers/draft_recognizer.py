# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes walls that have been deliberately drafted.

A cast or moulded part needs its walls leaning slightly away from the pull
direction so the part will release. A few degrees is enough, and it is
unmistakable when it is there: a wall at 87 degrees rather than 90 is not an
accident of modelling, it is somebody's mould design.

Only positive evidence is emitted. An earlier reading of this marked every
vertical wall as lacking draft, which is true of every pocket wall on every
milled part ever made and told nobody anything -- the absence of draft
features *is* the no-draft signal, and the casting rule reads it that way.

Two passes, because there are two ways to model a drafted wall. A planar one
declares its angle in its normal and can be read straight off the surface. A
sculpted one -- a fin flank swept along a curve, a housing wall that fairs
into a boss -- has no single normal, so it has to be sampled: mesh the face,
take the draft at every vertex, and ask whether the whole wall leans one way.
Both readings are in the reference engine, and the sculpted one accounts for
almost every drafted face in the corpus, because a wall that is both drafted
and curved is exactly what a CAD system emits as a B-spline.

Sampling a wall answers a second question for free. A sample that leans into
the pull is an overhang, and a wall built mostly of those is either a mould
that will not open or a surface a three-axis cutter cannot get behind. Which
of the two depends on whether anything is actually in the way, so the
overhanging samples are ray-cast: a majority of them trapped from all six
approaches makes the face an undercut, and anything less makes it an exterior
blend that merely happens to lean. Deciding that on the geometry rather than
on the lean alone is what keeps machinable styled walls off the report.

The pull direction is taken as +Z. That is the assumption a moulder makes
looking at a model that arrives without a parting line on it, and getting it
wrong is visible rather than silent: the drafted walls simply do not appear.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.gp import gp_Dir, gp_Pnt
from OCP.TopLoc import TopLoc_Location

from ...utils.geometry import FaceIndex
from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer
from .reachability import Reachability


# The band a deliberate draft falls in. Below half a degree is modelling
# noise or a wall that was meant to be vertical; past eight degrees the taper
# is doing something else -- a funnel, a lead-in, a chamfered flank.
_DRAFT_MIN_DEG = 0.5
_DRAFT_MAX_DEG = 8.0

# The direction the mould opens, absent anything on the model that says
# otherwise.
_PULL_DIRECTION = gp_Dir(0.0, 0.0, 1.0)

# The surface families that have to be sampled rather than read. A surface of
# revolution is left out on purpose: it was turned, and mould-pull draft is
# not what a lathe was doing.
_SAMPLED_TYPES = (SurfaceType.BSPLINE, SurfaceType.EXTRUDED, SurfaceType.OTHER)

# How far a sample may lean from vertical and still be on a wall. Steeper than
# forty-five degrees and it belongs to a top or a floor, which releases along
# the pull rather than across it and has no draft to speak of.
_WALL_BAND_DEG = 45.0

# How much of a face has to be wall before the face is one. A ruled quad
# tessellates to four vertices, so the gate is a fraction with a small
# absolute floor rather than a sample count.
_MIN_WALL_SAMPLES = 4
_MIN_WALL_FRACTION = 0.5

# Below this a sample leans the wrong way: an overhang rather than a draft.
_REVERSE_DRAFT_DEG = 0.5

# How many trapped samples it takes before an overhang is an undercut rather
# than a ray grazing an edge. A wrapped impeller channel buries nearly all of
# its overhang; a corner blend on the outside of a part loses a third of its
# samples to junction grazing and is reachable everywhere that matters.
_MIN_TRAPPED_SAMPLES = 3

# At most this many vertices are sampled per face. Enough to see the shape of
# a wall, few enough that a heavily tessellated casting does not cost minutes.
_MAX_SAMPLES = 64

# A sampled wall is allowed twice the draft band. A styled wall varies along
# its length, and it is the shallowest part of it that has to release.
_SAMPLED_MAX_MULTIPLE = 2.0


class DraftRecognizer(FeatureRecognizer):
    """Finds walls leaning away from the pull direction."""

    prefix = "df"

    @property
    def name(self) -> str:
        return "Draft Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        found: list[FeatureInstance] = []
        # A house or foundry draft standard is policy, not geometry.
        minimum = self.threshold("draft_min_deg", _DRAFT_MIN_DEG)
        maximum = self.threshold("draft_max_deg", _DRAFT_MAX_DEG)

        reach: Optional[Reachability] = None
        faces: Optional[FaceIndex] = None

        for node in graph.nodes:
            if node.surface_type in _SAMPLED_TYPES:
                if shape is None:
                    continue
                if faces is None:
                    faces = FaceIndex(shape)
                    reach = Reachability(shape)
                sampled = self._sampled_wall(node, faces, reach, minimum, maximum)
                if sampled is not None:
                    sampled.instance_id = self.instance_id(len(found))
                    found.append(sampled)
                continue

            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            # A wall with no concave neighbour is the outside of the part,
            # not the side of a cavity, and nothing has to release from it.
            if node.concave_neighbor_count == 0:
                continue

            draft = _draft_angle(normal)
            if not minimum <= draft <= maximum:
                continue

            found.append(
                FeatureInstance(
                    instance_id=self.instance_id(len(found)),
                    type=FeatureType.DRAFT_FACE,
                    faces=[node.face_id],
                    parameters={
                        "draft_angle_deg": round(draft, 6),
                        "pull_direction": (
                            _PULL_DIRECTION.X(),
                            _PULL_DIRECTION.Y(),
                            _PULL_DIRECTION.Z(),
                        ),
                        "face_normal": (
                            round(normal.X(), 6),
                            round(normal.Y(), 6),
                            round(normal.Z(), 6),
                        ),
                    },
                )
            )

        return found

    # -- sculpted walls -----------------------------------------------------

    def _sampled_wall(
        self,
        node: AagNode,
        faces: FaceIndex,
        reach: Reachability,
        minimum: float,
        maximum: float,
    ) -> Optional[FeatureInstance]:
        """Read a sculpted wall's draft off its tessellation.

        Returns the drafted face for a wall that leans away from the pull all
        along its length, the undercut for one whose overhang is genuinely
        trapped, or nothing at all -- which is the answer for most faces, and
        for every top, floor and reachable exterior blend.
        """
        try:
            face = faces.face_at(node.face_id)
        except Exception:
            return None
        if face is None or face.IsNull():
            return None

        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            BRepMesh_IncrementalMesh(face, 0.5, False, 0.5, False)
            location = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None or not triangulation.HasUVNodes():
            return None

        adaptor = BRepAdaptor_Surface(face)
        props = BRepLProp_SLProps(adaptor, 1, 1e-7)
        count = triangulation.NbNodes()
        stride = max(1, count // _MAX_SAMPLES)

        shallowest = None
        steepest = None
        wall_samples = 0
        total_samples = 0
        overhangs: list[tuple[gp_Pnt, gp_Dir]] = []

        for index in range(1, count + 1, stride):
            uv = triangulation.UVNode(index)
            props.SetParameters(uv.X(), uv.Y())
            if not props.IsNormalDefined():
                continue
            normal = props.Normal()
            if node.is_reversed:
                normal.Reverse()
            # Signed, and against the pull rather than off it: positive is a
            # wall opening the way the mould travels, negative is one closing
            # over it.
            alignment = max(-1.0, min(1.0, normal.Dot(_PULL_DIRECTION)))
            draft = math.degrees(math.asin(alignment))
            total_samples += 1
            if abs(draft) > _WALL_BAND_DEG:
                continue  # a top or a floor, not a releasing wall
            wall_samples += 1
            shallowest = draft if shallowest is None else min(shallowest, draft)
            steepest = draft if steepest is None else max(steepest, draft)
            if draft < -_REVERSE_DRAFT_DEG:
                overhangs.append((props.Value(), normal))

        if wall_samples < _MIN_WALL_SAMPLES:
            return None
        if total_samples and wall_samples / total_samples < _MIN_WALL_FRACTION:
            return None

        if shallowest < -_REVERSE_DRAFT_DEG:
            trapped = sum(
                1
                for point, normal in overhangs
                if not reach.reachable_from_any_cardinal(point, normal)
            )
            # A majority, and enough of them that it is not edge grazing.
            if trapped >= _MIN_TRAPPED_SAMPLES and trapped * 2 >= len(overhangs):
                return FeatureInstance(
                    instance_id="",
                    type=FeatureType.UNDERCUT,
                    faces=[node.face_id],
                    parameters={
                        "surface_type": "FREEFORM",
                        "face_count": 1,
                        "reverse_draft_deg": round(-shallowest, 6),
                        "source": "freeform_reverse_draft",
                        "pull_direction": (
                            _PULL_DIRECTION.X(),
                            _PULL_DIRECTION.Y(),
                            _PULL_DIRECTION.Z(),
                        ),
                    },
                )
            return None

        if shallowest >= minimum and steepest <= maximum * _SAMPLED_MAX_MULTIPLE:
            return FeatureInstance(
                instance_id="",
                type=FeatureType.DRAFT_FACE,
                faces=[node.face_id],
                parameters={
                    "draft_angle_deg": round(shallowest, 6),
                    "pull_direction": (
                        _PULL_DIRECTION.X(),
                        _PULL_DIRECTION.Y(),
                        _PULL_DIRECTION.Z(),
                    ),
                    "freeform": True,
                },
            )
        return None


def _draft_angle(normal: gp_Dir) -> float:
    """How far a wall leans off vertical, in degrees.

    Measured from the pull direction and reported as the departure from
    square, which is how a draft is called out on a drawing. The absolute
    value is deliberate: a wall drafted the other way is still drafted, and
    which way it leans depends on which side of the cavity it is.
    """
    alignment = max(-1.0, min(1.0, normal.Dot(_PULL_DIRECTION)))
    from_pull = math.degrees(math.acos(abs(alignment)))
    return 90.0 - from_pull
