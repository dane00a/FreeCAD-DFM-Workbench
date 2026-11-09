# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The fold topology of a sheet part: panels, the folds joining them, circuits.

A panel is metal, not a face id. Boolean seams split one physical flat into
several faces, and the two skins of a panel are two faces one gauge apart;
the panel graph interns each coplanar patch once and links two panels per
recognized fold. Over that graph the fold-topology question is asked:

* acyclic, and every fold accounted for -- foldable from one blank as a tree;
* a circuit whose panel normals turn a full 360 degrees -- a closed section,
  which cannot be folded from one blank at all;
* a circuit that does not close -- a loop needing a seam or a cut;
* anything the engine could not see -- INDETERMINATE, and said so.

The last is the point of the whole module. An absent fold leaves two joined
panels looking unjoined, and an unfoldability test on a holed graph does not
lose precision: it answers "acyclic, therefore foldable" with confidence. So
every fold the census cannot place -- a fold family recognized but not
walkable, a fold nothing recognized at all -- is recorded, and a recorded
drop blocks every foldability verdict.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Vec

from .aag import AttributedAdjacencyGraph, Concavity, SurfaceType
from .features import FeatureType
from .sheet_folds import (
    CoplanarPatchIndex,
    axes_colinear,
    band_axial_span,
    bend_band_is_sheared_free,
    cone_radius_at,
    cone_semi_angle_along,
    detect_sharp_folds,
    pair_sheet_skins,
)


# =============================================================================
# The invisible-fold census
# =============================================================================

#: Fold families nothing recognized, by name and by refusal code.
INVISIBLE_FAMILY_NAMES = {
    "conical": "CONICAL_BEND_UNRECOGNISED",
    "rolled": "ROLLED_SECTION_UNRECOGNISED",
    "sharp_fold": "SHARP_FOLD_UNMODELLED",
}


@dataclass
class InvisibleFold:
    family: str  # conical | rolled | sharp_fold
    faces: list[int] = field(default_factory=list)


def _tangent_planar_neighbour_count(graph: AttributedAdjacencyGraph, face_id: int) -> int:
    count = 0
    for edge in graph.edges_of(face_id):
        if edge.concavity is not Concavity.TANGENT:
            continue
        other = edge.other_face(face_id)
        if graph.has_node(other) and graph.node(other).surface_type is SurfaceType.PLANE:
            count += 1
    return count


def census_invisible_folds(
    graph: AttributedAdjacencyGraph, gauge: float, features: Sequence
) -> list[InvisibleFold]:
    """Folds present in the geometry that no recognized feature accounts for.

    A fold we DECLINED is not a fold we MISSED: a lanced strip is declined by
    the bend pass on purpose and is not counted here either, which is why the
    sheared-free test is the same one.
    """
    out: list[InvisibleFold] = []
    if gauge <= 1e-6:
        return out
    claimed: set[int] = set()
    for feature in features:
        claimed.update(feature.faces)

    cylinders = graph.nodes_by_surface_type(SurfaceType.CYLINDER)
    for inner in cylinders:
        if not inner.is_internal or inner.cyl_cone_axis is None:
            continue
        if inner.face_id in claimed:
            continue
        if _tangent_planar_neighbour_count(graph, inner.face_id) != 0:
            continue
        direction = inner.cyl_cone_axis.Direction()
        origin = inner.cyl_cone_axis.Location()
        i_lo, i_hi = band_axial_span(inner, origin, direction)
        for outer in cylinders:
            if outer.is_internal or outer.cyl_cone_axis is None:
                continue
            if outer.face_id in claimed:
                continue
            if abs((outer.cyl_radius - inner.cyl_radius) - gauge) > 0.2:
                continue
            if not axes_colinear(outer.cyl_cone_axis, inner.cyl_cone_axis):
                continue
            c_lo, c_hi = band_axial_span(outer, origin, direction)
            if c_hi < i_lo + 0.5 or c_lo > i_hi - 0.5:
                continue
            if bend_band_is_sheared_free(graph, inner, outer, gauge):
                continue
            out.append(InvisibleFold("rolled", [inner.face_id, outer.face_id]))
            break

    cones = graph.nodes_by_surface_type(SurfaceType.CONE)
    for inner in cones:
        if not inner.is_internal or inner.cyl_cone_axis is None:
            continue
        if inner.face_id in claimed:
            continue
        direction = inner.cyl_cone_axis.Direction()
        origin = inner.cyl_cone_axis.Location()
        semi = cone_semi_angle_along(inner, direction)
        i_lo, i_hi = band_axial_span(inner, origin, direction)
        for outer in cones:
            if outer.is_internal or outer.cyl_cone_axis is None:
                continue
            if outer.face_id in claimed:
                continue
            if abs(cone_semi_angle_along(outer, direction) - semi) > 0.02:
                continue
            if not axes_colinear(outer.cyl_cone_axis, inner.cyl_cone_axis):
                continue
            c_lo, c_hi = band_axial_span(outer, origin, direction)
            if c_hi < i_lo + 0.5 or c_lo > i_hi - 0.5:
                continue
            s = 0.5 * (max(i_lo, c_lo) + min(i_hi, c_hi))
            gap = abs(
                cone_radius_at(outer, origin, direction, s)
                - cone_radius_at(inner, origin, direction, s)
            ) * math.cos(semi)
            if abs(gap - gauge) > 0.25:
                continue
            out.append(InvisibleFold("conical", [inner.face_id, outer.face_id]))
            break

    modelled = {
        str(f.parameters.get("fold_key"))
        for f in features
        if f.type == FeatureType.BEND and f.parameters.get("fold_key")
    }
    for fold in detect_sharp_folds(graph, gauge, features):
        if fold.fold_key in modelled:
            continue
        out.append(InvisibleFold("sharp_fold", [fold.panel_a, fold.panel_b]))
    return out


