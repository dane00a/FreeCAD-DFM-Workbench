# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Rasterising a surface relief onto its host, for the marking verifier.

Turns a candidate relief (the faces a cheap geometric stage produced for
recall) into a clean bitonal image, so a verifier can restore precision on it.

This is not projecting a 3D scene. The ink wanted is the silhouette of the
relief seen along the host's own normal -- what a V-bit, a laser or a die
leaves -- and for a host-parallel floor mapping its boundary into the host
frame is a rigid transform, so the polygon is exact and the only error is the
discretisation of curved edges, held under half a pixel. A perpendicular wall
projects to measure zero. A cylindrical host is unrolled, which is an
isometry; its seam goes in the widest gap between the candidate's own
boundary points, and ink wrapping the whole circumference is declined rather
than split. Cones and freeform hosts are declined.

Resolution is derived, never fixed: see :func:`pick_px_per_mm`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_Plane
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_REVERSED, TopAbs_VERTEX, TopAbs_WIRE
from OCP.TopExp import TopExp_Explorer
from freecad.DFM.core.utils.occt import TopoDS

from ..aag import AagNode, SurfaceType
from .marking.model import add, cross, dot, neg, norm, pnt, scale, sub, unit
from ...utils.geometry import face_area

#: Same 15 degrees the marking stages use, so "floor" means one thing.
PARALLEL_DOT = 0.966


@dataclass
class ReliefCandidate:
    host: Optional[AagNode] = None
    members: list = field(default_factory=list)
    depth_mm: float = 0.0
    cap_height_mm: float = 0.0
    raised: bool = False
    has_frame: bool = False
    frame: Optional[tuple] = None  # (origin, normal, x_dir)


@dataclass
class ReliefFootprintMetrics:
    host_planar: bool = False
    host_cylindrical: bool = False
    floor_area_mm2: float = 0.0
    floor_perimeter_mm: float = 0.0
    stroke_w_mm: float = 0.0
    footprint_u_mm: float = 0.0
    footprint_v_mm: float = 0.0
    footprint_diag_mm: float = 0.0
    cap_height_est_mm: float = 0.0


@dataclass
class ReliefRasterConfig:
    # Cap height of about 32 px sits mid-band of what a reader wants.
    target_cap_px: float = 32.0
    # A hairline can be sub-pixel on a page sized for the cap height, and cap
    # and stroke are independent, so the resolution binds on the tighter.
    min_stroke_px: float = 3.5
    min_px_per_mm: float = 4.0
    max_px_per_mm: float = 600.0
    max_pixels: int = 6000000
    supersample: int = 4
    pad_px: int = 6
    boundary_defl_px: float = 0.4


@dataclass
class ReliefRaster:
    w: int = 0
    h: int = 0
    ink: Optional[np.ndarray] = None  # uint8 (h, w), row-major, +y down
    px_per_mm: float = 0.0
    origin_u_mm: float = 0.0
    origin_v_mm: float = 0.0
    ink_px: int = 0
    valid: bool = False
    decline_reason: str = ""
    resolution_driver: str = ""
    resolution_capped: bool = False
    metrics: ReliefFootprintMetrics = field(default_factory=ReliefFootprintMetrics)


