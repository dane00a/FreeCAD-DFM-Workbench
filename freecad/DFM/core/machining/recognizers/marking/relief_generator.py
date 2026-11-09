# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Stage 1: relief candidates, tuned for RECALL.

A relief is a maximal connected set of faces lying within a one-sided slab of
a host support surface, whose ink level is constant. That is all this stage
decides. Whether it reads as lettering is Stage 3's question and whether it
was cut or pressed is Stage 2's.

Two structural changes carry most of the recall:

* Hosts are coplanar SURFACES, not faces (see ``HostSurface``).
* The search window is derived, not tuned: the marking-process depth ceiling
  plus headroom for a drafted stroke, capped at half the LOCAL wall thickness.
  Lateral size is never bounded absolutely -- that asymmetry is what lets a
  logo on an 860 mm part be found while a 40 mm deep cavity that is
  scale-free-identical to a glyph is still refused.
"""

from __future__ import annotations

import math
import os
from typing import Optional

import numpy as np

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from ...aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ...features import FeatureType
from . import tuning as T
from .model import (
    HostKind,
    HostSurface,
    InkKind,
    ReliefRegion,
    Rejection,
    Vec,
    add,
    cross,
    dot,
    neg,
    norm,
    pnt,
    scale,
    sub,
    unit,
)

EMPTY = (1e18, -1e18, 1e18, -1e18, 1e18, -1e18)


def trace_on() -> bool:
    return os.environ.get("DFM_MARK_TRACE") is not None


# -- Face perimeters, out of the graph ----------------------------------------


class FaceTable:
    """Face perimeters summed from the graph's own edge lengths.

    Every edge of a closed solid is shared by two faces and the graph already
    stores its length, so this needs no kernel query. It can differ from a
    true perimeter only on a seam the graph does not record, which slightly
    widens the 2A/P estimate on such a face.
    """

    def __init__(self, graph: AttributedAdjacencyGraph) -> None:
        self.graph = graph
        self._cache: dict[int, float] = {}

    def perimeter(self, node: AagNode) -> float:
        cached = self._cache.get(node.face_id)
        if cached is None:
            cached = 0.0
            for edge in self.graph.edges_of(node.face_id):
                cached += edge.shared_edge_length
            self._cache[node.face_id] = cached
        return cached


# -- Scene ---------------------------------------------------------------------


class FaceSamples:
    """Points genuinely ON a face: its discretised boundary plus its centroid.

    A bounding box is the wrong object on a curved host -- a glyph floor on a
    collar at a 45 degree station has a box spanning 5 mm radially on a face
    whose every point is one radius from the axis -- and on a B-spline face,
    whose OCCT box bounds the control net rather than the face.
    """

    def __init__(self) -> None:
        self.pts: dict[int, np.ndarray] = {}

    def find(self, face_id: int) -> Optional[np.ndarray]:
        return self.pts.get(face_id)

    def empty(self) -> bool:
        return not self.pts


class ReliefScene:
    def __init__(self) -> None:
        self.hosts: list[HostSurface] = []
        self.regions: list[ReliefRegion] = []
        self.region_host: list[int] = []
        # Part extents over PLANAR faces only: OCCT pads curved boxes after a
        # boolean, and these numbers are denominators.
        self.part_diag_mm = 0.0
        self.part_min_dim_mm = 0.0
        self.samples = FaceSamples()
        self._corner_cache: dict[int, np.ndarray] = {}
        self._measure_cache: dict[tuple[int, int, bool], tuple] = {}

    # -- measuring a face in a host's frame ---------------------------------

    def corners(self, node: AagNode) -> Optional[np.ndarray]:
        cached = self._corner_cache.get(node.face_id)
        if cached is None:
            if node.bbox.IsVoid():
                cached = np.zeros((0, 3))
            else:
                xn, yn, zn, xx, yx, zx = node.bbox.Get()
                cached = np.array(
                    [
                        (cx, cy, cz)
                        for cx in (xn, xx)
                        for cy in (yn, yx)
                        for cz in (zn, zx)
                    ]
                )
            self._corner_cache[node.face_id] = cached
        return cached if len(cached) else None

    def measure(self, node: AagNode, host: HostSurface, use_samples: bool = True):
        """(u_lo, u_hi, v_lo, v_hi, s_lo, s_hi) of a face in the host's frame.

        The single owner of the difference between host kinds, so no stage can
        disagree with another about where a face is. A plane face on a plane
        host is measured by its eight box corners (exact for a polygon);
        anything else by its cached samples. A curved host with no samples
        measures nothing -- a miss is cheaper than a distorted measurement.
        """
        want = use_samples and (host.curved() or node.surface_type is not SurfaceType.PLANE)
        pts = self.samples.find(node.face_id) if want else None
        key = (id(host), node.face_id, pts is not None)
        cached = self._measure_cache.get(key)
        if cached is not None:
            return cached
        if pts is not None and len(pts):
            result = _measure_points(pts, host)
        elif host.curved():
            result = EMPTY
        else:
            c = self.corners(node)
            result = EMPTY if c is None else _measure_points(c, host)
        self._measure_cache[key] = result
        return result


def _measure_points(pts: np.ndarray, host: HostSurface) -> tuple:
    if host.kind is HostKind.PLANE:
        ox, oy, oz = host.origin
        dx = pts[:, 0] - ox
        dy = pts[:, 1] - oy
        dz = pts[:, 2] - oz
        u = dx * host.u[0] + dy * host.u[1] + dz * host.u[2]
        v = dx * host.v[0] + dy * host.v[1] + dz * host.v[2]
        s = dx * host.normal[0] + dy * host.normal[1] + dz * host.normal[2]
    else:
        u, v, s = _cyl_coords(pts, host)
    return (
        float(u.min()), float(u.max()), float(v.min()), float(v.max()),
        float(s.min()), float(s.max()),
    )


def _cyl_coords(pts: np.ndarray, host: HostSurface):
    ax = np.array(host.axis_dir)
    d = pts - np.array(host.axis_loc)
    along = d[:, 0] * ax[0] + d[:, 1] * ax[1] + d[:, 2] * ax[2]
    w = d - along[:, None] * ax[None, :]
    rad = np.sqrt(w[:, 0] ** 2 + w[:, 1] ** 2 + w[:, 2] ** 2)
    rp, rd = host.ref_perp, host.ref_dir
    theta = np.arctan2(
        w[:, 0] * rp[0] + w[:, 1] * rp[1] + w[:, 2] * rp[2],
        w[:, 0] * rd[0] + w[:, 1] * rd[1] + w[:, 2] * rd[2],
    )
    u = np.where(rad < 1e-9, 0.0, host.curv_sign * host.radius * theta)
    s = host.curv_sign * (rad - host.radius)
    return u, along, s


def merge(acc: list, r: tuple) -> None:
    """Widen a running (u_lo, u_hi, v_lo, v_hi, s_lo, s_hi) by a result."""
    acc[0] = min(acc[0], r[0])
    acc[1] = max(acc[1], r[1])
    acc[2] = min(acc[2], r[2])
    acc[3] = max(acc[3], r[3])
    acc[4] = min(acc[4], r[4])
    acc[5] = max(acc[5], r[5])


def outward(node: AagNode) -> Vec:
    n = node.plane_normal
    d = (n.X(), n.Y(), n.Z())
    return neg(d) if node.is_reversed else d


def any_perp(n: Vec) -> Vec:
    ax = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
    t = cross(ax, n)
    if norm(t) < 1e-9:
        t = (1.0, 0.0, 0.0)
    return unit(t)


class DSU:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, a: int) -> int:
        root = a
        while self.p[root] != root:
            root = self.p[root]
        while self.p[a] != root:
            self.p[a], a = root, self.p[a]
        return root

    def unite(self, a: int, b: int) -> None:
        self.p[self.find(a)] = self.find(b)


def reject(out, stage, gate, detail, margin, host_id, faces) -> None:
    if out is None:
        return
    out.append(
        Rejection(stage, gate, detail, margin, host_id, [f.face_id for f in faces])
    )


def bbox_in_plane_range(box, host: HostSurface):
    """(u_lo, u_hi, v_lo, v_hi) of a box's corners in a PLANAR host frame."""
    xn, yn, zn, xx, yx, zx = box.Get()
    ulo = vlo = 1e18
    uhi = vhi = -1e18
    for cx in (xn, xx):
        for cy in (yn, yx):
            for cz in (zn, zx):
                su, sv = host.in_plane((cx, cy, cz))
                ulo = min(ulo, su)
                uhi = max(uhi, su)
                vlo = min(vlo, sv)
                vhi = max(vhi, sv)
    return ulo, uhi, vlo, vhi


