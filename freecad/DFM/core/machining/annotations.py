# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tolerance and surface-finish callouts attached to a part.

What a drawing says, as opposed to what the solid says. A feature control
frame, the datums it measures from, a general finish note, a per-face Ra
requirement: none of these are geometry, and none of them can be recovered by
looking at the shape. They have to be carried alongside it.

**Nothing supplies them today, and that is deliberate.** A FreeCAD document
has nowhere to keep a feature control frame, so :func:`annotations_for`
returns an empty set for every part, and every rule that reads it is silent.
The rules are written, registered and tested anyway, so that the policy is
already in place -- and already editable by the shop -- on the day the
annotations arrive, rather than being a second project stacked on top of the
first.

Where they would come from
--------------------------
Two sources are plausible, and neither needs this module to change shape:

*STEP PMI on import.* AP242 carries semantic PMI, and OpenCascade's XDE layer
already reads most of it: ``XCAFDoc_DimTolTool`` yields the dimension and
geometric-tolerance entities and their datum associations,
``XCAFDoc_NotesTool`` yields free-text notes, and the AP242
``SURFACE_TEXTURE_REQUIREMENT`` entities carry Ra. Wiring that up means
walking the XDE document at import time, translating each entity into the
dataclasses below, and mapping every referenced ``TopoDS_Face`` back to a
one-based id through :class:`~...utils.geometry.FaceIndex` -- the same ids
the adjacency graph and every finding already use.

*A FreeCAD annotation object.* A part toleranced inside FreeCAD rather than
imported would keep its callouts on a document object -- a TechDraw GD&T
view, or a scripted object holding one frame per row. Reading that means the
same translation step against ``Part::TopoShape`` sub-element names, which
are already one-based ``FaceN`` strings.

What would have to change to light these rules up
-------------------------------------------------
1. Write the translator, wherever the callouts are coming from.
2. Call :func:`set_annotation_source` once at start-up with a callable that
   takes a :class:`~.context.MachiningContext` and returns an
   :class:`AnnotationSet` for that part (or ``None`` when it has none).
3. List the GD&T rules in the process definitions that should run them.

No rule and no check signature changes. The checks already read this module's
entry point, so supplying a source is the whole of the integration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from .context import MachiningContext


# =============================================================================
# Vocabulary
# =============================================================================


class ToleranceType:
    """The fourteen geometric characteristics of ASME Y14.5.

    Plain lower-case strings rather than an enum, for the same reason
    :class:`~.features.FeatureType` is: they are written into saved analyses
    and matched by rules, so their exact spelling is part of the contract.
    """

    # Form -- controlled on the feature alone, no datum needed.
    STRAIGHTNESS = "straightness"
    FLATNESS = "flatness"
    CIRCULARITY = "circularity"
    CYLINDRICITY = "cylindricity"
    # Profile -- controls a whole surface against its true profile.
    PROFILE_OF_A_LINE = "profile_of_a_line"
    PROFILE_OF_A_SURFACE = "profile_of_a_surface"
    # Orientation -- an angle held relative to a datum.
    ANGULARITY = "angularity"
    PERPENDICULARITY = "perpendicularity"
    PARALLELISM = "parallelism"
    # Location -- where the feature sits in the reference frame.
    POSITION = "position"
    CONCENTRICITY = "concentricity"
    SYMMETRY = "symmetry"
    # Runout -- what a dial indicator sees as the part turns.
    CIRCULAR_RUNOUT = "circular_runout"
    TOTAL_RUNOUT = "total_runout"


class ToleranceCategory:
    """The family a characteristic belongs to.

    Rules care about the family far more often than the individual
    characteristic: form and profile tolerances imply a finish, location
    tolerances are held by the machine's positioning accuracy.
    """

    FORM = "form"
    PROFILE = "profile"
    ORIENTATION = "orientation"
    LOCATION = "location"
    RUNOUT = "runout"


