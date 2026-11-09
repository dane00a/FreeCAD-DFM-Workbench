# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Deciding what a piece of geometry actually is when the recognizers disagree.

The recognizers are deliberately independent and deliberately eager. Each one
answers its own narrow question and none of them can see the whole part, so
the same faces get claimed several times over: a counterbore is also a blind
hole, a pocket wall is also a step, the strokes of an engraved serial number
are a dozen tiny slots. Every one of those readings is locally correct. Only
one of them is what the machinist is going to do.

Resolving that is a priority question, not a geometry one. When two features
share most of their faces, the more specific reading wins -- a counterbore
says more than the blind hole inside it, and a groove says more than the
counterbore it was mistaken for. The table at the top of this module is that
ordering, and it is the whole of the policy.

Two kinds of feature are exempt, because they are annotations rather than
readings. An undercut says the tool cannot reach a face; a draft says the wall
is tapered. Both are true *as well as* whatever the face otherwise is, so
letting a pocket displace them would erase exactly the manufacturing
constraint that was worth finding. Marking text is the one thing that
outranks an undercut, because nobody machines a serial number and the
"undercut" on a stroke wall is an artefact of the ray cast.

The second half of the module does something different: it puts back together
one bore that the recognizers saw as two. A hole crossed by another hole
arrives as fragments, and reporting a 20 mm hole and a 25 mm hole where the
part has one 48 mm hole misstates both the count and the depth. The guards
there are all about not merging two holes that genuinely face each other.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Vec

from .aag import AttributedAdjacencyGraph, SurfaceType
from .features import GROOVE_TYPES, FeatureInstance, FeatureType, RecognitionResult
from .marking_field import absorb_marking_field


# How authoritative each reading is. Higher wins when two features describe
# the same faces.
_PRIORITY: dict[str, int] = {
    # Marking sits at the top on its own. A text block's face set is the
    # union of every stroke, which is a superset of the per-stroke slot,
    # undercut and boss claims the other recognizers make on it. Nobody
    # end-mills a serial number, so those readings and the rules that would
    # fire on them must not survive.
    FeatureType.MARKING_TEXT: 200,
    # Formed sheet outranks every machined reading of the same faces. An
    # emboss reads as a boss over a pocket, a louver as a through cavity, a
    # notch as a step -- each correct had the part been cut from solid, and
    # each wrong on a part that came off a press. Without these the sheet
    # rules can never fire on a real part: the machining readings win every
    # overlap and the formed feature disappears before any rule sees it.
    FeatureType.SHEET_FORMED: 160,
    FeatureType.BEND: 155,
    # The groove family sits above counterbore: a groove's faces are a
    # superset of what the counterbore path emits for the same geometry, so
    # a counterbore mistaken for a groove is wholly contained and dropped.
    FeatureType.THREAD_RELIEF_GROOVE: 150,
    FeatureType.O_RING_GLAND: 140,
    FeatureType.RETAINING_RING_GROOVE: 130,
    FeatureType.GROOVE: 120,
    # Hole variants carry the most specific semantics there are -- a thread,
    # a counterbore seat, a countersink angle -- and must never lose to the
    # plain bore they necessarily overlap.
    FeatureType.THREADED_HOLE: 100,
    FeatureType.COUNTERBORE: 100,
    FeatureType.COUNTERSINK: 100,
    FeatureType.BLIND_HOLE: 80,
    # The same bore as a blind hole, differing only in what its far end
    # runs into.
    FeatureType.BREAKTHROUGH_HOLE: 80,
    FeatureType.THROUGH_HOLE: 80,
    FeatureType.PARTIAL_BORE: 80,
    # A cavity that passes right through says more than a pocket, and the
    # pocket and slot passes both seed on its walls.
    FeatureType.THROUGH_CAVITY: 70,
    # The slit family above pocket: the pocket pass seeds on a slit floor and
    # misreads the open channel as a deep narrow pocket.
    FeatureType.FLEXURE_SLIT: 65,
    FeatureType.BROACHED_SLOT: 65,
    FeatureType.V_GROOVE: 65,
    FeatureType.POCKET: 60,
    FeatureType.SLOT: 50,
    # A tab and a notch are cuts in the outline of a blank. The step
    # recognizer reads a notch's shoulder as a terrace, which it is, and
    # says nothing useful about it.
    FeatureType.TAB: 45,
    FeatureType.NOTCH: 45,
    FeatureType.STEP: 40,
}

