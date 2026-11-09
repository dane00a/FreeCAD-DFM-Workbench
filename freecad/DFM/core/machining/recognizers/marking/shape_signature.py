# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Stage 3: a scale-free shape signature, SCORED rather than gated.

Dimensionless, because the same mark on a 60 mm plate and on an 860 mm part
is one mark. Scored, because "is this text" has no decision boundary at the
level of geometry, and a cliff-edge threshold on an undecidable question is
what produces per-fixture tuning.

Two hypotheses survive side by side: TEXT-LIKE needs a baseline, consistent
cap heights and regular spacing; MARK-LIKE needs slenderness, sparse fill and
stroke count and deliberately no baseline, which is the structural fix for a
merged logotype (one component) dying at a three-glyph minimum.

Six hard rejects, each a physical or definitional impossibility: PMI on the
ink, a proud claim with no back-side evidence, too little B-rep mass, an ink
patch wider than any marking tool cuts, ink filling its own footprint, and --
with no baseline -- a shape that does not read as several strokes.
"""

from __future__ import annotations

import math
import os

from . import tuning as T
from .model import InkKind, MarkingCluster, Process, Rejection, add, cross, scale
from .relief_generator import EMPTY, ReliefScene, merge, trace_on


def _num(v: float) -> str:
    return f"{v:.4g}"


def _reject(out, gate, detail, margin, c: MarkingCluster) -> None:
    if out is None:
        return
    out.append(
        Rejection(
            "stage3", gate, detail, margin,
            c.host.primary_face_id if c.host else None, c.face_ids(),
        )
    )


def fit_baseline(xs, ys):
    """Principal direction of the region centroids and the RMS residual
    across it: SWT's chaining in closed form. None when degenerate.

    An ISOTROPIC point set has no baseline. Three bars at 120 degrees give an
    exactly isotropic covariance, the direction then comes from a tie-break
    rather than from the glyphs, and saying "baseline found" there switched on
    four scoring terms and skipped both no-baseline rejects, so a decorative
    rosette out-scored the gate meant to refuse it.
    """
    n = len(xs)
    if n < 2:
        return None
    mx = 0.0
    my = 0.0
    for i in range(n):
        mx += xs[i]
        my += ys[i]
    mx /= n
    my /= n
    sxx = syy = sxy = 0.0
    for i in range(n):
        a = xs[i] - mx
        b = ys[i] - my
        sxx += a * a
        syy += b * b
        sxy += a * b
    if sxx + syy < 1e-12:
        return None
    tr = sxx + syy
    det = sxx * syy - sxy * sxy
    disc = max(0.0, tr * tr * 0.25 - det)
    l1 = tr * 0.5 + math.sqrt(disc)
    l2 = tr * 0.5 - math.sqrt(disc)
    if trace_on():
        print(f"[mark] baseline fit n={n} l1={l1:.6g} l2={l2:.6g} "
              f"l2/l1={(l2 / l1 if l1 > 1e-12 else -1.0):.6g}")
    max_isotropy = 0.8
    if l1 > 1e-12 and l2 / l1 > max_isotropy:
        return None
    if abs(sxy) > 1e-12:
        du, dv = l1 - syy, sxy
    else:
        du = 1.0 if sxx >= syy else 0.0
        dv = 0.0 if sxx >= syy else 1.0
    length = math.sqrt(du * du + dv * dv)
    if length < 1e-12:
        return None
    return du / length, dv / length, math.sqrt(max(0.0, l2))


def support_width(ext_u, ext_v, du, dv) -> float:
    return abs(du) * ext_u + abs(dv) * ext_v


# -- The mark's outline ------------------------------------------------------------


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(points):
    """Andrew's monotone chain, counter-clockwise."""
    p = sorted(set(points))
    n = len(p)
    if n < 3:
        return p
    h = [None] * (2 * n)
    k = 0
    for i in range(n):
        while k >= 2 and _cross(h[k - 2], h[k - 1], p[i]) <= 0.0:
            k -= 1
        h[k] = p[i]
        k += 1
    t = k + 1
    for i in range(n - 1, 0, -1):
        while k >= t and _cross(h[k - 2], h[k - 1], p[i - 1]) <= 0.0:
            k -= 1
        h[k] = p[i - 1]
        k += 1
    return h[: k - 1]


