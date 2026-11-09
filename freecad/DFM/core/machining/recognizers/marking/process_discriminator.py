# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Stage 2: ENGRAVED vs RAISED vs FORMED, from back-side evidence only.

From the front the three are identical -- which is how an engraved logo once
came back from the sheet-formed recognizer as an "emboss 170 mm tall". The
discriminator is the constant-thickness property of a deformation feature,
MEASURED rather than predicted: is the wall the same thickness under the mark
as beside it?

    FORMED    t_under == t_beside            the skin was displaced
    ENGRAVED  t_under == t_beside - depth    material was removed
    RAISED    t_under == t_beside + depth    material stands proud

Vertical walls and a gauge-scale transition blend are recorded as
corroboration and decide nothing. Free edges inside the footprint (a louver
shears its skin) would be a fourth corroborator; the graph has no "free edge"
attribute, and the mirror test already excludes louvers and lances.
"""

from __future__ import annotations

from ...aag import SurfaceType
from . import tuning as T
from .model import InkKind, Process, dot, norm, pnt, scale, sub
from .relief_generator import EMPTY, ReliefScene, _box_corners, bbox_in_plane_range, merge, trace_on


def _rect_area(ru, rv) -> float:
    return max(ru[1] - ru[0], 1e-9) * max(rv[1] - rv[0], 1e-9)


def _covered(au, av, bu, bv) -> float:
    ou = min(au[1], bu[1]) - max(au[0], bu[0])
    ov = min(av[1], bv[1]) - max(av[0], bv[0])
    if ou <= 0.0 or ov <= 0.0:
        return 0.0
    return (ou * ov) / _rect_area(au, av)


def _region_rect(scene: ReliefScene, r, host):
    """For a floored or proud mark the footprint is the INK; for a relieved
    plaque or a floorless mark it is the whole component, because there the
    displaced material is not the ink."""
    use_ink = bool(r.ink) and r.ink_kind is not InkKind.ISLAND
    acc = list(EMPTY)
    for f in (r.ink if use_ink else r.faces):
        merge(acc, scene.measure(f, host))
    return (acc[0], acc[1]), (acc[2], acc[3])


def _coaxial_v_span(frame, other):
    if other.bbox is None or other.bbox.IsVoid():
        return (1e18, -1e18)
    ts = [dot(sub(c, frame.axis_loc), frame.axis_dir) for c in _box_corners(other.bbox)]
    return (min(ts), max(ts))


def thickness_under(scene: ReliefScene, host, level, ru, rv):
    """Material under a rectangle, measured from a level: the nearest
    anti-parallel surface on the material side that covers half of it."""
    best = -1e18
    for o in scene.hosts:
        if o is host:
            continue
        if o.curved() != host.curved():
            continue
        if host.curved():
            if abs(dot(host.axis_dir, o.axis_dir)) < T.COPLANAR_DOT:
                continue
            off = sub(o.axis_loc, host.axis_loc)
            off = sub(off, scale(host.axis_dir, dot(off, host.axis_dir)))
            if norm(off) > T.COPLANAR_OFFSET_MM:
                continue
            if host.curv_sign * o.curv_sign > 0.0:
                continue
            s = host.curv_sign * (o.radius - host.radius)
        else:
            if dot(host.normal, o.normal) > -T.COPLANAR_DOT:
                continue
            s = host.signed_offset(o.origin)
        if s > level - 4.0 * T.NOISE_FLOOR_MM:
            continue
        if host.curved():
            # A coaxial pair is decided AXIALLY: where both surfaces exist
            # they share an angular station by construction, and a back side
            # lies outside the search window so it is never sampled anyway.
            ou = ru
            ov = _coaxial_v_span(host, o)
        else:
            if o.bbox is None or o.bbox.IsVoid():
                ou, ov = (1e18, -1e18), (1e18, -1e18)
            else:
                u0, u1, v0, v1 = bbox_in_plane_range(o.bbox, host)
                ou, ov = (u0, u1), (v0, v1)
        if _covered(ru, rv, ou, ov) < 0.5:
            continue
        best = max(best, s)
    if best < -1e17:
        return None
    return level - best


def _all_walls_vertical(r, host) -> bool:
    if not r.walls:
        return False
    for w in r.walls:
        n = pnt(w.plane_normal)
        if abs(dot(n, host.normal_at(pnt(w.centroid)))) > 1.0 - T.ALIGN_DOT:
            return False
    return True


def _has_gauge_scale_blend(graph, r, gauge: float) -> bool:
    """A transition blend about the size of the gauge: forming cannot make a
    sharp corner, and a milled floor fillet is the cutter radius."""
    if gauge <= 0.0:
        return False
    for f in r.faces:
        if f.surface_type not in (SurfaceType.CYLINDER, SurfaceType.TORUS):
            continue
        rad = f.cyl_radius if f.surface_type is SurfaceType.CYLINDER else f.torus_minor_r
        if rad >= 0.5 * gauge:
            for edge in graph.edges_of(f.face_id):
                if edge.is_tangent:
                    return True
    return False


def discriminate_processes(graph, scene: ReliefScene, rejects=None) -> None:
    """In place on scene.regions. Never rejects: FORMED is reclassified."""
    for i, r in enumerate(scene.regions):
        host = scene.hosts[scene.region_host[i]]
        tol = max(3.0 * T.NOISE_FLOOR_MM, T.DEPTH_CV_MAX * r.depth_mm)

        # Shallower than the measurement tolerance: indistinguishable from the
        # skin, and equally too shallow to be forming. Decided on side alone.
        if r.depth_mm <= tol:
            r.process = Process.RAISED if r.proud else Process.ENGRAVED
            r.process_evidence.append("shallower_than_measurement_tolerance")
            continue

        ru, rv = _region_rect(scene, r, host)
        # Beside the mark: probed from the host surface over its largest
        # face, which is by construction not the mark.
        probe = None
        best_area = -1.0
        for f in host.faces:
            if f.area > best_area and not f.bbox.IsVoid():
                best_area = f.area
                probe = f
        t_beside = None
        if probe is not None:
            m = scene.measure(probe, host)
            t_beside = thickness_under(scene, host, 0.0, (m[0], m[1]), (m[2], m[3]))
        t_under = thickness_under(scene, host, r.level_mean, ru, rv)

        if _all_walls_vertical(r, host):
            r.process_evidence.append("vertical_walls")
        if t_beside is not None and _has_gauge_scale_blend(graph, r, t_beside):
            r.process_evidence.append("gauge_scale_blend")

        if t_beside is None or t_under is None:
            r.process = Process.INDETERMINATE
            r.process_evidence.append(
                "no_support_surface_under_the_footprint"
                if t_beside is not None
                else "no_support_surface_beside_the_mark"
            )
            continue

        # The prediction is SIGNED by the level the thickness was measured
        # from, not rebuilt from an unsigned depth and the `proud` flag: for a
        # relieved plaque the letters read proud but the FIELD was cut, and
        # `proud ? +depth` was wrong by twice the depth there.
        expected_removed = t_beside + r.level_mean
        d_formed = abs(t_under - t_beside)
        d_cut = abs(t_under - expected_removed)

        r.mirror_gap_mm = t_under
        if d_formed <= tol and d_formed < d_cut:
            r.process = Process.FORMED
            r.process_evidence.append("constant_gauge_under_the_feature")
            continue
        if d_cut <= tol:
            r.process = Process.RAISED if r.proud else Process.ENGRAVED
            r.process_evidence.append(
                "thickness_increased_by_proud_height"
                if r.level_mean > 0.0
                else "thickness_reduced_by_depth"
            )
            r.process_evidence.append("back_skin_flat_and_undisturbed")
            continue
        r.process = Process.INDETERMINATE
        r.process_evidence.append("thickness_matches_neither_read")

    if trace_on():
        for i, r in enumerate(scene.regions):
            h = scene.hosts[scene.region_host[i]]
            print(
                f"[mark] region kind={r.ink_kind.value} proud={int(r.proud)} "
                f"d={r.depth_mm:.4g} lvl={r.level_mean:.4g} t_under={r.mirror_gap_mm:.4g} "
                f"t_host={h.local_thickness_mm:.4g} w={r.stroke_w_mm:.4g} "
                f"patch={r.max_patch_w_mm:.4g} eu={r.extent_u_mm:.4g} "
                f"ev={r.extent_v_mm:.4g} cu={r.centroid_u:.4g} cv={r.centroid_v:.4g} "
                f"diag={r.diag_mm:.4g} fill={r.fill:.4g} S={r.slenderness:.4g} "
                f"hostcov={r.host_coverage:.4g} P={r.ink_perimeter_mm:.4g} "
                f"faces={len(r.faces)} proc={r.process.value} "
                + " ".join("{" + e + "}" for e in r.process_evidence)
                + f" host={h.primary_face_id}"
            )
