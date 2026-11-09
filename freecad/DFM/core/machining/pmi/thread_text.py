# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Thread callouts whose text the XCAF reader never surfaces.

The reference engine's ``PmiThreadTextExtractor``. The XCAF route
(:func:`.xde.extract_thread_callouts`) reads a designation from a dimension's
semantic name, presentation name or descriptions. Two common ways of writing a
thread callout reach none of those:

*Presentation text.* On a real CATIA/NX export the drawn text is a
``DESCRIPTIVE_REPRESENTATION_ITEM('equivalent unicode string',
'DIM\\w4X M12x1.75-6H')`` hanging off a "pmi validation property", tied back
to the semantic dimension only through the draughting callout. ``nist_ctc_04``
carries its four M12 tapped holes exactly that way.

*Shape aspect names.* OCCT's own AP242 writer, and some CAD exports, put the
callout on the ``SHAPE_ASPECT`` the dimension applies to --
``SHAPE_ASPECT('M8x1.25 - 6H',...)`` -- while the dimension itself is named
``'diameter'``.

Both are plain in the file, so this walks the :class:`.step_text.StepTextIndex`
graph. The text layer (:func:`extract_text_callouts`) takes the part's bores
and an entity-to-faces lookup as plain data, so it runs on synthetic STEP text
in the tests; :func:`extract_thread_text_callouts` supplies both from a live
XDE read.

The result merges UNDER the XCAF route (see :func:`merge_text_callouts`): a
semantic designation stays authoritative where a file has both.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..annotations import ThreadCallout
from .step_text import StepTextIndex

# The same designation grammar as the reference's XCAF-side extractor, except
# that the imperial series suffix is required here. ASCII so that ``\b`` and
# ``\d`` mean what they mean to ``std::regex``: a Latin-1 letter is not a word
# character there.
_METRIC = re.compile(r"\bM(\d+(?:\.\d+)?)(?:\s*[xX]\s*(\d+(?:\.\d+)?))?\b", re.ASCII)
_IMPERIAL = re.compile(
    r"\b(\d+/\d+|#\d+|\d+)\s*-\s*(\d+)\s*(UNC|UNF|UNEF|UN)\b",
    re.ASCII | re.IGNORECASE,
)


def _strip_trailing_zero(token: str) -> str:
    return token[:-2] if len(token) >= 2 and token.endswith(".0") else token


def detect_designation(text: str) -> str:
    """A thread designation in the text, canonicalized, or "" for none."""
    if not text:
        return ""
    match = _METRIC.search(text)
    if match:
        out = "M" + _strip_trailing_zero(match.group(1))
        if match.group(2) is not None:
            out += "x" + _strip_trailing_zero(match.group(2))
        return out
    match = _IMPERIAL.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)} {match.group(3).upper()}"
    return ""


def _is_dimension_entity(entity_type: str) -> bool:
    """The semantic dimension a presentation callout is the drawing of."""
    return entity_type in (
        "DIMENSIONAL_SIZE",
        "DIMENSIONAL_LOCATION",
        "ANGULAR_SIZE",
        "ANGULAR_LOCATION",
    )


def _is_shape_aspect(entity_type: str) -> bool:
    return "SHAPE_ASPECT" in entity_type


def _is_pmi_graph_node(entity_type: str) -> bool:
    """Entity types the callout-to-dimension walk may pass through.

    The walk runs both ways -- down from the property to the callout and up
    from the callout to its association -- so left unrestricted it would fall
    into the B-rep: a callout references curves, curves reference points, and
    points are referenced by thousands of entities. Keeping to the PMI
    presentation graph keeps it bounded and meaningful.
    """
    return any(
        needle in entity_type
        for needle in (
            "REPRESENTATION",
            "PROPERTY_DEFINITION",
            "CHARACTERIZED_ITEM",
            "DRAUGHTING",
            "ITEM_ASSOCIATION",
            "ANNOTATION",
        )
    )


@dataclass
class Bore:
    """One cylindrical face of the part, as the walk needs it."""

    origin: Tuple[float, float, float]
    direction: Tuple[float, float, float]  # unit
    radius: float
    #: (xmin, ymin, zmin, xmax, ymax, zmax), or None for a void box.
    bounds: Optional[Tuple[float, float, float, float, float, float]]
    face_id: int  # 0 when the face does not resolve


Point = Tuple[float, float, float]

#: STEP id -> the face ids that entity transfers to; None when the id names no
#: entity of the model at all (the reference then skips it outright).
FacesFor = Callable[[int], Optional[List[int]]]