def hull_area(h) -> float:
    if len(h) < 3:
        return 0.0
    a = 0.0
    n = len(h)
    for i in range(n):
        p = h[i]
        q = h[(i + 1) % n]
        a += p[0] * q[1] - q[0] * p[1]
    return abs(a) * 0.5


def inside_hull(h, q, tol) -> bool:
    if len(h) < 3:
        return False
    n = len(h)
    for i in range(n):
        a = h[i]
        b = h[(i + 1) % n]
        length = math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2)
        if length < 1e-12:
            continue
        if _cross(a, b, q) / length < -tol:
            return False
    return True


def cluster_outlines(scene: ReliefScene, clusters):
    hulls, areas = [], []
    for c in clusters:
        if c.host is None:
            hulls.append([])
            areas.append(0.0)
            continue
        pts = []
        for r in c.regions:
            for f in (r.ink if r.ink else r.faces):
                m = scene.measure(f, c.host)
                pts += [(m[0], m[2]), (m[0], m[3]), (m[1], m[2]), (m[1], m[3])]
        hull = convex_hull(pts)
        hulls.append(hull)
        areas.append(hull_area(hull))
    return hulls, areas


def region_corners(r):
    hu, hv = 0.5 * r.extent_u_mm, 0.5 * r.extent_v_mm
    return [
        (r.centroid_u - hu, r.centroid_v - hv),
        (r.centroid_u - hu, r.centroid_v + hv),
        (r.centroid_u + hu, r.centroid_v - hv),
        (r.centroid_u + hu, r.centroid_v + hv),
    ]


def co_level(a_depth, b_depth) -> bool:
    """One cutter, one Z: the tolerance the clustering itself linked on."""
    return abs(a_depth - b_depth) <= max(
        6.0 * T.NOISE_FLOOR_MM, 3.0 * T.DEPTH_CV_MAX * max(a_depth, b_depth)
    )


def dissolve_nested_readings(scene: ReliefScene, clusters: list) -> list:
    """A lone one-region reading inside a LARGER reading's outline is a piece
    of that mark, not a second one.

    Containment is decided against Stage 3's survivors, but which readings
    survive is not known until Stage 4. A four-glyph run whose overlapping
    letters fused into one 57-face component scored on its own, then held its
    region hostage from the containment pass and died in Stage 4 -- leaving
    those glyph faces owned by nothing. Narrow on purpose: one region only,
    strictly larger container, smallest container wins, same host, side and
    level.
    """
    if len(clusters) < 2:
        return clusters
    hulls, areas = cluster_outlines(scene, clusters)
    into = [-1] * len(clusters)
    for ci, x in enumerate(clusters):
        if x.host is None or len(x.regions) != 1:
            continue
        corners = region_corners(x.regions[0])
        best = -1
        for cj, y in enumerate(clusters):
            if cj == ci:
                continue
            if y.host is None or y.host is not x.host or y.proud != x.proud:
                continue
            if len(y.regions) < 2:
                continue
            if areas[cj] <= areas[ci]:
                continue
            if not co_level(x.depth_mm, y.depth_mm):
                continue
            if not all(inside_hull(hulls[cj], q, T.NOISE_FLOOR_MM) for q in corners):
                continue
            if best < 0 or areas[cj] < areas[best]:
                best = cj
        into[ci] = best

    drop = [False] * len(clusters)
    for ci in range(len(clusters)):
        cj = into[ci]
        if cj < 0 or into[cj] >= 0:
            continue
        y = clusters[cj]
        y.absorbed.extend(clusters[ci].regions)
        y.absorbed.extend(clusters[ci].absorbed)
        drop[ci] = True
        if trace_on():
            print(f"[mark] dissolved a 1-region reading of "
                  f"{len(clusters[ci].regions[0].faces)} face(s) into a "
                  f"{len(y.regions)}-region reading")
    return [c for ci, c in enumerate(clusters) if not drop[ci]]


