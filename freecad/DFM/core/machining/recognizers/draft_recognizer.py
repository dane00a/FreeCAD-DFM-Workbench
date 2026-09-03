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

The pull direction is taken as +Z. That is the assumption a moulder makes
looking at a model that arrives without a parting line on it, and getting it
wrong is visible rather than silent: the drafted walls simply do not appear.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Dir

from ..aag import AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer


# The band a deliberate draft falls in. Below half a degree is modelling
# noise or a wall that was meant to be vertical; past eight degrees the taper
# is doing something else -- a funnel, a lead-in, a chamfered flank.
_DRAFT_MIN_DEG = 0.5
_DRAFT_MAX_DEG = 8.0

# The direction the mould opens, absent anything on the model that says
# otherwise.
_PULL_DIRECTION = gp_Dir(0.0, 0.0, 1.0)


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

        for node in graph.nodes:
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
