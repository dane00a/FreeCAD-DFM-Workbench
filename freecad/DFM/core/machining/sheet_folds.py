# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""What the sheet-metal passes share about folds.

Several passes have to agree on what a fold is: the bend recognizer that
emits one, the sharp-fold rule that reports one nobody modelled, and the
census that counts the folds nothing could see. Each asking the question its
own way is how two of them came to disagree, so the answers live here once.

* :class:`CoplanarPatchIndex` -- which plane a flat belongs to. Boolean seams
  split one physical panel into several coplanar faces; this names the panel.
* :func:`bend_band_is_sheared_free` -- the crown of a lanced arch is a bend's
  surfaces exactly, and only what bounds the strip tells them apart.
* :func:`detect_sharp_folds` -- two panel skins meeting square, where a bend
  belongs and no radius was modelled.
* :func:`fold_sign` -- which way a fold turns, in a frame that is stated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from .aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from .features import FeatureType


# =============================================================================
# Small shared measurements
# =============================================================================


def outward_of(node: AagNode) -> Optional[gp_Dir]:
    """A plane's normal pointing out of material."""
    return node.outward_normal


def band_axial_span(node: AagNode, origin: gp_Pnt, direction: gp_Dir) -> tuple[float, float]:
    """How far a face's bounding box reaches along ``direction`` from ``origin``."""
    if node.bbox.IsVoid():
        return (0.0, 0.0)
    xn, yn, zn, xx, yx, zx = node.bbox.Get()
    d = gp_Vec(direction)
    values = [
        gp_Vec(origin, gp_Pnt(x, y, z)).Dot(d)
        for x in (xn, xx)
        for y in (yn, yx)
        for z in (zn, zx)
    ]
    return (min(values), max(values))


def band_mean_radius(node: AagNode) -> float:
    """One radius comparable across cylinders and cones.

    The cylinder's own radius, or the mean of a cone band's two end radii.
    ``cyl_radius`` is filled for cylinders only, so on a cone it reads 0 and
    any comparison between cones built on it passes for every cone on the
    part. The mean, not the reference radius: two coaxial cone bands one gauge
    apart over the same span have means exactly one gauge apart, while a
    frustum's reference radius can sit off the face entirely.
    """
    if node.surface_type is SurfaceType.CONE:
        return 0.5 * (node.cone_r0 + node.cone_r1)
    return node.cyl_radius


def cone_semi_angle_along(node: AagNode, direction: gp_Dir) -> float:
    """A cone's half-angle signed in the given frame.

    The same cone can be stored on either sense of its axis, with the
    half-angle's sign flipped to match -- which sense depends on how the
    solid was built, not on the geometry. Two coaxial cones are only
    comparable once both are read along one direction, so the half-angle is
    negated when the cone's own axis points the other way.
    """
    if node.cyl_cone_axis is None:
        return node.cone_semi_angle
    if node.cyl_cone_axis.Direction().Dot(direction) < 0.0:
        return -node.cone_semi_angle
    return node.cone_semi_angle


def cone_radius_at(node: AagNode, origin: gp_Pnt, direction: gp_Dir, s: float) -> float:
    """A cone band's radius at axial station ``s`` along the given frame."""
    if node.cone_p0 is None:
        return node.cone_r0
    s0 = gp_Vec(origin, node.cone_p0).Dot(gp_Vec(direction))
    return node.cone_r0 + (s - s0) * math.tan(cone_semi_angle_along(node, direction))


def axes_colinear(a, b, dot_tol: float = 0.999, line_tol: float = 0.2) -> bool:
    """Same line: directions agree and the origins sit on each other's axis."""
    if a is None or b is None:
        return False
    if abs(a.Direction().Dot(b.Direction())) < dot_tol:
        return False
    offset = gp_Vec(a.Location(), b.Location())
    return offset.Crossed(gp_Vec(a.Direction())).Magnitude() < line_tol