def covered_fraction(au, av, bu, bv) -> float:
    """Share of rectangle A that rectangle B covers."""
    ou = min(au[1], bu[1]) - max(au[0], bu[0])
    ov = min(av[1], bv[1]) - max(av[0], bv[0])
    if ou <= 0.0 or ov <= 0.0:
        return 0.0
    area_a = max(au[1] - au[0], 1e-9) * max(av[1] - av[0], 1e-9)
    return (ou * ov) / area_a


# -- Sampling a face's boundary --------------------------------------------------

_MAX_SAMPLES = 96


def sample_face(face, node: AagNode) -> np.ndarray:
    """Boundary points plus the area centroid, at a face-relative deflection.

    The centroid goes in because the radial extreme of a chord-plane face on a
    cylinder is interior, not on the boundary. Capped: every measurement made
    from these is a min or a max, and those converge fast.
    """
    out: list[Vec] = []
    diag = 1.0
    if not node.bbox.IsVoid():
        xn, yn, zn, xx, yx, zx = node.bbox.Get()
        dx, dy, dz = xx - xn, yx - yn, zx - zn
        diag = math.sqrt(dx * dx + dy * dy + dz * dz)
    defl = max(0.02, 0.01 * diag)
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        if BRep_Tool.Degenerated_s(edge):
            continue
        try:
            curve = BRepAdaptor_Curve(edge)
            disc = GCPnts_QuasiUniformDeflection(curve, defl)
            if disc.IsDone() and disc.NbPoints() >= 2:
                for i in range(1, disc.NbPoints() + 1):
                    out.append(pnt(disc.Value(i)))
            else:
                out.append(pnt(curve.Value(curve.FirstParameter())))
                out.append(pnt(curve.Value(curve.LastParameter())))
        except Exception:
            pass
        if len(out) > 8 * _MAX_SAMPLES:
            break
    if len(out) > _MAX_SAMPLES:
        step = len(out) / _MAX_SAMPLES
        out = [out[int(i * step)] for i in range(_MAX_SAMPLES)]
    out.append(pnt(node.centroid))
    return np.array(out, dtype=float)