# =============================================================================
# The panel graph
# =============================================================================


@dataclass
class PanelNode:
    patch: int
    companion: Optional[int] = None  # the opposing skin's patch, one gauge away
    area_mm2: float = 0.0
    outward_normal: Optional[gp_Dir] = None
    representative: int = 0  # anchor face, for messages only


@dataclass
class PanelLink:
    a: int
    b: int
    family: str = "unknown"
    feature_ids: list[str] = field(default_factory=list)
    axis: Optional[gp_Ax1] = None
    inner_radius_mm: Optional[float] = None
    included_angle_deg: Optional[float] = None
    length_mm: Optional[float] = None
    fold_sign_known: bool = False
    signed_angle_deg: Optional[float] = None
    fold_toward: Optional[gp_Dir] = None


@dataclass
class UnlinkedFold:
    """A recognized fold the graph could not place, and why."""

    feature_id: str
    family: str
    refusal: str
    panel_a: Optional[int]
    panel_b: Optional[int]


@dataclass
class PanelGraph:
    nodes: list[PanelNode] = field(default_factory=list)
    links: list[PanelLink] = field(default_factory=list)
    orphan_patches: list[int] = field(default_factory=list)
    unlinked_folds: list[UnlinkedFold] = field(default_factory=list)
    parallel_links_collapsed: bool = False
    collapsed_link_count: int = 0

    def root(self) -> Optional[int]:
        """The largest panel, which the walks start from."""
        if not self.nodes:
            return None
        best = 0
        for index in range(1, len(self.nodes)):
            if self.nodes[index].area_mm2 > self.nodes[best].area_mm2:
                best = index
        return best


def _family_of(feature) -> str:
    family = str(feature.parameters.get("bend_family") or "")
    if family in ("cylindrical", ""):
        return "hem" if feature.parameters.get("is_hem") else "cylindrical"
    if family in ("conical", "rolled"):
        return family
    if family == "sharp":
        return "sharp_fold"
    return "unknown"


def _as_dir(raw) -> Optional[gp_Dir]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    x, y, z = (float(raw[0]), float(raw[1]), float(raw[2]))
    if math.sqrt(x * x + y * y + z * z) <= 1e-9:
        return None
    return gp_Dir(x, y, z)


