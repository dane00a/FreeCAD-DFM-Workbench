# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Stage 4: the verification seam.

Precision in this problem cannot come from tightening geometry gates; it has
to come from RECOGNITION verifying DETECTION. Making that a seam lets stages
1-3 be tuned for recall without the false-positive half degrading, because an
unverified candidate lands in Tier B behind one staff confirmation rather than
in an unattended quote.

The one load-bearing distinction: UNVERIFIED ("I could not prove this is
text" -- the expected answer for logos, non-Latin type, tiny text) is NOT
REJECTED ("I have positive counter-evidence"). When in doubt a verifier says
UNVERIFIED: that costs one staff click, a wrong REJECTED a mis-priced part.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .model import InkKind, Process


@dataclass
class VerificationRequest:
    origin: tuple = (0.0, 0.0, 0.0)
    normal: tuple = (1.0, 0.0, 0.0)
    u: tuple = (1.0, 0.0, 0.0)
    v: tuple = (0.0, 1.0, 0.0)
    host_face_id: Optional[int] = None
    host_face_ids: list[int] = field(default_factory=list)
    ink_face_ids: list[int] = field(default_factory=list)
    member_face_ids: list[int] = field(default_factory=list)
    ink_component_of: list[int] = field(default_factory=list)
    component_count: int = 0
    process: Process = Process.UNDECIDED
    ink_kind: InkKind = InkKind.NONE
    depth_mm: float = 0.0
    stroke_width_mm: float = 0.0
    cap_height_mm: float = 0.0
    baseline_dir: tuple = (0.0, 0.0, 0.0)
    geometric_score: float = 0.0
    datamatrix_plausible: bool = False


class Verdict(Enum):
    UNVERIFIED = "unverified"
    VERIFIED_TEXT = "verified_text"
    VERIFIED_SYMBOL = "verified_symbol"
    REJECTED = "rejected"


@dataclass
class VerificationResult:
    verdict: Verdict = Verdict.UNVERIFIED
    decoded_text: str = ""
    confidence: float = 0.0
    method: str = "none"
    detail: str = ""
    refined_stroke_width_mm: float = 0.0
    stroke_width_cv: float = 0.0


class MarkingVerifier:
    name = "MarkingVerifier"

    def verify(self, request: VerificationRequest, graph, face_index) -> VerificationResult:
        raise NotImplementedError


class NullMarkingVerifier(MarkingVerifier):
    """Always UNVERIFIED: nothing is auto-quoted and nothing silently dropped."""

    name = "NullMarkingVerifier"

    def verify(self, request, graph, face_index) -> VerificationResult:
        return VerificationResult(
            Verdict.UNVERIFIED, method="none",
            detail="no raster verifier is installed in this build",
        )


_NULL = NullMarkingVerifier()
_active: Optional[MarkingVerifier] = None


def active_verifier() -> MarkingVerifier:
    """The installed verifier. The reference installs its raster verifier
    unconditionally, because with no decoder linked it can only measure and
    reject -- never confirm -- so this does the same on first use."""
    global _active
    if _active is None:
        from ..relief_marking_verifier import ReliefRasterMarkingVerifier

        _active = ReliefRasterMarkingVerifier()
    return _active


def set_active_verifier(verifier: Optional[MarkingVerifier]) -> None:
    """Install a verifier; ``None`` restores the null one."""
    global _active
    _active = verifier if verifier is not None else _NULL