def _axial(bore: Bore, point: Point) -> Tuple[float, float]:
    """(distance along the axis, distance off it) of a point."""
    v = tuple(p - o for p, o in zip(point, bore.origin))
    d = bore.direction
    along = v[0] * d[0] + v[1] * d[1] + v[2] * d[2]
    off = tuple(v[i] - d[i] * along for i in range(3))
    return along, math.sqrt(off[0] ** 2 + off[1] ** 2 + off[2] ** 2)


def _inside_box(bounds, point: Point, slack: float) -> bool:
    xmin, ymin, zmin, xmax, ymax, zmax = bounds
    x, y, z = point
    return not (
        x < xmin - slack
        or x > xmax + slack
        or y < ymin - slack
        or y > ymax + slack
        or z < zmin - slack
        or z > zmax + slack
    )


def _circle_centre(index: StepTextIndex, circle) -> Optional[Point]:
    """CIRCLE -> AXIS2_PLACEMENT_3D -> CARTESIAN_POINT, or None."""
    if circle.type != "CIRCLE" or not circle.refs:
        return None
    placement = index.get(circle.refs[0])
    if placement is None or not placement.refs:
        return None
    point = index.get(placement.refs[0])
    if point is None or point.type != "CARTESIAN_POINT" or len(point.reals) < 3:
        return None
    return (point.reals[0], point.reals[1], point.reals[2])


def _modelled_thread_depths(index: StepTextIndex, bores: Sequence[Bore]) -> Dict[int, float]:
    """Tap depth per bore face, from cosmetic thread-plane circles.

    A CATIA/NX cosmetic thread brackets the tapped length with named curve
    sets ("Upper Thread Plane" / "Lower Thread Plane", a "Thread Cylinder"
    between them, or one "Geometrical Representation of Thread"), drawn from
    circles centred exactly on the bore axis. The axial span of a bore's
    on-axis thread-circle centres is the thread the author modelled -- a
    geometric source for the tap depth, not an inference from the drill size.
    It lets the runout rule stop assuming a hole is tapped to the bottom
    (``nist_ctc_04``: 25 mm of thread in a 30 mm bore).
    """
    centres: List[Point] = []
    for group in index.of_type("GEOMETRIC_CURVE_SET"):
        if not any("thread" in s.lower() for s in group.strings):
            continue
        seen = {group.id}
        frontier = [group.id]
        depth = 0
        while depth < 4 and frontier:
            following: List[int] = []
            for current in frontier:
                entity = index.get(current)
                if entity is None:
                    continue
                centre = _circle_centre(index, entity)
                if centre is not None:
                    centres.append(centre)
                for ref in entity.refs:
                    if ref not in seen:
                        seen.add(ref)
                        following.append(ref)
            frontier = following
            depth += 1

    depths: Dict[int, float] = {}
    for bore in bores:
        if bore.bounds is None:
            continue
        lo = hi = 0.0
        count = 0
        for centre in centres:
            along, off = _axial(bore, centre)
            if off > 0.5 * bore.radius + 0.05:
                continue  # off this axis
            # Within the bore's own extent, so a coaxial hole elsewhere on
            # the same axis is not claimed.
            if not _inside_box(bore.bounds, centre, 1.0):
                continue
            if count == 0:
                lo = hi = along
            else:
                lo, hi = min(lo, along), max(hi, along)
            count += 1
        if count < 2:
            continue  # needs a top and a bottom marker
        span = hi - lo
        if span < 0.5 or span > 1000.0:
            continue  # degenerate or runaway
        if not bore.face_id:
            continue
        # The largest bracket wins when sets disagree.
        if span > depths.get(bore.face_id, 0.0):
            depths[bore.face_id] = span
    return depths


