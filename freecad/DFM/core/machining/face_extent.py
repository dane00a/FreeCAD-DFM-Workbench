# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""How far a face reaches along a direction, measured from the face.

The idiom this replaces was written in many places::

    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    extent = abs(gp_Vec(xmax - xmin, ymax - ymin, zmax - zmin).Dot(gp_Vec(d)))

An axis-aligned bounding box diagonal is not a measurement along an arbitrary
direction. Projecting it answers a question about the box's corners, and the
answer swings with the face's orientation even when its shape has not changed
-- and, on a curved face, with where the kernel put the parametric seam,
because the box is built from the boundary curves' parametrisation. The
reference found it on three congruent slot floors 120 degrees apart that
measured 0.600, 11.064 and 1.200 against a true 1.200, on a boss height and
a step height that moved when only the seam moved, and on hole depths.

Sample the boundary rather than taking the vertices: a face's extreme point
along a direction often sits in the middle of a rim curve, not at a vertex,
and a vertex-only version under-reports filleted and obliquely cut bores.
Each edge is sampled at 128 chords and the best sample on each side refined
by golden-section search, so the result no longer depends on where an edge
starts -- which, on a closed surface, is the seam.

Faces are addressed by the graph's face id (1-based ``TopExp::MapShapes``
index), the same numbering the reference's ``occt_index`` gives.
"""

from __future__ import annotations

import math
from typing import Optional

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.GeomAdaptor import GeomAdaptor_Curve
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS_Face, TopoDS_Shape
from freecad.DFM.core.utils.occt import TopoDS
from freecad.DFM.core.utils.occt import TopTools_IndexedMapOfShape
from OCP.gp import gp_Dir, gp_Pnt

_SAMPLES = 128
_GOLDEN = 0.6180339887498949
_REFINE_STEPS = 45

_BIG = 1.0e300

# The face map of the last shape asked about. Recognisers and rules measure
# many faces of one part in a row; rebuilding the map per query would be the
# cost of the whole measurement.
_face_map_cache: list = [None, None]

# Spans already measured on that shape, by (face id, direction). Rules and
# recognisers ask the same face about the same direction many times.
_span_cache: dict = {}


def _face_map(shape: TopoDS_Shape) -> TopTools_IndexedMapOfShape:
    cached_shape, cached_map = _face_map_cache
    if cached_shape is not None and cached_shape is shape:
        return cached_map
    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)
    _face_map_cache[0] = shape
    _face_map_cache[1] = face_map
    _span_cache.clear()
    return face_map


def face_by_id(shape: Optional[TopoDS_Shape], face_id: int) -> Optional[TopoDS_Face]:
    """The face with this graph id, or None."""
    if shape is None or face_id is None or face_id < 1:
        return None
    face_map = _face_map(shape)
    if face_id > face_map.Extent():
        return None
    return TopoDS.Face_s(face_map.FindKey(face_id))


def face_span_along(face: TopoDS_Face, d: gp_Dir, lo: float, hi: float):
    """Widen ``(lo, hi)`` by the face's span along ``d``.

    Returns ``(ok, lo, hi)``; ``ok`` is False when the face had no boundary
    to sample, and then ``lo``/``hi`` come back untouched. Several faces can
    be combined into one span by threading ``lo``/``hi`` through.
    """
    if face is None or face.IsNull():
        return False, lo, hi
    dx, dy, dz = d.X(), d.Y(), d.Z()
    n = 0
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        t0, t1 = BRep_Tool.Range_s(edge)
        curve = BRep_Tool.Curve_s(edge, t0, t1)
        if curve is None:
            continue

        def along(t: float) -> float:
            p = curve.Value(t)
            return p.X() * dx + p.Y() * dy + p.Z() * dz

        # Lines and circles have their extremes in closed form, which is
        # what the sampling below converges to (to ~1e-9 of the bracket) and
        # costs a fraction of it. Everything else is sampled.
        exact = _closed_form_span(curve, t0, t1, dx, dy, dz, along)
        if exact is not None:
            lo = min(lo, exact[0])
            hi = max(hi, exact[1])
            n += _SAMPLES + 1
            continue

        # Coarse pass. Chord sampling alone is a precision bound that moves
        # with the curve's parametrisation, so it is only the bracket.
        i_lo = i_hi = 0
        s_lo = _BIG
        s_hi = -_BIG
        span = t1 - t0
        for i in range(_SAMPLES + 1):
            s = along(t0 + span * (i / _SAMPLES))
            if s < s_lo:
                s_lo, i_lo = s, i
            if s > s_hi:
                s_hi, i_hi = s, i
            n += 1

        # Refine: golden-section search on the bracket either side of the
        # best sample converges on the true extreme whatever the
        # parametrisation. Clamped by the coarse value, so it can only move a
        # measurement towards the exact answer.
        def refine(i_best: int, sign: float) -> float:
            h = span / _SAMPLES
            a = t0 + h * max(0, i_best - 1)
            b = t0 + h * min(_SAMPLES, i_best + 1)
            c = b - _GOLDEN * (b - a)
            e = a + _GOLDEN * (b - a)
            fc = sign * along(c)
            fe = sign * along(e)
            for _ in range(_REFINE_STEPS):
                if fc > fe:
                    b = e
                    e = c
                    fe = fc
                    c = b - _GOLDEN * (b - a)
                    fc = sign * along(c)
                else:
                    a = c
                    c = e
                    fc = fe
                    e = a + _GOLDEN * (b - a)
                    fe = sign * along(e)
            return sign * max(fc, fe)

        lo = min(lo, s_lo, refine(i_lo, -1.0))
        hi = max(hi, s_hi, refine(i_hi, 1.0))
    return n >= 2, lo, hi


def _closed_form_span(curve, t0, t1, dx, dy, dz, along):
    """(lo, hi) of a line or circle segment along a direction, else None."""
    try:
        kind = GeomAdaptor_Curve(curve).GetType()
    except Exception:
        return None
    if kind == GeomAbs_CurveType.GeomAbs_Line:
        a, b = along(t0), along(t1)
        return (min(a, b), max(a, b))
    if kind == GeomAbs_CurveType.GeomAbs_Circle:
        circ = GeomAdaptor_Curve(curve).Circle()
        pos = circ.Position()
        xd, yd = pos.XDirection(), pos.YDirection()
        r = circ.Radius()
        cx = xd.X() * dx + xd.Y() * dy + xd.Z() * dz
        cy = yd.X() * dx + yd.Y() * dy + yd.Z() * dz
        loc = pos.Location()
        c0 = loc.X() * dx + loc.Y() * dy + loc.Z() * dz
        values = [along(t0), along(t1)]
        amp = math.hypot(cx, cy)
        if amp > 1e-15:
            t_max = math.atan2(cy, cx)
            for base in (t_max, t_max + math.pi):
                # Every turn of the extreme that falls inside the edge.
                k = math.ceil((t0 - base) / (2.0 * math.pi))
                t = base + k * 2.0 * math.pi
                while t <= t1:
                    values.append(c0 + r * (cx * math.cos(t) + cy * math.sin(t)))
                    t += 2.0 * math.pi
        return (min(values), max(values))
    return None


def face_extent_along(face: TopoDS_Face, d: gp_Dir) -> float:
    """The face's extent along ``d``; -1.0 when it cannot be measured."""
    if face is None or face.IsNull():
        return -1.0
    ok, lo, hi = face_span_along(face, d, _BIG, -_BIG)
    if not ok or not hi > lo:
        return -1.0
    return hi - lo


