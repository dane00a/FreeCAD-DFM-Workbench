# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The raster verifier behind the marking pipeline's Stage-4 seam.

With no OCR engine or symbol decoder available, this verifier never returns
VERIFIED_TEXT. It is a REJECTER (a region whose stroke width balloons or
tapers like a pocket is positive counter-evidence), a MEASURER (a stroke width
from a distance transform rather than 2A/P), and a RECORDER (a shape-only
"reads as text" goes into ``detail``, never into the verdict -- shape
consistency is not proof).
"""

from __future__ import annotations

from typing import Optional

from .marking.model import Process
from .marking.verifier import (
    MarkingVerifier,
    VerificationRequest,
    VerificationResult,
    Verdict,
)
from .relief_raster import ReliefCandidate
from .relief_verifier import (
    ReliefVerdict,
    ReliefVerification,
    ReliefVerifyConfig,
    verify_relief,
)


def candidate_from_request(request: VerificationRequest, graph):
    """(candidate, ok). Not ok when the host is unknown or there are fewer
    than two ink faces."""
    cand = ReliefCandidate()
    if request.host_face_id is None or not graph.has_node(request.host_face_id):
        return cand, False
    cand.host = graph.node(request.host_face_id)
    # Use the caller's frame, but only when it is not degenerate.
    n, u = request.normal, request.u
    if abs(n[0] * u[0] + n[1] * u[1] + n[2] * u[2]) < 0.999:
        cand.has_frame = True
        cand.frame = (request.origin, request.normal, request.u)
    # INK, not members: rasterising every member of a relieved plaque would
    # paint the milled field solid and bury the letters in it.
    src = request.ink_face_ids if request.ink_face_ids else request.member_face_ids
    cand.members = [graph.node(f) for f in src if graph.has_node(f)]
    if len(cand.members) < 2:
        return cand, False
    cand.depth_mm = request.depth_mm
    cand.cap_height_mm = request.cap_height_mm
    cand.raised = request.process is Process.RAISED
    return cand, True


class ReliefRasterMarkingVerifier(MarkingVerifier):
    name = "ReliefRasterMarkingVerifier"

    def __init__(self, cfg: Optional[ReliefVerifyConfig] = None) -> None:
        self.cfg = cfg or ReliefVerifyConfig()

    def verify_detailed(self, request, graph, face_index) -> ReliefVerification:
        cand, ok = candidate_from_request(request, graph)
        if not ok:
            return ReliefVerification(
                decline_reason="fewer_than_two_ink_faces" if cand.host is not None
                else "host_face_not_in_aag"
            )
        return verify_relief(cand, face_index, self.cfg)

    def verify(self, request, graph, face_index) -> VerificationResult:
        v = self.verify_detailed(request, graph, face_index)
        out = VerificationResult()
        out.refined_stroke_width_mm = v.stroke_w_mm
        out.stroke_width_cv = v.stroke_cv
        detail = ["raster"]
        if v.px_per_mm > 0.0:
            detail.append(f"{v.px_per_mm:.6g}px/mm({v.resolution_driver})")
        if v.component_count:
            detail.append(f"comps={v.component_count}")
        if v.ink_polarity:
            detail.append(f"ink={v.ink_polarity}")

        if v.verdict is ReliefVerdict.DECLINED:
            # No image, nothing to say: when in doubt, UNVERIFIED.
            out.verdict = Verdict.UNVERIFIED
            out.method = "none"
            detail.append(f"declined={v.decline_reason}")
        elif v.verdict is ReliefVerdict.REJECTED:
            out.verdict = Verdict.REJECTED
            out.method = "raster_shape"
            out.confidence = v.confidence
            detail.append(f"glyph_ink={v.glyph_ink_fraction:.6g}")
        elif v.verdict is ReliefVerdict.UNDECIDED:
            out.verdict = Verdict.UNVERIFIED
            out.method = "raster_shape"
            detail.append(f"undecided glyph_ink={v.glyph_ink_fraction:.6g}")
        else:
            if v.decoded_text:
                lattice = v.hypothesis == "module_lattice"
                out.verdict = Verdict.VERIFIED_SYMBOL if lattice else Verdict.VERIFIED_TEXT
                out.decoded_text = v.decoded_text
                out.confidence = v.confidence
                out.method = "datamatrix" if lattice else "ocr"
            else:
                # Deliberately NOT promoted: a quote on a shape argument is
                # what this seam exists to prevent.
                out.verdict = Verdict.UNVERIFIED
                out.method = "raster_shape"
                out.confidence = v.confidence
                detail.append(
                    f"reads_as={v.hypothesis} shape_confidence={v.confidence:.6g} "
                    "(no decoder linked; not promoted)"
                )
        detail.extend(v.evidence)
        out.detail = " ".join(detail)
        return out