# Everything else: boss, rib, fillet, chamfer, draft, undercut, unknown.
_DEFAULT_PRIORITY = 10

# One feature is inside another when it shares this much of its faces.
_CONTAINMENT_RATIO = 0.8

# A cavity this much longer than it is wide is a slot, whatever the pocket
# pass thought.
_SLOT_PREFER_ASPECT = 2.0

# Axes count as the same line within this, in millimetres and in dot product.
_AXIS_ALIGNMENT = 0.99
_AXIS_OFFSET_MM = 0.5

# Bore radii have to agree this closely to be one hole.
_RADIUS_TOLERANCE_MM = 1.0e-3

# How far either side of a gap a cap still blocks a merge. A blind hole's
# floor sits exactly at its fragment's facing end, so the window has to
# include its own boundary.
_CAP_WINDOW_MM = 0.6

# Spans overlapping by more than this are duplicates rather than fragments.
_SPAN_OVERLAP_MM = 0.5


def feature_priority(feature_type: str) -> int:
    return _PRIORITY.get(feature_type, _DEFAULT_PRIORITY)


def resolve(
    features: Sequence[FeatureInstance], graph: AttributedAdjacencyGraph
) -> RecognitionResult:
    """Settle overlapping claims and rejoin split bores."""
    survivors = list(features)
    dropped = [False] * len(survivors)
    result = RecognitionResult()

    # Everything inside a MARKING_TEXT's own relief envelope is that mark's
    # cut seen a second time, whether or not the clustering claimed those
    # exact faces. Before the pairwise pass, so the survivors are resolved
    # among themselves without the marking noise in the way.
    absorb_marking_field(survivors, graph, dropped)
    # Who beat whom in the containment dedup, so a feature dropped as a
    # duplicate of X does not vanish when X is itself dropped later; and
    # which drops were on merit (something else explained the geometry
    # better), which are final.
    beaten_by = [-1] * len(survivors)
    on_merit = [False] * len(survivors)
    _resolve_overlaps(survivors, dropped, result, beaten_by, on_merit)
    _merge_split_bores(survivors, dropped, graph, on_merit)
    _revive_orphaned_duplicates(survivors, dropped, beaten_by, on_merit)

    result.features = [
        feature for index, feature in enumerate(survivors) if not dropped[index]
    ]
    return result


# =============================================================================
# Overlapping claims
# =============================================================================