# -- Host grouping -----------------------------------------------------------------


def set_cyl_frame(h: HostSurface) -> None:
    """The tangent frame and touch point of a cylindrical host at its seam
    reference, so at that point it reads exactly like a planar host."""
    h.ref_perp = unit(cross(h.axis_dir, h.ref_dir))
    h.normal = neg(h.ref_dir) if h.curv_sign < 0.0 else h.ref_dir
    h.u = neg(h.ref_perp) if h.curv_sign < 0.0 else h.ref_perp
    h.v = h.axis_dir
    h.origin = add(h.axis_loc, scale(h.ref_dir, h.radius))


def build_hosts(graph: AttributedAdjacencyGraph, planes, cylinders) -> list[HostSurface]:
    from OCP.Bnd import Bnd_Box

    groups: list[list] = []  # [n, d, area, weighted_origin, faces]
    for node in planes:
        if node.bbox.IsVoid() or node.area <= 0.0:
            continue
        n = outward(node)
        c = pnt(node.centroid)
        d = dot(c, n)
        hit = None
        for g in groups:
            if dot(g[0], n) < T.COPLANAR_DOT:
                continue
            if abs(g[1] - d) > T.COPLANAR_OFFSET_MM:
                continue
            hit = g
            break
        if hit is None:
            hit = [n, d, 0.0, (0.0, 0.0, 0.0), []]
            groups.append(hit)
        hit[2] += node.area
        hit[3] = add(hit[3], scale(c, node.area))
        hit[4].append(node)

    hosts: list[HostSurface] = []
    for n, _d, area, weighted, faces in groups:
        if area <= 0.0 or not faces:
            continue
        h = HostSurface()
        h.normal = n
        h.u = any_perp(n)
        h.v = unit(cross(n, h.u))
        h.origin = scale(weighted, 1.0 / area)
        h.area = area
        h.faces = list(faces)
        # A coplanar face lying strictly inside a much larger face of the same
        # plane is an ISLAND, not support surface: the letters of raised text
        # in a milled-down field sit at host level. Ejected back into the
        # member pool and remembered by name.
        primary = None
        for f in h.faces:
            if primary is None or f.area > primary.area:
                primary = f
        if primary is not None and not primary.bbox.IsVoid() and len(h.faces) > 1:
            pu0, pu1, pv0, pv1 = bbox_in_plane_range(primary.bbox, h)
            keep = []
            for f in h.faces:
                if f is primary or f.bbox.IsVoid() or f.area > 0.25 * primary.area:
                    keep.append(f)
                    continue
                fu0, fu1, fv0, fv1 = bbox_in_plane_range(f.bbox, h)
                enclosed = fu0 > pu0 and fu1 < pu1 and fv0 > pv0 and fv1 < pv1
                if not enclosed:
                    keep.append(f)
                    continue
                h.level_face_ids.add(f.face_id)
                h.area -= f.area
            h.faces = keep
        h.bbox = Bnd_Box()
        best = -1.0
        for f in h.faces:
            h.face_ids.append(f.face_id)
            h.bbox.Add(f.bbox)
            if f.area > best:
                best = f.area
                h.primary_face_id = f.face_id
        hosts.append(h)

    # Cylindrical support surfaces: coaxial, co-radial and facing the same
    # way. The outward side comes from the face orientation, exactly as for a
    # plane: an OCCT cylinder's natural normal points away from the axis, so a
    # forward face is material-inside and a reversed one a bore.
    cgroups: list[list] = []  # [dir, loc, radius, sign, area, faces]
    for node in cylinders:
        if node.bbox.IsVoid() or node.area <= 0.0:
            continue
        if node.cyl_radius <= T.NOISE_FLOOR_MM or node.cyl_cone_axis is None:
            continue
        direction = pnt(node.cyl_cone_axis.Direction())
        loc = pnt(node.cyl_cone_axis.Location())
        sign = -1.0 if node.is_reversed else 1.0
        hit = None
        for g in cgroups:
            if abs(dot(g[0], direction)) < T.COPLANAR_DOT:
                continue
            if abs(g[2] - node.cyl_radius) > T.COPLANAR_OFFSET_MM:
                continue
            if g[3] != sign:
                continue
            off = sub(loc, g[1])
            off = sub(off, scale(g[0], dot(off, g[0])))
            if norm(off) > T.COPLANAR_OFFSET_MM:
                continue
            hit = g
            break
        if hit is None:
            hit = [direction, loc, node.cyl_radius, sign, 0.0, []]
            cgroups.append(hit)
        hit[4] += node.area
        hit[5].append(node)

    for direction, loc, radius, sign, area, faces in cgroups:
        if not faces or area <= 0.0:
            continue
        h = HostSurface()
        h.kind = HostKind.CYLINDER
        h.axis_dir = direction
        h.axis_loc = loc
        h.radius = radius
        h.curv_sign = sign
        # A provisional seam; re-chosen once the candidate faces are known.
        h.ref_dir = any_perp(direction)
        h.ref_perp = unit(cross(direction, h.ref_dir))
        h.area = area
        h.faces = list(faces)
        h.bbox = Bnd_Box()
        best = -1.0
        for f in h.faces:
            h.face_ids.append(f.face_id)
            h.bbox.Add(f.bbox)
            if f.area > best:
                best = f.area
                h.primary_face_id = f.face_id
        set_cyl_frame(h)
        hosts.append(h)
    return hosts