# =============================================================================
# Coplanar patches
# =============================================================================


@dataclass
class CoplanarPatch:
    """One connected run of coplanar faces: a panel as the metal sees it."""

    id: int
    plane: int
    outward_normal: gp_Dir
    member_faces: list[int] = field(default_factory=list)
    total_area: float = 0.0
    representative: int = 0


class CoplanarPatchIndex:
    """Every planar face, grouped by the plane it lies in and then by patch.

    Two faces share a plane when their outward normals agree within 0.999 and
    the second lies within 0.1 mm of the first member's centroid along that
    normal. Planes are numbered in face order, first come first named, so the
    numbering is reproducible.

    A PLANE is not a panel: the two halves of a channel's floor either side
    of a slot are coplanar and are separate metal. A PATCH is a connected run
    of one plane's faces through the adjacency graph, which is what a panel
    is. The representative is the largest member, for messages only.
    """

    NORMAL_DOT = 0.999
    OFFSET_TOL_MM = 0.1

    def __init__(self, graph: AttributedAdjacencyGraph) -> None:
        self._plane_of: dict[int, int] = {}
        self._patch_of: dict[int, int] = {}
        self.patches: list[CoplanarPatch] = []
        planes: list[tuple[gp_Dir, gp_Pnt]] = []
        members: list[list[int]] = []
        for node in graph.nodes:
            if node.surface_type is not SurfaceType.PLANE:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            found = None
            for index, (plane_normal, ref) in enumerate(planes):
                if plane_normal.Dot(normal) < self.NORMAL_DOT:
                    continue
                if abs(gp_Vec(ref, node.centroid).Dot(gp_Vec(plane_normal))) > self.OFFSET_TOL_MM:
                    continue
                found = index
                break
            if found is None:
                found = len(planes)
                planes.append((normal, node.centroid))
                members.append([])
            self._plane_of[node.face_id] = found
            members[found].append(node.face_id)

        for plane_index, plane_members in enumerate(members):
            seen: set[int] = set()
            for seed in plane_members:
                if seed in seen:
                    continue
                patch = CoplanarPatch(
                    id=len(self.patches),
                    plane=plane_index,
                    outward_normal=planes[plane_index][0],
                )
                stack = [seed]
                seen.add(seed)
                best_area = -1.0
                while stack:
                    current = stack.pop()
                    node = graph.node(current)
                    patch.member_faces.append(current)
                    patch.total_area += node.area
                    if node.area > best_area:
                        best_area = node.area
                        patch.representative = current
                    self._patch_of[current] = patch.id
                    for neighbour in graph.neighbors_of(current):
                        if neighbour in seen:
                            continue
                        if self._plane_of.get(neighbour) != plane_index:
                            continue
                        seen.add(neighbour)
                        stack.append(neighbour)
                patch.member_faces.sort()
                self.patches.append(patch)

    def plane_of(self, face_id: int) -> Optional[int]:
        return self._plane_of.get(face_id)

    def same_plane(self, a: int, b: int) -> bool:
        plane = self.plane_of(a)
        return plane is not None and plane == self.plane_of(b)

    def patch_of(self, face_id: int) -> Optional[int]:
        return self._patch_of.get(face_id)

    def patch(self, patch_id: int) -> CoplanarPatch:
        return self.patches[patch_id]


# =============================================================================
# A lanced strip is not a press-brake bend
# =============================================================================

#: A shear wall's normal runs along the fold axis.
SHEAR_NORMAL_DOT = 0.9
#: A shear wall sits AT the band's end.
SHEAR_END_TOL_MM = 0.5


