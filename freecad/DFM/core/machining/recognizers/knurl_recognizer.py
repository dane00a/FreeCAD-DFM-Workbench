# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""KNURL: a dense periodic field of SHALLOW grooves over a BOUNDED patch.

A modelled knurl is hundreds of tiny faces, and every general recognizer reads
them as its own kind of thing: a 40 x 16 mm diamond knurl came back as 38
slots carrying 45 sharp-internal-edge warnings. None of that is wrong about
the B-rep and all of it is wrong about the part -- nobody end-mills a knurl.

What this recognizer refuses to do: derive a SPECIFICATION. There is no
module, no DIN 82 designation, no TPI and no tooth form in anything it emits.
A knurl is specified by a callout; the B-rep carries the PATTERN, so the
pattern is all that is measured, and ``spec_determinable`` says so. A file
note naming a knurl is recorded verbatim as ``callout``, never parsed.

The signature: a knurl is the one thing that SHATTERS A SURFACE LEVEL. Cut a
crossed groove network into a face and the face survives only as a lattice
of tiny coplanar (or co-cylindrical) islands. That is the seed; the rest is
measurement:

1. LEVEL     a plane, or an OD cylinder unrolled to (axial, arc length),
             holding >= 16 islands of small MINOR in-chart extent (PCA over
             the face's vertices -- an axis-aligned box cannot measure a strip
             at 45 degrees).
2. WALLS     transverse neighbours of the islands that span no more than the
             groove; a face reaching far past it is a face of the PART.
3. FLOOR     the companion level one hop on, at a consistent shallow depth --
             measured PER PATCH, below the connectivity split.
4. FAMILIES  wall directions folded mod 180; one or two families holding
             70 % of the wall area, each with enough walls to carry six lines.
5. PITCH     per family, flank positions clustered into groove lines; the
             spacing must be periodic.

ONE family of shallow periodic grooves is not distinguishable from a
functional groove array on a flat face, so it is a knurl only when it closes
a cylindrical OD (``wrap_deg``, the true azimuth span, at least 280 degrees)
or a note names a knurl. Two crossed families are a knurl unconditionally.