def _box_corners(box):
    xn, yn, zn, xx, yx, zx = box.Get()
    return [(cx, cy, cz) for cx in (xn, xx) for cy in (yn, yx) for cz in (zn, zx)]


def axial_overlap(a: HostSurface, b: HostSurface) -> bool:
    """Whether two coaxial hosts' axial spans meet, both in A's frame."""

    def span(box):
        if box is None or box.IsVoid():
            return 1e18, -1e18
        ts = [dot(sub(c, a.axis_loc), a.axis_dir) for c in _box_corners(box)]
        return min(ts), max(ts)

    alo, ahi = span(a.bbox)
    blo, bhi = span(b.bbox)
    if alo > ahi or blo > bhi:
        return False
    return min(ahi, bhi) > max(alo, blo)


def rect_overlap_fraction(frame: HostSurface, a, b) -> float:
    if a.IsVoid() or b.IsVoid():
        return 0.0
    au0, au1, av0, av1 = bbox_in_plane_range(a, frame)
    bu0, bu1, bv0, bv1 = bbox_in_plane_range(b, frame)
    return covered_fraction((au0, au1), (av0, av1), (bu0, bu1), (bv0, bv1))


def resolve_thickness(hosts: list[HostSurface]) -> None:
    """Local wall thickness under each host: the nearest anti-parallel
    surface whose footprint overlaps it.

    Probed with the host's LARGEST face, not the whole coplanar group: several
    formed crests coplanar with each other span the whole panel as a group,
    and the inner skin of any one of them covers a negligible share of that.
    Planes pair with planes and cylinders with cylinders only.
    """
    for h in hosts:
        best = -1e18
        if h.curved():
            for o in hosts:
                if o is h or not o.curved():
                    continue
                if abs(dot(h.axis_dir, o.axis_dir)) < T.COPLANAR_DOT:
                    continue
                off = sub(o.axis_loc, h.axis_loc)
                off = sub(off, scale(h.axis_dir, dot(off, h.axis_dir)))
                if norm(off) > T.COPLANAR_OFFSET_MM:
                    continue
                s = h.curv_sign * (o.radius - h.radius)
                if s > -4.0 * T.NOISE_FLOOR_MM:
                    continue
                if h.curv_sign * o.curv_sign > 0.0:
                    continue
                if not axial_overlap(h, o):
                    continue
                best = max(best, s)
        else:
            probe = None
            best_area = -1.0
            for f in h.faces:
                if f.area > best_area and not f.bbox.IsVoid():
                    best_area = f.area
                    probe = f.bbox
            if probe is None:
                probe = h.bbox
            for o in hosts:
                if o is h or o.curved():
                    continue
                if dot(h.normal, o.normal) > -T.COPLANAR_DOT:
                    continue
                s = h.signed_offset(o.origin)
                if s > -4.0 * T.NOISE_FLOOR_MM:
                    continue
                if rect_overlap_fraction(h, probe, o.bbox) < 0.25:
                    continue
                best = max(best, s)
        if best > -1e17:
            h.local_thickness_mm = -best
            h.thickness_resolved = True
        else:
            h.local_thickness_mm = T.THICKNESS_FALLBACK_MM
            h.thickness_resolved = False


def opposing_wall_gap(walls, host: HostSurface) -> float:
    """Median opposing-wall gap: the stroke width of a floorless mark."""
    w = []
    for f in walls:
        c = pnt(f.centroid)
        hn = host.normal_at(c)
        fn = outward(f)
        pn = sub(fn, scale(hn, dot(fn, hn)))
        if norm(pn) < 1e-6:
            continue
        w.append((unit(pn), c))
    gaps = []
    for i, (di, pi) in enumerate(w):
        best = 1e18
        for j, (dj, pj) in enumerate(w):
            if i == j:
                continue
            if dot(di, dj) > T.OPPOSING_DOT:
                continue
            g = dot(sub(pj, pi), di)
            if g <= T.NOISE_FLOOR_MM:
                continue
            best = min(best, g)
        if best < 1e17:
            gaps.append(best)
    return T.median(gaps)