class SurfaceMap:
    """World points to page millimetres through the host surface."""

    def __init__(self) -> None:
        self.cylinder = False
        self.loc = (0.0, 0.0, 0.0)
        self.x = (1.0, 0.0, 0.0)
        self.y = (0.0, 1.0, 0.0)
        self.n = (0.0, 0.0, 1.0)
        self.axis_loc = (0.0, 0.0, 0.0)
        self.axis_dir = (0.0, 0.0, 1.0)
        self.radius = 0.0
        self.sign = 1.0
        self.ref = (1.0, 0.0, 0.0)
        self.ref_perp = (0.0, 1.0, 0.0)

    def set_axes(self, loc, n, x) -> None:
        # gp_Ax3(P, N, Vx): X is Vx made perpendicular to N, Y = N x X.
        xd = unit(cross(cross(n, x), n))
        self.loc, self.n, self.x = loc, n, xd
        self.y = cross(n, xd)

    def to_uv(self, pts: np.ndarray):
        if not self.cylinder:
            d = pts - np.array(self.loc)
            u = d[:, 0] * self.x[0] + d[:, 1] * self.x[1] + d[:, 2] * self.x[2]
            v = d[:, 0] * self.y[0] + d[:, 1] * self.y[1] + d[:, 2] * self.y[2]
            return u, v
        ad = np.array(self.axis_dir)
        d = pts - np.array(self.axis_loc)
        v = d[:, 0] * ad[0] + d[:, 1] * ad[1] + d[:, 2] * ad[2]
        w = d - v[:, None] * ad[None, :]
        mag = np.sqrt(w[:, 0] ** 2 + w[:, 1] ** 2 + w[:, 2] ** 2)
        rp, rf = self.ref_perp, self.ref
        th = np.arctan2(
            w[:, 0] * rp[0] + w[:, 1] * rp[1] + w[:, 2] * rp[2],
            w[:, 0] * rf[0] + w[:, 1] * rf[1] + w[:, 2] * rf[2],
        )
        u = np.where(mag < 1.0e-9, 0.0, self.sign * self.radius * th)
        return u, v

    def angles(self, pts: np.ndarray) -> np.ndarray:
        ad = np.array(self.axis_dir)
        d = pts - np.array(self.axis_loc)
        along = d[:, 0] * ad[0] + d[:, 1] * ad[1] + d[:, 2] * ad[2]
        w = d - along[:, None] * ad[None, :]
        mag = np.sqrt(w[:, 0] ** 2 + w[:, 1] ** 2 + w[:, 2] ** 2)
        rp, rf = self.ref_perp, self.ref
        th = np.arctan2(
            w[:, 0] * rp[0] + w[:, 1] * rp[1] + w[:, 2] * rp[2],
            w[:, 0] * rf[0] + w[:, 1] * rf[1] + w[:, 2] * rf[2],
        )
        return np.where(mag < 1.0e-9, 0.0, th)

    def normal_at(self, p) -> tuple:
        if not self.cylinder:
            return self.n
        d = sub(p, self.axis_loc)
        w = sub(d, scale(self.axis_dir, dot(d, self.axis_dir)))
        if norm(w) < 1.0e-9:
            return self.n
        n = unit(w)
        return neg(n) if self.sign < 0.0 else n


def _any_perp(n):
    a = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
    t = cross(a, n)
    if norm(t) < 1.0e-9:
        t = (1.0, 0.0, 0.0)
    return unit(t)


def _set_cyl_tangent(m: SurfaceMap) -> None:
    m.ref_perp = unit(cross(m.axis_dir, m.ref))
    n = neg(m.ref) if m.sign < 0.0 else m.ref
    x = neg(m.ref_perp) if m.sign < 0.0 else m.ref_perp
    m.set_axes(add(m.axis_loc, scale(m.ref, m.radius)), n, x)


def _host_frame(host: AagNode, face, m: SurfaceMap) -> bool:
    surf = BRepAdaptor_Surface(face, True)
    if surf.GetType() != GeomAbs_Plane:
        return False
    pln = surf.Plane()
    n = pnt(host.plane_normal)
    if host.is_reversed:
        n = neg(n)
    x = pnt(pln.Position().XDirection())
    if abs(dot(x, n)) > 0.9:
        alt = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
        x = unit(cross(alt, n))
    else:
        xv = sub(x, scale(n, dot(x, n)))
        if norm(xv) < 1.0e-9:
            return False
        x = unit(xv)
    m.set_axes(pnt(host.centroid), n, x)
    return True


def _build_surface_map(cand: ReliefCandidate, host_face, m: SurfaceMap) -> bool:
    host = cand.host
    if host.surface_type is SurfaceType.PLANE:
        m.cylinder = False
        if cand.has_frame:
            m.set_axes(*cand.frame)
            return True
        return _host_frame(host, host_face, m)
    if host.surface_type is not SurfaceType.CYLINDER:
        return False
    if host.cyl_radius <= 1.0e-6 or host.cyl_cone_axis is None:
        return False
    m.cylinder = True
    m.axis_dir = pnt(host.cyl_cone_axis.Direction())
    m.axis_loc = pnt(host.cyl_cone_axis.Location())
    m.radius = host.cyl_radius
    m.sign = -1.0 if host.is_reversed else 1.0
    m.ref = _any_perp(m.axis_dir)
    _set_cyl_tangent(m)
    return True


