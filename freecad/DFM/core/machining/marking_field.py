# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""A mark's own cut is not also a pocket, a boss, an undercut or a fillet.

The resolver's containment dedup removes the per-stroke reads of an engraving
only while the marking's face set is a COMPLETE cover of the relief, and it
is not always: a glyph with closed counters can be refused by the clustering
and come back as a pocket with its counter as a boss, sharing no face at all
with the mark; a curved-host mark can leave two sliver walls unmeasured so a
pocket shares 7 of its 9 faces, just under the 0.8 gate.

Face identity is the wrong test for "is this the mark's own cut". The mark
knows where its cut IS -- a footprint on a host and a depth band under it --
and a feature lying inside that envelope is the cut seen a second time. The
predicate is Stage 3's region containment lifted to features:

* the mark's host frame (its host face's normal is the depth axis);
* in depth, CONTAINMENT in the mark's band, widened to the host surface so an
  island top at host level counts, to a tenth of the depth;
* in plan, NEAR (edge gap within one and a half of the run's own character
  gaps) and NO BIGGER (span within the run's span plus that allowance) --
  containment would charge a refused glyph its own width;
* the candidate must reach INTO the relief on the mark's side and must not be
  the host face itself, and must have less surface area than the whole mark.

Bores (a drilled second operation), the groove and thread families (a
signature lettering cannot produce) and patterns are exempt.
"""

from __future__ import annotations

import os

from .aag import AttributedAdjacencyGraph, SurfaceType
from .features import FeatureType

_EXEMPT = (
    FeatureType.THROUGH_HOLE, FeatureType.BLIND_HOLE, FeatureType.BREAKTHROUGH_HOLE,
    FeatureType.COUNTERBORE, FeatureType.COUNTERSINK, FeatureType.PARTIAL_BORE,
    FeatureType.GROOVE, FeatureType.O_RING_GLAND, FeatureType.RETAINING_RING_GROOVE,
    FeatureType.THREAD_RELIEF_GROOVE, FeatureType.EXTERNAL_THREAD,
    FeatureType.THREADED_HOLE, FeatureType.MARKING_TEXT, FeatureType.PATTERN,
)

_ANALYTIC = (SurfaceType.PLANE, SurfaceType.CYLINDER, SurfaceType.CONE,
             SurfaceType.SPHERE, SurfaceType.TORUS)

#: Slack on one coordinate, so an exactly coincident coordinate is not lost to
#: floating-point noise.
_PAD_MM = 0.05
#: One and a half character gaps: the refused glyph is one spacing out plus
#: its own half-width; at two it reaches the glyph after next.
_LATERAL_GAPS_ALLOWED = 1.5


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _unit(a):
    n = (a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) ** 0.5
    return (a[0] / n, a[1] / n, a[2] / n)


def _host_frame(graph: AttributedAdjacencyGraph, host_id):
    """(u, v, w, planar). A non-planar host falls back to world axes, which
    makes the test a plain world-box containment: stricter, never looser."""
    frame = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), False)
    if host_id is None or not graph.has_node(host_id):
        return frame
    h = graph.node(host_id)
    if h.surface_type is not SurfaceType.PLANE or h.plane_normal is None:
        return frame
    n = (h.plane_normal.X(), h.plane_normal.Y(), h.plane_normal.Z())
    ax, ay, az = abs(n[0]), abs(n[1]), abs(n[2])
    e = (1.0, 0.0, 0.0)
    if ay <= ax and ay <= az:
        e = (0.0, 1.0, 0.0)
    elif az <= ax and az <= ay:
        e = (0.0, 0.0, 1.0)
    u = _unit(_cross(n, e))
    v = _unit(_cross(n, u))
    return (u, v, n, True)


def _widen_point(p, frame, lo, hi) -> None:
    for k in range(3):
        d = frame[k]
        t = p[0] * d[0] + p[1] * d[1] + p[2] * d[2]
        if t < lo[k]:
            lo[k] = t
        if t > hi[k]:
            hi[k] = t


def _widen_face(node, frame, lo, hi) -> None:
    """Box corners for an analytic face; the area centroid for a freeform
    one, whose OCCT box bounds the control net rather than the face."""
    if node.surface_type not in _ANALYTIC or node.bbox.IsVoid():
        c = node.centroid
        _widen_point((c.X(), c.Y(), c.Z()), frame, lo, hi)
        return
    xn, yn, zn, xx, yx, zx = node.bbox.Get()
    for cx in (xn, xx):
        for cy in (yn, yx):
            for cz in (zn, zx):
                _widen_point((cx, cy, cz), frame, lo, hi)


def _w_of(node, frame) -> float:
    c = node.centroid
    w = frame[2]
    return c.X() * w[0] + c.Y() * w[1] + c.Z() * w[2]


def absorb_marking_field(features, graph: AttributedAdjacencyGraph, dropped) -> None:
    """Mark as dropped every feature lying inside a mark's relief envelope."""
    trace = os.environ.get("DFM_MARK_TRACE") is not None
    for m, mark in enumerate(features):
        if mark.type != FeatureType.MARKING_TEXT:
            continue
        if len(mark.faces) < 4:
            continue
        host_id = mark.parameters.get("host_face")
        frame = _host_frame(graph, host_id)
        planar = frame[3]
        mlo = [1e18, 1e18, 1e18]
        mhi = [-1e18, -1e18, -1e18]
        mark_area = 0.0
        mark_side = 0.0
        mark_faces = 0
        for fid in mark.faces:
            if graph.has_node(fid):
                node = graph.node(fid)
                _widen_face(node, frame, mlo, mhi)
                mark_area += node.area
                mark_side += _w_of(node, frame)
                mark_faces += 1
        if mlo[0] > 1e17 or mark_faces == 0:
            continue
        mark_side /= mark_faces
        # The band is anchored on the HOST surface: the claimed faces'
        # centroids sit halfway up the walls and exclude both ends of the cut.
        depth = abs(float(mark.parameters.get("depth_mm", 0.0) or 0.0))
        host_w = 0.0
        if planar and graph.has_node(host_id):
            host_w = _w_of(graph.node(host_id), frame)
            mlo[2] = min(mlo[2], min(host_w, host_w - depth))
            mhi[2] = max(mhi[2], max(host_w, host_w + depth))
        # Laterally, the run's own tracking -- zero on a one-region logotype,
        # which publishes none, so it gets no lateral slack at all.
        char_gap = float(mark.parameters.get("char_gap_mm", 0.0) or 0.0)
        lateral = _PAD_MM + _LATERAL_GAPS_ALLOWED * max(0.0, char_gap)
        tol = (lateral, lateral, max(_PAD_MM, 0.1 * depth) if planar else _PAD_MM)
        # A candidate has to ENTER the cut: widening the band to the host
        # also admits the host's own neighbours, and a real draft annotation
        # on the face carrying a logo was deleted that way.
        cut_sign = -1.0 if mark_side < host_w else 1.0
        cut_eps = max(1e-3, 0.25 * depth)

        for k, cand in enumerate(features):
            if k == m or dropped[k] or not cand.faces:
                continue
            if cand.type in _EXEMPT:
                continue
            lo = [1e18, 1e18, 1e18]
            hi = [-1e18, -1e18, -1e18]
            cand_area = 0.0
            has_host = False
            enters_cut = not planar
            for fid in cand.faces:
                if fid == host_id:
                    has_host = True
                    break
                if not graph.has_node(fid):
                    continue
                node = graph.node(fid)
                _widen_face(node, frame, lo, hi)
                cand_area += node.area
                if (_w_of(node, frame) - host_w) * cut_sign > cut_eps:
                    enters_cut = True
            if has_host or not enters_cut or lo[0] > 1e17:
                continue
            # The mark's own cut is not bigger than the mark.
            if cand_area > mark_area:
                continue
            failed = False
            under = (mlo[2] - tol[2]) - lo[2]
            over = hi[2] - (mhi[2] + tol[2])
            if max(under, over) > 0.0:
                failed = True
            for q in range(2):
                if failed:
                    break
                gap = max(mlo[q] - hi[q], lo[q] - mhi[q])
                if gap > tol[q]:
                    failed = True
                    break
                span_over = (hi[q] - lo[q]) - ((mhi[q] - mlo[q]) + tol[q])
                if span_over > 0.0:
                    failed = True
            if failed:
                continue
            dropped[k] = True
            if trace:
                print(f"[mark] field-absorbed {cand.type} {cand.instance_id} "
                      f"({len(cand.faces)} faces) into {mark.instance_id}")