def radial_interval(h: HostSurface, box):
    """A conservative interval of the radial distance from the axis to any
    point of a box; it can never wrongly exclude a face."""
    if box.IsVoid():
        return None
    xn, yn, zn, xx, yx, zx = box.Get()
    ctr = (0.5 * (xn + xx), 0.5 * (yn + yx), 0.5 * (zn + zx))
    dx, dy, dz = xx - xn, yx - yn, zx - zn
    half = 0.5 * math.sqrt(dx * dx + dy * dy + dz * dz)
    d = sub(ctr, h.axis_loc)
    d = sub(d, scale(h.axis_dir, dot(d, h.axis_dir)))
    dc = norm(d)
    return max(0.0, dc - half), dc + half


def choose_seam(h: HostSurface, candidates) -> None:
    """Put the unrolling's branch cut in the widest angular gap between the
    faces this host could carry: the one place cutting separates nothing."""
    ang = []
    for f in candidates:
        d = sub(pnt(f.centroid), h.axis_loc)
        d = sub(d, scale(h.axis_dir, dot(d, h.axis_dir)))
        if norm(d) < 1e-9:
            continue
        ang.append(math.atan2(dot(d, h.ref_perp), dot(d, h.ref_dir)))
    if not ang:
        return
    ang.sort()
    two_pi = 2.0 * 3.14159265358979323846
    gap = two_pi - (ang[-1] - ang[0])
    mid = ang[-1] + 0.5 * gap
    for i in range(1, len(ang)):
        g = ang[i] - ang[i - 1]
        if g > gap:
            gap = g
            mid = ang[i - 1] + 0.5 * g
    rot = mid + 3.14159265358979323846
    nd = add(scale(h.ref_dir, math.cos(rot)), scale(h.ref_perp, math.sin(rot)))
    if norm(nd) < 1e-9:
        return
    h.ref_dir = unit(nd)
    set_cyl_frame(h)


def union_area_perimeter(faces, ft: FaceTable, graph) -> tuple[float, float]:
    """Area and union perimeter of a face set, shared seams removed twice.

    Clamped at a quarter of the raw perimeter: a set tiling a closed patch
    otherwise drives the union perimeter to nothing and 2A/P to nonsense.
    """
    if not faces:
        return 0.0, 1e-6
    ids = {f.face_id for f in faces}
    area = 0.0
    sum_p = 0.0
    shared = 0.0
    for f in faces:
        area += f.area
        sum_p += ft.perimeter(f)
        for edge in graph.edges_of(f.face_id):
            if edge.other_face(f.face_id) in ids:
                shared += edge.shared_edge_length
    return area, max(sum_p - 2.0 * shared, max(0.25 * sum_p, 1e-6))


def union_width(faces, ft: FaceTable, graph) -> float:
    a, p = union_area_perimeter(faces, ft, graph)
    return 2.0 * a / p


# -- Stage 1 -------------------------------------------------------------------------

#: Feature types whose recognition rests on a signature lettering cannot
#: produce -- a cylinder between two coaxial shoulders, or explicit thread
#: evidence. Their faces are not available to a marking candidate, because a
#: claim that swallowed one would delete a real functional feature. The
#: generic reads (slot, undercut, boss, pocket...) are deliberately NOT here:
#: absorbing those on its own letterforms is why marking clusters at all.
PROTECTED_TYPES = (
    FeatureType.GROOVE,
    FeatureType.O_RING_GLAND,
    FeatureType.RETAINING_RING_GROOVE,
    FeatureType.THREAD_RELIEF_GROOVE,
    FeatureType.EXTERNAL_THREAD,
    FeatureType.THREADED_HOLE,
)


def protected_prior_faces(prior_features) -> set[int]:
    out: set[int] = set()
    for feature in prior_features or ():
        if feature.type in PROTECTED_TYPES:
            out.update(feature.faces)
    return out