def extract_text_callouts(
    index: StepTextIndex, bores: Sequence[Bore], faces_for: FacesFor
) -> Dict[int, ThreadCallout]:
    """Face id -> thread callout, from presentation text and aspect names."""
    from ..threads import find_by_designation

    out: Dict[int, ThreadCallout] = {}
    if index.empty():
        return out

    thread_depths = _modelled_thread_depths(index, bores)

    def axis_point_under(root: int) -> Optional[Point]:
        """Mean of the circle centres under an entity.

        OCCT transfers no cosmetic annotation geometry -- the curve set comes
        back null -- so where a thread cylinder sits is read from the text.
        Only circle centres are used: a circle's placement lies ON the axis,
        whereas the generatrix lines run along the surface one radius out and
        would drag the average off-axis until no bore matched.
        """
        sx = sy = sz = 0.0
        count = 0
        seen = {root}
        frontier = [root]
        depth = 0
        while depth < 6 and frontier:
            following: List[int] = []
            for current in frontier:
                entity = index.get(current)
                if entity is None:
                    continue
                centre = _circle_centre(index, entity)
                if centre is not None:
                    sx, sy, sz = sx + centre[0], sy + centre[1], sz + centre[2]
                    count += 1
                    continue
                for ref in entity.refs:
                    if ref not in seen:
                        seen.add(ref)
                        following.append(ref)
            frontier = following
            depth += 1
        if not count:
            return None
        return (sx / count, sy / count, sz / count)

    def faces_near_point(point: Point) -> List[int]:
        """Bores whose axis passes through the point, within their extent.

        Matched on the axis rather than the radius: a cosmetic thread
        cylinder is drawn at the NOMINAL diameter, larger than the tap-drill
        bore it annotates (Ø12 over Ø10.2).
        """
        found = set()
        for bore in bores:
            _, off = _axial(bore, point)
            if off > 0.25 * bore.radius + 0.05:
                continue
            if bore.bounds is None:
                continue
            if not _inside_box(bore.bounds, point, 1.0):
                continue
            if bore.face_id:
                found.add(bore.face_id)
        return sorted(found)

    # Aspect id -> the faces it identifies, through GEOMETRIC_ITEM_SPECIFIC_USAGE
    # (name, description, definition, used_representation, identified_item):
    # the refs are [definition, representation, item...].
    aspect_faces: Dict[int, List[int]] = {}
    for usage in index.of_type("GEOMETRIC_ITEM_SPECIFIC_USAGE"):
        if len(usage.refs) < 3:
            continue
        definition = usage.refs[0]
        for item in usage.refs[2:]:
            faces = faces_for(item)
            if faces is None:
                continue
            if not faces:
                # Not a face. On nist_ctc_04 the M12 callout points at
                # GEOMETRIC_CURVE_SET('Thread Cylinder.1') -- cosmetic curves
                # that OCCT does not transfer -- so their points are read from
                # the text and used to find the bore they wrap.
                point = axis_point_under(item)
                if point is not None:
                    faces = faces_near_point(point)
            aspect_faces.setdefault(definition, []).extend(faces)

    def faces_of_aspect(aspect_id: int) -> List[int]:
        """An aspect's faces, following composite groups two levels down.

        A "4X" callout hangs off a COMPOSITE_GROUP_SHAPE_ASPECT whose
        children are the individual features.
        """
        found = set()
        seen = {aspect_id}
        frontier = [aspect_id]
        depth = 0
        while depth <= 2 and frontier:
            following: List[int] = []
            for aspect in frontier:
                found.update(aspect_faces.get(aspect, ()))
                # SHAPE_ASPECT_RELATIONSHIP(name, desc, relating, related)
                for referrer in index.referrers(aspect):
                    relation = index.get(referrer)
                    if relation is None or relation.type != "SHAPE_ASPECT_RELATIONSHIP":
                        continue
                    for side in relation.refs:
                        if side == aspect or side in seen:
                            continue
                        other = index.get(side)
                        if other is None or not _is_shape_aspect(other.type):
                            continue
                        seen.add(side)
                        following.append(side)
            frontier = following
            depth += 1
        return sorted(found)

    def callout_for(designation: str, source: str) -> ThreadCallout:
        spec = find_by_designation(designation)
        return ThreadCallout(
            designation=designation,
            nominal_mm=spec.nominal_mm if spec else 0.0,
            pitch_mm=spec.pitch_mm if spec else 0.0,
            source=source,
        )

    def with_depth(callout: ThreadCallout, face_id: int) -> ThreadCallout:
        depth = thread_depths.get(face_id, 0.0)
        return replace(callout, thread_depth_mm=depth) if depth > 0.0 else replace(callout)

    def first_designation(strings: Sequence[str]) -> str:
        for text in strings:
            designation = detect_designation(text)
            if designation:
                return designation
        return ""

    # Every string literal naming a thread, walked UP to the semantic
    # dimension it annotates. On a CATIA/NX export (nist_ctc_04):
    #   DESCRIPTIVE_REPRESENTATION_ITEM('equivalent unicode string', 'DIM\w4X M12x1.75-6H')
    #     -> REPRESENTATION -> PROPERTY_DEFINITION_REPRESENTATION
    #     -> PROPERTY_DEFINITION('pmi validation property')
    #     -> CHARACTERIZED_ITEM_WITHIN_REPRESENTATION -> DRAUGHTING_CALLOUT
    #     -> DRAUGHTING_MODEL_ITEM_ASSOCIATION -> DIMENSIONAL_SIZE
    #     -> COMPOSITE_GROUP_SHAPE_ASPECT (the "4X") -> the four bores
    # A bounded breadth-first search rather than a fixed chain: vendors vary
    # the intermediate entities, and reaching a dimension is the stable part.
    for item in index.of_type("DESCRIPTIVE_REPRESENTATION_ITEM"):
        designation = first_designation(item.strings)
        if not designation:
            continue

        dimension_id = 0
        seen = {item.id}
        frontier = [item.id]
        depth = 0
        while depth < 8 and frontier and not dimension_id:
            following: List[int] = []
            for current in frontier:
                entity = index.get(current)
                if entity is None:
                    continue
                for neighbour in list(index.referrers(current)) + list(entity.refs):
                    if dimension_id or neighbour in seen:
                        continue
                    other = index.get(neighbour)
                    if other is None:
                        continue
                    seen.add(neighbour)
                    if _is_dimension_entity(other.type):
                        dimension_id = neighbour
                    elif _is_pmi_graph_node(other.type):
                        following.append(neighbour)
                if dimension_id:
                    break
            frontier = following
            depth += 1
        if not dimension_id:
            continue

        dimension = index.get(dimension_id)
        if dimension is None or not dimension.refs:
            continue
        faces = faces_of_aspect(dimension.refs[0])
        if not faces:
            continue
        callout = callout_for(designation, "presentation_text")
        for face_id in faces:
            out[face_id] = with_depth(callout, face_id)

    # Callouts carried as SHAPE_ASPECT names (OCCT's own AP242 writer). Gap
    # filling only: a presentation-text designation for the same face wins.
    # ``of_type`` matches by substring, so composite aspects take part too.
    for aspect in index.of_type("SHAPE_ASPECT"):
        designation = first_designation(aspect.strings)
        if not designation:
            continue
        faces = faces_of_aspect(aspect.id)
        if not faces:
            continue
        callout = callout_for(designation, "shape_aspect_name")
        for face_id in faces:
            if face_id not in out:
                out[face_id] = with_depth(callout, face_id)

    return out