def span_of_face(shape, face_id: int, d: gp_Dir, lo: float, hi: float):
    """``face_span_along`` for the face with this graph id."""
    face = face_by_id(shape, face_id)
    if face is None:
        return False, lo, hi
    key = (face_id, d.X(), d.Y(), d.Z())
    cached = _span_cache.get(key)
    if cached is None:
        cached = face_span_along(face, d, _BIG, -_BIG)
        _span_cache[key] = cached
    ok, s_lo, s_hi = cached
    if not ok:
        return False, lo, hi
    return True, min(lo, s_lo), max(hi, s_hi)


def extent_of_face(shape, face_id: int, d: gp_Dir) -> float:
    """``face_extent_along`` for the face with this graph id."""
    ok, lo, hi = span_of_face(shape, face_id, d, _BIG, -_BIG)
    if not ok or not hi > lo:
        return -1.0
    return hi - lo


def band_along(
    shape,
    face_id: int,
    fallback_box: Bnd_Box,
    origin: gp_Pnt,
    d: gp_Dir,
    lo: float,
    hi: float,
):
    """The band a face covers along ``d``, measured from ``origin``.

    What every "project the 8 corners of node.bbox onto the axis" loop was
    approximating; the box projection survives only as the fallback for a
    face with no sampleable boundary. Accumulates into ``lo``/``hi``.
    Returns ``(ok, lo, hi)``; ``ok`` is False only when neither path gave
    anything.
    """
    o = origin.X() * d.X() + origin.Y() * d.Y() + origin.Z() * d.Z()
    ok, s_lo, s_hi = span_of_face(shape, face_id, d, _BIG, -_BIG)
    if ok and s_hi > s_lo:
        return True, min(lo, s_lo - o), max(hi, s_hi - o)
    if fallback_box is None or fallback_box.IsVoid():
        return False, lo, hi
    xn, yn, zn, xx, yx, zx = fallback_box.Get()
    for cx in (xn, xx):
        for cy in (yn, yx):
            for cz in (zn, zx):
                t = (
                    (cx - origin.X()) * d.X()
                    + (cy - origin.Y()) * d.Y()
                    + (cz - origin.Z()) * d.Z()
                )
                lo = min(lo, t)
                hi = max(hi, t)
    return True, lo, hi


def face_vertices_extent(shape, face_id: int, d: gp_Dir) -> float:
    """How far a face's vertices reach along ``d``; -1.0 with fewer than two.

    The slot recogniser's across-gate measures from vertices, not from the
    sampled boundary (reference 326480b7): exact for straight edges, and it
    understates a bulging arc by its sagitta, which makes that gate more
    permissive -- the safe direction for a sanity bound with 2.5x headroom.
    """
    from OCP.TopAbs import TopAbs_VERTEX

    face = face_by_id(shape, face_id)
    if face is None:
        return -1.0
    lo = _BIG
    hi = -_BIG
    count = 0
    explorer = TopExp_Explorer(face, TopAbs_VERTEX)
    while explorer.More():
        p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
        explorer.Next()
        t = p.X() * d.X() + p.Y() * d.Y() + p.Z() * d.Z()
        lo = min(lo, t)
        hi = max(hi, t)
        count += 1
    if count < 2:
        return -1.0
    return hi - lo