def _resolve_overlaps(
    features, dropped, result: RecognitionResult, beaten_by, on_merit
) -> None:
    count = len(features)
    for i in range(count):
        if dropped[i]:
            continue
        for j in range(i + 1, count):
            if dropped[j]:
                continue

            first, second = features[i], features[j]
            shared = set(first.faces) & set(second.faces)
            if not shared:
                continue

            # A counterbore's recess, when its cylinder is split, leaves one
            # arc to the counterbore and the other to a standalone blind
            # hole. They share only the seat, so containment misses it, and
            # the same physical bore is reported twice. Coaxial, and the
            # blind's diameter equal to the counterbore's outer -- that
            # signature belongs to nothing else.
            recess = _counterbore_recess(first, second)
            if recess is not None:
                loser = i if recess is first else j
                dropped[loser] = True
                on_merit[loser] = True
                if loser == i:
                    break
                continue

            # A counterbore whose "seat" is a groove is not a counterbore.
            # The counterbore test reads any larger coaxial band joined by a
            # shoulder as one, including a thread relief or gland part-way
            # down a bore. Grooves used to claim their flank bores as well,
            # which made the groove a superset of that false counterbore and
            # containment dropped it; now the bore owns its flanks, so the
            # counterbore is demoted instead -- band and shoulder stripped,
            # and kept as the bore it really is.
            if _demote_groove_seated_counterbore(first, second):
                continue

            ratio_first = len(shared) / len(first.faces)
            ratio_second = len(shared) / len(second.faces)
            contains = (
                ratio_first >= _CONTAINMENT_RATIO
                or ratio_second >= _CONTAINMENT_RATIO
            )
            result.relations.append(
                (
                    first.instance_id,
                    second.instance_id,
                    "contains" if contains else "intersects",
                )
            )

            # A step mis-seeded on a rib wall is describing the rib from the
            # side. The overlap is only a fifth to a half of the faces, so
            # the containment gate never sees it -- but a step sharing any
            # face with a rib is that step, and the rib says more.
            step_index = _step_beside_rib(first, second, i, j)
            if step_index is not None:
                dropped[step_index] = True
                on_merit[step_index] = True
                if step_index == i:
                    break
                continue

            if not contains:
                continue

            # A pattern's faces are the union of its children's by design, so
            # every child is contained in it. Both readings are wanted: the
            # pattern is meta-level and the children still need per-hole
            # rules run on them.
            if FeatureType.PATTERN in (first.type, second.type):
                continue

            # Undercut and draft are annotations orthogonal to the taxonomy.
            # A face can be a slot wall and unreachable, or a pocket wall and
            # drafted, and dropping either erases the constraint. Marking is
            # the exception: a stroke wall reads as an undercut to the ray
            # cast, and nobody machines a serial number.
            marked = FeatureType.MARKING_TEXT in (first.type, second.type)
            if FeatureType.UNDERCUT in (first.type, second.type) and not marked:
                continue
            if FeatureType.DRAFT_FACE in (first.type, second.type):
                continue

            # A hole drilled through a boss and the boss both exist. The boss
            # keeps those bores in its face set on purpose, so each is wholly
            # contained in it, and by priority the boss lost -- a pad
            # vanished behind its own holes. Coexist only when it is the hole
            # that is contained and the boss is not mostly the hole; a "boss"
            # whose faces are the hole's is a phantom and still loses.
            if _hole_through_boss(first, second, ratio_first, ratio_second):
                continue

            # The same argument one level out: a pad standing on the floor of
            # a slot or pocket, or inside a through cavity, is material added
            # and the cavity is material removed; neither makes the other
            # redundant. Load-bearing since the boss stopped padding its face
            # set with the bores cut into it: a raised pad inside a slot
            # shares four of its five faces with the slot (0.80) and lost.
            # Guarded like the hole rule: the boss must be the contained one
            # and the cavity must not be mostly the boss.
            if _boss_in_cavity(first, second, ratio_first, ratio_second):
                continue

            slot_index = _slot_over_pocket(
                first, second, i, j, ratio_first, ratio_second
            )
            if slot_index is not None:
                dropped[slot_index] = True
                on_merit[slot_index] = True
                if slot_index == i:
                    break
                continue

            # A widening of a cut is not a bore. A profile that swells into an
            # arc leaves a cylindrical wall and the hole family seeds on it,
            # and a hole outranks a through cavity, which is right when a bore
            # pierces a cavity: it runs across the cavity and its wall is its
            # own face. A "bore" made entirely of the cavity's own walls and
            # swept along the cavity's own axis is part of one profile, and
            # those two conditions together cannot describe a cross-drilling.
            widening = _cavity_widening(first, second, i, j)
            if widening is not None:
                dropped[widening] = True
                on_merit[widening] = True
                if widening == i:
                    break
                continue

            loser = _pick_loser(first, second, i, j)
            # Only a loss to a bigger instance of the same type is
            # absorption, undone if the absorber goes away. A loss to a
            # different type is on merit -- the other type explained those
            # faces better -- and the winner being dropped later for its own
            # reasons does not make the loser right again.
            winner = j if loser == i else i
            if features[loser].type == features[winner].type:
                beaten_by[loser] = winner
            dropped[loser] = True
            if loser == i:
                break