def _place_seam(m: SurfaceMap, pts: np.ndarray) -> float:
    """Cut the cylinder open in the widest angular gap between the candidate's
    own points. Returns that gap, so a caller can decline when there is none."""
    if len(pts) == 0:
        return 0.0
    ang = np.sort(m.angles(pts))
    k_pi = 3.14159265358979323846
    gap = 2.0 * k_pi - (float(ang[-1]) - float(ang[0]))
    mid = float(ang[-1]) + 0.5 * gap
    if len(ang) > 1:
        diffs = ang[1:] - ang[:-1]
        # First strictly-greater wins, as a left-to-right scan would choose.
        running = gap
        for i in np.nonzero(diffs > gap)[0]:
            g = float(diffs[i])
            if g > running:
                running = g
                mid = float(ang[i]) + 0.5 * g
        gap = running
    rot = mid + k_pi
    nd = add(scale(m.ref, math.cos(rot)), scale(m.ref_perp, math.sin(rot)))
    if norm(nd) > 1.0e-9:
        m.ref = unit(nd)
        _set_cyl_tangent(m)
    return gap


def _face_polygons3d(face, defl_mm: float) -> list[np.ndarray]:
    """One face's wires as closed 3D polylines, edges in wire order."""
    out = []
    wx = TopExp_Explorer(face, TopAbs_WIRE)
    while wx.More():
        wire = TopoDS.Wire_s(wx.Current())
        wx.Next()
        poly: list = []
        wex = BRepTools_WireExplorer(wire, face)
        while wex.More():
            edge = wex.Current()
            wex.Next()
            if BRep_Tool.Degenerated_s(edge):
                continue
            curve = BRepAdaptor_Curve(edge)
            pts: list = []
            try:
                disc = GCPnts_QuasiUniformDeflection(curve, defl_mm)
                if disc.IsDone() and disc.NbPoints() >= 2:
                    pts = [pnt(disc.Value(i)) for i in range(1, disc.NbPoints() + 1)]
            except Exception:
                pts = []
            if len(pts) < 2:
                pts = [pnt(curve.Value(curve.FirstParameter())),
                       pnt(curve.Value(curve.LastParameter()))]
            if edge.Orientation() == TopAbs_REVERSED:
                pts.reverse()
            first = 0 if not poly else 1
            poly.extend(pts[first:])
        if len(poly) >= 3:
            out.append(np.array(poly, dtype=float))
    return out


def pick_px_per_mm(cap_height_mm: float, stroke_w_mm: float, cfg: ReliefRasterConfig):
    """max(target cap px / cap height, min stroke px / stroke width), clamped.

    Cap height and stroke width are independent, so sizing the page for the
    cap alone can leave strokes too thin for a distance transform to measure.
    Returns (px_per_mm, driver).
    """
    cap_term = cfg.target_cap_px / cap_height_mm if cap_height_mm > 1.0e-6 else 0.0
    stroke_term = cfg.min_stroke_px / stroke_w_mm if stroke_w_mm > 1.0e-6 else 0.0
    driver = "stroke_width" if stroke_term > cap_term else "cap_height"
    pmm = max(cap_term, stroke_term)
    if pmm <= 0.0:
        driver = "fallback"
        pmm = cfg.min_px_per_mm
    return min(max(pmm, cfg.min_px_per_mm), cfg.max_px_per_mm), driver


def _vertices(face) -> list:
    out = []
    vx = TopExp_Explorer(face, TopAbs_VERTEX)
    while vx.More():
        out.append(pnt(BRep_Tool.Pnt_s(TopoDS.Vertex_s(vx.Current()))))
        vx.Next()
    return out