def generate_relief_candidates(
    graph: AttributedAdjacencyGraph,
    face_index,
    priors=None,
    blocked: Optional[set[int]] = None,
    rejects: Optional[list] = None,
) -> ReliefScene:
    from OCP.Bnd import Bnd_Box

    scene = ReliefScene()
    ft = FaceTable(graph)
    trace = trace_on()
    nodes = graph.nodes
    planes = [n for n in nodes if n.surface_type is SurfaceType.PLANE]
    cylinders = [n for n in nodes if n.surface_type is SurfaceType.CYLINDER]
    blocked = blocked or set()

    part_box = Bnd_Box()
    for n in planes:
        if not n.bbox.IsVoid():
            part_box.Add(n.bbox)
    if not part_box.IsVoid():
        xn, yn, zn, xx, yx, zx = part_box.Get()
        dx, dy, dz = xx - xn, yx - yn, zx - zn
        scene.part_diag_mm = math.sqrt(dx * dx + dy * dy + dz * dz)
        scene.part_min_dim_mm = min(dx, dy, dz)

    scene.hosts = build_hosts(graph, planes, cylinders)
    resolve_thickness(scene.hosts)

    def ensure_samples(node: AagNode) -> None:
        if face_index is None or node.face_id in scene.samples.pts:
            return
        if node.face_id < 1 or node.face_id > len(face_index):
            return
        scene.samples.pts[node.face_id] = sample_face(
            face_index.face_at(node.face_id), node
        )

    # All-node box corners, for the vectorised planar prefilter.
    valid = [n for n in nodes if not n.bbox.IsVoid()]
    corners = np.array([scene.corners(n) for n in valid]) if valid else np.zeros((0, 8, 3))

    for hi, host in enumerate(scene.hosts):
        # The PROCESS depth ceiling plus stroke-bottom headroom, never more
        # than half the local wall.
        window = min(
            T.SEARCH_MARGIN * T.MAX_MARK_DEPTH_MM,
            T.DEPTH_THICKNESS_MAX * host.local_thickness_mm,
        )
        if window <= T.NOISE_FLOOR_MM:
            continue
        host_faces = set(host.face_ids)

        if host.curved():
            # Find what could be in the slab (cheaply, never wrongly
            # excluding), put the seam in the widest gap between THEM, and
            # only then sample: a sample's (u, v) means nothing until the seam
            # is fixed.
            near = []
            for node in nodes:
                if node.face_id in host_faces or node.face_id in blocked:
                    continue
                interval = radial_interval(host, node.bbox)
                if interval is None:
                    continue
                a = host.curv_sign * (interval[0] - host.radius)
                b = host.curv_sign * (interval[1] - host.radius)
                if max(a, b) < -window or min(a, b) > window:
                    continue
                near.append(node)
            if not near:
                continue
            choose_seam(host, near)
            for n in near:
                ensure_samples(n)
            for n in host.faces:
                ensure_samples(n)
            foot = list(EMPTY)
            for n in host.faces:
                merge(foot, scene.measure(n, host))
            host.foot_u_lo, host.foot_u_hi = foot[0], foot[1]
            host.foot_v_lo, host.foot_v_hi = foot[2], foot[3]
            scan = [(n, None) for n in near]
        else:
            if host.bbox is not None and not host.bbox.IsVoid():
                r = bbox_in_plane_range(host.bbox, host)
                host.foot_u_lo, host.foot_u_hi, host.foot_v_lo, host.foot_v_hi = r
            # Vectorised box offsets of every face; exact for a plane face and
            # the prefilter for anything else.
            if len(valid):
                ox, oy, oz = host.origin
                nx, ny, nz = host.normal
                s = (corners[:, :, 0] - ox) * nx + (corners[:, :, 1] - oy) * ny + (
                    corners[:, :, 2] - oz
                ) * nz
                smin_all = s.min(axis=1)
                smax_all = s.max(axis=1)
                inside = (smin_all >= -window) & (smax_all <= window)
            scan = []
            for k, node in enumerate(valid):
                if node.surface_type is SurfaceType.PLANE:
                    if not inside[k]:
                        continue
                    scan.append((node, (float(smin_all[k]), float(smax_all[k]))))
                else:
                    scan.append((node, None))

        hru = (host.foot_u_lo, host.foot_u_hi)
        hrv = (host.foot_v_lo, host.foot_v_hi)

        members = []
        for node, pre in scan:
            if node.face_id in host_faces or node.face_id in blocked:
                continue
            if node.bbox.IsVoid():
                continue
            if pre is not None:
                smin, smax = pre
            else:
                m = scene.measure(node, host)
                smin, smax = m[4], m[5]
                # A non-planar face is WALKED, not boxed: OCCT bounds a
                # B-spline by its control net, and an over-estimated reach
                # turns a floored engraving into a floorless one. The box is
                # kept only as the prefilter.
                if (
                    node.surface_type is not SurfaceType.PLANE
                    and smin <= smax
                    and smin >= -window
                    and smax <= window
                ):
                    ensure_samples(node)
                    m = scene.measure(node, host)
                    smin, smax = m[4], m[5]
            if smin > smax:
                continue
            if smin < -window or smax > window:
                continue
            if node.surface_type is SurfaceType.PLANE:
                # SIGNED: the slab is one-sided, so ink faces the way the host
                # faces (locally, on a cylinder). An anti-parallel face is the
                # far side of a wall -- a formed strip's recess ceiling was
                # read as a stroke floor and six formed strips came back as an
                # engraved legend.
                sd = dot(outward(node), host.normal_at(pnt(node.centroid)))
                a = abs(sd)
                if a < T.ALIGN_DOT and a > 1.0 - T.ALIGN_DOT:
                    continue
                if sd < -T.ALIGN_DOT:
                    continue
            members.append(node)
        if not members:
            continue

        idx = {m.face_id: i for i, m in enumerate(members)}
        dsu = DSU(len(members))
        for i, m in enumerate(members):
            for edge in graph.edges_of(m.face_id):
                j = idx.get(edge.other_face(m.face_id))
                if j is not None:
                    dsu.unite(i, j)
        comps: dict[int, list] = {}
        for i, m in enumerate(members):
            comps.setdefault(dsu.find(i), []).append(m)

        for root in sorted(comps):
            faces = comps[root]
            region = _build_region(
                scene, host, faces, ft, graph, priors, rejects, hru, hrv, trace
            )
            if region is None:
                continue
            region.index = len(scene.regions)
            scene.regions.append(region)
            scene.region_host.append(hi)
    return scene