def bend_band_is_sheared_free(
    graph: AttributedAdjacencyGraph,
    inner: AagNode,
    outer: AagNode,
    gauge: float,
) -> bool:
    """True when a bend-shaped band is the crown of a lanced strip.

    The crown of a lanced arch is a bend's signature exactly: a ramp plane
    tangent to two cylinders one gauge apart. Analytically that is no
    mistake, because the surfaces really are the same. What separates them is
    what the folded strip is bounded by. A press brake folds continuous metal
    and the band ends at the blank's outer profile; a lance is sheared first,
    so its band ends on two cuts through the gauge, interior to the blank,
    one at each end, each facing back across the band at the metal the strip
    was cut from. A brake cannot make that: its tooling would have to pass
    through the surrounding panel.

    Four conditions on one pair of walls, each doing separating work: the
    wall bridges the gauge (the outline recognizer's own definition of a wall
    cut through the blank); one sits at each end of the band; they face
    inward, one from each side -- which keeps a bend relieved at one end a
    bend, since a relief gives only one inward wall; and the shear line runs
    past the fold. Graph adjacency cannot scope this: the deck's shear wall
    can be three hops from the crown, and three hops on a small part reach
    every face. The scoping is geometric on purpose.
    """
    from .recognizers.sheet_outline_recognizer import is_sheet_profile_edge

    if gauge <= 0.0 or inner.cyl_cone_axis is None:
        return False
    direction = inner.cyl_cone_axis.Direction()
    origin = inner.cyl_cone_axis.Location()
    i_lo, i_hi = band_axial_span(inner, origin, direction)
    o_lo, o_hi = band_axial_span(outer, origin, direction)
    lo = min(i_lo, o_lo)
    hi = max(i_hi, o_hi)

    band_box = Bnd_Box()
    band_box.Add(inner.bbox)
    band_box.Add(outer.bbox)
    if band_box.IsVoid():
        return False
    bxn, byn, bzn, bxx, byx, bzx = band_box.Get()
    b_min = (bxn, byn, bzn)
    b_max = (bxx, byx, bzx)
    dc = (abs(direction.X()), abs(direction.Y()), abs(direction.Z()))
    if dc[0] >= dc[1] and dc[0] >= dc[2]:
        ax_d = 0
    elif dc[1] >= dc[2]:
        ax_d = 1
    else:
        ax_d = 2

    def shear_line_covers(wall: AagNode) -> bool:
        # The cut that freed the strip runs the length of the strip, so the
        # band lies within the wall's own long in-plane extent.
        if wall.bbox.IsVoid():
            return False
        wxn, wyn, wzn, wxx, wyx, wzx = wall.bbox.Get()
        w_min = (wxn, wyn, wzn)
        w_max = (wxx, wyx, wzx)
        run = -1
        best = -1.0
        for a in range(3):
            if a == ax_d:
                continue
            if w_max[a] - w_min[a] > best:
                best = w_max[a] - w_min[a]
                run = a
        if run < 0:
            return False
        return (
            b_min[run] >= w_min[run] - SHEAR_END_TOL_MM
            and b_max[run] <= w_max[run] + SHEAR_END_TOL_MM
        )

    at_lo = False
    at_hi = False
    for node in graph.nodes_by_surface_type(SurfaceType.PLANE):
        normal = node.outward_normal
        if normal is None:
            continue
        along = normal.Dot(direction)
        if abs(along) < SHEAR_NORMAL_DOT:
            continue
        w_lo, w_hi = band_axial_span(node, origin, direction)
        s = 0.5 * (w_lo + w_hi)
        # A wall at the HIGH end faces back down the axis and vice versa; one
        # facing the other way is the blank's outside, not a shear.
        ends_hi = along < 0.0 and abs(s - hi) < SHEAR_END_TOL_MM
        ends_lo = along > 0.0 and abs(s - lo) < SHEAR_END_TOL_MM
        if not ends_hi and not ends_lo:
            continue
        if not shear_line_covers(node):
            continue
        if not is_sheet_profile_edge(graph, node.face_id, gauge):
            continue
        at_hi = at_hi or ends_hi
        at_lo = at_lo or ends_lo
    return at_lo and at_hi