Runs LAST of the geometric recognizers, after marking (whose faces never seed
a knurl), and :func:`drop_knurl_absorbed_features` then retires whatever the
others claimed inside the patch -- a blast radius of exactly the parts that
have a knurl.
"""

from __future__ import annotations

import math
import os
from typing import Optional, Sequence

from OCP.BRep import BRep_Tool
from OCP.TopAbs import TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from ...utils.geometry import FaceIndex
from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer

# -- Gates: each decides whether the geometry IS a knurl -----------------------
MIN_CELLS = 16
MAX_CELL_MINOR_MM = 4.0
MIN_COMPANIONS = 8
MIN_DEPTH_MM = 0.03
MAX_DEPTH_MM = 1.0
MIN_PITCH_MM = 0.08
MAX_PITCH_MM = 4.0
MAX_DEPTH_PITCH_RATIO = 0.6   # a fin field is far above this
MAX_PITCH_CV = 0.35
MIN_GROOVE_LINES = 6
FAMILY_MERGE_DEG = 8.0
MIN_FAMILY_SHARE = 0.20
MIN_FAMILY_COVERAGE = 0.70
# A flank runs crest to floor, so its extent along the level normal IS the
# depth; the factor is slack for boolean seams and planar flanks on a
# cylindrical chart.
WALL_HEIGHT_FACTOR = 3.0
WALL_HEIGHT_PAD_MM = 0.1
# A cluster too small to hold MIN_GROOVE_LINES lines is not a family: it is
# guaranteed to fail the periodicity test applied to every kept family.
MIN_FAMILY_WALLS = MIN_GROOVE_LINES
DEPTH_CONSIST_FRAC = 0.70
DEPTH_TOL_FRAC = 0.25
TRANSVERSE_DOT = 0.30
# How much of the OD a single family must span to corroborate a straight
# knurl, against the TRUE azimuth span. 280 rather than 300 so an ordinary
# knurled-and-flatted shaft is recognised (a flat at distance a from an axis
# of radius r removes 2*acos(a/r)): it admits flats up to about 1.6 mm on a
# 14 mm diameter. The pitch-CV gate already enforces a floor near 278 on any
# interrupted patch, and no value admits a double-D, whose second flat severs
# the texture into two separately measured components.
WRAP_CLOSED_DEG = 280.0
AXIAL_FAMILY_DEG = 20.0

# Faces another recognizer already explained never seed a knurl. Slot, step
# and pocket are deliberately absent: those are the claims a knurl replaces.
_BLOCKING_TYPES = frozenset({
    FeatureType.MARKING_TEXT, FeatureType.THREADED_HOLE, FeatureType.EXTERNAL_THREAD,
    FeatureType.THREAD_RELIEF_GROOVE, FeatureType.GROOVE, FeatureType.O_RING_GLAND,
    FeatureType.RETAINING_RING_GROOVE, FeatureType.THROUGH_HOLE, FeatureType.BLIND_HOLE,
    FeatureType.BREAKTHROUGH_HOLE, FeatureType.PARTIAL_BORE, FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK, FeatureType.FILLET, FeatureType.CHAMFER, FeatureType.BEND,
    FeatureType.SHEET_FORMED, FeatureType.TAB, FeatureType.NOTCH,
})


def _trace() -> bool:
    return os.environ.get("DFM_KNURL_TRACE") is not None


def _median(values) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    m = len(v) // 2
    return v[m] if len(v) % 2 else 0.5 * (v[m - 1] + v[m])


def _llround(x: float) -> int:
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _fold180(deg: float) -> float:
    while deg < 0.0:
        deg += 180.0
    while deg >= 180.0:
        deg -= 180.0
    return deg


def _angle_sep(a: float, b: float) -> float:
    d = abs(_fold180(a) - _fold180(b))
    return min(d, 180.0 - d)


def _max_flank_height(depth: float) -> float:
    return WALL_HEIGHT_FACTOR * depth + WALL_HEIGHT_PAD_MM


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    return (a[0] / n, a[1] / n, a[2] / n)


def _t(p):
    return (p.X(), p.Y(), p.Z())


def note_names_knurl(text: str) -> bool:
    """Did the designer say knurl -- a NAME test, not a spec parser."""
    u = (text or "").upper()
    return "KNURL" in u or "SERRAT" in u


class _Chart:
    """A surface level made measurable: height off it, and position on it.
    On a cylinder v is ARC LENGTH, so pitch comes out in millimetres -- what
    a knurl on a turned OD is quoted in -- and wraps at the period."""

    def __init__(self) -> None:
        self.cyl = False
        self.n = (0.0, 0.0, 1.0)
        self.d = 0.0
        self.e1 = (1.0, 0.0, 0.0)
        self.e2 = (0.0, 1.0, 0.0)
        self.loc = (0.0, 0.0, 0.0)
        self.ad = (0.0, 0.0, 1.0)
        self.radius = 0.0

    def _radial(self, p):
        w = (p[0] - self.loc[0], p[1] - self.loc[1], p[2] - self.loc[2])
        a = _dot(w, self.ad)
        return w, a, (w[0] - self.ad[0] * a, w[1] - self.ad[1] * a, w[2] - self.ad[2] * a)

    def height(self, p) -> float:
        if not self.cyl:
            return _dot(self.n, p) - self.d
        _, _, radial = self._radial(p)
        return _norm(radial) - self.radius

    def uv(self, p):
        if not self.cyl:
            return _dot(p, self.e1), _dot(p, self.e2)
        w, a, _ = self._radial(p)
        return a, self.radius * math.atan2(_dot(w, self.e2), _dot(w, self.e1))

    def v_period(self) -> float:
        return 2.0 * math.pi * self.radius if self.cyl else 0.0

    def level_normal(self, p):
        if not self.cyl:
            return self.n
        _, _, radial = self._radial(p)
        if _norm(radial) < 1.0e-9:
            return self.ad
        return _unit(radial)

    def tangents(self, p):
        if not self.cyl:
            return self.e1, self.e2
        r = self.level_normal(p)
        return self.ad, _unit(_cross(self.ad, r))


def _principal_minor(pts) -> float:
    """Minor extent of a 2D point set along its principal axes."""
    if len(pts) < 2:
        return 0.0
    mu = sum(p[0] for p in pts) / len(pts)
    mv = sum(p[1] for p in pts) / len(pts)
    suu = svv = suv = 0.0
    for p in pts:
        a = p[0] - mu
        b = p[1] - mv
        suu += a * a
        svv += b * b
        suv += a * b
    theta = 0.5 * math.atan2(2.0 * suv, suu - svv)
    c, s = math.cos(theta), math.sin(theta)
    lo1 = lo2 = 1e30
    hi1 = hi2 = -1e30
    for p in pts:
        a = p[0] - mu
        b = p[1] - mv
        x = c * a + s * b
        y = -s * a + c * b
        lo1 = min(lo1, x)
        hi1 = max(hi1, x)
        lo2 = min(lo2, y)
        hi2 = max(hi2, y)
    return min(hi1 - lo1, hi2 - lo2)


def pitch_from_positions(s, period):
    """(pitch, cv, lines, line_width) from one family's flank positions, or
    None. Positions arrive as flank PAIRS a groove width apart, so the cluster
    tolerance comes from the largest internal jump between the gap modes --
    parity-free -- not from a median that flips mode on a parity change. A
    circular axis is rotated so its widest gap becomes the seam."""
    if len(s) < 4:
        return None
    s = sorted(s)
    if period > 0.0:
        cut = 0
        widest = period - (s[-1] - s[0])
        for i in range(1, len(s)):
            g = s[i] - s[i - 1]
            if g > widest:
                widest = g
                cut = i
        if cut > 0:
            s = s[cut:] + [x + period for x in s[:cut]]
    nz = [s[i] - s[i - 1] for i in range(1, len(s)) if s[i] - s[i - 1] > 0.005]
    if len(nz) < 3:
        return None
    sorted_nz = sorted(nz)
    eps = 0.5 * _median(nz)
    best_jump = 0.0
    lo = hi = 0.0
    for i in range(1, len(sorted_nz)):
        j = sorted_nz[i] - sorted_nz[i - 1]
        if j > best_jump:
            best_jump = j
            lo = sorted_nz[i - 1]
            hi = sorted_nz[i]
    if lo > 0.0 and hi > 2.5 * lo:
        eps = 0.5 * (lo + hi)
    if eps <= 0.0:
        return None
    centres, widths = [], []
    start = 0
    for i in range(1, len(s) + 1):
        if i != len(s) and not (s[i] - s[i - 1] > eps):
            continue
        acc = 0.0
        for k in range(start, i):
            acc += s[k]
        centres.append(acc / (i - start))
        widths.append(s[i - 1] - s[start])
        start = i
    if len(centres) < MIN_GROOVE_LINES:
        return None
    spacing = [centres[i] - centres[i - 1] for i in range(1, len(centres))]
    if period > 0.0:
        wrap = period - (centres[-1] - centres[0])
        if wrap > 0.0:
            spacing.append(wrap)
    if not spacing:
        return None
    pitch = _median(spacing)
    if pitch <= 0.0:
        return None
    acc = 0.0
    for x in spacing:
        acc += abs(x - pitch)
    cv = (acc / len(spacing)) / pitch
    return pitch, cv, len(centres), _median(widths)


def azimuth_span_deg(iv, period) -> float:
    """How much of the circumference the patch OCCUPIES: 360 less the widest
    arc nothing covers. A land, its flanks and the floor beside it are
    contiguous, so an uninterrupted band reads 360 at any pitch and an
    interruption reads its own angular width."""
    if not iv or period <= 0.0:
        return 0.0
    arcs = []
    for lo, hi in iv:
        length = hi - lo
        if length <= 0.0:
            continue
        if length >= period:
            return 360.0
        a = math.fmod(lo, period)
        if a < 0.0:
            a += period
        b = a + length
        if b <= period:
            arcs.append((a, b))
        else:
            arcs.append((a, period))
            arcs.append((0.0, b - period))
    if not arcs:
        return 0.0
    arcs.sort()
    merged = []
    for a in arcs:
        if merged and a[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], a[1]))
        else:
            merged.append(a)
    bare = period - (merged[-1][1] - merged[0][0])
    for i in range(1, len(merged)):
        bare = max(bare, merged[i][0] - merged[i - 1][1])
    bare = max(bare, 0.0)
    return 360.0 * (1.0 - bare / period)


class _Wall:
    __slots__ = ("node", "angle_deg", "classified", "area", "u", "v")

    def __init__(self, node):
        self.node = node
        self.angle_deg = 0.0
        self.classified = False
        self.area = node.area
        self.u = 0.0
        self.v = 0.0


class _Cell:
    __slots__ = ("node", "u", "v", "minor")

    def __init__(self, node, minor, u, v):
        self.node = node
        self.minor = minor
        self.u = u
        self.v = v


class KnurlRecognizer(FeatureRecognizer):
    """Recognizes knurled patches on flat faces and turned ODs."""

    prefix = "kn"

    #: File notes (objects with ``text``), when the file side supplies any:
    #: the only authority for a knurl specification. None here by default.
    notes = None

    @property
    def name(self) -> str:
        return "Knurl Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        nodes = graph.nodes
        if len(nodes) < MIN_CELLS + 2 or shape is None:
            return []
        trace = _trace()
        callout = ""
        for note in self.notes or ():
            text = getattr(note, "text", "") or ""
            if note_names_knurl(text):
                callout = text
                break
        blocked: set[int] = set()
        for f in prior or ():
            if f.type in _BLOCKING_TYPES:
                blocked.update(f.faces)

        index = FaceIndex(shape)
        verts: dict[int, list] = {}

        def face_points(n: AagNode):
            pts = verts.get(n.face_id)
            if pts is None:
                pts = []
                if 1 <= n.face_id <= len(index):
                    ex = TopExp_Explorer(index.face_at(n.face_id), TopAbs_VERTEX)
                    while ex.More():
                        pts.append(_t(BRep_Tool.Pnt_s(TopoDS.Vertex_s(ex.Current()))))
                        ex.Next()
                verts[n.face_id] = pts
            return pts

        def minor_extent(chart: _Chart, n: AagNode) -> float:
            pts = face_points(n)
            if len(pts) < 3:
                return -1.0
            per = chart.v_period()
            _, v0 = chart.uv(pts[0])
            uv = []
            for p in pts:
                u, v = chart.uv(p)
                if per > 0.0:
                    while v - v0 > 0.5 * per:
                        v -= per
                    while v - v0 < -0.5 * per:
                        v += per
                uv.append((u, v))
            return _principal_minor(uv)

        def height_extent(chart: _Chart, n: AagNode) -> float:
            pts = face_points(n)
            if len(pts) < 2:
                return -1.0
            hs = [chart.height(p) for p in pts]
            return max(hs) - min(hs)

        def outward(n: AagNode):
            d = _t(n.plane_normal)
            return (-d[0], -d[1], -d[2]) if n.is_reversed else d

        levels: dict[str, list] = {}
        charts: dict[str, _Chart] = {}
        for n in nodes:
            if n.surface_type is SurfaceType.PLANE and n.plane_normal is not None:
                nd = outward(n)
                d = _dot(nd, _t(n.centroid))
                key = "P|%d|%d|%d|%d" % (
                    _llround(nd[0] / 1e-4), _llround(nd[1] / 1e-4),
                    _llround(nd[2] / 1e-4), _llround(d / 1e-3))
                if key not in charts:
                    c = _Chart()
                    c.n = nd
                    c.d = d
                    ref = (0.0, 0.0, 1.0) if abs(nd[2]) < 0.9 else (1.0, 0.0, 0.0)
                    c.e1 = _unit(_cross(nd, ref))
                    c.e2 = _unit(_cross(nd, c.e1))
                    charts[key] = c
                levels.setdefault(key, []).append(n)
            elif (n.surface_type is SurfaceType.CYLINDER and n.cyl_radius > 1.0e-6
                  and n.cyl_cone_axis is not None):
                # A knurl lives on an OD; a reversed cylinder is a bore.
                if n.is_reversed:
                    continue
                cad = _t(n.cyl_cone_axis.Direction())
                ssum = cad[0] + cad[1] + cad[2]
                if ssum < 0.0 or (abs(ssum) < 1e-12 and cad[2] < 0.0):
                    cad = (-cad[0], -cad[1], -cad[2])
                ow = _t(n.cyl_cone_axis.Location())
                a = _dot(ow, cad)
                foot = (ow[0] - cad[0] * a, ow[1] - cad[1] * a, ow[2] - cad[2] * a)
                key = "C|%d|%d|%d|%d|%d|%d|%d" % (
                    _llround(cad[0] / 1e-4), _llround(cad[1] / 1e-4), _llround(cad[2] / 1e-4),
                    _llround(foot[0] / 1e-3), _llround(foot[1] / 1e-3), _llround(foot[2] / 1e-3),
                    _llround(n.cyl_radius / 1e-3))
                if key not in charts:
                    c = _Chart()
                    c.cyl = True
                    c.loc = foot
                    c.ad = cad
                    c.radius = n.cyl_radius
                    ref = (0.0, 0.0, 1.0) if abs(cad[2]) < 0.9 else (1.0, 0.0, 0.0)
                    c.e1 = _unit(_cross(cad, ref))
                    c.e2 = _unit(_cross(cad, c.e1))
                    charts[key] = c
                levels.setdefault(key, []).append(n)

        cands: list[FeatureInstance] = []
        for key in sorted(levels):
            members = levels[key]
            if len(members) < MIN_CELLS:
                continue
            chart = charts[key]
            cells: dict[int, _Cell] = {}
            for n in members:
                if n.face_id in blocked or n.area <= 0.0:
                    continue
                # No area cap: a straight knurl's lands are long strips. The
                # MINOR extent is the test that means something.
                minor = minor_extent(chart, n)
                if minor < 0.0 or minor > MAX_CELL_MINOR_MM:
                    continue
                u, v = chart.uv(_t(n.centroid))
                cells[n.face_id] = _Cell(n, minor, u, v)
            if trace:
                print(f"[knurl] level {key}: {len(members)} members, {len(cells)} cells")
            if len(cells) < MIN_CELLS:
                continue

            # Walls: transverse, non-tangent neighbours of the cells, spanning
            # no more than any knurl could.
            walls: dict[int, _Wall] = {}
            for cid in cells:
                for edge in graph.edges_of(cid):
                    if edge.is_tangent:
                        continue
                    oid = edge.other_face(cid)
                    if oid in cells or oid in walls or oid in blocked:
                        continue
                    if not graph.has_node(oid):
                        continue
                    w = graph.node(oid)
                    if height_extent(chart, w) > _max_flank_height(MAX_DEPTH_MM):
                        continue
                    ln = chart.level_normal(_t(w.centroid))
                    wl = _Wall(w)
                    wl.u, wl.v = chart.uv(_t(w.centroid))
                    if w.surface_type is SurfaceType.PLANE and w.plane_normal is not None:
                        wn = outward(w)
                        if abs(_dot(ln, wn)) > TRANSVERSE_DOT:
                            continue
                        t = _cross(ln, wn)
                        if _norm(t) > 1.0e-9:
                            tu, tv = chart.tangents(_t(w.centroid))
                            wl.angle_deg = _fold180(
                                math.atan2(_dot(t, tv), _dot(t, tu)) * 180.0 / math.pi)
                            wl.classified = True
                    walls[oid] = wl
            if not walls:
                continue

            # The companion level, one hop on, ABOVE or BELOW: which level a
            # knurl shatters depends on the knurl.
            floors: dict[int, AagNode] = {}
            for wid in walls:
                for edge in graph.edges_of(wid):
                    oid = edge.other_face(wid)
                    if oid in cells or oid in walls or oid in floors:
                        continue
                    # A face another recognizer explained is no floor of this
                    # knurl: it poisoned the depth median once.
                    if oid in blocked or not graph.has_node(oid):
                        continue
                    f = graph.node(oid)
                    a = abs(chart.height(_t(f.centroid)))
                    if a < MIN_DEPTH_MM or a > MAX_DEPTH_MM:
                        continue
                    # The host face is not part of the knurl.
                    fm = minor_extent(chart, f)
                    if fm < 0.0 or fm > MAX_CELL_MINOR_MM:
                        continue
                    floors[oid] = f
            if len(floors) < MIN_COMPANIONS:
                if trace:
                    print(f"[knurl] {key}: only {len(floors)} companion face(s) on the "
                          f"whole level, need {MIN_COMPANIONS}")
                continue

            def split_components(ids):
                member = set(ids)
                seen: set[int] = set()
                comps = []
                for seed in sorted(ids):
                    if seed in seen:
                        continue
                    comp = []
                    stack = [seed]
                    seen.add(seed)
                    while stack:
                        cur = stack.pop()
                        comp.append(cur)
                        for edge in graph.edges_of(cur):
                            oid = edge.other_face(cur)
                            if oid not in member or oid in seen:
                                continue
                            seen.add(oid)
                            stack.append(oid)
                    comps.append(comp)
                return comps

            queue = split_components(list(cells) + list(walls) + list(floors))
            while queue:
                ids = queue.pop()
                p_cells = [cells[i] for i in ids if i in cells]
                p_walls = [walls[i] for i in ids if i not in cells and i in walls]
                p_floors = [floors[i] for i in ids
                            if i not in cells and i not in walls and i in floors]
                # Two halves of one land are one cell, for counting only. The
                # kernel cuts a closed surface at its parametric seam, so a
                # crest land the seam crosses can arrive as two faces --
                # joined by the seam itself, or, after a boolean, by an
                # ordinary edge between two faces cut from one cylinder
                # (is_surface_continuation covers both). Nothing about the
                # knurl differs, only where the modeller's X axis pointed,
                # and the MIN_CELLS gate counts these. The split faces stay
                # in p_cells for the measurements: wrap is measured from cell
                # centroids, and dropping one half of a land that straddles
                # the seam drops its azimuth too. Two separate lands never
                # share an edge -- a groove lies between them.
                distinct_cells = len(p_cells)
                cell_ids = sorted({c.node.face_id for c in p_cells})
                cell_set = set(cell_ids)
                twin: set[int] = set()
                for cid in cell_ids:
                    if cid in twin:
                        continue
                    for edge in graph.edges_of(cid):
                        if not edge.is_surface_continuation:
                            continue
                        other = edge.other_face(cid)
                        if other == cid or other not in cell_set:
                            continue
                        twin.add(other)
                if twin:
                    distinct_cells -= len(twin)
                    if trace:
                        print(f"[knurl]   {len(twin)} split land half/halves: "
                              f"{len(p_cells)} faces -> {distinct_cells} cells")
                if trace:
                    print(f"[knurl]   comp {len(ids)} faces: {len(p_cells)} cells, "
                          f"{len(p_walls)} walls, {len(p_floors)} companions")
                if distinct_cells < MIN_CELLS:
                    continue
                if len(p_floors) < MIN_COMPANIONS:
                    continue

                # Depth over THIS patch: one poisoned patch used to take the
                # whole cylinder down with it.
                up = sum(1 for f in p_floors if chart.height(_t(f.centroid)) > 0.0)
                down = len(p_floors) - up
                sign = 1.0 if up >= down else -1.0
                kept_floors, depths = [], []
                for f in p_floors:
                    hgt = chart.height(_t(f.centroid))
                    if hgt * sign <= 0.0:
                        continue
                    kept_floors.append(f)
                    depths.append(abs(hgt))
                if len(depths) < MIN_COMPANIONS:
                    continue
                depth = _median(depths)
                if depth < MIN_DEPTH_MM or depth > MAX_DEPTH_MM:
                    continue
                consistent = sum(1 for x in depths if abs(x - depth) <= DEPTH_TOL_FRAC * depth)
                if consistent / len(depths) < DEPTH_CONSIST_FRAC:
                    if trace:
                        print(f"[knurl] {key}: patch depth inconsistent "
                              f"({consistent}/{len(depths)} at {depth} mm)")
                    continue

                # A wall is a groove FLANK, and now the depth says how tall.
                max_h = _max_flank_height(depth)
                kept_walls = []
                dropped = 0
                for wl in p_walls:
                    if height_extent(chart, wl.node) > max_h:
                        dropped += 1
                        continue
                    kept_walls.append(wl)
                removed = (len(p_floors) - len(kept_floors)) + dropped
                if removed > 0:
                    surv = ([c.node.face_id for c in p_cells]
                            + [w.node.face_id for w in kept_walls]
                            + [f.face_id for f in kept_floors])
                    queue.extend(split_components(surv))
                    continue
                p_walls = kept_walls
                p_floors = kept_floors
                if len(p_walls) < 2 or not p_floors:
                    continue

                wall_area_total = sum(w.area for w in p_walls)
                if wall_area_total <= 0.0:
                    continue
                pool = [w for w in p_walls if w.classified]
                fams = []
                for _pass in range(3):
                    if not pool:
                        break
                    best_angle, best_area = 0.0, -1.0
                    for a in pool:
                        acc = 0.0
                        for b in pool:
                            if _angle_sep(a.angle_deg, b.angle_deg) <= FAMILY_MERGE_DEG:
                                acc += b.area
                        if acc > best_area:
                            best_area = acc
                            best_angle = a.angle_deg
                    fam_walls, rest = [], []
                    fam_area = wsum = asum = 0.0
                    for b in pool:
                        if _angle_sep(best_angle, b.angle_deg) <= FAMILY_MERGE_DEG:
                            fam_walls.append(b)
                            fam_area += b.area
                            delta = _fold180(b.angle_deg) - _fold180(best_angle)
                            if delta > 90.0:
                                delta -= 180.0
                            if delta < -90.0:
                                delta += 180.0
                            wsum += b.area
                            asum += b.area * delta
                        else:
                            rest.append(b)
                    angle = _fold180(best_angle + (asum / wsum if wsum > 0.0 else 0.0))
                    fams.append((angle, fam_area, fam_walls))
                    pool = rest
                fams.sort(key=lambda f: -f[1])

                keep = []
                for f in fams:
                    if f[1] / wall_area_total >= MIN_FAMILY_SHARE and len(f[2]) >= MIN_FAMILY_WALLS:
                        keep.append(f)
                    if len(keep) == 2:
                        break
                if not keep:
                    continue
                coverage = sum(f[1] for f in keep) / wall_area_total
                if coverage < MIN_FAMILY_COVERAGE:
                    if trace:
                        print(f"[knurl] {key}: family coverage {coverage} < {MIN_FAMILY_COVERAGE}")
                    continue
                strong = sum(1 for f in fams
                             if f[1] / wall_area_total >= MIN_FAMILY_SHARE
                             and len(f[2]) >= MIN_FAMILY_WALLS)
                if strong > 2:
                    continue

                # Pitch on each family's own FLANK WALLS, projected on its
                # normal; circumferential (and circular) on a cylinder.
                per_pitch, per_cv, per_width = [], [], []
                lines = 0
                for angle, _area, fam_walls in keep:
                    th = angle * math.pi / 180.0
                    px, py = -math.sin(th), math.cos(th)
                    period = 0.0
                    if chart.cyl:
                        px, py = 0.0, 1.0
                        period = chart.v_period()
                    res = pitch_from_positions([px * w.u + py * w.v for w in fam_walls], period)
                    if trace:
                        print(f"[knurl]   family {angle} deg, {len(fam_walls)} walls: {res}")
                    if res is not None:
                        per_pitch.append(res[0])
                        per_cv.append(res[1])
                        per_width.append(res[3])
                        lines += res[2]
                if not per_pitch or len(per_pitch) != len(keep):
                    continue
                pitch = _median(per_pitch)
                cv = max(per_cv)
                groove_width = _median(per_width)
                if pitch < MIN_PITCH_MM or pitch > MAX_PITCH_MM:
                    continue
                if cv > MAX_PITCH_CV:
                    if trace:
                        print(f"[knurl] {key}: pitch CV {cv} > {MAX_PITCH_CV}")
                    continue
                if depth / pitch > MAX_DEPTH_PITCH_RATIO:
                    continue

                subtype = "diamond" if len(keep) == 2 else "straight"
                wrap_deg = 0.0
                if chart.cyl:
                    per = chart.v_period()
                    arcs = []
                    for node in ([c.node for c in p_cells] + [w.node for w in p_walls]
                                 + list(p_floors)):
                        pts = face_points(node)
                        if len(pts) < 2:
                            continue
                        _, v0 = chart.uv(pts[0])
                        lo = hi = v0
                        for p in pts:
                            _, v = chart.uv(p)
                            while v - v0 > 0.5 * per:
                                v -= per
                            while v - v0 < -0.5 * per:
                                v += per
                            lo = min(lo, v)
                            hi = max(hi, v)
                        if hi > lo:
                            arcs.append((lo, hi))
                    wrap_deg = azimuth_span_deg(arcs, per)
                if len(keep) == 1:
                    closed_od = chart.cyl and wrap_deg >= WRAP_CLOSED_DEG
                    if not closed_od and not callout:
                        if trace:
                            print(f"[knurl] {key}: ONE groove family, host "
                                  f"{'cylindrical' if chart.cyl else 'planar'}, wrap "
                                  f"{wrap_deg} deg, no knurl callout -- REJECTED")
                        continue

                claimed_ids = sorted(
                    {c.node.face_id for c in p_cells}
                    | {w.node.face_id for w in p_walls}
                    | {f.face_id for f in p_floors}
                )
                u_lo = v_lo = 1e30
                u_hi = v_hi = -1e30
                v_ref = p_cells[0].v
                per = chart.v_period()
                for fid in claimed_ids:
                    for p in face_points(graph.node(fid)):
                        u, v = chart.uv(p)
                        if per > 0.0:
                            while v - v_ref > 0.5 * per:
                                v -= per
                            while v - v_ref < -0.5 * per:
                                v += per
                        u_lo = min(u_lo, u)
                        u_hi = max(u_hi, u)
                        v_lo = min(v_lo, v)
                        v_hi = max(v_hi, v)
                patch_u = u_hi - u_lo if u_hi > u_lo else 0.0
                patch_v = v_hi - v_lo if v_hi > v_lo else 0.0
                if chart.cyl and wrap_deg >= WRAP_CLOSED_DEG:
                    patch_v = chart.v_period()

                params = {
                    # `subtype` is a baseline discriminant; nothing else here is.
                    "subtype": subtype,
                    "host": "cylindrical" if chart.cyl else "planar",
                    "families": len(keep),
                    "cell_count": distinct_cells,
                    "groove_count": lines,
                    "pitch_mm": pitch,
                    "pitch_cv": cv,
                    "groove_depth_mm": depth,
                    "patch_extent_mm": [patch_u, patch_v],
                    "patch_area_mm2": patch_u * patch_v,
                }
                if groove_width > 0.0:
                    params["groove_width_mm"] = groove_width
                if len(keep) == 2:
                    # The MEASURED crossing angle, not a tooth-form angle.
                    params["included_angle_deg"] = _angle_sep(keep[0][0], keep[1][0])
                if chart.cyl:
                    params["diameter_at_crest_mm"] = 2.0 * chart.radius
                    params["wrap_deg"] = wrap_deg
                    off_axis = _angle_sep(keep[0][0], 0.0)
                    params["direction"] = (
                        "axial" if off_axis <= AXIAL_FAMILY_DEG
                        else "circumferential" if off_axis >= 90.0 - AXIAL_FAMILY_DEG
                        else "helical"
                    )
                else:
                    params["direction"] = "crossed" if len(keep) == 2 else "linear"
                # The honesty flag: the specification is not derivable.
                params["spec_determinable"] = False
                if callout:
                    params["callout"] = callout
                if trace:
                    print(f"[knurl] candidate {key} {subtype}: {len(p_cells)} cells, "
                          f"{len(p_walls)} walls, {len(p_floors)} floors, pitch {pitch} "
                          f"(cv {cv}), depth {depth}, {len(claimed_ids)} faces")
                cands.append(FeatureInstance(instance_id="", type=FeatureType.KNURL,
                                             faces=claimed_ids, parameters=params))

        # One candidate per physical patch: both levels of a crossed knurl
        # seed independently over the same union of faces.
        cands.sort(key=lambda c: (-len(c.faces), -int(c.parameters.get("cell_count", 0)),
                                  c.faces))
        taken: set[int] = set()
        out: list[FeatureInstance] = []
        for c in cands:
            overlap = sum(1 for f in c.faces if f in taken)
            if c.faces and overlap / len(c.faces) > 0.5:
                continue
            taken.update(c.faces)
            c.instance_id = self.instance_id(len(out))
            out.append(c)

        # Retire what the earlier passes claimed inside a knurl, before the
        # pattern pass can build anything out of those claims. The prior list
        # is the pipeline's own running feature list, so this is the same
        # in-place step the reference takes between the knurl and pattern
        # passes; with no knurl it touches nothing.
        if out and isinstance(prior, list):
            drop_knurl_absorbed_features(prior, out)
        return out


def drop_knurl_absorbed_features(features: list, knurl_features=None) -> None:
    """Retire what other recognizers claimed INSIDE a knurl.

    The claim-ordering answer, deliberately not a reordering: a feature at
    least 0.8 inside a knurl's face set is a facet of the texture. Patterns
    (a union of children by design) and markings (which run first and are
    excluded from the knurl seed) are never dropped. A part with no knurl is
    untouched.
    """
    source = list(features) + list(knurl_features or ())
    knurls = [set(f.faces) for f in source if f.type == FeatureType.KNURL]
    if not knurls:
        return

    def absorbed(f) -> bool:
        if f.type in (FeatureType.KNURL, FeatureType.PATTERN, FeatureType.MARKING_TEXT):
            return False
        if not f.faces:
            return False
        for kn in knurls:
            inside = sum(1 for fid in f.faces if fid in kn)
            if inside / len(f.faces) >= 0.8:
                return True
        return False

    features[:] = [f for f in features if not absorbed(f)]