def relief_footprint_metrics(cand: ReliefCandidate, face_index) -> ReliefFootprintMetrics:
    m = ReliefFootprintMetrics()
    if cand.host is None or not cand.members:
        return m
    count = len(face_index)
    if cand.host.face_id < 1 or cand.host.face_id > count:
        return m
    smap = SurfaceMap()
    if not _build_surface_map(cand, face_index.face_at(cand.host.face_id), smap):
        return m
    m.host_planar = not smap.cylinder
    m.host_cylindrical = smap.cylinder

    # 2A/P over HOST-PARALLEL members only, asked at the face.
    for f in cand.members:
        if f is None or f.surface_type is not SurfaceType.PLANE:
            continue
        if abs(dot(pnt(f.plane_normal), smap.normal_at(pnt(f.centroid)))) < PARALLEL_DOT:
            continue
        if f.face_id < 1 or f.face_id > count:
            continue
        face = face_index.face_at(f.face_id)
        lp = GProp_GProps()
        BRepGProp.LinearProperties_s(face, lp)
        m.floor_area_mm2 += face_area(face)
        m.floor_perimeter_mm += lp.Mass()
    if m.floor_perimeter_mm > 1.0e-9:
        m.stroke_w_mm = 2.0 * m.floor_area_mm2 / m.floor_perimeter_mm

    # Footprint: member box corners on a plane (conservative); on a cylinder
    # the members' VERTICES, after placing the seam from the same evidence.
    pts: list = []
    if smap.cylinder:
        for f in cand.members:
            if f is None or f.face_id < 1 or f.face_id > count:
                continue
            pts.extend(_vertices(face_index.face_at(f.face_id)))
        arr = np.array(pts, dtype=float) if pts else np.zeros((0, 3))
        _place_seam(smap, arr)
    else:
        for f in cand.members:
            if f is None or f.bbox.IsVoid():
                continue
            xn, yn, zn, xx, yx, zx = f.bbox.Get()
            pts.extend([(xn, yn, zn), (xx, yn, zn), (xn, yx, zn), (xx, yx, zn),
                        (xn, yn, zx), (xx, yn, zx), (xn, yx, zx), (xx, yx, zx)])
        arr = np.array(pts, dtype=float) if pts else np.zeros((0, 3))
    if len(arr) == 0:
        return m
    u, v = smap.to_uv(arr)
    umin, umax = float(u.min()), float(u.max())
    vmin, vmax = float(v.min()), float(v.max())
    if umax <= umin or vmax <= vmin:
        return m
    m.footprint_u_mm = umax - umin
    m.footprint_v_mm = vmax - vmin
    m.footprint_diag_mm = math.hypot(m.footprint_u_mm, m.footprint_v_mm)
    m.cap_height_est_mm = min(m.footprint_u_mm, m.footprint_v_mm)
    return m


def _accumulate_coverage(polys, w, h, ppm, ou, ov, S, cov, diff) -> None:
    """Even-odd scanline fill of ONE face's wire set, accumulating exact S x S
    subpixel coverage. Filling per face and summing makes overlaps harmless."""
    if not polys:
        return
    sub_mm = ppm * S
    sw = w * S
    nrows = h * S
    u0s, v0s, u1s, v1s = [], [], [], []
    for u, v in polys:
        u0s.append(np.roll(u, 1))
        v0s.append(np.roll(v, 1))
        u1s.append(u)
        v1s.append(v)
    u0 = np.concatenate(u0s)
    v0 = np.concatenate(v0s)
    u1 = np.concatenate(u1s)
    v1 = np.concatenate(v1s)
    lo = np.minimum(v0, v1)
    hi = np.maximum(v0, v1)
    keep = hi > lo
    u0, v0, u1, v1, lo, hi = u0[keep], v0[keep], u1[keep], v1[keep], lo[keep], hi[keep]
    if len(u0) == 0:
        return
    first = np.floor((lo - ov) * sub_mm - 0.5).astype(np.int64) - 1
    last = np.ceil((hi - ov) * sub_mm - 0.5).astype(np.int64) + 1
    first = np.clip(first, 0, nrows)
    last = np.clip(last, 0, nrows - 1)
    counts = np.maximum(last - first + 1, 0)
    total = int(counts.sum())
    if total == 0:
        return
    edge = np.repeat(np.arange(len(u0)), counts)
    offs = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
    sy = first[edge] + offs
    vy = ov + (sy.astype(float) + 0.5) / sub_mm
    e0 = v0[edge]
    e1 = v1[edge]
    hit = (e0 <= vy) != (e1 <= vy)
    sy, vy, edge = sy[hit], vy[hit], edge[hit]
    if len(sy) == 0:
        return
    t = (vy - v0[edge]) / (v1[edge] - v0[edge])
    x = u0[edge] + t * (u1[edge] - u0[edge])
    order = np.lexsort((x, sy))
    sy = sy[order]
    x = x[order]
    n = len(sy)
    starts = np.r_[0, np.nonzero(np.diff(sy))[0] + 1]
    group_start = np.repeat(starts, np.diff(np.r_[starts, n]))
    rank = np.arange(n) - group_start
    group_len = np.repeat(np.diff(np.r_[starts, n]), np.diff(np.r_[starts, n]))
    pair = (rank % 2 == 0) & (rank + 1 < group_len)
    ia = np.nonzero(pair)[0]
    xa = x[ia]
    xb = x[ia + 1]
    rows = sy[ia] // S
    a = np.floor((xa - ou) * sub_mm).astype(np.int64)
    b = np.ceil((xb - ou) * sub_mm).astype(np.int64)
    a = np.maximum(a, 0)
    b = np.minimum(b, sw)
    ok = b > a
    a, b, rows = a[ok], b[ok], rows[ok]
    if len(a) == 0:
        return
    pa = a // S
    pb = (b - 1) // S
    same = pa == pb
    np.add.at(cov, (rows[same], pa[same]), (b - a)[same])
    d = ~same
    ra, pa_d, pb_d, a_d, b_d = rows[d], pa[d], pb[d], a[d], b[d]
    np.add.at(cov, (ra, pa_d), (pa_d + 1) * S - a_d)
    np.add.at(cov, (ra, pb_d), b_d - pb_d * S)
    np.add.at(diff, (ra, pa_d + 1), S)
    np.add.at(diff, (ra, pb_d), -S)


