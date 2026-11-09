# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""MARKING_TEXT: engraved or raised lettering, part numbers and logos.

Engraving is a separate operation that has to be quoted, and raised text on a
machined face means milling the whole face down around islanded strokes. Left
unrecognized, one engraved line fires dozens of thin-wall, sharp-edge and
minimum-feature findings, and every stroke reads as a slot, a boss and an
undercut at once.

"No missed text and absolutely no false positives" is not achievable as one
automatic flag: a decorative relief, a machined logo and a formed logo are
identical from the front. It IS achievable as two thresholds with a human
between them -- the tier contract this module emits:

* Tier A -- verified: read back, a decoded symbol, or a file note naming a
  marking operation plus corroborating geometry. Safe to quote unattended.
* Tier B -- geometrically scored, unverified. Per-stroke findings are
  suppressed and exactly one staff confirmation is raised.
* Tier C -- rejected; not emitted, the failing gate recorded in
  ``rejections``.

The geometry lives in the ``marking`` package (one module per stage). This
module runs the stages in order, calls the verification seam, decides the
tier, resolves overlapping claims and emits the features. The feature owns
all member faces, so the resolver's containment dedup absorbs the per-stroke
claims the earlier recognizers made; it runs late for exactly that reason.
"""

from __future__ import annotations

import os
from typing import Optional, Sequence

from ...utils.geometry import FaceIndex
from ..aag import AttributedAdjacencyGraph
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer
from .marking import tuning as T
from .marking.model import INK_ORDER, InkKind, MarkingCluster, Process, Rejection, Tier
from .marking.process_discriminator import discriminate_processes
from .marking.relief_generator import generate_relief_candidates, protected_prior_faces
from .marking.shape_signature import score_candidates
from .marking.verifier import VerificationRequest, Verdict, active_verifier


def _med(values) -> float:
    return T.median(values)


def dominant_ink_kind(c: MarkingCluster) -> InkKind:
    """The MAJORITY ink kind over the cluster's regions, ties to enum order.

    A cluster need not be uniform -- one thin stroke bounded only by its walls
    among five floored ones is a property of the letterform, not a second
    operation -- and reading the first region mislabelled such a cluster.
    """
    counts = [0] * len(INK_ORDER)
    for r in c.regions:
        counts[INK_ORDER.index(r.ink_kind)] += 1
    best = 0
    for i in range(1, len(counts)):
        if counts[i] > counts[best]:
            best = i
    return INK_ORDER[best]


def cluster_is_floorless(c: MarkingCluster) -> bool:
    """Floorless only when NO region has a flat ink face: the same question
    the verifier asks, "is there anything to rasterise"."""
    return all(not r.ink for r in c.regions)


def make_request(c: MarkingCluster) -> VerificationRequest:
    h = c.host
    q = VerificationRequest(
        origin=h.origin, normal=h.normal, u=h.u, v=h.v,
        host_face_id=h.primary_face_id, host_face_ids=list(h.face_ids),
        process=c.process, depth_mm=c.depth_mm, stroke_width_mm=c.stroke_w_mm,
        cap_height_mm=c.cap_height_mm, geometric_score=c.score,
        component_count=len(c.regions), baseline_dir=tuple(c.baseline_dir),
        ink_kind=dominant_ink_kind(c),
    )
    for comp, r in enumerate(c.regions):
        for f in r.ink:
            q.ink_face_ids.append(f.face_id)
            q.ink_component_of.append(comp)
        for f in r.faces:
            q.member_face_ids.append(f.face_id)
    # MIL-STD-130 direct part marking uses a 0.19-0.635 mm module.
    aspect = c.cap_height_mm / c.diag_mm if c.diag_mm > 1e-6 else 0.0
    q.datamatrix_plausible = 0.19 <= c.stroke_w_mm <= 0.635 and aspect > 0.5
    return q


class MarkingRecognizer(FeatureRecognizer):
    """Recognizes engraved and embossed text and logos."""

    prefix = "mk"

    #: Stage-0 priors (``marking.priors.Priors``), when the file side supplies
    #: any. With none the pipeline runs on geometry alone.
    priors = None

    def __init__(self) -> None:
        self.rejections: list[Rejection] = []
        # Every face of every relief Stage 2 positively called ENGRAVED,
        # whether or not it became a feature: the structural form of the
        # sheet-forming veto. Published, not yet consumed (see the reference
        # notes: a veto computed from the marking side cannot tell a formed
        # hood's own back from a cut).
        self.engraved_relief_faces: set[int] = set()

    @property
    def name(self) -> str:
        return "Marking Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored: every stroke has already been
        # claimed as a slot, a boss, an undercut, and overruling those claims
        # is the point. Only the groove and thread families are protected.
        self.rejections = []
        self.engraved_relief_faces = set()
        trace = os.environ.get("DFM_MARK_TRACE") is not None
        face_index = FaceIndex(shape) if shape is not None else None

        blocked = protected_prior_faces(prior or ())
        scene = generate_relief_candidates(
            graph, face_index, self.priors, blocked or None, self.rejections
        )
        discriminate_processes(graph, scene, self.rejections)

        # ENGRAVED only, and a face any host saw as FORMED is struck back out:
        # vetoing on RAISED took real louvers and lances off sheet parts.
        formed_anywhere = {
            f.face_id for r in scene.regions if r.process is Process.FORMED for f in r.faces
        }
        for r in scene.regions:
            if r.process is not Process.ENGRAVED:
                continue
            for f in r.faces:
                if f.face_id not in formed_anywhere:
                    self.engraved_relief_faces.add(f.face_id)

        clusters = score_candidates(scene, self.priors, self.rejections)

        verifier = active_verifier()
        for c in clusters:
            if c.host is None:
                continue
            req = make_request(c)
            # Nothing to rasterise means nothing to verify. A floorless mark
            # has no ink face by definition; handing the verifier an empty
            # image got it REJECTED and lost the mark. Unverifiable is Tier B.
            if not req.ink_face_ids:
                c.tier = Tier.B
                c.verification_method = "none"
                c.confidence = c.score
                c.evidence.append("unverifiable:no_rasterisable_ink")
                continue
            if face_index is None:
                vr = None
            else:
                vr = verifier.verify(req, graph, face_index)
            if vr is not None and vr.verdict in (Verdict.VERIFIED_TEXT, Verdict.VERIFIED_SYMBOL):
                c.tier = Tier.A
                c.decoded_text = vr.decoded_text
                c.verification_method = vr.method
                c.confidence = max(c.score, vr.confidence)
                c.evidence.append(f"verified:{vr.verdict.value}")
            elif vr is not None and vr.verdict is Verdict.REJECTED:
                c.tier = Tier.C
                c.verification_method = vr.method
                c.evidence.append("verifier_rejected")
                self.rejections.append(Rejection(
                    "stage4", "verifier_rejected", vr.detail or verifier.name, 0.0,
                    c.host.primary_face_id, c.face_ids(),
                ))
            else:
                if c.note_backed and c.score >= T.TIER_A_NOTE_SCORE:
                    c.tier = Tier.A
                    c.verification_method = "note"
                    c.confidence = c.score
                    c.evidence.append("verified:note_plus_geometry")
                else:
                    c.tier = Tier.B
                    c.verification_method = "none"
                    c.confidence = c.score
            # A raster stroke width sees the distribution, not its mean.
            if vr is not None and vr.refined_stroke_width_mm > 0.0:
                c.stroke_w_mm = vr.refined_stroke_width_mm

        # Stage 5: best claim first, by TIER then SCORE. A verifier rejection
        # blocks the geometry from its own rank downward -- it must not let a
        # worse reading of the same faces through, nor veto a better one.
        ranked = sorted(
            clusters, key=lambda c: (0 if c.tier is Tier.A else 1, -c.score)
        )
        vetoed: set[int] = set()
        claimed_faces: set[int] = set()
        out: list[FeatureInstance] = []
        for c in ranked:
            ids = c.face_ids()
            if c.tier is Tier.C:
                for fid in ids:
                    if fid not in claimed_faces:
                        vetoed.add(fid)
                continue
            if any(fid in vetoed for fid in ids):
                self.rejections.append(Rejection(
                    "stage5", "shares_faces_with_a_verifier_rejection",
                    "a verifier rejected another reading of this same relief", 0.0,
                    c.host.primary_face_id if c.host else None, ids,
                ))
                continue
            if any(fid in claimed_faces for fid in ids):
                self.rejections.append(Rejection(
                    "stage5", "superseded_by_better_claim",
                    "a higher-tier or higher-scoring cluster already owns these faces",
                    0.0, c.host.primary_face_id if c.host else None, ids,
                ))
                continue
            claimed_faces.update(ids)

            # Glyph count: components, or the islands of a relieved plaque,
            # plus the punctuation the pairing constraint keeps out.
            glyphs = len(c.regions)
            if len(c.regions) == 1 and c.regions[0].ink_kind is InkKind.ISLAND:
                glyphs = len(c.regions[0].ink)
            glyphs += c.satellite_count
            depths = [r.depth_mm for r in c.regions]
            strokes = [r.stroke_w_mm for r in c.regions]
            kind = dominant_ink_kind(c)
            params = {
                "marking_type": "raised" if c.proud else "engraved",
                "glyph_count": glyphs,
                "depth_mm": _med(depths),
                "stroke_width_mm": c.stroke_w_mm if c.stroke_w_mm > 0.0 else _med(strokes),
                "host_face": c.host.primary_face_id if c.host else None,
            }
            # The run's own tracking, for the resolver's marking-field pass.
            # Absent on a one-region logotype, where no lateral allowance may
            # be made.
            if c.char_gap_mm > 0.0:
                params["char_gap_mm"] = c.char_gap_mm
            params.update({
                "tier": c.tier.value,
                "confidence": c.confidence,
                "score": c.score,
                "verification": c.verification_method or "none",
                "evidence": list(c.evidence),
                "process": c.process.value,
                "ink_kind": kind.value,
                # A STAFF question, never a customer one.
                "requires_staff_confirmation": c.tier is Tier.B,
            })
            if c.decoded_text:
                params["decoded_text"] = c.decoded_text
            if kind is InkKind.ISLAND:
                params["background_relief"] = True
            if cluster_is_floorless(c):
                params["floorless"] = True
            if len(c.regions) == 1 and kind is not InkKind.ISLAND:
                params["logotype"] = True

            if trace:
                print(f"[mark] emit tier={c.tier.value} score={c.score:.4g} "
                      f"type={params['marking_type']} kind={kind.value} "
                      f"regions={len(c.regions)} absorbed={len(c.absorbed)} "
                      f"faces={len(ids)} depth={_med(depths):.4g} "
                      f"stroke={c.stroke_w_mm:.4g} host={params['host_face']}")
            out.append(FeatureInstance(
                instance_id=self.instance_id(len(out)),
                type=FeatureType.MARKING_TEXT,
                faces=list(ids),
                parameters=params,
            ))

        if trace:
            for rj in self.rejections:
                print(f"[mark] reject {rj.stage}/{rj.gate} ({len(rj.face_ids)} faces) {rj.detail}")
        return out
