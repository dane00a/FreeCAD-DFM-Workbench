# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""A STEP file's entity graph, read straight from the text.

OpenCascade's XDE layer is lossy for PMI in ways that matter. A datum spanning
two faces is bound through an ``ITEM_IDENTIFIED_REPRESENTATION_USAGE`` whose
item list the reader leaves empty; a general drawing note lives in a
``DESCRIPTIVE_REPRESENTATION_ITEM`` no XCAF accessor exposes; and on the 7.8
kernel this workbench runs, a tolerance whose magnitude is written as a
complex measure entity comes back with a value of zero. In every case the
information is plain in the file, so this index makes the graph walkable
without fighting the typed-entity coverage.

Parsing is deliberately minimal, and deliberately the same as the reference
engine's ``StepTextIndex``: an entity id, its type name, the ids it
references, its string literals and its numeric literals, each in argument
order. Nested aggregates are flattened, so argument *position* is only
reliable before the first aggregate. String literals are skipped for
structure -- a name may contain ``#`` or ``;`` -- and a doubled quote is an
embedded quote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class StepTextEntity:
    """One entity instance as it appears in the file."""

    id: int = 0
    type: str = ""  # upper-case; empty for a complex "(A(..)B(..))" instance
    refs: List[int] = field(default_factory=list)
    strings: List[str] = field(default_factory=list)
    reals: List[float] = field(default_factory=list)
    #: The argument text, verbatim. Only for the few readers that need a
    #: sub-entity's type name inside a complex instance -- a unit written as
    #: ``(CONVERSION_BASED_UNIT('INCH',#9) LENGTH_UNIT() NAMED_UNIT(#8))``.
    raw: str = ""


# One scanner for the whole DATA section. Order matters: a string literal is
# consumed whole before anything inside it can be read as structure, and an
# entity header is tried before a bare reference.
#
# The numeric alternative mirrors the reference's ``strtod`` trigger: a number
# starts at a digit, '-' or '+', anywhere outside a string -- including inside
# an identifier such as ``CAMERA_MODEL_D3``, which the reference reads as 3.0
# too. Nothing downstream depends on those strays, but the two parsers agree
# on them all the same.
_TOKEN = re.compile(
    r"'((?:[^']|'')*)'"  # 1: string literal
    r"|#(\d+)\s*=\s*([A-Za-z0-9_]*)"  # 2, 3: entity header and its type
    r"|#(\d+)"  # 4: reference
    r"|([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"  # 5: number
    r"|(;)"  # 6: terminator
)


class StepTextIndex:
    """A read-only index of one STEP file's entities."""

    def __init__(self) -> None:
        self._by_id: Dict[int, StepTextEntity] = {}
        self._referrers: Dict[int, List[int]] = {}

    def load(self, path: str) -> bool:
        """Parse a file. False when it cannot be read or holds no entities."""
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            return False
        self.parse(raw.decode("latin-1"))
        return bool(self._by_id)

    def parse(self, text: str) -> None:
        """Parse STEP text already in memory (used by the tests)."""
        start = text.find("DATA;")
        if start >= 0:
            text = text[start + 5 :]

        current: Optional[StepTextEntity] = None
        body_start = 0
        for match in _TOKEN.finditer(text):
            literal, header_id, header_type, ref, number, end = match.groups()
            if current is None:
                # Outside an entity only a header means anything.
                if header_id is not None:
                    current = StepTextEntity(
                        id=int(header_id), type=header_type.upper()
                    )
                    body_start = match.end()
                continue
            if literal is not None:
                current.strings.append(literal.replace("''", "'"))
            elif end is not None:
                current.raw = text[body_start : match.start()]
                self._store(current)
                current = None
            elif header_id is not None:
                # "#12=" cannot occur inside a well-formed entity; read it the
                # way the reference does, as a reference to #12.
                current.refs.append(int(header_id))
            elif ref is not None:
                current.refs.append(int(ref))
            elif number is not None:
                try:
                    current.reals.append(float(number))
                except ValueError:
                    pass
        if current is not None:
            self._store(current)

    def _store(self, entity: StepTextEntity) -> None:
        for ref in entity.refs:
            self._referrers.setdefault(ref, []).append(entity.id)
        self._by_id.setdefault(entity.id, entity)

    def get(self, entity_id: int) -> Optional[StepTextEntity]:
        return self._by_id.get(entity_id)

    def of_type(self, needle: str) -> List[StepTextEntity]:
        """Entities whose type name contains ``needle`` (upper-case substring)."""
        return [e for e in self._by_id.values() if needle in e.type]

    def referrers(self, entity_id: int) -> List[int]:
        """Ids of the entities that reference ``entity_id`` anywhere."""
        return self._referrers.get(entity_id, [])

    def entities(self) -> List[StepTextEntity]:
        return list(self._by_id.values())

    def empty(self) -> bool:
        return not self._by_id