def absorb_contained_regions(scene: ReliefScene, clusters: list) -> None:
    """Membership by CONTAINMENT: a mark's extent.

    The pool of material inside a counter-form graphic is cut in the same
    operation by the same cutter at the same depth, so pricing it as a second
    pocket double-counts the work. The predicate is co-planarity plus
    containment and nothing in it is a distance, so a DISTANT same-level
    recess still fails it and stays a functional pocket.
    """
    if not clusters:
        return
    spoken_for: set[int] = set()
    for c in clusters:
        spoken_for.update(r.index for r in c.regions)
        spoken_for.update(r.index for r in c.absorbed)
    hulls, areas = cluster_outlines(scene, clusters)

    for i, r in enumerate(scene.regions):
        if r.index in spoken_for:
            continue
        if r.process is Process.FORMED:
            continue
        rh = scene.hosts[scene.region_host[i]]
        corners = region_corners(r)
        best = -1
        for ci, c in enumerate(clusters):
            if c.host is not rh or c.proud != r.proud:
                continue
            lvl_tol = max(
                6.0 * T.NOISE_FLOOR_MM, 3.0 * T.DEPTH_CV_MAX * max(r.depth_mm, c.depth_mm)
            )
            if abs(r.depth_mm - c.depth_mm) > lvl_tol:
                continue
            if not all(inside_hull(hulls[ci], q, T.NOISE_FLOOR_MM) for q in corners):
                continue
            if best < 0 or areas[ci] < areas[best]:
                best = ci
        if best < 0:
            continue
        c = clusters[best]
        c.absorbed.append(r)
        spoken_for.add(r.index)
        # Now part of the mark's body, so no longer a character beside it.
        c.satellites = [s for s in c.satellites if s is not r]
        c.satellite_count = len(c.satellites)