def _revive_orphaned_duplicates(features, dropped, beaten_by, on_merit) -> None:
    """Bring back a duplicate whose winner did not survive.

    The dedup is pairwise, so a chain is possible: a one-face chamfer loses
    to a bigger merged chamfer, and that one then loses wholesale to a step
    it overlaps. The first was dropped for duplicating something that is now
    gone, and its faces are left owned by nobody. Revived only when the
    winner was not itself rejected on merit and nothing that survived claims
    the faces, so a real duplicate never comes back. Repeated, because the
    chain can be longer than two.
    """
    count = len(features)
    changed = True
    while changed:
        changed = False
        for k in range(count):
            winner = beaten_by[k]
            if not dropped[k] or winner < 0:
                continue
            if not dropped[winner] or on_merit[winner]:
                continue
            faces = features[k].faces
            if not faces:
                continue
            claimed = 0
            for face_id in faces:
                for m in range(count):
                    if dropped[m] or m == k:
                        continue
                    if face_id in features[m].faces:
                        claimed += 1
                        break
            if claimed / len(faces) >= _CONTAINMENT_RATIO:
                continue
            dropped[k] = False
            changed = True


def _demote_groove_seated_counterbore(
    first: FeatureInstance, second: FeatureInstance
) -> bool:
    """Retype a counterbore seated on a groove's band as the bore it is.

    True when the counterbore was demoted, after which the two no longer
    overlap and the pair needs nothing more.
    """
    if first.type in GROOVE_TYPES and second.type == FeatureType.COUNTERBORE:
        groove, counterbore = first, second
    elif second.type in GROOVE_TYPES and first.type == FeatureType.COUNTERBORE:
        groove, counterbore = second, first
    else:
        return False
    if not groove.faces:
        return False
    band = groove.faces[0]  # the groove's floor cylinder
    if band not in counterbore.faces:
        return False
    groove_faces = set(groove.faces)
    kept = [face_id for face_id in counterbore.faces if face_id not in groove_faces]
    if not kept:
        return False
    counterbore.faces = kept
    through = bool(counterbore.parameters.get("is_through", False))
    counterbore.type = FeatureType.THROUGH_HOLE if through else FeatureType.BLIND_HOLE
    counterbore.parameters["hole_type"] = counterbore.type
    for key in (
        "outer_diameter_mm",
        "counterbore_depth_mm",
        "outer2_diameter_mm",
        "counterbore2_depth_mm",
        "counterbore_double_ended",
    ):
        counterbore.parameters.pop(key, None)
    counterbore.parameters["seat_is_groove"] = groove.instance_id
    return True


def _counterbore_recess(
    first: FeatureInstance, second: FeatureInstance
) -> Optional[FeatureInstance]:
    """The blind hole that is really a counterbore's own recess.

    A seat can sit above a bore that breaks into a cavity just as readily as
    above one with a floor, so both count as the recess.
    """
    bore_like = (FeatureType.BLIND_HOLE, FeatureType.BREAKTHROUGH_HOLE)
    if first.type in bore_like and second.type == FeatureType.COUNTERBORE:
        blind, counterbore = first, second
    elif second.type in bore_like and first.type == FeatureType.COUNTERBORE:
        blind, counterbore = second, first
    else:
        return None

    outer = counterbore.number("outer_diameter_mm") or 0.0
    if outer <= 0.0 or abs((blind.number("diameter_mm") or 0.0) - outer) >= 0.1:
        return None
    if not _axes_parallel(blind, counterbore):
        return None
    return blind


_BORE_FAMILY = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.BREAKTHROUGH_HOLE,
    FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK,
    FeatureType.THREADED_HOLE,
    FeatureType.PARTIAL_BORE,
)


def _hole_through_boss(
    first: FeatureInstance,
    second: FeatureInstance,
    ratio_first: float,
    ratio_second: float,
) -> bool:
    if first.type == FeatureType.BOSS and second.type in _BORE_FAMILY:
        return ratio_second >= _CONTAINMENT_RATIO and ratio_first < _CONTAINMENT_RATIO
    if second.type == FeatureType.BOSS and first.type in _BORE_FAMILY:
        return ratio_first >= _CONTAINMENT_RATIO and ratio_second < _CONTAINMENT_RATIO
    return False


_CAVITY_FAMILY = (
    FeatureType.SLOT,
    FeatureType.POCKET,
    FeatureType.THROUGH_CAVITY,
    FeatureType.CHANNEL,
    FeatureType.STEP,
)


