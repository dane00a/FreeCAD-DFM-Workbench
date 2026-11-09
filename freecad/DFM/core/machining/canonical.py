# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Canonical surface recognition: fit a primitive to a freeform face, and say
how far off it was.

A face whose surface is freeform (B-spline, Bezier, revolution, extrusion) but
whose GEOMETRY is within tolerance of a plane, cylinder, cone, sphere or torus
can be PROMOTED, so every recognizer that seeds on a surface type sees it. A
promoted face stays distinguishable (``canonical_promoted``,
``canonical_native_type``, ``canonical_deviation_mm``), because a wrong
promotion is worse than none.

PROMOTION IS OFF, and that is the reference's measured decision rather than
caution: its census over the whole corpus found the plane family has no
separation at any ceiling (promotions rise monotonically with the tolerance),
and the motivating case -- lofted louver hoods -- does not promote at all,
because those hoods are genuinely doubly curved. ``DFM_CANONICAL_PROMOTION=1``
turns it on for one process, which is how a census or an A/B runs.

The planarity and cylindricity shape tests follow Analysis Situs' canonical
recognition (``asiAlgo_RecognizeCanonical``, BSD-3, itself derived from
OpenCascade's ``GeomLib_IsPlanarSurface``): the algorithm is re-expressed
here, not copied. The cone, sphere and torus constructions and every
deviation measurement are this module's own.

Licence of the upstream algorithm (BSD-3-Clause): Copyright (c) 2017-present,
Sergey Slyadnev and contributors. Redistribution and use in source and binary
forms, with or without modification, are permitted provided that the
copyright notice, this list of conditions and the disclaimer are retained;
neither the name of the copyright holder nor the names of its contributors may
be used to endorse or promote products derived from this software without
specific prior written permission. THIS SOFTWARE IS PROVIDED "AS IS" AND ANY
EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Optional

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
from OCP.GeomAbs import (
    GeomAbs_BezierCurve,
    GeomAbs_BezierSurface,
    GeomAbs_BSplineCurve,
    GeomAbs_BSplineSurface,
    GeomAbs_Circle,
    GeomAbs_CN,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Ellipse,
    GeomAbs_Hyperbola,
    GeomAbs_Line,
    GeomAbs_Parabola,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_SurfaceOfExtrusion,
    GeomAbs_SurfaceOfRevolution,
    GeomAbs_Torus,
)
from OCP.GeomAdaptor import GeomAdaptor_Curve, GeomAdaptor_Surface
from OCP.GeomLib import GeomLib
from OCP.GeomLProp import GeomLProp_CLProps, GeomLProp_SLProps
from OCP.GProp import GProp_GProps
from OCP.Precision import Precision
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopAbs import TopAbs_OUT
from OCP.TopLoc import TopLoc_Location
from OCP.gp import gp, gp_Ax1, gp_Ax3, gp_Dir, gp_Pln, gp_Pnt, gp_Pnt2d, gp_Vec

from .aag import AagNode, SurfaceType

NONE, PLANE, CYLINDER, CONE, SPHERE, TORUS = (
    "NONE", "PLANE", "CYLINDER", "CONE", "SPHERE", "TORUS",
)

#: A face whose normals barely move has a curvature centre set by a tiny lever
#: arm, so any curved primitive fitted to it is numerically arbitrary. Inert
#: on the reference corpus (everything that promotes sweeps far more), kept as
#: a cheap guard against a crowned panel fitting a huge "cylinder".
MIN_NORMAL_SPREAD_DEG = 20.0
#: |k| below this counts as flat, as in the graph's own curvature sampler.
FLAT_CURVATURE = 1e-6
_INF_LIMIT = 100.0


@dataclass
class CanonicalRecognizerConfig:
    max_deviation_mm: float = 0.05
    min_face_area_mm2: float = 1.0
    min_samples: int = 12
    max_samples: int = 256
    mesh_deflection: float = 0.5
    min_radius_mm: float = 0.2
    max_radius_mm: float = 500.0
    enable_plane: bool = True
    enable_cylinder: bool = True
    enable_cone: bool = True
    enable_sphere: bool = True
    enable_torus: bool = True


@dataclass
class CanonicalCandidate:
    kind: str = NONE
    fitted: bool = False
    shape_test: bool = False
    max_dev_mm: float = 0.0
    rms_dev_mm: float = 0.0
    radius_mm: float = 0.0
    reject_reason: str = ""


@dataclass
class CanonicalFit:
    kind: str = NONE
    sample_count: int = 0
    normal_spread_deg: float = 0.0
    max_dev_mm: float = 0.0
    rms_dev_mm: float = 0.0
    face_area_mm2: float = 0.0
    candidates: list[CanonicalCandidate] = field(default_factory=list)
    plane_origin: Optional[gp_Pnt] = None
    plane_normal: Optional[gp_Dir] = None
    axis: Optional[gp_Ax1] = None
    radius: float = 0.0
    p0: Optional[gp_Pnt] = None
    p1: Optional[gp_Pnt] = None
    r0: float = 0.0
    r1: float = 0.0
    cone_semi_angle: float = 0.0
    cone_apex: Optional[gp_Pnt] = None
    sphere_center: Optional[gp_Pnt] = None
    sphere_radius: float = 0.0
    torus_major_r: float = 0.0
    torus_minor_r: float = 0.0
    torus_center: Optional[gp_Pnt] = None

    def accepted(self) -> bool:
        return self.kind != NONE

    def candidate(self, kind: str) -> Optional[CanonicalCandidate]:
        for c in self.candidates:
            if c.kind == kind:
                return c
        return None


# -- The derived shape tests ----------------------------------------------------


def _trim_inf(val: float, limit: float = _INF_LIMIT) -> float:
    if Precision.IsPositiveInfinite_s(val):
        return limit
    if Precision.IsNegativeInfinite_s(val):
        return -limit
    return val


def _array(points) -> TColgp_Array1OfPnt:
    arr = TColgp_Array1OfPnt(1, len(points))
    for i, p in enumerate(points):
        arr.SetValue(i + 1, p)
    return arr


def _check_surface_planar(poles: list, toler: float, surface) -> Optional[gp_Pln]:
    """Inertia of the control net (or of samples): flat if the out-of-plane
    dispersion is under the tolerance and the cloud is not a line."""
    nb = len(poles)
    essai = True
    if nb > 10:
        aux = [poles[0], poles[nb // 3 - 1], poles[nb // 2 - 1],
               poles[nb // 2 + nb // 3 - 1], poles[nb - 1]]
        bary, dx, dy = gp_Pnt(), gp_Dir(), gp_Dir()
        _gx, _gy, gz = GeomLib.Inertia_s(_array(aux), bary, dx, dy)
        essai = gz < toler
    if not essai:
        return None
    bary, dx, dy = gp_Pnt(), gp_Dir(), gp_Dir()
    _gx, gy, gz = GeomLib.Inertia_s(_array(poles), bary, dx, dy)
    if not (gz < toler and gy > toler):
        return None
    umin, umax, vmin, vmax = surface.Bounds()
    p, du_v, dv_v = gp_Pnt(), gp_Vec(), gp_Vec()
    surface.D1((umin + umax) / 2, (vmin + vmax) / 2, p, du_v, dv_v)
    du = gp_Dir(du_v)
    a1 = du.Angle(dx)
    a2 = du.Angle(dy)
    if a1 > math.pi / 2:
        a1 = math.pi - a1
    if a2 > math.pi / 2:
        a2 = math.pi - a2
    if a2 < a1:
        du = dy
        dy = dx
        dx = du
    if dx.Angle(gp_Dir(du_v)) > math.pi / 2:
        dx.Reverse()
    if dy.Angle(gp_Dir(dv_v)) > math.pi / 2:
        dy.Reverse()
    plane = gp_Pln()
    plane.SetPosition(gp_Ax3(bary, dx.Crossed(dy), dx))
    plane.SetLocation(bary)
    return plane


def _check_curve_on_plane(curve, plane: gp_Pln, tol: float) -> bool:
    ac = GeomAdaptor_Curve(curve)
    kind = ac.GetType()
    poles = None
    if kind == GeomAbs_Line:
        nb = 2
    elif kind == GeomAbs_Circle:
        nb = 3
    elif kind in (GeomAbs_Ellipse, GeomAbs_Hyperbola, GeomAbs_Parabola):
        nb = 5
    elif kind == GeomAbs_BezierCurve:
        bz = ac.Bezier()
        poles = [bz.Pole(i) for i in range(1, ac.NbPoles() + 1)]
    elif kind == GeomAbs_BSplineCurve:
        bs = ac.BSpline()
        poles = [bs.Pole(i) for i in range(1, ac.NbPoles() + 1)]
    else:
        nb = 8 + 3 * ac.NbIntervals(GeomAbs_CN)
    if poles is not None:
        return all(plane.Distance(p) < tol for p in poles)
    f, l = ac.FirstParameter(), ac.LastParameter()
    du = (l - f) / (nb - 1)
    for i in range(1, nb + 1):
        if not plane.Distance(curve.Value((i - 1) * du + f)) < tol:
            return False
    return True


def check_is_planar(surface, toler: float) -> bool:
    if surface is None:
        return False
    adt = GeomAdaptor_Surface(surface)
    kind = adt.GetType()
    if kind == GeomAbs_Plane:
        return True
    if kind in (GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus):
        return False
    if kind in (GeomAbs_BezierSurface, GeomAbs_BSplineSurface):
        nbu, nbv = adt.NbUPoles(), adt.NbVPoles()
        if nbu < 2 or nbv < 2:
            return False
        src = adt.Bezier() if kind == GeomAbs_BezierSurface else adt.BSpline()
        poles = [src.Pole(i, j) for i in range(1, nbu + 1) for j in range(1, nbv + 1)]
        return _check_surface_planar(poles, toler, surface) is not None
    if kind in (GeomAbs_SurfaceOfRevolution, GeomAbs_SurfaceOfExtrusion):
        umin, umax, vmin, vmax = (_trim_inf(x) for x in surface.Bounds())
        p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
        surface.D1((umin + umax) / 2, (vmin + vmax) / 2, p, du, dv)
        if du.Magnitude() <= gp.Resolution_s() or dv.Magnitude() <= gp.Resolution_s():
            surface.D1((umin + umax) / 2 + (umax - umin) * 0.1,
                       (vmin + vmax) / 2 + (vmax - vmin) * 0.1, p, du, dv)
        dn = du.Crossed(dv)
        if kind == GeomAbs_SurfaceOfRevolution:
            axis_dir = adt.AxeOfRevolution().Direction()
            essai = True
            if dn.Magnitude() > Precision.Confusion_s():
                angle = axis_dir.Angle(gp_Dir(dn))
                if angle > math.pi / 2:
                    angle = math.pi - angle
                    axis_dir.Reverse()
                essai = angle < 0.1
            if not essai:
                return False
            axe = gp_Ax3(p, axis_dir)
            axe.SetXDirection(gp_Dir(du))
            plane = gp_Pln(axe)
            plane.SetLocation(p)
            return _check_curve_on_plane(surface.UIso(umin), plane, toler)
        essai = False
        n = dn.Magnitude()
        if n > 1.0e-15:
            dn.Divide(n)
            angmax = toler / (vmax - vmin) if vmax > vmin else 0.0
            essai = gp_Dir(dn).IsNormal(adt.Direction(), angmax)
        if not essai:
            return False
        plane = gp_Pln(gp_Ax3(p, gp_Dir(dn), gp_Dir(du)))
        plane.SetLocation(p)
        return _check_curve_on_plane(surface.VIso((vmin + vmax) / 2), plane, toler)
    nbu = 8 + 3 * adt.NbUIntervals(GeomAbs_CN)
    nbv = 8 + 3 * adt.NbVIntervals(GeomAbs_CN)
    umin, umax, vmin, vmax = (_trim_inf(x) for x in surface.Bounds())
    du = (umax - umin) / (nbu - 1)
    dv = (vmax - vmin) / (nbv - 1)
    pts = []
    for i in range(nbu):
        for j in range(nbv):
            q = gp_Pnt()
            surface.D0(umin + du * i, vmin + dv * j, q)
            pts.append(q)
    return _check_surface_planar(pts, toler, surface) is not None


def _evaluate_curvature(surface, angle: float, u: float, v: float) -> Optional[float]:
    props = GeomLProp_SLProps(surface, u, v, 2, 1e-7)
    if not props.IsCurvatureDefined():
        return None
    xu, xv = props.D1U(), props.D1V()
    xuu, xuv, xvv = props.D2U(), props.DUV(), props.D2V()
    n = gp_Vec(props.Normal())
    e, f, g = xu.Dot(xu), xu.Dot(xv), xv.Dot(xv)
    ll, m, nn = n.Dot(xuu), n.Dot(xuv), n.Dot(xvv)
    if abs(angle - math.pi * 0.5) < 1.0e-5:
        return nn / g
    lam = math.tan(angle)
    return (ll + 2 * m * lam + nn * lam * lam) / (e + 2 * f * lam + g * lam * lam)


def check_is_cylindrical(surface, u0, u1, v0, v1, toler):
    """A constant radius-of-curvature field ruled in one direction. Returns
    (axis_location, axis_direction) or None."""
    if surface is None:
        return None
    adt = GeomAdaptor_Surface(surface)
    kind = adt.GetType()
    if kind == GeomAbs_Cylinder:
        ax = adt.Cylinder().Axis()
        return ax.Location(), ax.Direction()
    if kind in (GeomAbs_Plane, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus):
        return None
    steps = 10
    umin, umax, vmin, vmax = (_trim_inf(x) for x in (u0, u1, v0, v1))
    if not (umax > umin) or not (vmax > vmin):
        return None
    ustep = (umax - umin) / steps
    vstep = (vmax - vmin) / steps

    def values(lo, hi, step):
        out = []
        x = lo
        while True:
            stop = False
            if x > hi:
                x = hi
                stop = True
            out.append(x)
            if stop:
                return out
            x += step

    k1, k2 = [], []
    for uu in values(umin, umax, ustep):
        for vv in values(vmin, vmax, vstep):
            # Upstream ignores an undefined curvature too: it contributes 0.
            a = _evaluate_curvature(surface, 0.0, uu, vv)
            b = _evaluate_curvature(surface, math.pi / 2.0, uu, vv)
            k1.append(a if a is not None else 0.0)
            k2.append(b if b is not None else 0.0)
    if not k1:
        return None
    zero, inf = 1.0e-6, 1.0e5

    def radius(k):
        return inf if abs(k) < zero else abs(1.0 / k)

    for ks in (k1, k2):
        ref = radius(ks[0])
        if any(abs(radius(k) - ref) > toler for k in ks[1:]):
            return None
    if abs(k1[0]) < zero and abs(k2[0]) > zero:
        curved_u = False
    elif abs(k1[0]) > zero and abs(k2[0]) < zero:
        curved_u = True
    else:
        return None
    pu, pv = (umin + umax) * 0.5, (vmin + vmax) * 0.5
    p3, d1u, d1v = gp_Pnt(), gp_Vec(), gp_Vec()
    surface.D1(pu, pv, p3, d1u, d1v)
    iso = surface.VIso(pv) if curved_u else surface.UIso(pu)
    if iso is None:
        return None
    t = (iso.FirstParameter() + iso.LastParameter()) * 0.5
    clp = GeomLProp_CLProps(iso, 2, Precision.Confusion_s())
    clp.SetParameter(t)
    if not clp.IsTangentDefined():
        return None
    if abs(clp.Curvature()) < Precision.Confusion_s():
        return None
    centre = gp_Pnt()
    clp.CentreOfCurvature(centre)
    axis_vec = d1v if curved_u else d1u
    if axis_vec.Magnitude() <= gp.Resolution_s():
        return None
    return centre, gp_Dir(axis_vec)


# -- Our own fits --------------------------------------------------------------


def _solve_dense(n: int, a: list) -> Optional[list]:
    for col in range(n):
        piv = col
        for r in range(col + 1, n):
            if abs(a[r][col]) > abs(a[piv][col]):
                piv = r
        if abs(a[piv][col]) < 1e-12:
            return None
        if piv != col:
            a[piv], a[col] = a[col], a[piv]
        for r in range(col + 1, n):
            f = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= f * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = a[r][n]
        for c in range(r + 1, n):
            s -= a[r][c] * x[c]
        x[r] = s / a[r][r]
    return x


def _fit_plane_ls(points):
    if len(points) < 3:
        return None
    bary, dx, dy = gp_Pnt(), gp_Dir(), gp_Dir()
    try:
        _xg, yg, zg = GeomLib.Inertia_s(_array(points), bary, dx, dy)
    except Exception:
        return None
    cross = gp_Vec(dx).Crossed(gp_Vec(dy))
    if cross.Magnitude() < 1e-9:
        return None
    return bary, gp_Dir(cross), yg, zg


def _fit_circle_2d(pts):
    if len(pts) < 3:
        return None
    sx = sy = sxx = syy = sxy = sz = szx = szy = 0.0
    n = float(len(pts))
    for x, y in pts:
        z = x * x + y * y
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
        sz += z
        szx += z * x
        szy += z * y
    sol = _solve_dense(3, [[sxx, sxy, sx, -szx], [sxy, syy, sy, -szy], [sx, sy, n, -sz]])
    if sol is None:
        return None
    cx, cy = -sol[0] / 2.0, -sol[1] / 2.0
    rr = cx * cx + cy * cy - sol[2]
    if not rr > 0.0:
        return None
    return cx, cy, math.sqrt(rr)


def _fit_sphere_ls(samples):
    if len(samples) < 4:
        return None
    n = float(len(samples))
    sx = sy = sz = sxx = syy = szz = sxy = sxz = syz = sw = swx = swy = swz = 0.0
    for s in samples:
        x, y, z = s.p.X(), s.p.Y(), s.p.Z()
        w = x * x + y * y + z * z
        sx += x
        sy += y
        sz += z
        sxx += x * x
        syy += y * y
        szz += z * z
        sxy += x * y
        sxz += x * z
        syz += y * z
        sw += w
        swx += w * x
        swy += w * y
        swz += w * z
    sol = _solve_dense(4, [
        [sxx, sxy, sxz, sx, -swx],
        [sxy, syy, syz, sy, -swy],
        [sxz, syz, szz, sz, -swz],
        [sx, sy, sz, n, -sw],
    ])
    if sol is None:
        return None
    cx, cy, cz = -sol[0] / 2, -sol[1] / 2, -sol[2] / 2
    rr = cx * cx + cy * cy + cz * cz - sol[3]
    if not rr > 0.0:
        return None
    return gp_Pnt(cx, cy, cz), math.sqrt(rr)


def _finish(devs):
    if not devs:
        return 0.0, 0.0
    return max(devs), math.sqrt(sum(d * d for d in devs) / len(devs))


class _Sample:
    __slots__ = ("p", "n", "has_n", "kmax", "kmin", "dmax", "dmin", "has_k")

    def __init__(self):
        self.p = None
        self.n = None
        self.has_n = False
        self.kmax = self.kmin = 0.0
        self.dmax = self.dmin = None
        self.has_k = False


def recognize_canonical(face, cfg: Optional[CanonicalRecognizerConfig] = None) -> CanonicalFit:
    cfg = cfg or CanonicalRecognizerConfig()
    out = CanonicalFit()
    if face is None or face.IsNull():
        return out
    gprops = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, gprops)
    out.face_area_mm2 = gprops.Mass()
    if out.face_area_mm2 < cfg.min_face_area_mm2:
        return out

    # Sample the TRIMMED domain: triangulation UV nodes, because a B-spline's
    # parametric bounds run past its trim.
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None:
        try:
            BRepMesh_IncrementalMesh(face, cfg.mesh_deflection, False, cfg.mesh_deflection)
            tri = BRep_Tool.Triangulation_s(face, loc)
        except Exception:
            return out
    if tri is None or not tri.HasUVNodes():
        return out

    surf = BRepAdaptor_Surface(face)  # must outlive props
    props = BRepLProp_SLProps(surf, 2, 1e-7)
    n_nodes = tri.NbNodes()
    step = max(1, n_nodes // max(1, cfg.max_samples))
    samples: list[_Sample] = []

    def take(u, v):
        s = _Sample()
        try:
            props.SetParameters(u, v)
            s.p = gp_Pnt(props.Value().XYZ())
            if props.IsNormalDefined():
                s.n = gp_Dir(props.Normal().XYZ())
                s.has_n = True
            if props.IsCurvatureDefined():
                s.kmax = props.MaxCurvature()
                s.kmin = props.MinCurvature()
                s.dmax, s.dmin = gp_Dir(), gp_Dir()
                props.CurvatureDirections(s.dmax, s.dmin)
                s.has_k = True
        except Exception:
            return
        samples.append(s)

    for i in range(1, n_nodes + 1, step):
        uv = tri.UVNode(i)
        take(uv.X(), uv.Y())

    # A planar face tessellates to as few as four nodes; supplement with a
    # UV grid kept INSIDE the trim.
    if len(samples) < cfg.min_samples:
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        if u1 > u0 and v1 > v0:
            cls = BRepTopAdaptor_FClass2d(face, Precision.Confusion_s())
            grid = 16
            for iu in range(grid + 1):
                u = u0 + (u1 - u0) * iu / grid
                for iv in range(grid + 1):
                    v = v0 + (v1 - v0) * iv / grid
                    if cls.Perform(gp_Pnt2d(u, v)) == TopAbs_OUT:
                        continue
                    take(u, v)
                    if len(samples) >= cfg.max_samples + n_nodes:
                        break

    out.sample_count = len(samples)
    if out.sample_count < cfg.min_samples:
        return out
    pts = [s.p for s in samples]

    # Spread: twice the largest angle from the mean normal.
    sx = sy = sz = 0.0
    cnt = 0
    for s in samples:
        if s.has_n:
            sx += s.n.X()
            sy += s.n.Y()
            sz += s.n.Z()
            cnt += 1
    spread = 0.0
    if cnt >= 2:
        length = math.sqrt(sx * sx + sy * sy + sz * sz)
        if length < 1e-9:
            spread = 180.0
        else:
            mean = gp_Dir(sx / length, sy / length, sz / length)
            spread = 2.0 * max(mean.Angle(s.n) for s in samples if s.has_n) * 180.0 / math.pi
    out.normal_spread_deg = spread
    geom = BRep_Tool.Surface_s(face)

    def in_band(r):
        return cfg.min_radius_mm <= r <= cfg.max_radius_mm

    # PLANE: shape test on the surface, deviation against our LS plane.
    plane_c = CanonicalCandidate(kind=PLANE)
    plane_bary = plane_normal = None
    if cfg.enable_plane:
        fit = _fit_plane_ls(pts)
        if fit is not None:
            plane_bary, plane_normal = fit[0], fit[1]
            plane_c.fitted = True
            # Align with the surface's NATURAL normal, as a native plane's is.
            mean = gp_Vec(sx, sy, sz)
            if mean.Magnitude() > 1e-9 and gp_Vec(plane_normal).Dot(mean) < 0:
                plane_normal.Reverse()
            pl = gp_Pln(plane_bary, plane_normal)
            plane_c.max_dev_mm, plane_c.rms_dev_mm = _finish([pl.Distance(p) for p in pts])
            plane_c.shape_test = check_is_planar(geom, cfg.max_deviation_mm)
        else:
            plane_c.reject_reason = "degenerate point cloud"
        out.candidates.append(plane_c)

    # CYLINDER: the derived local test, then a least-squares circle refine.
    cyl_c = CanonicalCandidate(kind=CYLINDER)
    cyl_axis = None
    cyl_radius = 0.0
    if cfg.enable_cylinder:
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        test = check_is_cylindrical(geom, u0, u1, v0, v1, cfg.max_deviation_mm)
        cyl_c.shape_test = test is not None
        if test is not None:
            ax_loc, ax_dir = test
            frame = gp_Ax3(ax_loc, ax_dir)
            ex, ey = gp_Vec(frame.XDirection()), gp_Vec(frame.YDirection())
            flat = [(gp_Vec(ax_loc, p).Dot(ex), gp_Vec(ax_loc, p).Dot(ey)) for p in pts]
            circ = _fit_circle_2d(flat)
            if circ is not None and circ[2] > 1e-9:
                cx, cy, rr = circ
                cyl_c.fitted = True
                centre = ax_loc.Translated(ex.Multiplied(cx).Added(ey.Multiplied(cy)))
                cyl_axis = gp_Ax1(centre, ax_dir)
                cyl_radius = rr
                cyl_c.radius_mm = rr
                devs = []
                dv = gp_Vec(ax_dir)
                for p in pts:
                    d = gp_Vec(centre, p)
                    radial = d.Subtracted(dv.Multiplied(d.Dot(dv)))
                    devs.append(abs(radial.Magnitude() - rr))
                cyl_c.max_dev_mm, cyl_c.rms_dev_mm = _finish(devs)
            else:
                cyl_c.reject_reason = "circle fit failed"
        else:
            cyl_c.reject_reason = "curvature field not cylindrical"
        out.candidates.append(cyl_c)

    # CONE: rulings meet at the apex; their unit directions lie on a circle
    # of the unit sphere whose plane normal is the axis.
    cone_c = CanonicalCandidate(kind=CONE)
    cone_apex = cone_axis = None
    cone_alpha = 0.0
    if cfg.enable_cone:
        ruled = 0
        a = [[0.0] * 4 for _ in range(3)]
        for s in samples:
            if not s.has_k:
                continue
            max_is_flat = abs(s.kmax) < abs(s.kmin)
            k_flat = s.kmax if max_is_flat else s.kmin
            k_curve = s.kmin if max_is_flat else s.kmax
            if abs(k_flat) > FLAT_CURVATURE or abs(k_curve) < FLAT_CURVATURE:
                continue
            g = s.dmax if max_is_flat else s.dmin
            ruled += 1
            gx, gy, gz = g.X(), g.Y(), g.Z()
            pm = [[1 - gx * gx, -gx * gy, -gx * gz],
                  [-gx * gy, 1 - gy * gy, -gy * gz],
                  [-gx * gz, -gy * gz, 1 - gz * gz]]
            pp = [s.p.X(), s.p.Y(), s.p.Z()]
            for r in range(3):
                for c in range(3):
                    a[r][c] += pm[r][c]
                a[r][3] += sum(pm[r][c] * pp[c] for c in range(3))
        if ruled < cfg.min_samples:
            cone_c.reject_reason = "not ruled at enough samples"
        else:
            sol = _solve_dense(3, [row[:] for row in a])
            if sol is None:
                cone_c.reject_reason = "rulings parallel (cylinder, not cone)"
            else:
                cone_apex = gp_Pnt(*sol)
                tips = []
                for p in pts:
                    d = gp_Vec(cone_apex, p)
                    if d.Magnitude() < 1e-9:
                        continue
                    u = gp_Dir(d)
                    tips.append(gp_Pnt(u.X(), u.Y(), u.Z()))
                fit = _fit_plane_ls(tips) if len(tips) >= 3 else None
                if fit is None:
                    cone_c.reject_reason = "generatrix cloud degenerate"
                else:
                    gb, gn = fit[0], fit[1]
                    cos_a = gp_Vec(gb.X(), gb.Y(), gb.Z()).Dot(gp_Vec(gn))
                    if cos_a < 0:
                        gn.Reverse()
                        cos_a = -cos_a
                    cos_a = min(1.0, max(-1.0, cos_a))
                    cone_axis = gn
                    cone_alpha = math.acos(cos_a)
                    if cone_alpha < 1e-3 or cone_alpha > math.pi / 2 - 1e-3:
                        cone_c.reject_reason = "degenerate semi-angle"
                    else:
                        cone_c.fitted = True
                        cone_c.shape_test = True
                        devs = []
                        for p in pts:
                            d = gp_Vec(cone_apex, p)
                            length = d.Magnitude()
                            if length < 1e-9:
                                devs.append(0.0)
                                continue
                            theta = gp_Dir(d).Angle(cone_axis)
                            devs.append(length * abs(math.sin(theta - cone_alpha)))
                        cone_c.max_dev_mm, cone_c.rms_dev_mm = _finish(devs)
        out.candidates.append(cone_c)

    # SPHERE: algebraic fit plus an umbilicity check.
    sph_c = CanonicalCandidate(kind=SPHERE)
    sph_center = None
    sph_radius = 0.0
    if cfg.enable_sphere:
        umbilic = with_k = 0
        for s in samples:
            if not s.has_k:
                continue
            with_k += 1
            m = max(abs(s.kmax), abs(s.kmin))
            if m < FLAT_CURVATURE:
                continue
            if abs(s.kmax - s.kmin) / m < 0.05:
                umbilic += 1
        sph_c.shape_test = with_k > 0 and umbilic >= int(0.9 * with_k)
        fit = _fit_sphere_ls(samples)
        if fit is not None and fit[1] > 1e-9:
            sph_center, sph_radius = fit
            sph_c.fitted = True
            sph_c.radius_mm = sph_radius
            sph_c.max_dev_mm, sph_c.rms_dev_mm = _finish(
                [abs(p.Distance(sph_center) - sph_radius) for p in pts])
        else:
            sph_c.reject_reason = "sphere fit failed"
        out.candidates.append(sph_c)

    # TORUS: one principal radius constant (the meridian); its centres of
    # curvature lie on the major circle.
    tor_c = CanonicalCandidate(kind=TORUS)
    tor_axis = None
    tor_big = tor_small = 0.0
    if cfg.enable_torus:
        r_max = [1.0 / abs(s.kmax) for s in samples if s.has_k and abs(s.kmax) > FLAT_CURVATURE]
        r_min = [1.0 / abs(s.kmin) for s in samples if s.has_k and abs(s.kmin) > FLAT_CURVATURE]

        def spread_of(v):
            if len(v) < 3:
                return math.inf
            v = sorted(v)
            med = v[len(v) // 2]
            if med < 1e-9:
                return math.inf
            return (v[len(v) - 1 - len(v) // 10] - v[len(v) // 10]) / med

        sp_max, sp_min = spread_of(r_max), spread_of(r_min)
        use_max = sp_max <= sp_min
        chosen = r_max if use_max else r_min
        chosen_spread = sp_max if use_max else sp_min
        if len(chosen) < 3 or chosen_spread > 0.05:
            tor_c.reject_reason = "no constant principal radius"
        else:
            tor_small = sorted(chosen)[len(chosen) // 2]
            centres = []
            for s in samples:
                if not s.has_k or not s.has_n:
                    continue
                k = s.kmax if use_max else s.kmin
                if abs(k) < FLAT_CURVATURE:
                    continue
                centres.append(s.p.Translated(gp_Vec(s.n).Multiplied(1.0 / k)))
            fit = _fit_plane_ls(centres) if len(centres) >= 3 else None
            if fit is None:
                tor_c.reject_reason = "meridian centres degenerate"
            else:
                cb, cn = fit[0], fit[1]
                frame = gp_Ax3(cb, cn)
                ex, ey = gp_Vec(frame.XDirection()), gp_Vec(frame.YDirection())
                flat = [(gp_Vec(cb, c).Dot(ex), gp_Vec(cb, c).Dot(ey)) for c in centres]
                circ = _fit_circle_2d(flat)
                if circ is None or circ[2] <= tor_small:
                    tor_c.reject_reason = "major circle fit failed or R <= r"
                else:
                    cx, cy, tor_big = circ
                    tcentre = cb.Translated(ex.Multiplied(cx).Added(ey.Multiplied(cy)))
                    tor_axis = gp_Ax1(tcentre, cn)
                    tor_c.fitted = True
                    tor_c.shape_test = True
                    tor_c.radius_mm = tor_small
                    nv = gp_Vec(cn)
                    devs = []
                    for p in pts:
                        d = gp_Vec(tcentre, p)
                        z = d.Dot(nv)
                        rho = d.Subtracted(nv.Multiplied(z)).Magnitude()
                        dr = rho - tor_big
                        devs.append(abs(math.sqrt(dr * dr + z * z) - tor_small))
                    tor_c.max_dev_mm, tor_c.rms_dev_mm = _finish(devs)
        out.candidates.append(tor_c)

    # Acceptance, simplest primitive first.
    def accept(c):
        return c.fitted and c.shape_test and c.max_dev_mm <= cfg.max_deviation_mm

    if cfg.enable_plane and accept(plane_c):
        out.kind = PLANE
        out.max_dev_mm, out.rms_dev_mm = plane_c.max_dev_mm, plane_c.rms_dev_mm
        out.plane_origin, out.plane_normal = plane_bary, plane_normal
        return out
    if spread < MIN_NORMAL_SPREAD_DEG:
        return out
    if cfg.enable_cylinder and accept(cyl_c) and in_band(cyl_radius):
        out.kind = CYLINDER
        out.max_dev_mm, out.rms_dev_mm = cyl_c.max_dev_mm, cyl_c.rms_dev_mm
        out.axis = cyl_axis
        out.radius = cyl_radius
        dv = gp_Vec(cyl_axis.Direction())
        ts = [gp_Vec(cyl_axis.Location(), p).Dot(dv) for p in pts]
        out.p0 = cyl_axis.Location().Translated(dv.Multiplied(min(ts)))
        out.p1 = cyl_axis.Location().Translated(dv.Multiplied(max(ts)))
        out.r0 = out.r1 = cyl_radius
        return out
    if cfg.enable_cone and accept(cone_c):
        dv = gp_Vec(cone_axis)
        ts = [gp_Vec(cone_apex, p).Dot(dv) for p in pts]
        lo, hi = min(ts), max(ts)
        r_lo = abs(lo) * math.tan(cone_alpha)
        r_hi = abs(hi) * math.tan(cone_alpha)
        if in_band(max(r_lo, r_hi)):
            out.kind = CONE
            out.max_dev_mm, out.rms_dev_mm = cone_c.max_dev_mm, cone_c.rms_dev_mm
            out.axis = gp_Ax1(cone_apex, cone_axis)
            out.cone_apex = cone_apex
            out.cone_semi_angle = cone_alpha
            out.p0 = cone_apex.Translated(dv.Multiplied(lo))
            out.p1 = cone_apex.Translated(dv.Multiplied(hi))
            out.r0, out.r1 = r_lo, r_hi
            return out
    if cfg.enable_sphere and accept(sph_c) and in_band(sph_radius):
        out.kind = SPHERE
        out.max_dev_mm, out.rms_dev_mm = sph_c.max_dev_mm, sph_c.rms_dev_mm
        out.sphere_center, out.sphere_radius = sph_center, sph_radius
        return out
    if cfg.enable_torus and accept(tor_c) and in_band(tor_small) and in_band(tor_big):
        out.kind = TORUS
        out.max_dev_mm, out.rms_dev_mm = tor_c.max_dev_mm, tor_c.rms_dev_mm
        out.axis = tor_axis
        out.torus_center = tor_axis.Location()
        out.torus_major_r = tor_big
        out.torus_minor_r = tor_small
        return out
    return out


def canonical_promotion_enabled() -> bool:
    """Off unless DFM_CANONICAL_PROMOTION is set for this process."""
    return os.environ.get("DFM_CANONICAL_PROMOTION") is not None


def apply_canonical_promotion(face, node: AagNode,
                              cfg: Optional[CanonicalRecognizerConfig] = None) -> bool:
    """Rewrite a freeform node as the primitive it fits, when one is accepted.

    The freeform curvature statistics are kept on a promoted face on purpose:
    the freeform rule family gates on them, and clearing them would trade one
    false negative for another.
    """
    fit = recognize_canonical(face, cfg)
    if not fit.accepted():
        return False
    native = node.surface_type
    if fit.kind == PLANE:
        node.surface_type = SurfaceType.PLANE
        node.plane_normal = fit.plane_normal
    elif fit.kind == CYLINDER:
        node.surface_type = SurfaceType.CYLINDER
        node.cyl_cone_axis = fit.axis
        node.cyl_radius = fit.radius
        node.cyl_p0, node.cyl_p1 = fit.p0, fit.p1
    elif fit.kind == CONE:
        node.surface_type = SurfaceType.CONE
        node.cyl_cone_axis = fit.axis
        node.cone_semi_angle = fit.cone_semi_angle
        node.cone_p0, node.cone_p1 = fit.p0, fit.p1
        node.cone_r0, node.cone_r1 = fit.r0, fit.r1
    elif fit.kind == SPHERE:
        node.surface_type = SurfaceType.SPHERE
        node.sphere_center = fit.sphere_center
        node.sphere_radius = fit.sphere_radius
    elif fit.kind == TORUS:
        node.surface_type = SurfaceType.TORUS
        node.torus_major_r = fit.torus_major_r
        node.torus_minor_r = fit.torus_minor_r
        node.torus_axis = fit.axis
    else:
        return False
    node.canonical_promoted = True
    node.canonical_native_type = native
    node.canonical_deviation_mm = fit.max_dev_mm
    return True
