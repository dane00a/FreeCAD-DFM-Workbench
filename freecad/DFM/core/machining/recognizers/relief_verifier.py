# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Verifying a rasterised surface relief.

The precision-restoring half of the marking verifier. It runs only on
survivors of a recall-tuned stage, so it can afford to be expensive, and it is
allowed to be wrong in one direction only: Rejected is a strong claim,
Confirmed a consistency finding, Undecided means the image did not settle it.

It is a distance-transform / connected-component verifier applying the Stroke
Width Transform paper's criteria (stroke-width constancy, bounded
slenderness, bounded containment, then pairing and chaining) to an image we
generated ourselves, so it is noise-free and bitonal. No OCR engine is used:
none ships with the workbench, and a decode hook is the seam for one.

What a plan view cannot do: tell machined relief from FORMED relief. A louver
is a long thin bar inside the glyph slenderness band; it comes back Undecided,
and separating it is the back-side test's job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import numpy as np

from .relief_raster import (
    ReliefCandidate,
    ReliefRaster,
    ReliefRasterConfig,
    invert_enclosed,
    label_runs,
    outside_background,
    rasterise_relief,
)


class ReliefVerdict(Enum):
    DECLINED = "declined"
    REJECTED = "rejected"
    UNDECIDED = "undecided"
    CONFIRMED = "confirmed"


_RANK = {
    ReliefVerdict.CONFIRMED: 3,
    ReliefVerdict.UNDECIDED: 2,
    ReliefVerdict.REJECTED: 1,
    ReliefVerdict.DECLINED: 0,
}

# Box fill and shape homogeneity are SCORED, not gated: a heavy font fills its
# boxes nearly as well as a bank of identical formed bars, three points apart,
# and a gate in that band threw away a true positive.
CHAIN_FILL_PENALTY_ABOVE = 0.85
HOMOGENEITY_PENALTY_BELOW = 0.08


@dataclass
class ReliefComponent:
    area_px: int = 0
    x0: int = 0
    y0: int = 0
    x1: int = 0
    y1: int = 0
    cx: float = 0.0
    cy: float = 0.0
    stroke_w_px: float = 0.0
    stroke_cv: float = 0.0
    slenderness: float = 0.0
    aspect: float = 0.0
    bbox_fill: float = 0.0
    hole_count: int = 0
    contained: int = 0
    glyph_like: bool = False
    reject_reason: str = ""


@dataclass
class ReliefVerifyConfig:
    raster: ReliefRasterConfig = field(default_factory=ReliefRasterConfig)
    min_area_px: float = 12.0
    min_stroke_px: float = 2.0
    max_stroke_cv: float = 0.55
    # diag / stroke width: a filled square 1.41, a 2:1 rectangle 2.24, a
    # seven-segment digit ~11, a hairline or knurl groove hundreds.
    min_slenderness: float = 2.5
    max_slenderness: float = 30.0
    max_aspect: float = 12.0
    max_contained: int = 2
    max_holes: int = 3
    # Reporting only: a stroke segment legitimately fills its own box.
    max_bbox_fill: float = 1.01
    pair_stroke_ratio: float = 2.0
    pair_height_ratio: float = 2.0
    pair_gap_heights: float = 3.0
    min_chain: int = 3
    max_baseline_residual: float = 0.28
    max_cap_height_cv: float = 0.40
    min_chain_ink: float = 0.35
    reject_below_glyph_ink: float = 0.30
    logotype_max_bbox_fill: float = 0.62
    logotype_min_slender: float = 3.0
    logotype_max_components: int = 4
    logotype_max_stroke_over_cap: float = 0.30
    decode: Optional[Callable[[ReliefRaster], str]] = None