def score_candidates(scene: ReliefScene, priors=None, rejects=None) -> list:
    out: list[MarkingCluster] = []
    trace = trace_on()
    note_backed = priors is not None and bool(priors.note_demands)

    by_host: dict[int, list[int]] = {}
    for i, r in enumerate(scene.regions):
        if r.process is Process.FORMED:
            continue  # forming is not marking, by construction
        by_host.setdefault(scene.region_host[i], []).append(i)

    for hi in sorted(by_host):
        ids = by_host[hi]
        host = scene.hosts[hi]
        for proud in (False, True):
            group = [i for i in ids if scene.regions[i].proud == proud]
            if not group:
                continue
            parent = list(range(len(group)))

            def root(x: int) -> int:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            # Pairs near enough and co-level that only the SWT ratio test
            # keeps apart: that is what punctuation is.
            ratio_blocked: list[list[int]] = [[] for _ in group]
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a = scene.regions[group[i]]
                    b = scene.regions[group[j]]
                    reach = T.LINK_SPAN_FACTOR * max(
                        max(a.extent_u_mm, a.extent_v_mm),
                        max(b.extent_u_mm, b.extent_v_mm),
                    )
                    du = a.centroid_u - b.centroid_u
                    dv = a.centroid_v - b.centroid_v
                    if math.sqrt(du * du + dv * dv) > reach:
                        continue
                    lvl_tol = max(
                        6.0 * T.NOISE_FLOOR_MM,
                        3.0 * T.DEPTH_CV_MAX * max(a.depth_mm, b.depth_mm),
                    )
                    if abs(a.depth_mm - b.depth_mm) > lvl_tol:
                        continue
                    # SWT's pairing constraint: a solid recess beside a
                    # logotype once linked in and dragged its component fill
                    # to 1.0, losing the real mark.
                    w_lo = min(a.stroke_w_mm, b.stroke_w_mm)
                    w_hi = max(a.stroke_w_mm, b.stroke_w_mm)
                    e_a = max(a.extent_u_mm, a.extent_v_mm)
                    e_b = max(b.extent_u_mm, b.extent_v_mm)
                    e_lo, e_hi = min(e_a, e_b), max(e_a, e_b)
                    w_ok = w_lo > 1e-9 and w_hi / w_lo <= T.PAIR_RATIO_MAX
                    e_ok = e_lo > 1e-9 and e_hi / e_lo <= T.PAIR_RATIO_MAX
                    if not w_ok or not e_ok:
                        ratio_blocked[i].append(j)
                        ratio_blocked[j].append(i)
                        continue
                    parent[root(i)] = root(j)
            buckets: dict[int, list[int]] = {}
            bucket_size: dict[int, int] = {}
            for i in range(len(group)):
                rt = root(i)
                buckets.setdefault(rt, []).append(group[i])
                bucket_size[rt] = bucket_size.get(rt, 0) + 1

            for b_root in sorted(buckets):
                c = _score_bucket(
                    scene, host, proud, buckets[b_root], b_root, group, root,
                    ratio_blocked, bucket_size, note_backed, rejects, trace,
                )
                if c is not None:
                    out.append(c)

    out = dissolve_nested_readings(scene, out)
    absorb_contained_regions(scene, out)
    for c in out:
        if c.absorbed:
            if trace:
                print(f"[mark] absorbed {len(c.absorbed)} region(s) into a "
                      f"{len(c.regions)}-region cluster on host={c.host.primary_face_id}")
            c.evidence.append(f"absorbed_contained_regions={len(c.absorbed)}")

    # Satellite OWNERSHIP: counted is not claimed. A full stop counted and not
    # owned fell through to the blend recognizer as a fillet network. A region
    # two survivors both list goes to the one with the SMALLEST cap height --
    # the line whose characters it is the size of -- ties to the earlier.
    body: set[int] = set()
    for c in out:
        body.update(r.index for r in c.regions)
        body.update(r.index for r in c.absorbed)
    owner: dict[int, int] = {}
    by_index = {}
    for ci, c in enumerate(out):
        for r in c.satellites:
            if r.index in body:
                continue
            by_index[r.index] = r
            prev = owner.get(r.index)
            if prev is None or c.cap_height_mm < out[prev].cap_height_mm:
                owner[r.index] = ci
    for idx in sorted(owner):
        out[owner[idx]].claimed_satellites.append(by_index[idx])
    for c in out:
        if not c.claimed_satellites:
            continue
        c.claimed_satellites.sort(key=lambda r: r.index)
        c.evidence.append(f"claimed_satellites={len(c.claimed_satellites)}")
    return out