def _boss_in_cavity(
    first: FeatureInstance,
    second: FeatureInstance,
    ratio_first: float,
    ratio_second: float,
) -> bool:
    if first.type == FeatureType.BOSS and second.type in _CAVITY_FAMILY:
        return ratio_first >= _CONTAINMENT_RATIO and ratio_second < _CONTAINMENT_RATIO
    if second.type == FeatureType.BOSS and first.type in _CAVITY_FAMILY:
        return ratio_second >= _CONTAINMENT_RATIO and ratio_first < _CONTAINMENT_RATIO
    return False


_WIDENING_HOLE_TYPES = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.PARTIAL_BORE,
    FeatureType.BREAKTHROUGH_HOLE,
)


def _unit_axis(feature: FeatureInstance) -> Optional[tuple[float, float, float]]:
    key = "through_axis" if "through_axis" in feature.parameters else "axis"
    value = feature.parameters.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        x, y, z = (float(c) for c in value)
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-9:
        return None
    return (x / norm, y / norm, z / norm)


def _cavity_widening(
    first: FeatureInstance, second: FeatureInstance, i: int, j: int
) -> Optional[int]:
    """The index of a hole that is only a through cavity's profile swelling."""
    if first.type == FeatureType.THROUGH_CAVITY and second.type in _WIDENING_HOLE_TYPES:
        cavity, hole, index = first, second, j
    elif second.type == FeatureType.THROUGH_CAVITY and first.type in _WIDENING_HOLE_TYPES:
        cavity, hole, index = second, first, i
    else:
        return None
    if not hole.faces or not set(hole.faces) <= set(cavity.faces):
        return None
    a_hole = _unit_axis(hole)
    a_cav = _unit_axis(cavity)
    if a_hole is None or a_cav is None:
        return None
    if abs(sum(p * q for p, q in zip(a_hole, a_cav))) <= 0.98:
        return None
    return index


def _step_beside_rib(
    first: FeatureInstance, second: FeatureInstance, i: int, j: int
) -> Optional[int]:
    if first.type == FeatureType.STEP and second.type == FeatureType.RIB:
        step, rib, index = first, second, i
    elif second.type == FeatureType.STEP and first.type == FeatureType.RIB:
        step, rib, index = second, first, j
    else:
        return None
    return index if set(step.faces) & set(rib.faces) else None


def _slot_over_pocket(
    first: FeatureInstance,
    second: FeatureInstance,
    i: int,
    j: int,
    ratio_first: float,
    ratio_second: float,
) -> Optional[int]:
    """Whether a genuinely slot-shaped cavity should beat the pocket reading.

    The table puts pocket above slot to suppress slot fragments mis-seeded on
    cavity walls, but a real slot deserves the slot rules. Three conditions,
    all needed: the two cover the same faces, the slot is at least twice as
    long as it is wide, and its length beats its depth -- a phantom slot read
    off a deep cavity wall reports more depth than length, and that is the
    case the pocket priority exists to catch.
    """
    if first.type == FeatureType.SLOT and second.type == FeatureType.POCKET:
        slot, pocket_index = first, j
    elif second.type == FeatureType.SLOT and first.type == FeatureType.POCKET:
        slot, pocket_index = second, i
    else:
        return None

    if not (ratio_first >= _CONTAINMENT_RATIO and ratio_second >= _CONTAINMENT_RATIO):
        return None
    length = slot.number("length_mm") or 0.0
    width = slot.number("width_mm") or 0.0
    depth = slot.number("depth_mm") or 0.0
    if width <= 0.0 or length / width < _SLOT_PREFER_ASPECT:
        return None
    if length < depth:
        return None
    return pocket_index


def _pick_loser(
    first: FeatureInstance, second: FeatureInstance, i: int, j: int
) -> int:
    priority_first = feature_priority(first.type)
    priority_second = feature_priority(second.type)
    if priority_first != priority_second:
        return i if priority_first < priority_second else j
    if len(first.faces) != len(second.faces):
        return i if len(first.faces) < len(second.faces) else j
    # Deterministic: drop the later instance id.
    return j if first.instance_id < second.instance_id else i


def _axes_parallel(first: FeatureInstance, second: FeatureInstance) -> bool:
    a = first.direction("axis")
    b = second.direction("axis")
    if a is None or b is None:
        return False
    return abs(a.Dot(b)) > 0.98