def rasterise_relief(cand: ReliefCandidate, face_index, cfg: Optional[ReliefRasterConfig] = None) -> ReliefRaster:
    cfg = cfg or ReliefRasterConfig()
    r = ReliefRaster()
    if cand.host is None:
        r.decline_reason = "no_host"
        return r
    if not cand.members:
        r.decline_reason = "no_members"
        return r
    if cand.host.surface_type not in (SurfaceType.PLANE, SurfaceType.CYLINDER):
        r.decline_reason = "host_not_developable"
        return r
    count = len(face_index)
    if cand.host.face_id < 1 or cand.host.face_id > count:
        r.decline_reason = "host_index_out_of_range"
        return r
    smap = SurfaceMap()
    if not _build_surface_map(cand, face_index.face_at(cand.host.face_id), smap):
        r.decline_reason = "host_frame_degenerate"
        return r

    r.metrics = relief_footprint_metrics(cand, face_index)
    if r.metrics.footprint_diag_mm <= 1.0e-6:
        r.decline_reason = "empty_footprint"
        return r
    cap = cand.cap_height_mm if cand.cap_height_mm > 1.0e-6 else r.metrics.cap_height_est_mm
    r.px_per_mm, r.resolution_driver = pick_px_per_mm(cap, r.metrics.stroke_w_mm, cfg)

    S = max(1, cfg.supersample)
    defl = cfg.boundary_defl_px / r.px_per_mm
    per_face3 = []
    for f in cand.members:
        if f is None or f.face_id < 1 or f.face_id > count:
            continue
        polys = _face_polygons3d(face_index.face_at(f.face_id), defl)
        if polys:
            per_face3.append(polys)
    if not per_face3:
        r.decline_reason = "no_projectable_boundary"
        return r

    if smap.cylinder:
        allpts = np.concatenate([p for f3 in per_face3 for p in f3])
        gap = _place_seam(smap, allpts)
        # With no clear angle of at least one stroke width there is no cut,
        # and a severed mark rasterises as two marks.
        need = r.metrics.stroke_w_mm / smap.radius if r.metrics.stroke_w_mm > 1.0e-6 else 1.0e-3
        if gap <= need:
            r.decline_reason = "ink_wraps_the_seam"
            return r

    per_face = []
    umin = vmin = 1e308
    umax = vmax = -1e308
    for f3 in per_face3:
        polys = []
        for p3 in f3:
            u, v = smap.to_uv(p3)
            if len(u) >= 3:
                polys.append((u, v))
                umin = min(umin, float(u.min()))
                umax = max(umax, float(u.max()))
                vmin = min(vmin, float(v.min()))
                vmax = max(vmax, float(v.max()))
        if polys:
            per_face.append(polys)
    if not per_face or umax <= umin or vmax <= vmin:
        r.decline_reason = "no_projectable_boundary"
        return r

    pad = max(0, cfg.pad_px)

    def page(pmm):
        w = int(math.ceil((umax - umin) * pmm)) + 2 * pad
        h = int(math.ceil((vmax - vmin) * pmm)) + 2 * pad
        return max(w, 1), max(h, 1)

    r.w, r.h = page(r.px_per_mm)
    if r.w * r.h > cfg.max_pixels:
        sc = math.sqrt(cfg.max_pixels / (r.w * r.h))
        r.px_per_mm = max(cfg.min_px_per_mm, r.px_per_mm * sc * 0.98)
        r.resolution_capped = True
        r.w, r.h = page(r.px_per_mm)
        if r.w * r.h > cfg.max_pixels:
            r.decline_reason = "footprint_exceeds_raster_budget"
            return r
    r.origin_u_mm = umin - pad / r.px_per_mm
    r.origin_v_mm = vmin - pad / r.px_per_mm

    cov = np.zeros((r.h, r.w), dtype=np.int64)
    diff = np.zeros((r.h, r.w + 1), dtype=np.int64)
    for polys in per_face:
        _accumulate_coverage(polys, r.w, r.h, r.px_per_mm, r.origin_u_mm,
                             r.origin_v_mm, S, cov, diff)
    cov += np.cumsum(diff, axis=1)[:, : r.w]
    np.minimum(cov, 65535, out=cov)
    half = (S * S) // 2
    r.ink = (cov >= half).astype(np.uint8)
    r.ink_px = int(r.ink.sum())
    if r.ink_px == 0:
        r.decline_reason = "raster_empty"
        return r
    r.valid = True
    return r