def _score_bucket(scene, host, proud, member_ids, b_root, group, root, ratio_blocked,
                  bucket_size, note_backed, rejects, trace):
    c = MarkingCluster(host=host, proud=proud, note_backed=note_backed)
    c.regions = [scene.regions[i] for i in member_ids]

    # The strongest verdict any member carries.
    c.process = Process.RAISED if proud else Process.ENGRAVED
    if not any(r.process in (Process.ENGRAVED, Process.RAISED) for r in c.regions):
        c.process = Process.INDETERMINATE

    acc = list(EMPTY)
    depths, strokes, slenders, region_fills = [], [], [], []
    max_patch_w = 0.0
    ink_area = 0.0
    ink_perim = 0.0
    holes = 0
    pmi_attached = False
    distinct_style = False
    for r in c.regions:
        for f in (r.ink if r.ink else r.faces):
            merge(acc, scene.measure(f, host))
        depths.append(r.depth_mm)
        strokes.append(r.stroke_w_mm)
        slenders.append(r.slenderness)
        region_fills.append(r.fill)
        max_patch_w = max(max_patch_w, r.max_patch_w_mm)
        ink_area += r.ink_area_mm2
        ink_perim += r.ink_perimeter_mm
        holes += r.hole_count
        pmi_attached = pmi_attached or r.ink_carries_pmi
        distinct_style = distinct_style or r.distinct_colour
    ext_u = max(0.0, acc[1] - acc[0])
    ext_v = max(0.0, acc[3] - acc[2])
    bbox_area = max(ext_u, 1e-6) * max(ext_v, 1e-6)
    c.diag_mm = math.sqrt(ext_u * ext_u + ext_v * ext_v)
    c.fill = T.clamp01(ink_area / bbox_area)
    c.depth_mm = T.median(depths)
    c.stroke_w_mm = T.median(strokes)
    c.slenderness_med = T.median(slenders)
    c.depth_cv = T.cv_of(depths)
    c.footprint_host_ratio = bbox_area / host.area if host.area > 0.0 else 1.0
    complexity = ink_perim / max(2.0 * c.diag_mm, 1e-6)

    xs = [r.centroid_u for r in c.regions]
    ys = [r.centroid_v for r in c.regions]
    du, dv, residual = 1.0, 0.0, 0.0
    have_baseline = False
    if len(c.regions) >= 3:
        fit = fit_baseline(xs, ys)
        if fit is not None:
            du, dv, residual = fit
            have_baseline = True
    caps = [support_width(r.extent_u_mm, r.extent_v_mm, -dv, du) for r in c.regions]
    c.cap_height_mm = T.median(caps)
    if c.cap_height_mm <= 1e-6:
        c.cap_height_mm = min(ext_u, ext_v)
    c.cap_height_cv = T.cv_of(caps)
    c.baseline_residual_ratio = (
        residual / c.cap_height_mm if have_baseline and c.cap_height_mm > 1e-6 else 1e18
    )

    # -- Punctuation, counted not clustered ---------------------------------
    # A hyphen is ~0.09 cap tall, so the pairing constraint excludes it by
    # construction: right as clustering, wrong as a character count. A
    # satellite is a LONE region, near enough, co-level, kept out only by the
    # ratio test, and smaller than the cap height. It needs a RUN to be
    # punctuation in -- on a one-region logotype the "cap height" is the whole
    # mark's width, and an unrelated recess beside it read as punctuation and
    # got claimed. Nearness answers to either ruler: 0.6 cap heights, or three
    # of the run's own measured gaps.
    have_run = have_baseline and len(c.regions) >= 2
    env = os.environ.get("DFM_SAT_GAP_CAP")
    max_gap_cap = float(env) if env else 0.60
    max_gap_runs = 3.0
    run_gap = 0.0
    nn = []
    for a_i, ra in enumerate(c.regions):
        best = 1e18
        for b_i, rb in enumerate(c.regions):
            if a_i == b_i:
                continue
            gu = abs(ra.centroid_u - rb.centroid_u) - 0.5 * (ra.extent_u_mm + rb.extent_u_mm)
            gv = abs(ra.centroid_v - rb.centroid_v) - 0.5 * (ra.extent_v_mm + rb.extent_v_mm)
            best = min(best, max(0.0, max(gu, gv)))
        if best < 1e17:
            nn.append(best)
    if nn:
        run_gap = T.median(nn)
    c.char_gap_mm = run_gap
    sat: set[int] = set()
    if have_run:
        for li in range(len(group)):
            if root(li) != b_root:
                continue
            for lj in ratio_blocked[li]:
                if root(lj) == b_root:
                    continue
                if bucket_size.get(root(lj)) != 1:
                    continue
                s = scene.regions[group[lj]]
                if max(s.extent_u_mm, s.extent_v_mm) >= c.cap_height_mm:
                    continue
                gap = 1e18
                for lk in range(len(group)):
                    if root(lk) != b_root:
                        continue
                    g = scene.regions[group[lk]]
                    du_ = abs(s.centroid_u - g.centroid_u) - 0.5 * (s.extent_u_mm + g.extent_u_mm)
                    dv_ = abs(s.centroid_v - g.centroid_v) - 0.5 * (s.extent_v_mm + g.extent_v_mm)
                    gap = min(gap, max(0.0, max(du_, dv_)))
                near_by_cap = c.cap_height_mm > 1e-9 and gap <= max_gap_cap * c.cap_height_mm
                near_by_run = run_gap > 1e-9 and gap <= max_gap_runs * run_gap
                if not near_by_cap and not near_by_run:
                    continue
                sat.add(lj)
    for lj in sorted(sat):
        c.satellites.append(scene.regions[group[lj]])
    c.satellite_count = len(sat)

    if have_baseline:
        along = sorted(r.centroid_u * du + r.centroid_v * dv for r in c.regions)
        gaps = [along[i] - along[i - 1] for i in range(1, len(along))]
        c.spacing_regularity = T.clamp01(1.0 - T.cv_of(gaps))
        # The baseline is a world direction, which on a cylinder is a LOCAL
        # statement: the frame is rebuilt at the cluster's own station.
        eu, ev = host.u, host.v
        if host.curved():
            cu_sum = 0.0
            for r in c.regions:
                cu_sum += r.centroid_u
            theta = host.curv_sign * cu_sum / (len(c.regions) * max(host.radius, 1e-9))
            rad = add(scale(host.ref_dir, math.cos(theta)), scale(host.ref_perp, math.sin(theta)))
            tan = cross(host.axis_dir, rad)
            eu = scale(tan, host.curv_sign)
            ev = host.axis_dir
        w = add(scale(eu, du), scale(ev, dv))
        c.baseline_dir = (w[0], w[1], w[2])

    # HARD REJECT 1: semantic PMI on the ink.
    if pmi_attached:
        _reject(rejects, "pmi_attached",
                "a semantic dimension/datum/tolerance references this candidate's ink faces",
                1.0, c)
        return None
    # HARD REJECT 2: a PROUD claim needs the back side; from the front it is
    # indistinguishable from sculpture, a boss field or a rib top. Recessed
    # indeterminate candidates are deliberately not gated.
    if c.proud and c.process is Process.INDETERMINATE:
        _reject(rejects, "raised_without_back_side_evidence",
                "a proud claim needs a measured back side to be told apart from "
                "sculpture, a boss or a rib top", 1.0, c)
        return None
    # HARD REJECT 3: too little B-rep mass to be several strokes.
    member_faces = sum(len(r.faces) for r in c.regions)
    if member_faces < T.MIN_MEMBER_FACES:
        _reject(rejects, "too_few_member_faces",
                f"{member_faces} faces < {T.MIN_MEMBER_FACES}",
                float(T.MIN_MEMBER_FACES - member_faces), c)
        return None
    # HARD REJECT 4: no marking tool cuts a stroke this wide -- on the widest
    # single patch, relative to cap height where there is one.
    if trace:
        print(f"[mark] cluster host={host.primary_face_id} regions={len(c.regions)} "
              f"sat={c.satellite_count} proud={int(c.proud)} baseline={int(have_baseline)} "
              f"cap={c.cap_height_mm:.4g} patch_w={max_patch_w:.4g} stroke_cap="
              f"{(max_patch_w / c.cap_height_mm if c.cap_height_mm > 1e-6 else -1.0):.4g}")
    if have_baseline and c.cap_height_mm > 1e-6:
        stroke_cap = max_patch_w / c.cap_height_mm
        if stroke_cap > T.STROKE_CAP_MAX:
            _reject(rejects, "wider_than_marking_process",
                    f"widest ink patch {_num(max_patch_w)} mm is {_num(stroke_cap)} of the "
                    f"{_num(c.cap_height_mm)} mm cap height, over the "
                    f"{_num(T.STROKE_CAP_MAX)} ceiling for a letterform",
                    stroke_cap - T.STROKE_CAP_MAX, c)
            return None
    elif max_patch_w > T.MAX_STROKE_WIDTH_MM:
        _reject(rejects, "wider_than_marking_process",
                f"widest ink patch {_num(max_patch_w)} mm exceeds the marking-process "
                f"ceiling {_num(T.MAX_STROKE_WIDTH_MM)} mm with no cap height to measure "
                f"it against (union mean was {_num(c.stroke_w_mm)})",
                max_patch_w - T.MAX_STROKE_WIDTH_MM, c)
        return None
    # HARD REJECT 5: the ink fills its own footprint.
    if c.fill > T.CLUSTER_FILL_MAX:
        _reject(rejects, "ink_fills_footprint",
                f"cluster fill {_num(c.fill)} > {_num(T.CLUSTER_FILL_MAX)}",
                c.fill - T.CLUSTER_FILL_MAX, c)
        return None
    if c.slenderness_med < T.POCKET_SLENDER_MAX and c.fill > T.POCKET_FILL_MIN:
        _reject(rejects, "pocket_like",
                f"slenderness {_num(c.slenderness_med)} with fill {_num(c.fill)}",
                T.POCKET_SLENDER_MAX - c.slenderness_med, c)
        return None
    # HARD REJECT 6: with no baseline, the shape carries it all. Three or more
    # separate ink patches is the repetition evidence the complexity floor
    # stands in for (a relieved plaque's letters are patches of ONE region).
    region_fill_med = T.median(region_fills)
    complexity_floor = (
        T.SINGLE_REGION_COMPLEXITY_MIN if len(c.regions) == 1 else T.NO_BASELINE_COMPLEXITY_MIN
    )
    ink_patches = sum(len(r.ink) for r in c.regions)
    repetition_evidence = ink_patches >= 3
    if not have_baseline and not repetition_evidence and complexity < complexity_floor:
        _reject(rejects, "no_baseline_low_complexity",
                f"boundary complexity {_num(complexity)} < {_num(complexity_floor)} and "
                "fewer than three regions (a mark with no baseline must be made of several "
                "strokes)", complexity_floor - complexity, c)
        return None
    if not have_baseline and region_fill_med > T.FILL_HI:
        _reject(rejects, "no_baseline_solid_components",
                f"median region fill {_num(region_fill_med)} > {_num(T.FILL_HI)}",
                region_fill_med - T.FILL_HI, c)
        return None
    # HARD REJECT 8: a REFUSED baseline is a verdict, not a gap.
    # ``have_baseline`` folds two things into one False: the fit was never
    # attempted (fewer than three regions), or it was attempted and failed.
    # Only the first is an absence of evidence; the second says these regions
    # were measured against a line and do not lie on one. Lettering is
    # collinear or it is one blob. The reference's corpus census (29 marks on
    # 19 fixtures) found twenty of the 22 marks with three or more regions
    # carrying a baseline, every curved and rotary case included; the other
    # two were text_neg_decor_groove and its seam variant, the rosette this
    # gate exists for, which reaches the no-baseline path only because
    # claiming its inner ring as a GROOVE splits one relief region into three.
    # The cost, deliberate: text set all the way round a closed circle is
    # isotropic and would be refused.
    if len(c.regions) >= 3 and not have_baseline:
        _reject(rejects, "baseline_refused",
                f"{_num(float(len(c.regions)))} regions were fitted to a baseline and do "
                "not lie on one (lettering is collinear, or it is a single merged "
                "region; a ring of patches is neither)", 1.0, c)
        return None

    s_slender =T.band(c.slenderness_med, T.SLENDER_LO, T.SLENDER_LO_CORE,
                       T.SLENDER_HI_CORE, T.SLENDER_HI)
    s_fill = T.band(c.fill, T.FILL_LO, T.FILL_LO_CORE, T.FILL_HI_CORE, T.FILL_HI)
    all_wall_gap = all(r.ink_kind is InkKind.WALL_GAP for r in c.regions)
    s_region_fill = 0.5 if all_wall_gap else T.band(
        region_fill_med, T.REGION_FILL_LO, T.REGION_FILL_LO_CORE,
        T.REGION_FILL_HI_CORE, T.REGION_FILL_HI)
    s_complex = T.band(complexity, T.COMPLEXITY_LO, T.COMPLEXITY_LO_CORE,
                       T.COMPLEXITY_HI_CORE, T.COMPLEXITY_HI)
    s_ds = T.band(c.depth_mm / max(c.stroke_w_mm, 1e-6), T.DEPTH_STROKE_LO,
                  T.DEPTH_STROKE_LO_CORE, T.DEPTH_STROKE_HI_CORE, T.DEPTH_STROKE_HI)
    # Neutral, not zero, when the thickness could not be measured.
    s_dt = (
        T.band_low(c.depth_mm / max(host.local_thickness_mm, 1e-6), 0.25,
                   T.DEPTH_THICKNESS_MAX)
        if host.thickness_resolved else 0.5
    )
    s_level = T.clamp01(1.0 - c.depth_cv / T.DEPTH_CV_MAX)
    holes_per_region = holes / len(c.regions)
    # Latin glyphs enclose 0, 1 or 2 counters.
    s_topo = T.band(holes_per_region, -1.0, 0.0, 2.0, 4.0)
    s_foot = max(
        T.band_low(c.footprint_host_ratio, T.FOOTPRINT_HOST_HI_CORE, T.FOOTPRINT_HOST_HI),
        T.band_low(c.diag_mm / scene.part_diag_mm, T.PART_DIAG_HI_CORE, T.PART_DIAG_HI)
        if scene.part_diag_mm > 1e-6 else 0.0,
    )
    s_base = (
        T.band_low(c.baseline_residual_ratio, T.BASELINE_RESIDUAL_HI_CORE,
                   T.BASELINE_RESIDUAL_HI)
        if have_baseline else 0.0
    )
    s_cap = (
        T.band_low(c.cap_height_cv, T.CAP_HEIGHT_CV_HI_CORE, T.CAP_HEIGHT_CV_HI)
        if len(c.regions) >= 3 else 0.0
    )
    s_space = c.spacing_regularity if have_baseline else 0.0
    s_count = T.clamp01((len(c.regions) - 1.0) / 4.0)

    def score_with(w: T.Weights) -> float:
        return (w.slenderness * s_slender + w.fill * s_fill
                + w.region_fill * s_region_fill
                + w.complexity * s_complex + w.depth_stroke * s_ds
                + w.depth_thickness * s_dt + w.level * s_level
                + w.topology * s_topo + w.footprint * s_foot
                + w.baseline * s_base + w.cap_height * s_cap
                + w.spacing * s_space + w.member_count * s_count)

    c.mark_score = score_with(T.MARK_WEIGHTS)
    c.text_score = score_with(T.TEXT_WEIGHTS)
    c.score = max(c.mark_score, c.text_score)
    if distinct_style:
        c.score = T.clamp01(c.score + T.COLOUR_BONUS)
        c.evidence.append("distinct_face_style")
    if note_backed:
        c.score = T.clamp01(c.score + T.NOTE_BONUS)
        c.evidence.append("step_note_demands_marking")

    c.evidence.append(f"slenderness={_num(c.slenderness_med)}")
    c.evidence.append(f"fill={_num(c.fill)}")
    c.evidence.append(f"complexity={_num(complexity)}")
    c.evidence.append(f"region_fill_med={_num(region_fill_med)}")
    c.evidence.append(f"max_patch_w={_num(max_patch_w)}")
    c.evidence.append(f"depth_over_stroke={_num(c.depth_mm / max(c.stroke_w_mm, 1e-6))}")
    if host.thickness_resolved:
        c.evidence.append(f"depth_over_thickness={_num(c.depth_mm / host.local_thickness_mm)}")
    c.evidence.append(f"depth_cv={_num(c.depth_cv)}")
    if have_baseline:
        c.evidence.append(f"baseline_residual_ratio={_num(c.baseline_residual_ratio)}")
        c.evidence.append(f"cap_height_cv={_num(c.cap_height_cv)}")
        c.evidence.append(f"spacing_regularity={_num(c.spacing_regularity)}")
    c.evidence.append("hypothesis=text" if c.text_score >= c.mark_score else "hypothesis=mark")
    for r in c.regions:
        for e in r.process_evidence:
            c.evidence.append(f"process:{e}")

    if c.score < T.TIER_B_SCORE:
        _reject(rejects, "score_below_tier_b",
                f"score {_num(c.score)} < {_num(T.TIER_B_SCORE)} (text "
                f"{_num(c.text_score)}, mark {_num(c.mark_score)})",
                T.TIER_B_SCORE - c.score, c)
        return None
    return c