def _as_int(raw) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def build_panel_graph(
    index: CoplanarPatchIndex,
    graph: AttributedAdjacencyGraph,
    features: Sequence,
    gauge: float,
) -> PanelGraph:
    """Intern each fold's two panels as patches and link them.

    A fold whose panel is not a plane -- a rolled section -- cannot be
    interned: a panel node needs an outward normal, and a fabricated one
    would not refuse, it would produce a number the closure test then reads.
    Such a fold is RECORDED as unlinked instead, which blocks every
    foldability verdict downstream.

    Panels no fold reaches are orphans: metal the fold census does not
    account for, found by walking from the largest panel across links and
    across to each panel's own other skin.
    """
    result = PanelGraph()
    node_by_patch: dict[int, int] = {}

    def intern(face_id: Optional[int]) -> Optional[int]:
        if face_id is None:
            return None
        patch_id = index.patch_of(face_id)
        if patch_id is None:
            return None
        existing = node_by_patch.get(patch_id)
        if existing is not None:
            return existing
        patch = index.patch(patch_id)
        result.nodes.append(
            PanelNode(
                patch=patch_id,
                area_mm2=patch.total_area,
                outward_normal=patch.outward_normal,
                representative=patch.representative,
            )
        )
        node_by_patch[patch_id] = len(result.nodes) - 1
        return len(result.nodes) - 1

    for feature in features:
        if feature.type != FeatureType.BEND:
            continue
        pa = _as_int(feature.parameters.get("panel_a"))
        pb = _as_int(feature.parameters.get("panel_b"))
        # A fold with only one panel is still a fold. A rolled scroll's last
        # arc runs out to the blank's cut edge and carries no panel_b; skipping
        # it took the arc out of an accounting whose purpose is that no fold
        # goes missing. An empty side takes the un-internable path: recorded
        # as unlinked, never dropped.
        ia = intern(pa) if pa is not None else None
        ib = intern(pb) if pb is not None else None
        if ia is None or ib is None:
            refusal = str(feature.parameters.get("refusal") or "") or "FLANGE_FACE_NOT_PLANAR"
            result.unlinked_folds.append(
                UnlinkedFold(feature.instance_id, _family_of(feature), refusal, pa, pb)
            )
            continue
        link = PanelLink(a=ia, b=ib, family=_family_of(feature))
        link.feature_ids.append(feature.instance_id)
        params = feature.parameters
        if "signed_angle_deg" in params:
            link.fold_sign_known = True
            link.signed_angle_deg = float(params.get("signed_angle_deg") or 0.0)
        link.fold_toward = _as_dir(params.get("fold_toward"))
        axis = _as_dir(params.get("axis"))
        origin = params.get("axis_origin")
        if axis is not None and isinstance(origin, (list, tuple)) and len(origin) >= 3:
            link.axis = gp_Ax1(
                gp_Pnt(float(origin[0]), float(origin[1]), float(origin[2])), axis
            )
        if "inner_radius_mm" in params:
            link.inner_radius_mm = float(params.get("inner_radius_mm") or 0.0)
        if "angle_deg" in params:
            link.included_angle_deg = float(params.get("angle_deg") or 0.0)
        if "length_mm" in params:
            link.length_mm = float(params.get("length_mm") or 0.0)
        result.links.append(link)

    # Each patch's other skin, one gauge away.
    skins = pair_sheet_skins(graph, gauge, set())
    companion_of: dict[int, int] = {}
    for face_id, partner in skins.partner_of.items():
        a = index.patch_of(face_id)
        b = index.patch_of(partner)
        if a is None or b is None or a == b:
            continue
        companion_of.setdefault(a, b)
    for node in result.nodes:
        node.companion = companion_of.get(node.patch)

    # The metal the census must account for: every skin pair with a panel
    # many gauges across, named by its lower patch.
    min_area = 20.0 * gauge * gauge
    universe: set[int] = set()
    for pv, cv in sorted(companion_of.items()):
        if max(index.patch(pv).total_area, index.patch(cv).total_area) < min_area:
            continue
        universe.add(min(pv, cv))

    def canonical(pv: int) -> int:
        other = companion_of.get(pv)
        return pv if other is None else min(pv, other)

    root = result.root()
    if root is not None and universe:
        seen = [False] * len(result.nodes)
        stack = [root]
        seen[root] = True
        reached = {canonical(result.nodes[root].patch)}
        while stack:
            current = stack.pop()
            for link in result.links:
                if link.a == current:
                    nxt = link.b
                elif link.b == current:
                    nxt = link.a
                else:
                    continue
                if nxt == current or seen[nxt]:
                    continue
                seen[nxt] = True
                reached.add(canonical(result.nodes[nxt].patch))
                stack.append(nxt)
            companion = companion_of.get(result.nodes[current].patch)
            if companion is not None:
                nxt = node_by_patch.get(companion)
                if nxt is not None and not seen[nxt]:
                    seen[nxt] = True
                    reached.add(canonical(result.nodes[nxt].patch))
                    stack.append(nxt)
        result.orphan_patches = [pv for pv in sorted(universe) if pv not in reached]
    return result