# =============================================================================
# Fold sign
# =============================================================================


@dataclass
class FoldSign:
    """Which way a fold turns.

    ``signed_deg`` is the turn from the first panel's outward normal to the
    second's about the fold axis, both projected into the axis-perpendicular
    plane first. There is no absolute sign -- a sheet has two skins and no
    intrinsic up -- so it is only meaningful with the frame it was taken in.
    ``toward`` is the frame-free one: the bisector of the two outward
    normals, pointing from the crease toward the fold's concave side.
    """

    signed_deg: float = 0.0
    sign_known: bool = False
    toward: Optional[gp_Dir] = None
    toward_known: bool = False


def fold_sign(n_from: gp_Dir, n_to: gp_Dir, axis: gp_Dir) -> FoldSign:
    """Sign a fold from its two panel outward normals.

    A 180-degree fold (a hem) cannot be signed: antiparallel normals have no
    turn whose sense the cross product could give, and no bisector.
    """
    result = FoldSign()
    a = gp_Vec(axis)
    uf = gp_Vec(n_from)
    ut = gp_Vec(n_to)
    uf = uf.Subtracted(a.Multiplied(uf.Dot(a)))
    ut = ut.Subtracted(a.Multiplied(ut.Dot(a)))
    if uf.Magnitude() >= 0.1 and ut.Magnitude() >= 0.1:
        uf.Normalize()
        ut.Normalize()
        dot = max(-1.0, min(1.0, uf.Dot(ut)))
        crs = uf.Crossed(ut).Dot(a)
        if not (dot < -1.0 + 1e-6 and abs(crs) < 1e-6):
            result.signed_deg = math.degrees(math.atan2(crs, dot))
            result.sign_known = True
    bisector = gp_Vec(n_from).Added(gp_Vec(n_to))
    if bisector.Magnitude() >= 0.1:
        bisector.Normalize()
        result.toward = gp_Dir(bisector)
        result.toward_known = True
    return result


# =============================================================================
# Sharp folds
# =============================================================================


def gauge_thin_bbox(node: AagNode, gauge: float) -> bool:
    """A face no wider than the gauge across its second-smallest extent."""
    dims = sorted(node.bbox_dims())
    return dims[1] <= gauge * 1.3


@dataclass
class SkinPairing:
    """Planar faces that are one skin of a gauge-thick slab, and their partner."""

    partner_of: dict[int, int] = field(default_factory=dict)

    def is_skin(self, face_id: int) -> bool:
        return face_id in self.partner_of

    def canonical(self, face_id: int) -> int:
        partner = self.partner_of.get(face_id)
        if partner is None:
            return face_id
        return min(partner, face_id)


def pair_sheet_skins(
    graph: AttributedAdjacencyGraph, gauge: float, excluded: set[int]
) -> SkinPairing:
    """Pair every non-strip plane with an antiparallel plane one gauge away.

    A sheared edge strip can find a "partner" of its own -- any cutout wall
    one gauge inboard of the outline -- but a panel and its own cut edge do
    not make a fold, so strips are excluded before pairing starts.
    """
    out = SkinPairing()
    if gauge <= 1e-6:
        return out
    planes = graph.nodes_by_surface_type(SurfaceType.PLANE)
    for p in planes:
        if gauge_thin_bbox(p, gauge):
            continue
        if p.face_id in excluded:
            continue
        np_ = p.outward_normal
        if np_ is None:
            continue
        for q in planes:
            if q is p:
                continue
            nq = q.outward_normal
            if nq is None or nq.Dot(np_) > -0.999:
                continue
            d = abs(gp_Vec(p.centroid, q.centroid).Dot(gp_Vec(np_)))
            if abs(d - gauge) > 0.25:
                continue
            if p.bbox.IsVoid() or q.bbox.IsVoid():
                continue
            reach = Bnd_Box()
            reach.Add(p.bbox)
            reach.Enlarge(gauge + 0.5)
            if reach.IsOut(q.bbox):
                continue
            out.partner_of[p.face_id] = q.face_id
            break
    return out