@dataclass
class ReliefVerification:
    verdict: ReliefVerdict = ReliefVerdict.DECLINED
    confidence: float = 0.0
    decoded_text: str = ""
    hypothesis: str = ""
    ink_polarity: str = ""
    decline_reason: str = ""
    px_per_mm: float = 0.0
    stroke_w_mm: float = 0.0
    stroke_w_2ap_mm: float = 0.0
    stroke_cv: float = 0.0
    cap_height_mm: float = 0.0
    resolution_driver: str = ""
    resolution_capped: bool = False
    component_count: int = 0
    glyph_like_count: int = 0
    chained_count: int = 0
    chain_count: int = 0
    chain_median_fill: float = 0.0
    chain_area_cv: float = 0.0
    glyph_ink_fraction: float = 0.0
    chain_ink_fraction: float = 0.0
    unchained_max_ink_fraction: float = 0.0
    baseline_residual_ratio: float = 0.0
    cap_height_cv: float = 0.0
    has_module_lattice: bool = False
    lattice_rows: int = 0
    lattice_cols: int = 0
    lattice_pitch_mm: float = 0.0
    evidence: list[str] = field(default_factory=list)
    components: list[ReliefComponent] = field(default_factory=list)


def _median_of(values) -> float:
    """The TRUE median here (even counts averaged), unlike the marking stages."""
    v = sorted(values)
    n = len(v)
    if n == 0:
        return 0.0
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def _cv_of(values) -> float:
    v = list(values)
    if len(v) < 2:
        return 0.0
    mean = sum(v) / len(v)
    if abs(mean) < 1.0e-12:
        return 0.0
    s = 0.0
    for x in v:
        s += (x - mean) * (x - mean)
    return math.sqrt(s / len(v)) / abs(mean)


def squared_edt(ink: np.ndarray) -> np.ndarray:
    """Exact squared Euclidean distance from each ink pixel to the nearest
    background pixel, in pixel units (zero on background).

    Separable: the vertical distance g per column, then
    d^2(x) = min over x' of g(x')^2 + (x - x')^2. The minimising x' lies
    within the true distance of x, which is at most min(vertical, horizontal)
    run distance, so a window of that radius is exact.
    """
    h, w = ink.shape
    big = h + w + 10
    inkb = ink.astype(bool)

    def axis_dist(mask: np.ndarray) -> np.ndarray:
        n_rows = mask.shape[0]
        out = np.empty(mask.shape, dtype=np.int64)
        cur = np.full(mask.shape[1], big, dtype=np.int64)
        for r in range(n_rows):
            cur = np.where(mask[r], cur + 1, 0)
            out[r] = cur
        cur = np.full(mask.shape[1], big, dtype=np.int64)
        for r in range(n_rows - 1, -1, -1):
            cur = np.where(mask[r], cur + 1, 0)
            np.minimum(out[r], cur, out=out[r])
        return out

    g = axis_dist(inkb)                 # vertical distance
    gh = axis_dist(inkb.T).T            # horizontal distance
    g2 = g.astype(np.float64) ** 2
    d2 = g2.copy()
    bound = np.minimum(g, gh)
    ys, xs = np.nonzero(inkb)
    lim = bound[ys, xs]
    k = 1
    while len(ys):
        live = lim >= k
        if not live.any():
            break
        ys, xs, lim = ys[live], xs[live], lim[live]
        kk = float(k * k)
        left = xs - k
        ok = left >= 0
        if ok.any():
            cand = g2[ys[ok], left[ok]] + kk
            cur = d2[ys[ok], xs[ok]]
            d2[ys[ok], xs[ok]] = np.minimum(cur, cand)
        right = xs + k
        ok = right < w
        if ok.any():
            cand = g2[ys[ok], right[ok]] + kk
            cur = d2[ys[ok], xs[ok]]
            d2[ys[ok], xs[ok]] = np.minimum(cur, cand)
        k += 1
    d2[~inkb] = 0.0
    return d2


