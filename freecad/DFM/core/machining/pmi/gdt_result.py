# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The PMI read off one STEP file, before it becomes an annotation set.

The reference engine's ``GdtResult``, field for field, so that the passes that
tidy it -- de-duplicating dimension labels, dropping phantoms, folding in what
the raw-text extractors recovered -- can be ported line for line against the
same shape. :func:`to_annotation_set` is the one place it turns into the
workbench's own vocabulary.

Face ids here are already this workbench's: one-based, the
:class:`~...utils.geometry.FaceIndex` numbering the adjacency graph uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..annotations import (
    AnnotationSet,
    Datum,
    FeatureControlFrame,
    SurfaceFinish,
    SurfaceFinishNote,
    ToleranceCategory,
    ToleranceType,
)


#: The dimension types, in the reference's spelling. Size dimensions on round
#: features are kept apart from linear ones -- collapsing them hid the
#: size-versus-location distinction from everything downstream.
DIMENSION_TYPES = frozenset(
    {
        ToleranceType.DIMENSION_LINEAR,
        ToleranceType.DIMENSION_ANGULAR,
        ToleranceType.DIMENSION_DIAMETER,
        ToleranceType.DIMENSION_RADIAL,
    }
)


def category_of(type_: str) -> str:
    """The family a type belongs to (the reference's ``gdt_type_category``)."""
    if type_ in DIMENSION_TYPES:
        return ToleranceCategory.DIMENSION
    probe = FeatureControlFrame(annotation_id="", type=type_)
    return probe.category or ToleranceCategory.OTHER


@dataclass
class GdtAnnotation:
    annotation_id: str
    type: str
    category: str
    tolerance_value: float = 0.0  # mm, or degrees for an angle
    tolerance_zone: str = ""
    datum_refs: List[str] = field(default_factory=list)
    material_condition: str = ""
    face_ids: List[int] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_dimension(self) -> bool:
        return self.category == ToleranceCategory.DIMENSION

    @property
    def is_valueless_dimension(self) -> bool:
        return (
            self.is_dimension
            and self.tolerance_value <= 0.0
            and self.details.get("upper_limit", 0.0) == 0.0
            and self.details.get("lower_limit", 0.0) == 0.0
        )


@dataclass
class GdtDatum:
    datum_id: str
    label: str = ""
    face_ids: List[int] = field(default_factory=list)
    is_target: bool = False
    target_type: str = ""


@dataclass
class GdtNote:
    note_id: str
    text: str = ""
    source: str = ""
    face_ids: List[int] = field(default_factory=list)


@dataclass
class GdtSurfaceFinish:
    surface_finish_id: str
    ra_um: float = 0.0
    face_ids: List[int] = field(default_factory=list)
    source: str = ""


@dataclass
class GdtResult:
    annotations: List[GdtAnnotation] = field(default_factory=list)
    datums: List[GdtDatum] = field(default_factory=list)
    notes: List[GdtNote] = field(default_factory=list)
    surface_finishes: List[GdtSurfaceFinish] = field(default_factory=list)
    has_pmi: bool = False

    def renumber_annotations(self) -> None:
        for n, annotation in enumerate(self.annotations, start=1):
            annotation.annotation_id = f"gdt_{n}"


def to_annotation_set(result: GdtResult) -> AnnotationSet:
    """The workbench's view of a parsed result.

    Every annotation becomes a frame -- dimensions and unclassified
    presentation labels included -- because that is what the reference's
    feature attachment and its tally both walk: a valued dimension is judged
    by the capability rule like any tolerance, and an unclassified label is
    what the baseline counts as a note.
    """
    frames = [
        FeatureControlFrame(
            annotation_id=a.annotation_id,
            type=a.type,
            tolerance_value_mm=float(a.tolerance_value),
            category=a.category,
            datum_refs=list(a.datum_refs),
            material_condition=a.material_condition,
            face_ids=list(a.face_ids),
            tolerance_zone=a.tolerance_zone,
            details=dict(a.details),
        )
        for a in result.annotations
    ]
    datums = [
        Datum(
            datum_id=d.datum_id,
            label=d.label,
            face_ids=list(d.face_ids),
            is_target=d.is_target,
            target_type=d.target_type,
        )
        for d in result.datums
    ]
    notes = [
        SurfaceFinishNote(
            note_id=n.note_id, text=n.text, face_ids=list(n.face_ids), source=n.source
        )
        for n in result.notes
    ]
    finishes = [
        SurfaceFinish(
            surface_finish_id=s.surface_finish_id,
            ra_um=s.ra_um,
            face_ids=list(s.face_ids),
            source=s.source,
        )
        for s in result.surface_finishes
    ]
    return AnnotationSet(
        frames=frames,
        datums=datums,
        notes=notes,
        surface_finishes=finishes,
        has_pmi=result.has_pmi,
    )