@dataclass
class SharpFold:
    panel_a: int
    panel_b: int
    fold_key: str
    crease_midpoint: Optional[gp_Pnt]
    crease_length_mm: float
    n_a: gp_Dir
    n_b: gp_Dir
    crease_dir: Optional[gp_Dir]
    fold_angle_deg: float


def sharp_fold_key(canon_a: int, canon_b: int) -> str:
    """Canonical, order-free identity of a fold, built from its two skin pairs."""
    low, high = sorted((canon_a, canon_b))
    return f"{low}|{high}"


#: Faces owned by these are not sharp-fold candidates: a formed hood's crest
#: corners are coined sharp on purpose, and a bend is a fold done right.
_FOLD_OWNERS = (FeatureType.BEND, FeatureType.SHEET_FORMED, FeatureType.TAB, FeatureType.NOTCH)

#: A panel is many gauges across; below this it is a sliver or an outline
#: fragment, and a junction it takes part in is not a fold.
_MIN_PANEL_AREA_GAUGES = 25.0


def detect_sharp_folds(
    graph: AttributedAdjacencyGraph,
    gauge: float,
    features: Iterable,
) -> list[SharpFold]:
    """Every zero-radius fold: two panel skins meeting square.

    The one walk the sharp-fold rule, the bend census and the invisible-fold
    count all share, so the three cannot disagree. Both skins of the joining
    slab meet the panel, so one fold arrives twice; naming each side by the
    lower id of its own skin pair collapses the two.
    """
    out: list[SharpFold] = []
    if gauge <= 1e-6:
        return out
    feature_faces: set[int] = set()
    for feature in features:
        if feature.type in _FOLD_OWNERS:
            feature_faces.update(feature.faces)
    skins = pair_sheet_skins(graph, gauge, feature_faces)
    min_panel_area = _MIN_PANEL_AREA_GAUGES * gauge * gauge
    seen: set[tuple[int, int]] = set()
    for p in graph.nodes_by_surface_type(SurfaceType.PLANE):
        if not skins.is_skin(p.face_id):
            continue
        if p.area < min_panel_area:
            continue
        np_ = p.outward_normal
        if np_ is None:
            continue
        for edge in graph.edges_of(p.face_id):
            if edge.concavity is Concavity.TANGENT:
                continue
            other = edge.other_face(p.face_id)
            if not skins.is_skin(other):
                continue
            if other in feature_faces:
                continue
            if not graph.has_node(other):
                continue
            q = graph.node(other)
            if q.area < min_panel_area:
                continue
            nq = q.outward_normal
            if nq is None or abs(nq.Dot(np_)) > 0.7:
                continue
            ca = skins.canonical(p.face_id)
            cb = skins.canonical(other)
            key = (min(ca, cb), max(ca, cb))
            if key in seen:
                continue
            seen.add(key)
            cross = gp_Vec(np_).Crossed(gp_Vec(nq))
            crease = gp_Dir(cross) if cross.Magnitude() > 1e-12 else None
            out.append(
                SharpFold(
                    panel_a=p.face_id,
                    panel_b=other,
                    fold_key=sharp_fold_key(ca, cb),
                    crease_midpoint=edge.midpoint,
                    crease_length_mm=edge.shared_edge_length,
                    n_a=np_,
                    n_b=nq,
                    crease_dir=crease,
                    fold_angle_deg=math.degrees(np_.Angle(nq)),
                )
            )
    return out


def feature_fold_refusals(features: Sequence) -> list[str]:
    """Every distinct refusal a BEND carries, sorted."""
    codes = set()
    for feature in features:
        if feature.type != FeatureType.BEND:
            continue
        code = feature.parameters.get("refusal")
        if code:
            codes.add(str(code))
    return sorted(codes)