def merge_text_callouts(
    semantic: Dict[int, ThreadCallout], text: Dict[int, ThreadCallout]
) -> Dict[int, ThreadCallout]:
    """The text callouts merged UNDER the XCAF ones, as the reference merges.

    A face the XCAF route already names keeps its designation, but adopts
    the modelled thread depth when it carries none of its own.
    """
    merged = dict(semantic)
    for face_id, callout in text.items():
        existing = merged.get(face_id)
        if existing is None:
            merged[face_id] = callout
        elif existing.thread_depth_mm <= 0.0 and callout.thread_depth_mm > 0.0:
            # XCAF callouts are shared between the faces of one dimension, so
            # the depth goes on a copy.
            merged[face_id] = replace(existing, thread_depth_mm=callout.thread_depth_mm)
    return merged


def extract_thread_text_callouts(xde, resolver, entity_faces) -> Dict[int, ThreadCallout]:
    """:func:`extract_text_callouts` against a live XDE read.

    ``xde`` is the :class:`.xde.XdeDocument`, ``resolver`` its
    :class:`.faces.FaceResolver` and ``entity_faces`` the
    :class:`.xde.EntityFaces` built from the two. The bores are the healed
    shape's cylinders, as the reference walks its healed shape.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder

    from .faces import _bounds

    index = xde.text
    if index is None or index.empty():
        return {}

    bores: List[Bore] = []
    for face in resolver.healed_faces:
        surface = BRepAdaptor_Surface(face, False)
        if surface.GetType() != GeomAbs_Cylinder:
            continue
        cylinder = surface.Cylinder()
        axis = cylinder.Axis()
        location, direction = axis.Location(), axis.Direction()
        bores.append(
            Bore(
                origin=(location.X(), location.Y(), location.Z()),
                direction=(direction.X(), direction.Y(), direction.Z()),
                radius=cylinder.Radius(),
                bounds=_bounds(face),
                face_id=resolver.id_of_healed_face(face),
            )
        )

    def faces_for(step_id: int) -> Optional[List[int]]:
        entity = xde.by_text_id.get(step_id)
        if entity is None:
            return None
        return entity_faces.faces_for(entity)

    return extract_text_callouts(index, bores, faces_for)