def label_runs(mask: np.ndarray, eight: bool):
    """Connected components of a boolean image by row runs.

    Labels are 1..n numbered in raster order of each component's first
    pixel -- the order a raster-scan flood fill assigns them. Returns
    (labels, n).
    """
    h, w = mask.shape
    padded = np.zeros((h, w + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    d = np.diff(padded, axis=1)
    sr, sc = np.nonzero(d == 1)
    er, ec = np.nonzero(d == -1)
    nruns = len(sr)
    labels = np.zeros((h, w), dtype=np.int32)
    if nruns == 0:
        return labels, 0
    row_first = np.searchsorted(sr, np.arange(h + 1))
    parent = list(range(nruns))

    def find(a):
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    k = 1 if eight else 0
    for row in range(1, h):
        cur0, cur1 = row_first[row], row_first[row + 1]
        prev0, prev1 = row_first[row - 1], row_first[row]
        if cur0 == cur1 or prev0 == prev1:
            continue
        ps = sc[prev0:prev1]
        pe = ec[prev0:prev1]
        cs = sc[cur0:cur1]
        ce = ec[cur0:cur1]
        lo = np.searchsorted(pe + k, cs, side="right")
        hi = np.searchsorted(ps, ce + k, side="left")
        for i in range(len(cs)):
            for j in range(lo[i], hi[i]):
                ra = find(cur0 + i)
                rb = find(prev0 + j)
                if ra != rb:
                    if ra < rb:
                        parent[rb] = ra
                    else:
                        parent[ra] = rb
    roots = np.array([find(i) for i in range(nruns)])
    # Roots are always a component's lowest run index, i.e. its first pixel
    # in raster order, so numbering roots ascending is raster order.
    uniq, inverse = np.unique(roots, return_inverse=True)
    lab = (inverse + 1).astype(np.int32)
    lengths = ec - sc
    flat_rows = np.repeat(sr, lengths)
    flat_cols = np.repeat(sc, lengths) + (
        np.arange(lengths.sum()) - np.repeat(np.cumsum(lengths) - lengths, lengths)
    )
    labels[flat_rows, flat_cols] = np.repeat(lab, lengths)
    return labels, len(uniq)


def outside_background(ink: np.ndarray) -> np.ndarray:
    """Background 4-connected to the image border."""
    bg = ink == 0
    labels, n = label_runs(bg, eight=False)
    if n == 0:
        return np.zeros_like(bg)
    border = np.unique(np.concatenate(
        [labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]
    ))
    border = border[border > 0]
    return np.isin(labels, border)


def invert_enclosed(src: ReliefRaster) -> ReliefRaster:
    """The enclosed counters and islands: the ink of a background relief,
    where the direct ink is the milled field and the letters are its holes."""
    r = ReliefRaster(
        w=src.w, h=src.h, px_per_mm=src.px_per_mm, origin_u_mm=src.origin_u_mm,
        origin_v_mm=src.origin_v_mm, resolution_driver=src.resolution_driver,
        resolution_capped=src.resolution_capped, metrics=src.metrics,
        valid=src.valid, decline_reason=src.decline_reason,
    )
    if not src.valid:
        r.ink = src.ink
        return r
    outside = outside_background(src.ink)
    r.ink = ((src.ink == 0) & ~outside).astype(np.uint8)
    r.ink_px = int(r.ink.sum())
    r.valid = r.ink_px > 0
    if not r.valid:
        r.decline_reason = "no_enclosed_background"
    return r
