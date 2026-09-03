# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognized manufacturing features.

A feature is a set of faces that mean something to a machinist: this group of
surfaces is a drilled hole, that group is a pocket. Rules are written against
features rather than faces because the interesting questions are about the
whole -- how deep is the hole, how close to the edge -- not about any one
surface.

Parameters are kept as a plain dictionary rather than a class per feature
type. A rule can then read any field without the taxonomy having to grow a
subclass every time a recognizer learns to measure something new.

One consequence is worth stating: a *missing* parameter is not the same as a
zero one. `thread_pitch_mm` absent means the pitch is unknown; present and
zero would mean a pitch of nothing. Rules must check presence, not truthiness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from OCP.gp import gp_Dir


class FeatureType:
    """The kinds of feature a recognizer can emit.

    Plain strings rather than an enum: they are written into saved analyses
    and matched by rules, so their exact spelling is part of the contract.
    """

    # Drilled and bored holes
    THROUGH_HOLE = "THROUGH_HOLE"
    BLIND_HOLE = "BLIND_HOLE"
    COUNTERBORE = "COUNTERBORE"
    COUNTERSINK = "COUNTERSINK"
    THREADED_HOLE = "THREADED_HOLE"
    # A concave cylinder covering well under half a revolution: a bearing
    # saddle or line-bore cradle. Milled open-side-up rather than drilled, so
    # drill-centric rules do not apply to it.
    PARTIAL_BORE = "PARTIAL_BORE"
    EXTERNAL_THREAD = "EXTERNAL_THREAD"

    # Milled cavities
    POCKET = "POCKET"
    SPHERICAL_POCKET = "SPHERICAL_POCKET"
    SLOT = "SLOT"
    CHANNEL = "CHANNEL"
    THROUGH_CAVITY = "THROUGH_CAVITY"
    FLEXURE_SLIT = "FLEXURE_SLIT"
    BROACHED_SLOT = "BROACHED_SLOT"
    V_GROOVE = "V_GROOVE"

    # Protrusions
    BOSS = "BOSS"
    RIB = "RIB"
    STEP = "STEP"

    # Turned features
    GROOVE = "GROOVE"
    THREAD_RELIEF_GROOVE = "THREAD_RELIEF_GROOVE"
    O_RING_GLAND = "O_RING_GLAND"
    RETAINING_RING_GROOVE = "RETAINING_RING_GROOVE"
    TURNED_PROFILE = "TURNED_PROFILE"

    # Transitions and surfaces
    FILLET = "FILLET"
    CHAMFER = "CHAMFER"
    DRAFT_FACE = "DRAFT_FACE"
    UNDERCUT = "UNDERCUT"
    FREEFORM_SURFACE = "FREEFORM_SURFACE"

    # Other
    PATTERN = "PATTERN"
    MARKING_TEXT = "MARKING_TEXT"
    UNKNOWN = "UNKNOWN"


HOLE_TYPES = frozenset(
    {
        FeatureType.THROUGH_HOLE,
        FeatureType.BLIND_HOLE,
        FeatureType.COUNTERBORE,
        FeatureType.COUNTERSINK,
        FeatureType.THREADED_HOLE,
    }
)

# Every type a hole recognizer can produce, including the ones that are not
# drillable and so are exempt from drill-centric rules.
BORE_TYPES = HOLE_TYPES | {FeatureType.PARTIAL_BORE}

CAVITY_TYPES = frozenset(
    {
        FeatureType.POCKET,
        FeatureType.SLOT,
        FeatureType.CHANNEL,
        FeatureType.THROUGH_CAVITY,
        FeatureType.SPHERICAL_POCKET,
    }
)

GROOVE_TYPES = frozenset(
    {
        FeatureType.GROOVE,
        FeatureType.THREAD_RELIEF_GROOVE,
        FeatureType.O_RING_GLAND,
        FeatureType.RETAINING_RING_GROOVE,
    }
)

BLEND_TYPES = frozenset({FeatureType.FILLET, FeatureType.CHAMFER})


@dataclass
class FeatureInstance:
    """One recognized feature: what it is, which faces it owns, and its size."""

    instance_id: str
    type: str
    faces: list[int] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)

    def has_face(self, face_id: int) -> bool:
        return face_id in self.faces

    def param(self, key: str, default: Any = None) -> Any:
        return self.parameters.get(key, default)

    def number(self, key: str, default: Optional[float] = None) -> Optional[float]:
        """A numeric parameter, or the default when absent or unparseable."""
        if key not in self.parameters:
            return default
        try:
            return float(self.parameters[key])
        except (TypeError, ValueError):
            return default

    def direction(self, key: str) -> Optional[gp_Dir]:
        """A stored axis, back as a direction.

        Recognizers write axes as plain triples so a feature stays
        serialisable. Rules that need to do geometry with one want it back as
        a direction, and a degenerate triple means the axis was never really
        established.
        """
        raw = self.parameters.get(key)
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            return None
        try:
            return gp_Dir(float(raw[0]), float(raw[1]), float(raw[2]))
        except (TypeError, ValueError, RuntimeError):
            return None

    def has(self, key: str) -> bool:
        """Whether the parameter was measured at all.

        Distinct from a falsy value: a recognizer that could not establish a
        depth omits it rather than writing zero.
        """
        return key in self.parameters

    @property
    def is_hole(self) -> bool:
        return self.type in HOLE_TYPES

    @property
    def geometry_refs(self) -> list[tuple[str, int]]:
        """Faces in the form a check reports as failing geometry."""
        return [("Face", face_id) for face_id in self.faces]

    def __repr__(self) -> str:
        return f"<{self.type} {self.instance_id} faces={self.faces}>"


@dataclass
class RecognitionResult:
    """Everything recognized in one pass, and how the features relate."""

    features: list[FeatureInstance] = field(default_factory=list)
    # (instance_id, instance_id, relationship) where relationship is
    # "contains" or "intersects".
    relations: list[tuple[str, str, str]] = field(default_factory=list)

    def of_type(self, *types: str) -> list[FeatureInstance]:
        wanted = set(types)
        return [f for f in self.features if f.type in wanted]

    def holes(self) -> list[FeatureInstance]:
        return [f for f in self.features if f.type in HOLE_TYPES]

    def faces_of_type(self, *types: str) -> set[int]:
        """Every face claimed by a feature of the given types."""
        wanted = set(types)
        return {
            face_id
            for feature in self.features
            if feature.type in wanted
            for face_id in feature.faces
        }

    def owner_of(self, face_id: int) -> Optional[FeatureInstance]:
        for feature in self.features:
            if feature.has_face(face_id):
                return feature
        return None

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for feature in self.features:
            tally[feature.type] = tally.get(feature.type, 0) + 1
        return dict(sorted(tally.items()))

    def extend(self, features: Iterable[FeatureInstance]) -> None:
        self.features.extend(features)

    def __len__(self) -> int:
        return len(self.features)

    def __iter__(self):
        return iter(self.features)