def _build_region(scene, host, faces, ft, graph, priors, rejects, hru, hrv, trace):
    r = ReliefRegion(faces=list(faces))
    floors, prouds, islands, walls = [], [], [], []
    curved = 0
    lu = [1e18, -1e18]
    lv = [1e18, -1e18]
    reach = 0.0
    face_s: dict[int, tuple[float, float]] = {}
    for f in faces:
        m = scene.measure(f, host)
        lu[0] = min(lu[0], m[0])
        lu[1] = max(lu[1], m[1])
        lv[0] = min(lv[0], m[2])
        lv[1] = max(lv[1], m[3])
        smin, smax = m[4], m[5]
        if smin > smax:
            smin = smax = 0.0
        face_s[f.face_id] = (smin, smax)
        reach = max(reach, max(abs(smin), abs(smax)))
        if f.surface_type is not SurfaceType.PLANE:
            curved += 1
            continue
        a = dot(outward(f), host.normal_at(pnt(f.centroid)))
        if a > T.ALIGN_DOT:
            mid = 0.5 * (smin + smax)
            # At host level in the only two senses the pipeline recognises:
            # the grouping ejected it as an island of its own plane, or there
            # is no readable level difference at all.
            if f.face_id in host.level_face_ids or abs(mid) < T.NOISE_FLOOR_MM:
                islands.append(f)
            elif mid < 0.0:
                floors.append(f)
            else:
                prouds.append(f)
        else:
            walls.append(f)
    r.curved_faces = curved
    r.walls = walls
    r.reach_mm = reach
    r.extent_u_mm = max(0.0, lu[1] - lu[0])
    r.extent_v_mm = max(0.0, lv[1] - lv[0])
    r.diag_mm = math.sqrt(r.extent_u_mm * r.extent_u_mm + r.extent_v_mm * r.extent_v_mm)
    r.bbox_area_mm2 = max(r.extent_u_mm, 1e-6) * max(r.extent_v_mm, 1e-6)
    r.centroid_u = 0.5 * (lu[0] + lu[1])
    r.centroid_v = 0.5 * (lv[0] + lv[1])

    if floors and prouds:
        reject(rejects, "stage1", "mixed_sides",
               "component has both recessed and proud host-parallel faces",
               0.0, host.primary_face_id, faces)
        return None

    def level_of(subset):
        if not subset:
            return 0.0, 0.0
        lo, hi_, total = 1e18, -1e18, 0.0
        for f in subset:
            s0, s1 = face_s[f.face_id]
            mid = 0.5 * (s0 + s1)
            lo = min(lo, mid)
            hi_ = max(hi_, mid)
            total += mid
        return total / len(subset), hi_ - lo

    floor_mean, floor_spread = level_of(floors)
    proud_mean, proud_spread = level_of(prouds)
    floor_area = 0.0
    for f in floors:
        floor_area += f.area
    island_area = 0.0
    for f in islands:
        island_area += f.area
    floor_fill = floor_area / r.bbox_area_mm2

    # A cleared field is wider than one cutter pass, so the ISLAND reading
    # (the floor is a field, the host-level islands are the letters) has to be
    # tested on the floor: a seven-segment 8 encloses two counters and fills
    # half its box, which is everything else the reading asks for. Where the
    # component could be ONE glyph (two islands at most, the most a Latin
    # letterform encloses) the yardstick is that glyph, against the same
    # stroke/cap ratio Stage 3 uses.
    floor_width = union_width(floors, ft, graph)
    glyph_extent = max(r.extent_u_mm, r.extent_v_mm)
    one_stroke = (
        len(islands) <= 2
        and glyph_extent > 1e-6
        and floor_width <= T.STROKE_CAP_MAX * glyph_extent
    )
    floor_is_a_field = floor_width > T.MAX_STROKE_WIDTH_MM and not one_stroke

    if prouds:
        r.ink = prouds
        r.ink_kind = InkKind.PROUD_TOP
        r.proud = True
        r.level_mean = proud_mean
        r.level_spread_mm = proud_spread
        r.depth_mm = abs(proud_mean)
    elif (
        floors
        and len(islands) >= 2
        and island_area < floor_area
        and floor_fill > 0.5
        and floor_is_a_field
    ):
        r.ink = islands
        r.ink_kind = InkKind.ISLAND
        r.proud = True
        r.level_mean = floor_mean
        r.level_spread_mm = floor_spread
        r.depth_mm = abs(floor_mean)
    elif floors:
        r.ink = floors
        r.ink_kind = InkKind.FLOOR
        r.proud = False
        r.level_mean = floor_mean
        r.level_spread_mm = floor_spread
        r.depth_mm = abs(floor_mean)
    elif len(walls) >= 2:
        r.ink_kind = InkKind.WALL_GAP
        r.proud = False
        r.depth_mm = reach
        r.level_mean = -reach
        r.level_spread_mm = 0.0
    else:
        reject(rejects, "stage1", "no_ink",
               "no host-parallel face and fewer than two walls", 0.0,
               host.primary_face_id, faces)
        return None

    if trace:
        print(
            f"[mark] ink kind={r.ink_kind.value} floors={len(floors)} "
            f"islands={len(islands)} prouds={len(prouds)} walls={len(walls)} "
            f"floor_fill={floor_fill:.4g} floor_w={floor_width:.4g} "
            f"field={int(floor_is_a_field)} hostkind={host.kind.value} "
            f"host={host.primary_face_id}"
        )

    # Is the flat actually the BOTTOM? A component that reaches meaningfully
    # deeper than its flat is drafted or V-cut; reclassify rather than drop.
    if r.ink_kind is InkKind.FLOOR:
        tol = max(3.0 * T.NOISE_FLOOR_MM, T.DEPTH_CV_MAX * r.depth_mm)
        if reach > r.depth_mm + tol and len(walls) >= 2:
            r.ink = []
            r.ink_kind = InkKind.WALL_GAP
            r.depth_mm = reach
            r.level_mean = -reach
            r.level_spread_mm = 0.0

    # The support surface has to carry the ink. Denied a real host, a mark's
    # own glyph floors get promoted to hosts and the neighbouring glyphs read
    # proud of them -- an engraving came back raised.
    acc = list(EMPTY)
    for f in (r.ink if r.ink else r.faces):
        merge(acc, scene.measure(f, host))
    iru, irv = (acc[0], acc[1]), (acc[2], acc[3])
    if iru[1] < iru[0] or hru[1] < hru[0]:
        r.host_coverage = 1.0
    else:
        r.host_coverage = covered_fraction(iru, irv, hru, hrv)
    if r.host_coverage < T.INK_WITHIN_HOST_MIN:
        reject(rejects, "stage1", "ink_outruns_its_host",
               f"only {r.host_coverage:.6f} of the ink's footprint lies over the "
               "host surface claiming to carry it",
               T.INK_WITHIN_HOST_MIN - r.host_coverage, host.primary_face_id, faces)
        return None

    if r.depth_mm < T.NOISE_FLOOR_MM:
        reject(rejects, "stage1", "noise_floor", "relief depth below B-rep noise floor",
               T.NOISE_FLOOR_MM - r.depth_mm, host.primary_face_id, faces)
        return None
    if r.depth_mm > T.MAX_MARK_DEPTH_MM:
        reject(rejects, "stage1", "deeper_than_marking_process",
               f"depth {r.depth_mm:.6f} mm exceeds the marking-process ceiling "
               f"{T.MAX_MARK_DEPTH_MM:.6f} mm",
               r.depth_mm - T.MAX_MARK_DEPTH_MM, host.primary_face_id, faces)
        return None
    if host.thickness_resolved and r.depth_mm > T.DEPTH_THICKNESS_MAX * host.local_thickness_mm:
        reject(rejects, "stage1", "depth_vs_thickness",
               f"depth {r.depth_mm:.6f} mm exceeds {T.DEPTH_THICKNESS_MAX} x local "
               f"thickness {host.local_thickness_mm:.6f} mm",
               r.depth_mm - T.DEPTH_THICKNESS_MAX * host.local_thickness_mm,
               host.primary_face_id, faces)
        return None

    if r.ink_kind is InkKind.WALL_GAP:
        r.stroke_w_mm = opposing_wall_gap(walls, host)
        r.max_patch_w_mm = r.stroke_w_mm
        # No ink face exists, so the footprint is rebuilt from each wall's
        # in-plane run: a stroke's two long walls are what its floor's outer
        # perimeter would have been.
        run = 0.0
        for f in walls:
            m = scene.measure(f, host)
            eu = max(0.0, m[1] - m[0])
            ev = max(0.0, m[3] - m[2])
            run += math.sqrt(eu * eu + ev * ev)
        r.ink_perimeter_mm = max(run, 1e-6)
        r.ink_area_mm2 = 0.5 * run * r.stroke_w_mm
    else:
        for f in r.ink:
            r.hole_count += f.inner_loop_count
            # The WIDEST SINGLE patch: a union mean averages a too-wide patch
            # away, and the process ceiling has to bite on the patch.
            fp = ft.perimeter(f)
            if fp > 1e-9:
                r.max_patch_w_mm = max(r.max_patch_w_mm, 2.0 * f.area / fp)
        r.ink_area_mm2, r.ink_perimeter_mm = union_area_perimeter(r.ink, ft, graph)
        r.stroke_w_mm = 2.0 * r.ink_area_mm2 / r.ink_perimeter_mm

    if r.stroke_w_mm <= 1e-6:
        reject(rejects, "stage1", "stroke_width_unmeasurable",
               "2A/P (or the opposing-wall gap) came out non-positive", 0.0,
               host.primary_face_id, faces)
        return None

    r.fill = T.clamp01(r.ink_area_mm2 / r.bbox_area_mm2)
    r.slenderness = r.diag_mm / r.stroke_w_mm

    if priors is not None:
        for f in (r.ink if r.ink else r.faces):
            if f.face_id in priors.pmi_referenced_faces:
                r.ink_carries_pmi = True
            style = priors.face_style.get(f.face_id)
            if style is not None and style in priors.rare_styles:
                r.distinct_colour = True
    return r
