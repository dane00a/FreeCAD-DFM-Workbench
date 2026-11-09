# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Stage 0: what the FILE says, as opposed to what the geometry says.

Three channels, and none of them ever gates. Exporters write names, colours
and layers arbitrarily and many write nothing, so requiring a prior would turn
every silent exporter into a miss. A prior can raise a score or a tier.

1. Annotation text. Marking is usually *specified* rather than drawn -- a part
   marked per MIL-STD-130 carries a note, not a modelled Data Matrix, because
   the mark is applied downstream. A keyword scan over the notes catches
   marking with no modelled geometry at all.
2. PMI attachment, as a NEGATIVE prior: a real pocket floor often carries a
   dimension, a datum or a tolerance; a glyph floor never does.
3. Face colour or layer: a style carried by a small minority of the faces is a
   hint. Best effort.

Nothing in this workbench supplies the file side yet (see
``machining.annotations``), so by default every channel is empty and the
pipeline runs on geometry alone, losing only the bonuses and the PMI gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

#: About the OPERATION, not what the mark says. A false positive costs one
#: staff prompt, which is why the list can afford to be generous.
#:
#: "MARK " and "IDENT " carry a trailing space because they are stems: the
#: matcher lets a match END mid-word on purpose (ENGRAVE catches ENGRAVED),
#: which is right for a verb and wrong for a truncation -- bare IDENT matched
#: IDENTIFIED inside ASME statistical-tolerancing boilerplate and billed a
#: marking operation on a part with none.
MARKING_KEYWORDS = (
    "MIL-STD-130", "MIL STD 130", "MILSTD130",
    "SILKSCREEN", "SILK SCREEN", "NAMEPLATE", "NAME PLATE",
    "ENGRAVING", "ENGRAVE", "ENGRAVED",
    "MARKING", "MARK ",
    "ETCHING", "ETCH",
    "LASER",
    "STAMPING", "STAMP",
    "IDENTIFICATION", "IDENTIFY", "IDENT ",
    "SERIAL", "S/N",
    "PART NUMBER", "PART NO", "P/N",
    "DOT PEEN", "DOT-PEEN", "DATA MATRIX", "DATAMATRIX",
    "UII", "UID",
)


def marking_keyword_in(text: str) -> str:
    """The marking keyword found in ``text``, or "" when there is none.

    A match may not BEGIN mid-word (REMARKS must not trip MARK) but may end
    mid-word.
    """
    if not text:
        return ""
    hay = text.upper() + " "
    for needle in MARKING_KEYWORDS:
        at = hay.find(needle)
        if at < 0:
            continue
        if at > 0 and hay[at - 1].isascii() and hay[at - 1].isalnum():
            continue
        return needle.rstrip(" ")
    return ""


@dataclass
class NoteDemand:
    note_id: str
    text: str
    keyword: str
    face_ids: list[int] = field(default_factory=list)


@dataclass
class Priors:
    note_demands: list[NoteDemand] = field(default_factory=list)
    pmi_referenced_faces: set[int] = field(default_factory=set)
    face_style: dict[int, str] = field(default_factory=dict)
    rare_styles: set[str] = field(default_factory=set)

    def empty(self) -> bool:
        return not (self.note_demands or self.pmi_referenced_faces or self.face_style)


def rare_styles_of(face_style: dict[int, str]) -> set[str]:
    """Styles carried by at most a quarter of the styled faces."""
    counts: dict[str, int] = {}
    for key in face_style.values():
        counts[key] = counts.get(key, 0) + 1
    styled = sum(counts.values())
    if styled == 0:
        return set()
    return {k for k, n in counts.items() if n <= 0.25 * styled}


def build_priors(
    notes: Iterable = (),
    referenced_faces: Iterable[int] = (),
    face_style: Optional[dict[int, str]] = None,
) -> Priors:
    """Assemble the three channels from whatever the file side supplied.

    ``notes`` are objects with ``text`` and optionally ``note_id`` and
    ``face_ids``; ``referenced_faces`` the face ids any dimension, datum or
    tolerance names; ``face_style`` face id -> "colour:r,g,b" / "layer:NAME".
    """
    priors = Priors()
    for note in notes or ():
        text = getattr(note, "text", "") or ""
        keyword = marking_keyword_in(text)
        if not keyword:
            continue
        priors.note_demands.append(
            NoteDemand(
                note_id=str(getattr(note, "note_id", "") or ""),
                text=text,
                keyword=keyword,
                face_ids=list(getattr(note, "face_ids", []) or []),
            )
        )
    priors.pmi_referenced_faces = set(referenced_faces or ())
    if face_style:
        priors.face_style = dict(face_style)
        priors.rare_styles = rare_styles_of(priors.face_style)
    return priors