def _folds_the_same_way(a: Optional[gp_Dir], b: Optional[gp_Dir]) -> bool:
    if a is None or b is None:
        return True  # cannot separate them
    return gp_Vec(a).Dot(gp_Vec(b)) > 0.0


def collapse_parallel_links(panel_graph: PanelGraph) -> int:
    """Merge links that are one fold split by a relief.

    Same two panels, same fold line, same radius and angle, folding the same
    way: a bend line interrupted by a relief cut comes back as two bends, and
    it is one fold of the blank.
    """
    kept: list[PanelLink] = []
    removed = 0
    for link in panel_graph.links:
        into = None
        for k in kept:
            same_pair = (k.a == link.a and k.b == link.b) or (k.a == link.b and k.b == link.a)
            if not same_pair:
                continue
            if k.axis is None or link.axis is None:
                continue
            if not axes_colinear(k.axis, link.axis):
                continue
            if (
                k.inner_radius_mm is not None
                and link.inner_radius_mm is not None
                and abs(k.inner_radius_mm - link.inner_radius_mm) > 0.05
            ):
                continue
            if (
                k.included_angle_deg is not None
                and link.included_angle_deg is not None
                and abs(k.included_angle_deg - link.included_angle_deg) > 1.0
            ):
                continue
            if not _folds_the_same_way(k.fold_toward, link.fold_toward):
                continue
            into = k
            break
        if into is None:
            kept.append(link)
            continue
        into.feature_ids.extend(link.feature_ids)
        if into.length_mm is not None and link.length_mm is not None:
            into.length_mm += link.length_mm
        removed += 1
    panel_graph.links = kept
    panel_graph.parallel_links_collapsed = True
    panel_graph.collapsed_link_count = removed
    return removed


def merge_aligned_bends(panel_graph: PanelGraph) -> int:
    """The MINIMUM number of brake hits: folds on one line, one radius, one
    angle and one direction are formed in a single hit, whichever panels they
    join."""
    ops: list[dict] = []
    for link in panel_graph.links:
        into = None
        for op in ops:
            if op["axis"] is None or link.axis is None:
                continue
            if not axes_colinear(op["axis"], link.axis):
                continue
            if (
                op["radius"] is not None
                and link.inner_radius_mm is not None
                and abs(op["radius"] - link.inner_radius_mm) > 0.05
            ):
                continue
            if (
                op["angle"] is not None
                and link.included_angle_deg is not None
                and abs(op["angle"] - link.included_angle_deg) > 1.0
            ):
                continue
            if not _folds_the_same_way(op["toward"], link.fold_toward):
                continue
            into = op
            break
        if into is None:
            ops.append(
                {
                    "axis": link.axis,
                    "toward": link.fold_toward,
                    "radius": link.inner_radius_mm,
                    "angle": link.included_angle_deg,
                }
            )
    return len(ops)


# =============================================================================
# Circuits and the verdict
# =============================================================================

#: Johnson's enumeration is exponential in the worst case; past this many
#: circuits the answer is refused rather than guessed.
MAX_CIRCUITS = 256


@dataclass
class FtgCircuit:
    nodes: list[int] = field(default_factory=list)
    links: list[int] = field(default_factory=list)
    closure_known: bool = False
    closure_deg: float = 0.0
    closed_section: bool = False
    closure_refusal: str = ""


def _canonical_circuit(cycle: list[int]) -> tuple[int, ...]:
    def rotate_min(values: list[int]) -> list[int]:
        i = values.index(min(values))
        return values[i:] + values[:i]

    forward = rotate_min(list(cycle))
    backward = rotate_min(list(reversed(cycle)))
    return tuple(min(forward, backward))