def verify_raster(raster: ReliefRaster, cfg: Optional[ReliefVerifyConfig] = None) -> ReliefVerification:
    cfg = cfg or ReliefVerifyConfig()
    r = ReliefVerification()
    r.px_per_mm = raster.px_per_mm
    r.stroke_w_2ap_mm = raster.metrics.stroke_w_mm
    r.resolution_driver = raster.resolution_driver
    r.resolution_capped = raster.resolution_capped
    if not raster.valid:
        r.decline_reason = raster.decline_reason or "invalid_raster"
        return r

    ink = raster.ink
    h, w = ink.shape
    labels, n = label_runs(ink.astype(bool), eight=True)
    if n == 0:
        r.decline_reason = "no_components"
        return r
    d2 = squared_edt(ink)

    # Ridge: a non-strict local maximum of the distance transform.
    pad = np.full((h + 2, w + 2), -1.0)
    pad[1:-1, 1:-1] = d2
    ridge = np.ones((h, w), dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            ridge &= ~(pad[1 + dy:h + 1 + dy, 1 + dx:w + 1 + dx] > d2)

    ys, xs = np.nonzero(labels)
    lab = labels[ys, xs]
    area = np.bincount(lab, minlength=n + 1)
    sx = np.bincount(lab, weights=xs, minlength=n + 1)
    sy = np.bincount(lab, weights=ys, minlength=n + 1)
    x0 = np.full(n + 1, np.iinfo(np.int64).max)
    y0 = np.full(n + 1, np.iinfo(np.int64).max)
    x1 = np.full(n + 1, -1)
    y1 = np.full(n + 1, -1)
    np.minimum.at(x0, lab, xs)
    np.minimum.at(y0, lab, ys)
    np.maximum.at(x1, lab, xs)
    np.maximum.at(y1, lab, ys)
    rmask = ridge[ys, xs]
    rlab = lab[rmask]
    rval = np.sqrt(d2[ys[rmask], xs[rmask]])
    order = np.argsort(rlab, kind="stable")
    rlab = rlab[order]
    rval = rval[order]
    rstart = np.searchsorted(rlab, np.arange(n + 2))

    # Holes: background regions that cannot reach the border, each owned by
    # the ink label a raster-order flood meets first (right, else left).
    holes = np.zeros(n + 1, dtype=np.int64)
    bg = ink == 0
    outside = outside_background(ink)
    enclosed = bg & ~outside
    if enclosed.any():
        hl, hn = label_runs(enclosed, eight=False)
        hy, hx = np.nonzero(hl)
        hlab = hl[hy, hx]
        # np.nonzero is raster order, so a label's first index is its first
        # pixel in raster order: where a raster-scan flood would start.
        uniq, first_idx = np.unique(hlab, return_index=True)
        for L, i in zip(uniq.tolist(), first_idx.tolist()):
            py, px = int(hy[i]), int(hx[i])
            owner = 0
            if px + 1 < w and ink[py, px + 1]:
                owner = int(labels[py, px + 1])
            elif px - 1 >= 0 and ink[py, px - 1]:
                owner = int(labels[py, px - 1])
            if owner:
                holes[owner] += 1

    total_ink = 0
    keep = []
    for L in range(1, n + 1):
        if area[L] < cfg.min_area_px:
            continue
        keep.append(L)
        total_ink += int(area[L])
    if not keep:
        r.decline_reason = "all_components_below_min_area"
        return r

    for L in keep:
        c = ReliefComponent()
        c.area_px = int(area[L])
        c.x0, c.y0, c.x1, c.y1 = int(x0[L]), int(y0[L]), int(x1[L]), int(y1[L])
        c.cx = float(sx[L]) / c.area_px
        c.cy = float(sy[L]) / c.area_px
        c.hole_count = int(holes[L])
        bw = c.x1 - c.x0 + 1
        bh = c.y1 - c.y0 + 1
        c.bbox_fill = c.area_px / (bw * bh)
        c.aspect = max(bw, bh) / max(1.0, min(bw, bh))
        ridge_vals = rval[rstart[L]:rstart[L + 1]].tolist()
        med = _median_of(ridge_vals)
        c.stroke_w_px = 2.0 * med
        c.stroke_cv = _cv_of(ridge_vals)
        diag = math.hypot(bw, bh)
        c.slenderness = diag / c.stroke_w_px if c.stroke_w_px > 1.0e-9 else 0.0
        r.components.append(c)

    comps = r.components
    if len(comps) <= 1024:
        for i, A in enumerate(comps):
            for j, B in enumerate(comps):
                if i == j:
                    continue
                if B.x0 >= A.x0 and B.x1 <= A.x1 and B.y0 >= A.y0 and B.y1 <= A.y1:
                    A.contained += 1

    glyph_ink = 0
    for c in comps:
        why = ""
        if c.stroke_w_px < cfg.min_stroke_px:
            why = "stroke_sub_pixel"
        elif c.stroke_cv > cfg.max_stroke_cv:
            why = "stroke_width_varies"
        elif c.slenderness < cfg.min_slenderness:
            why = "too_blobby"
        elif c.slenderness > cfg.max_slenderness:
            why = "hairline_or_groove"
        elif c.aspect > cfg.max_aspect:
            why = "aspect_out_of_band"
        elif c.bbox_fill > cfg.max_bbox_fill:
            why = "fills_its_box"
        elif c.contained > cfg.max_contained:
            why = "contains_too_many"
        elif c.hole_count > cfg.max_holes:
            why = "too_many_holes"
        if why:
            c.reject_reason = why
            continue
        c.glyph_like = True
        glyph_ink += c.area_px
    r.component_count = len(comps)
    r.glyph_like_count = sum(1 for c in comps if c.glyph_like)
    r.glyph_ink_fraction = glyph_ink / total_ink if total_ink else 0.0

    sw = [c.stroke_w_px for c in comps if c.glyph_like]
    cvs = [c.stroke_cv for c in comps if c.glyph_like]
    if not sw:
        sw = [c.stroke_w_px for c in comps]
        cvs = [c.stroke_cv for c in comps]
    r.stroke_w_mm = _median_of(sw) / max(1.0e-9, raster.px_per_mm)
    r.stroke_cv = _median_of(cvs)

    gl = [i for i, c in enumerate(comps) if c.glyph_like]
    chain_median_fill = 0.0
    chain_area_cv = 1.0
    if len(gl) >= 2:
        spans = [float(max(comps[i].x1 - comps[i].x0 + 1, comps[i].y1 - comps[i].y0 + 1))
                 for i in gl]
        med_span = max(1.0, _median_of(spans))
        parent = list(range(len(gl)))

        def root(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a in range(len(gl)):
            for b in range(a + 1, len(gl)):
                A = comps[gl[a]]
                B = comps[gl[b]]
                sr = max(A.stroke_w_px, B.stroke_w_px) / max(1.0e-9, min(A.stroke_w_px, B.stroke_w_px))
                if sr > cfg.pair_stroke_ratio:
                    continue
                ha = max(A.x1 - A.x0 + 1, A.y1 - A.y0 + 1)
                hb = max(B.x1 - B.x0 + 1, B.y1 - B.y0 + 1)
                hr = max(ha, hb) / max(1.0, min(ha, hb))
                if hr > cfg.pair_height_ratio:
                    continue
                if math.hypot(A.cx - B.cx, A.cy - B.cy) > cfg.pair_gap_heights * med_span:
                    continue
                parent[root(a)] = root(b)
        groups: list[list[int]] = [[] for _ in gl]
        for i in range(len(gl)):
            groups[root(i)].append(gl[i])

        # EVERY group reaching min_chain counts: text lines, plural.
        largest: list[int] = []
        chain_ink = 0
        qualifying = 0
        for g in groups:
            if len(g) < cfg.min_chain:
                continue
            qualifying += 1
            for i in g:
                chain_ink += comps[i].area_px
            if len(g) > len(largest):
                largest = g
        r.chained_count = sum(len(g) for g in groups if len(g) >= cfg.min_chain)
        r.chain_count = qualifying
        r.chain_ink_fraction = chain_ink / total_ink if total_ink else 0.0

        if largest:
            chain_median_fill = _median_of([comps[i].bbox_fill for i in largest])
            chain_area_cv = _cv_of([float(comps[i].area_px) for i in largest])

        if len(largest) >= 2:
            mx = my = 0.0
            for i in largest:
                mx += comps[i].cx
                my += comps[i].cy
            mx /= len(largest)
            my /= len(largest)
            sxx = syy = sxy = 0.0
            for i in largest:
                dx = comps[i].cx - mx
                dy = comps[i].cy - my
                sxx += dx * dx
                syy += dy * dy
                sxy += dx * dy
            theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
            ux, uy = math.cos(theta), math.sin(theta)
            nx, ny = -uy, ux
            res = 0.0
            for i in largest:
                e = (comps[i].cx - mx) * nx + (comps[i].cy - my) * ny
                res += e * e
            res = math.sqrt(res / len(largest))
            caps = []
            for i in largest:
                c = comps[i]
                prs = [bx * nx + by * ny for bx, by in
                       ((c.x0, c.y0), (c.x1, c.y0), (c.x0, c.y1), (c.x1, c.y1))]
                caps.append(max(prs) - min(prs))
            med_cap = max(1.0, _median_of(caps))
            # Cap-height consistency over the CAP BAND only: punctuation is a
            # fraction of a cap by design and would wreck a perfect string.
            band = [cp for cp in caps if cp >= 0.5 * med_cap]
            r.baseline_residual_ratio = res / med_cap
            r.cap_height_cv = _cv_of(band)
            r.cap_height_mm = med_cap / max(1.0e-9, raster.px_per_mm)
    elif comps:
        c = comps[0]
        chain_median_fill = c.bbox_fill
        r.cap_height_mm = min(c.x1 - c.x0 + 1, c.y1 - c.y0 + 1) / max(1.0e-9, raster.px_per_mm)
    r.chain_median_fill = chain_median_fill
    r.chain_area_cv = chain_area_cv

    # Module lattice: a Data Matrix / dot-peen hint, never proof.
    if len(comps) >= 16:
        areas = []
        all_square = True
        for c in comps:
            areas.append(float(c.area_px))
            if c.aspect > 1.45 or c.bbox_fill < 0.70:
                all_square = False
                break
        if all_square and _cv_of(areas) < 0.35:
            nn = []
            for i, a in enumerate(comps):
                best = 1.0e18
                for j, b in enumerate(comps):
                    if i == j:
                        continue
                    best = min(best, math.hypot(a.cx - b.cx, a.cy - b.cy))
                if best < 1.0e18:
                    nn.append(best)
            pitch = _median_of(nn)
            if pitch > 1.0 and _cv_of(nn) < 0.25:
                minx = min(c.cx for c in comps)
                maxx = max(c.cx for c in comps)
                miny = min(c.cy for c in comps)
                maxy = max(c.cy for c in comps)
                on_lattice = True
                for c in comps:
                    fx = (c.cx - minx) / pitch
                    fy = (c.cy - miny) / pitch
                    if abs(fx - _cround(fx)) > 0.22 or abs(fy - _cround(fy)) > 0.22:
                        on_lattice = False
                        break
                if on_lattice:
                    r.has_module_lattice = True
                    r.lattice_cols = int(_cround((maxx - minx) / pitch)) + 1
                    r.lattice_rows = int(_cround((maxy - miny) / pitch)) + 1
                    r.lattice_pitch_mm = pitch / max(1.0e-9, raster.px_per_mm)
                    r.evidence.append("+module_lattice")

    if cfg.decode is not None:
        r.decoded_text = cfg.decode(raster) or ""
        if r.decoded_text:
            r.evidence.append("+decoded")

    # The comparison uses the largest NON-glyph-like component: one fat
    # functional recess dwarfing a handful of strokes.
    unchained_max_ink = 0.0
    if total_ink > 0:
        for c in comps:
            if not c.glyph_like:
                unchained_max_ink = max(unchained_max_ink, c.area_px / total_ink)
    r.unchained_max_ink_fraction = unchained_max_ink

    text_line = (
        r.chained_count >= cfg.min_chain
        and r.chain_ink_fraction >= cfg.min_chain_ink
        and r.chain_ink_fraction > unchained_max_ink
        and r.baseline_residual_ratio <= cfg.max_baseline_residual
        and r.cap_height_cv <= cfg.max_cap_height_cv
    )
    dom = None
    for c in comps:
        if dom is None or c.area_px > dom.area_px:
            dom = c
    dom_cap = max(1.0, float(min(dom.x1 - dom.x0 + 1, dom.y1 - dom.y0 + 1))) if dom else 1.0
    dom_stroke_over_cap = dom.stroke_w_px / dom_cap if dom else 1.0
    logotype = (
        dom is not None
        and dom.glyph_like
        and r.component_count <= cfg.logotype_max_components
        and r.glyph_ink_fraction >= 0.60
        and dom.bbox_fill <= cfg.logotype_max_bbox_fill
        and dom.slenderness >= cfg.logotype_min_slender
        and dom_stroke_over_cap <= cfg.logotype_max_stroke_over_cap
    )

    note = r.evidence.append
    if (r.has_module_lattice and r.lattice_rows >= 8 and r.lattice_cols >= 8
            and r.lattice_rows % 2 == 0 and r.lattice_cols % 2 == 0):
        r.hypothesis = "module_lattice"
        r.verdict = ReliefVerdict.CONFIRMED
        r.confidence = 0.80 if not r.decoded_text else 0.97
        note("+ecc200_shaped_lattice")
    elif text_line:
        r.hypothesis = "text_line"
        r.verdict = ReliefVerdict.CONFIRMED
        conf = 0.80
        if r.chained_count >= 5:
            conf += 0.04
            note("+chain_len")
        if r.cap_height_cv <= 0.18:
            conf += 0.04
            note("+cap_height_uniform")
        if r.baseline_residual_ratio <= 0.12:
            conf += 0.04
            note("+baseline_tight")
        if r.stroke_cv <= 0.30:
            conf += 0.03
            note("+stroke_width_constant")
        if r.chain_count >= 2:
            conf += 0.02
            note("+multiple_lines")
        if chain_median_fill > CHAIN_FILL_PENALTY_ABOVE:
            conf -= 0.10
            note("-chain_fills_boxes")
        if chain_area_cv < HOMOGENEITY_PENALTY_BELOW:
            conf -= 0.10
            note("-shapes_too_uniform")
        if r.decoded_text:
            conf = 0.99
        r.confidence = min(max(conf, 0.50), 0.95)
        note("+chained_glyphs")
    elif logotype:
        r.hypothesis = "logotype"
        r.verdict = ReliefVerdict.CONFIRMED
        conf = 0.62
        if dom.stroke_cv <= 0.30:
            conf += 0.05
            note("+stroke_width_constant")
        if dom.bbox_fill <= 0.40:
            conf += 0.05
            note("+ink_sparse")
        if dom.hole_count >= 1:
            conf += 0.03
            note("+enclosed_counters")
        if r.decoded_text:
            conf = 0.99
        r.confidence = min(conf, 0.75)
        note("+single_component_letterform")
    elif r.glyph_ink_fraction < cfg.reject_below_glyph_ink:
        r.verdict = ReliefVerdict.REJECTED
        r.confidence = min(max(1.0 - r.glyph_ink_fraction, 0.60), 0.95)
        note("-blob_dominates_ink")
        if dom is not None and dom.reject_reason:
            note("-" + dom.reject_reason)
    else:
        r.verdict = ReliefVerdict.UNDECIDED
        r.confidence = min(max(r.glyph_ink_fraction, 0.0), 0.60)
        if r.chained_count < cfg.min_chain:
            note("-no_chain")
        if r.chain_ink_fraction < cfg.min_chain_ink:
            note("-chain_is_a_minority_of_ink")
        if r.chain_ink_fraction <= unchained_max_ink:
            note("-one_blob_outweighs_the_chain")
        if (dom is not None and not logotype
                and r.component_count <= cfg.logotype_max_components
                and dom_stroke_over_cap > cfg.logotype_max_stroke_over_cap):
            note("-stroke_too_fat_for_its_height")
        if r.baseline_residual_ratio > cfg.max_baseline_residual:
            note("-baseline_scattered")
        if r.cap_height_cv > cfg.max_cap_height_cv:
            note("-cap_heights_vary")
    return r


def _cround(x: float) -> float:
    """C's round(): half away from zero."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def verify_relief(cand: ReliefCandidate, face_index, cfg: Optional[ReliefVerifyConfig] = None) -> ReliefVerification:
    cfg = cfg or ReliefVerifyConfig()
    # One face is not a marking: a stroke needs a floor and a wall. A lone
    # annular wall rasterises to a ring indistinguishable from the letter O.
    if len(cand.members) < 2:
        return ReliefVerification(decline_reason="single_face_candidate")
    direct = rasterise_relief(cand, face_index, cfg.raster)
    best = verify_raster(direct, cfg)
    best.ink_polarity = "direct"
    if not direct.valid:
        return best
    # A background relief reads as one dense blob in direct ink and as the
    # letters themselves in the enclosed complement.
    dense = direct.ink_px / max(1.0, float(direct.w * direct.h)) > 0.35
    if best.verdict is ReliefVerdict.CONFIRMED or not dense:
        return best
    inv = invert_enclosed(direct)
    if not inv.valid:
        return best
    alt = verify_raster(inv, cfg)
    alt.ink_polarity = "inverted"
    if _RANK[alt.verdict] > _RANK[best.verdict] or (
        _RANK[alt.verdict] == _RANK[best.verdict]
        and alt.confidence > best.confidence
        and alt.verdict is ReliefVerdict.CONFIRMED
    ):
        return alt
    return best