# =============================================================================
# Bores split by a crossing void
# =============================================================================


class _Bore:
    """One surviving hole, reduced to its centreline and axial extent."""

    def __init__(self, index: int, feature: FeatureInstance):
        self.index = index
        self.feature = feature
        self.radius = 0.0
        self.origin = None
        self.direction = None
        self.low = math.inf
        self.high = -math.inf
        self.caps: list[float] = []


def _merge_split_bores(
    features, dropped, graph: AttributedAdjacencyGraph, on_merit=None
) -> None:
    """Rejoin fragments of one bore that a crossing void broke in two.

    A hole crossed by another hole survives resolution as two features, which
    misstates both the count and the depth of each. Every guard here is about
    the opposite case: two holes drilled toward each other from opposite
    faces are also coaxial, also the same diameter, and must never be welded
    into one impossible bore.
    """
    bores = [
        bore
        for bore in (
            _measure_bore(index, features[index], graph)
            for index in range(len(features))
            if not dropped[index]
        )
        if bore is not None
    ]

    # A bore cannot be longer than the part. Every test below is topological
    # and has no sense of scale, so the part's bounding diagonal is kept as a
    # backstop on a runaway chain of merges. It is not a diagnosis: bores
    # that look impossibly long are usually opposed drillings across a
    # central void, which is exactly what this merge is for, so measure the
    # part before reaching for it.
    part_diagonal = 0.0
    whole = Bnd_Box()
    for node in graph.nodes:
        if not node.bbox.IsVoid():
            whole.Add(node.bbox)
    if not whole.IsVoid():
        xmin, ymin, zmin, xmax, ymax, zmax = whole.Get()
        part_diagonal = math.sqrt(
            (xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2
        )

    for i in range(len(bores)):
        for j in range(i + 1, len(bores)):
            first, second = bores[i], bores[j]
            if dropped[first.index] or dropped[second.index]:
                continue
            if not _same_centreline(first, second):
                continue

            # Two through holes are two real holes that happen to share an
            # axis. A genuinely interrupted bore reads as capless fragments,
            # never as two complete through holes.
            if (
                features[first.index].type == FeatureType.THROUGH_HOLE
                and features[second.index].type == FeatureType.THROUGH_HOLE
            ):
                continue

            projected = _project(first, second)
            if projected is None:
                continue
            low, high, base, sign = projected

            # Fragments, not duplicates: the spans must not overlap.
            if min(first.high, high) - max(first.low, low) > _SPAN_OVERLAP_MM:
                continue

            gap_low = min(first.high, high)
            gap_high = max(first.low, low)
            if _cap_in_gap(first, second, base, sign, gap_low, gap_high):
                continue

            # A blind hole with a real floor is a complete hole, not a piece
            # of one. Two of them facing each other, floors at the far ends,
            # slip past the gap test -- and welding them gives one bore
            # longer than the part. A real interrupted bore has at most one
            # floored end.
            if _floored_blind(features[first.index]) and _floored_blind(
                features[second.index]
            ):
                continue

            # Refuse a merge longer than the part; 2% absorbs box padding.
            if part_diagonal > 0.0:
                merged_span = max(first.high, high) - min(first.low, low)
                if merged_span > part_diagonal * 1.02:
                    continue

            _absorb(features, first, second, base, sign, low, high)
            dropped[second.index] = True
            if on_merit is not None:
                on_merit[second.index] = True


def _measure_bore(
    index: int, feature: FeatureInstance, graph: AttributedAdjacencyGraph
) -> Optional[_Bore]:
    if feature.type not in (
        FeatureType.THROUGH_HOLE,
        FeatureType.BLIND_HOLE,
        FeatureType.BREAKTHROUGH_HOLE,
        FeatureType.COUNTERBORE,
    ):
        return None

    bore = _Bore(index, feature)
    for face_id in feature.faces:
        if not graph.has_node(face_id):
            continue
        node = graph.node(face_id)
        if node.surface_type is not SurfaceType.CYLINDER or node.cyl_cone_axis is None:
            continue
        if bore.radius == 0.0 or node.cyl_radius < bore.radius:
            bore.radius = node.cyl_radius
            bore.origin = node.cyl_cone_axis.Location()
            bore.direction = node.cyl_cone_axis.Direction()
    if bore.radius <= 0.0 or bore.origin is None:
        return None

    axis_vector = gp_Vec(bore.direction)
    for face_id in feature.faces:
        if not graph.has_node(face_id):
            continue
        node = graph.node(face_id)
        if node.surface_type is SurfaceType.CYLINDER:
            if abs(node.cyl_radius - bore.radius) >= _RADIUS_TOLERANCE_MM:
                continue
            # From the face's own rim points, not its bounding box. The box
            # is world-aligned, so for an off-cardinal bore its corners
            # project well past the face's real extent -- far enough to
            # swallow the drill points of two opposed drillings and merge
            # them into one hole the part could not contain.
            for point in (node.cyl_p0, node.cyl_p1):
                if point is None:
                    continue
                along = gp_Vec(bore.origin, point).Dot(axis_vector)
                bore.low = min(bore.low, along)
                bore.high = max(bore.high, along)
        elif node.centroid is not None:
            bore.caps.append(gp_Vec(bore.origin, node.centroid).Dot(axis_vector))

    return bore if bore.high > bore.low else None


def _same_centreline(first: _Bore, second: _Bore) -> bool:
    if abs(first.radius - second.radius) > _RADIUS_TOLERANCE_MM:
        return False
    if abs(first.direction.Dot(second.direction)) < _AXIS_ALIGNMENT:
        return False
    offset = gp_Vec(first.origin, second.origin)
    return (
        offset.Crossed(gp_Vec(first.direction)).Magnitude() <= _AXIS_OFFSET_MM
    )


def _project(first: _Bore, second: _Bore):
    """The second bore's span, expressed along the first bore's axis."""
    offset = gp_Vec(first.origin, second.origin)
    base = offset.Dot(gp_Vec(first.direction))
    sign = 1.0 if first.direction.Dot(second.direction) >= 0 else -1.0
    low = base + sign * second.low
    high = base + sign * second.high
    if low > high:
        low, high = high, low
    return low, high, base, sign


def _cap_in_gap(first, second, base, sign, gap_low, gap_high) -> bool:
    """Whether anything closes the bore between the two fragments."""
    window_low = gap_low - _CAP_WINDOW_MM
    window_high = gap_high + _CAP_WINDOW_MM
    for along in first.caps:
        if window_low < along < window_high:
            return True
    for along in second.caps:
        if window_low < base + sign * along < window_high:
            return True
    return False


def _floored_blind(feature: FeatureInstance) -> bool:
    """A blind hole with a real floor of its own: a complete hole.

    With one exception, and it is a proof rather than a guess: a bore the
    counterbore test showed re-entering past its "floor" was stopped by the
    mouth of an internal groove, not by a terminus, so it is a fragment. A
    gland cut into a bore wall splits it into two such pieces.
    """
    if feature.type != FeatureType.BLIND_HOLE:
        return False
    if feature.param("terminates_in_cavity", False):
        return False
    if feature.param("bore_continues_past_floor", False):
        return False
    return True


def _absorb(features, first: _Bore, second: _Bore, base, sign, low, high) -> None:
    keeper = features[first.index]
    absorbed = features[second.index]

    keeper.faces = sorted(set(keeper.faces) | set(absorbed.faces))
    keeper.parameters["depth_mm"] = round(
        max(first.high, high) - min(first.low, low), 6
    )
    keeper.parameters["merged_across_void"] = True

    if keeper.type == FeatureType.COUNTERBORE and absorbed.type == FeatureType.COUNTERBORE:
        keeper.parameters["counterbore_double_ended"] = True
    elif absorbed.type == FeatureType.COUNTERBORE:
        keeper.type = FeatureType.COUNTERBORE
        keeper.parameters["hole_type"] = FeatureType.COUNTERBORE

    # The merged bore spans the void, so the per-fragment bookkeeping about
    # running out into a cavity no longer describes anything.
    keeper.parameters.pop("terminates_in_cavity", None)

    first.low = min(first.low, low)
    first.high = max(first.high, high)
    first.caps.extend(base + sign * along for along in second.caps)