def elementary_circuits(panel_graph: PanelGraph) -> tuple[list[FtgCircuit], bool]:
    """Every elementary circuit of the panel graph (Johnson), and whether the
    enumeration was capped."""
    out: list[FtgCircuit] = []
    n = len(panel_graph.nodes)
    if n == 0:
        return out, False
    adjacency: list[list[tuple[int, int]]] = [[] for _ in range(n)]
    for li, link in enumerate(panel_graph.links):
        if link.a >= n or link.b >= n or link.a == link.b:
            continue
        adjacency[link.a].append((link.b, li))
        adjacency[link.b].append((link.a, li))

    seen: set[tuple[int, ...]] = set()
    blocked = [False] * n
    blocks: list[set[int]] = [set() for _ in range(n)]
    stack_nodes: list[int] = []
    stack_links: list[int] = []
    state = {"capped": False, "start": 0}

    def unblock(u: int) -> None:
        blocked[u] = False
        pending = list(blocks[u])
        blocks[u].clear()
        for w in pending:
            if blocked[w]:
                unblock(w)

    def circuit(v: int) -> bool:
        start = state["start"]
        found = False
        stack_nodes.append(v)
        blocked[v] = True
        for to, li in adjacency[v]:
            if to < start:
                continue  # only vertices at or past the start
            if to == start:
                # Two links between one pair of panels close a circuit; the
                # same link walked back does not.
                if len(stack_nodes) == 2 and stack_links and stack_links[-1] == li:
                    continue
                if len(stack_nodes) < 2:
                    continue
                key = _canonical_circuit(stack_nodes)
                if key not in seen:
                    seen.add(key)
                    if len(out) >= MAX_CIRCUITS:
                        state["capped"] = True
                    else:
                        out.append(
                            FtgCircuit(nodes=list(stack_nodes), links=list(stack_links) + [li])
                        )
                found = True
            elif not blocked[to]:
                stack_links.append(li)
                if circuit(to):
                    found = True
                stack_links.pop()
            if state["capped"]:
                break
        if found:
            unblock(v)
        else:
            for to, _ in adjacency[v]:
                if to < start:
                    continue
                blocks[to].add(v)
        stack_nodes.pop()
        return found

    for start in range(n):
        if state["capped"]:
            break
        state["start"] = start
        for i in range(n):
            blocked[i] = False
            blocks[i].clear()
        stack_nodes.clear()
        stack_links.clear()
        circuit(start)
    return out, state["capped"]


def measure_closure(panel_graph: PanelGraph, c: FtgCircuit) -> None:
    """How far the panel normals turn around a circuit, about its fold axis.

    A full 360 degrees is a closed cross-section. Refused, with a reason,
    whenever the turn cannot be measured honestly: the folds are not on one
    axis, a panel normal runs along it, or a fold is a 180-degree hem whose
    sense the normals cannot give.
    """
    c.closure_known = False
    c.closed_section = False
    if len(c.nodes) < 2:
        c.closure_refusal = "DEGENERATE_CIRCUIT"
        return
    axis_dir: Optional[gp_Dir] = None
    for li in c.links:
        if li >= len(panel_graph.links) or panel_graph.links[li].axis is None:
            c.closure_refusal = "BEND_AXIS_MISSING"
            return
        d = panel_graph.links[li].axis.Direction()
        if axis_dir is None:
            axis_dir = d
        elif abs(axis_dir.Dot(d)) < 0.999:
            c.closure_refusal = "OBLIQUE_BEND_AXIS"
            return
    if axis_dir is None:
        c.closure_refusal = "BEND_AXIS_MISSING"
        return
    a = gp_Vec(axis_dir)
    projected: list[gp_Vec] = []
    for ni in c.nodes:
        v = gp_Vec(panel_graph.nodes[ni].outward_normal)
        v = v.Subtracted(a.Multiplied(v.Dot(a)))
        if v.Magnitude() < 0.1:
            c.closure_refusal = "PANEL_NORMAL_ALONG_FOLD_AXIS"
            return
        v.Normalize()
        projected.append(v)
    total = 0.0
    for i, p in enumerate(projected):
        q = projected[(i + 1) % len(projected)]
        dot = max(-1.0, min(1.0, p.Dot(q)))
        crs = p.Crossed(q).Dot(a)
        if dot < -1.0 + 1e-6 and abs(crs) < 1e-6:
            c.closure_refusal = "FOLD_SIGN_AMBIGUOUS"
            return
        total += math.atan2(crs, dot)
    c.closure_known = True
    c.closure_deg = abs(total) * 180.0 / math.pi
    c.closed_section = abs(c.closure_deg - 360.0) <= 15.0