_CATEGORY_BY_TYPE: dict[str, str] = {
    ToleranceType.STRAIGHTNESS: ToleranceCategory.FORM,
    ToleranceType.FLATNESS: ToleranceCategory.FORM,
    ToleranceType.CIRCULARITY: ToleranceCategory.FORM,
    ToleranceType.CYLINDRICITY: ToleranceCategory.FORM,
    ToleranceType.PROFILE_OF_A_LINE: ToleranceCategory.PROFILE,
    ToleranceType.PROFILE_OF_A_SURFACE: ToleranceCategory.PROFILE,
    ToleranceType.ANGULARITY: ToleranceCategory.ORIENTATION,
    ToleranceType.PERPENDICULARITY: ToleranceCategory.ORIENTATION,
    ToleranceType.PARALLELISM: ToleranceCategory.ORIENTATION,
    ToleranceType.POSITION: ToleranceCategory.LOCATION,
    ToleranceType.CONCENTRICITY: ToleranceCategory.LOCATION,
    ToleranceType.SYMMETRY: ToleranceCategory.LOCATION,
    ToleranceType.CIRCULAR_RUNOUT: ToleranceCategory.RUNOUT,
    ToleranceType.TOTAL_RUNOUT: ToleranceCategory.RUNOUT,
}


class MaterialCondition:
    """The modifier inside the circle after the tolerance value.

    Empty means regardless of feature size, which is the default when a
    drawing says nothing.
    """

    RFS = ""
    MMC = "MMC"  # maximum material condition
    LMC = "LMC"  # least material condition


# A modifier that trades size departure for position, which only a feature of
# size can do -- it needs an actual size dimension to depart from.
MATERIAL_MODIFIERS = frozenset({MaterialCondition.MMC, MaterialCondition.LMC})


# =============================================================================
# The callouts themselves
# =============================================================================


@dataclass
class FeatureControlFrame:
    """One feature control frame: a characteristic, a zone, and its datums.

    ``feature_id`` is the recognized feature the frame is attached to, when
    the source could work that out. ``face_ids`` are the faces the frame
    points at, one-based as everywhere else in the workbench. Either may be
    empty: a frame read from a drawing may name neither.
    """

    annotation_id: str
    type: str = ToleranceType.POSITION
    tolerance_value_mm: float = 0.0
    category: str = ""
    datum_refs: list[str] = field(default_factory=list)
    material_condition: str = MaterialCondition.RFS
    feature_id: str = ""
    face_ids: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The family follows from the characteristic, so a source that knows
        # the characteristic need not spell the family out. An explicit
        # category still wins: a source reading a frame it cannot classify
        # can say so directly.
        if not self.category:
            self.category = _CATEGORY_BY_TYPE.get(self.type, "")

    @property
    def has_material_modifier(self) -> bool:
        return self.material_condition in MATERIAL_MODIFIERS

    @property
    def label(self) -> str:
        """How to name this frame in a finding when nothing else identifies it."""
        return self.annotation_id or self.type


@dataclass
class Datum:
    """A datum feature: a letter, and the geometry it is established on.

    ``face_ids`` empty means the letter parsed but the geometry link did not
    -- the case :class:`GdtDatumUnresolvedCheck` exists for. A datum *target*
    is the exception: it is anchored by its own placement point rather than by
    a face, so empty faces are correct for one.
    """

    datum_id: str
    label: str = ""
    face_ids: list[int] = field(default_factory=list)
    is_target: bool = False


@dataclass
class SurfaceFinishNote:
    """A free-text PMI note, which may or may not contain a finish callout.

    Kept as text rather than a parsed value because that is what the source
    has: a note is a string in the title block, and the Ra callout inside it
    -- if there is one -- has to be dug out with :func:`parse_ra_um`.
    """

    note_id: str
    text: str = ""
    face_ids: list[int] = field(default_factory=list)


@dataclass
class SurfaceFinish:
    """A semantic surface-texture requirement, already carrying its Ra.

    Distinct from a note: this is a machine-readable requirement (an AP242
    ``SURFACE_TEXTURE_REQUIREMENT``, say) with the value already extracted and
    the faces it applies to already resolved. No parsing, no ambiguity about
    units.
    """

    surface_finish_id: str
    ra_um: float = 0.0
    face_ids: list[int] = field(default_factory=list)
    source: str = ""


@dataclass
class AnnotationSet:
    """Every callout attached to one part.

    Empty is the normal case today, and :meth:`is_empty` is what lets a rule
    stand down in one line rather than four.
    """

    frames: list[FeatureControlFrame] = field(default_factory=list)
    datums: list[Datum] = field(default_factory=list)
    notes: list[SurfaceFinishNote] = field(default_factory=list)
    surface_finishes: list[SurfaceFinish] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.frames or self.datums or self.notes or self.surface_finishes)

    def sorted_frames(self) -> list[FeatureControlFrame]:
        """Frames in a stable order, so findings come out the same every run."""
        return sorted(self.frames, key=lambda f: (f.annotation_id, f.type))

    def sorted_datums(self) -> list[Datum]:
        return sorted(self.datums, key=lambda d: (d.label, d.datum_id))

    def sorted_notes(self) -> list[SurfaceFinishNote]:
        return sorted(self.notes, key=lambda n: n.note_id)

    def sorted_surface_finishes(self) -> list[SurfaceFinish]:
        return sorted(self.surface_finishes, key=lambda s: s.surface_finish_id)

    def frames_for(self, feature_id: str) -> list[FeatureControlFrame]:
        """Frames attached to one recognized feature."""
        return [f for f in self.sorted_frames() if f.feature_id == feature_id]

    def cited_datum_labels(self) -> set[str]:
        """Every datum letter some frame actually references.

        A datum record nothing cites is inert -- it locates nothing, so
        nothing is at risk if it fails to resolve.
        """
        return {ref for frame in self.frames for ref in frame.datum_refs if ref}


# =============================================================================
# Reading Ra out of note text
# =============================================================================

# 1 microinch = 0.0254 micrometres.
_MICROINCH_TO_MICROMETRE = 0.0254

# A bare "Ra 32" with no unit is microinches: nobody mills to Ra 32 um, and
# 32 uin (0.8 um) is a common ground-finish callout. Below this a bare number
# is read as micrometres, which is how a metric drawing writes it.
_BARE_MICROINCH_MIN = 5.0

_RA_PATTERN = re.compile(
    r"(?:^|[^A-Za-z])Ra\s*([0-9]*\.?[0-9]+)\s*(um|µm|μm|microm|uin|µin|microin)?",
    re.IGNORECASE,
)

_MICROINCH_MARKERS = ("uin", "µin", "μin", "microin")


def parse_ra_um(text: str) -> float:
    """The Ra value a note calls out, in micrometres, or 0.0 for none.

    Aimed at the common shop-drawing case: "Ra 0.8" or "125 Ra" style text in
    a title block or general note. Microinches are told apart from
    micrometres by an explicit suffix where there is one, and by magnitude
    where there is not -- see :data:`_BARE_MICROINCH_MIN`.
    """
    if not text:
        return 0.0

    match = _RA_PATTERN.search(text)
    if match is None:
        return 0.0

    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return 0.0
    if value <= 0.0:
        return 0.0

    unit = (match.group(2) or "").lower()
    is_microinch = any(marker in unit for marker in _MICROINCH_MARKERS) or (
        not unit and value > _BARE_MICROINCH_MIN
    )
    return value * _MICROINCH_TO_MICROMETRE if is_microinch else value


# =============================================================================
# The entry point
# =============================================================================

AnnotationSource = Callable[["MachiningContext"], Optional[AnnotationSet]]

_source: Optional[AnnotationSource] = None


def set_annotation_source(source: Optional[AnnotationSource]) -> None:
    """Install the callable that supplies callouts for a part.

    This is the whole of the integration seam. A STEP importer that has read
    PMI, or a document scanner that has found an annotation object, calls
    this once with a function taking a :class:`~.context.MachiningContext`
    and returning an :class:`AnnotationSet`. Passing ``None`` removes the
    source and puts every GD&T rule back to sleep.
    """
    global _source
    _source = source


def annotation_source() -> Optional[AnnotationSource]:
    """The installed source, or None when nothing supplies callouts."""
    return _source


def annotations_for(context: "MachiningContext") -> AnnotationSet:
    """Every tolerance and finish callout on this part.

    Returns an empty set when no source is installed, which is every part
    today -- so a rule reading this finds nothing to judge rather than being
    switched off. A source that raises is treated as having nothing to say:
    a broken PMI translator must not take the whole analysis down with it.
    """
    source = _source
    if source is None:
        return AnnotationSet()
    try:
        supplied = source(context)
    except Exception:
        return AnnotationSet()
    return supplied if isinstance(supplied, AnnotationSet) else AnnotationSet()