UNFOLDABLE_TREE = "unfoldable_tree"
SEAM_REQUIRED = "seam_required"
CLOSED_SECTION = "closed_section"
INDETERMINATE = "indeterminate"


@dataclass
class FtgResult:
    circuits: list[FtgCircuit] = field(default_factory=list)
    verdict: str = INDETERMINATE
    refusals: list[str] = field(default_factory=list)
    node_count: int = 0
    link_count: int = 0
    collapsed_links: int = 0
    bend_operations: int = 0
    orphan_count: int = 0
    invisible_folds: int = 0
    unlinked_folds: int = 0
    invisible_fold_families: list[str] = field(default_factory=list)
    is_prismatic_closed_section: bool = False

    def closed_circuit(self) -> Optional[FtgCircuit]:
        for c in self.circuits:
            if c.closed_section:
                return c
        return None


def analyse_ftg(panel_graph: PanelGraph, invisible: Sequence[InvisibleFold]) -> FtgResult:
    """The foldability verdict, refused whenever the census is incomplete."""
    r = FtgResult(
        node_count=len(panel_graph.nodes),
        link_count=len(panel_graph.links),
        collapsed_links=panel_graph.collapsed_link_count,
        orphan_count=len(panel_graph.orphan_patches),
        invisible_folds=len(invisible),
        unlinked_folds=len(panel_graph.unlinked_folds),
        bend_operations=merge_aligned_bends(panel_graph),
        invisible_fold_families=sorted({f.family for f in invisible}),
    )
    if not panel_graph.parallel_links_collapsed:
        r.refusals.append("PANEL_GRAPH_NOT_COLLAPSED")
        r.verdict = INDETERMINATE
        return r
    r.circuits, capped = elementary_circuits(panel_graph)
    if capped:
        r.refusals.append("CIRCUIT_ENUMERATION_CAPPED")
        r.verdict = INDETERMINATE
        return r
    for c in r.circuits:
        measure_closure(panel_graph, c)

    codes: set[str] = {INVISIBLE_FAMILY_NAMES[f.family] for f in invisible}
    for unlinked in panel_graph.unlinked_folds:
        codes.add(unlinked.refusal or "FLANGE_FACE_NOT_PLANAR")

    if not r.circuits:
        r.verdict = UNFOLDABLE_TREE if not codes else INDETERMINATE
    else:
        any_closed = any(c.closed_section for c in r.circuits)
        any_unknown = False
        for c in r.circuits:
            if not c.closure_known:
                any_unknown = True
                if c.closure_refusal:
                    codes.add(c.closure_refusal)
        if any_closed:
            r.verdict = CLOSED_SECTION
            codes.add("CLOSED_SECTION_2PI")
            closed = r.closed_circuit()
            r.is_prismatic_closed_section = closed is not None and len(closed.links) >= 3
        elif any_unknown or codes:
            r.verdict = INDETERMINATE
        else:
            r.verdict = SEAM_REQUIRED
            codes.add("SEAM_REQUIRED")
    r.refusals.extend(sorted(codes))
    return r


@dataclass
class SheetFoldTopology:
    """Everything the fold-topology rules read, built once."""

    graph: PanelGraph
    invisible: list[InvisibleFold]
    ftg: FtgResult


def analyse_sheet_folds(
    graph: AttributedAdjacencyGraph, features: Sequence, gauge: float
) -> SheetFoldTopology:
    index = CoplanarPatchIndex(graph)
    panel_graph = build_panel_graph(index, graph, features, gauge)
    collapse_parallel_links(panel_graph)
    invisible = census_invisible_folds(graph, gauge, features)
    return SheetFoldTopology(panel_graph, invisible, analyse_ftg(panel_graph, invisible))
