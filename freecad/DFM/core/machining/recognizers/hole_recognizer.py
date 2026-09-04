# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes drilled and bored holes.

The single most valuable recognizer: holes drive more DFM findings than any
other feature, and almost every part has some. Also the fussiest, because a
cylindrical face is not automatically a hole -- it might be a boss, a corner
fillet, or a blend band running along an edge, and each of those has to be
told apart from a real bore.

The chain of reasoning per cylinder is:

1. Is it internal at all? A bore's face is stored reversed; a boss's is not.
2. Is it actually a bore rather than a fillet? A fillet band wraps a quarter
   turn and meets its neighbours tangentially; a bore wraps fully and meets
   them in sharp rims.
3. What is at its ends? Faces perpendicular to the axis are caps. A small
   one centred on the axis is a floor; a large one, or one the bore pierces,
   is an opening.
4. Two openings and no floor means it goes through.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.gp import gp_Dir, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import BORE_TYPES, FeatureInstance, FeatureType
from ..helix import candidate_axes, find_helices
from ..thread_sources import (
    MODELLED_HELIX,
    ThreadEvidence,
    bore_wall,
    centreline_of,
)
from ..threads import match_tap_drill
from .base import (
    FeatureRecognizer,
    axes_are_coaxial,
    axial_coordinate,
    cross_section_area,
    cylinder_length,
    cylinder_wrap,
    neighbours,
    radial_distance,
    shares_inner_wire_with,
)


# A face counts as a cap when its normal is within about 45 degrees of the
# bore axis. Generous on purpose: a cap can be a sloped spot-face.
_CAP_AXIS_ALIGNMENT = 0.7

# A cone is part of this bore when its axis is nearly the bore's.
_COAXIAL_ALIGNMENT = 0.95

# A cap larger than this many bore cross-sections is the surface the hole was
# drilled into, not the bottom of it.
_OPENING_AREA_MULTIPLE = 3.0

# Below this wrap a cylindrical face is a blend band rather than a bore,
# provided its lateral edges are tangent. Quarter-round scale: real bores
# interrupted by a crossing cavity still sit well above it.
_BLEND_BAND_MAX_WRAP = 0.35

# A would-be bore covering less than this much of a circle laterally is an
# open saddle -- a bearing seat milled open-side-up, not a drilled hole.
#: How close to the end of the part a face has to be to count as the outside
#: of it. A few millimetres covers a shoulder set slightly in from the face
#: without letting an internal step pass for an external one.
_PART_EXTREME_TOL_MM = 2.0

_PARTIAL_BORE_LATERAL_MAX = 1.7

#: How alike two cylinders must be to belong to one coaxial system: the same
#: radius to a micron, axes parallel and within half a millimetre of each
#: other. Fragments of one bore separated by a slit or a crossing pocket have
#: to be gathered back together before the circle can be measured.
_COAXIAL_RADIUS_TOL_MM = 1.0e-3
_COAXIAL_PARALLEL_MIN_DOT = 0.99
_COAXIAL_OFFSET_TOL_MM = 0.5

# Countersinks wider than this are funnels or chamfers, not screw seats.
_COUNTERSINK_MAX_RIM_DIAMETER = 30.0


class HoleRecognizer(FeatureRecognizer):
    """Finds through holes, blind holes, counterbores and countersinks."""

    prefix = "h"

    #: What the document and the user have already said about this part's
    #: threads, set by the analyzer before the run. Nothing when the analysis
    #: has no document behind it, which is the headless case, and then the
    #: helix search is the only source there is.
    thread_evidence: Optional[ThreadEvidence] = None

    @property
    def name(self) -> str:
        return "Hole Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        taken: set[int] = set(claimed or ())
        found: list[FeatureInstance] = []

        # Helices are measured once for the whole shape rather than per bore:
        # the scan is over every spline edge in the part, and a tapped hole
        # is far from the only reason to walk that list.
        self._helices = find_helices(shape, candidate_axes(graph)) if shape else []

        # Smallest bore first, so a counterbore is always seeded from its
        # inner cylinder and can absorb the larger coaxial one. Seeded the
        # other way round, the outer cylinder would be emitted as a blind
        # hole before the inner got a chance to claim it.
        cylinders = sorted(
            graph.nodes_by_surface_type(SurfaceType.CYLINDER),
            key=lambda n: (n.cyl_radius, n.face_id),
        )

        for cylinder in cylinders:
            if cylinder.face_id in taken:
                continue
            feature = self._recognize_one(graph, cylinder, taken)
            if feature is not None:
                found.append(feature)
                taken.update(feature.faces)

        merged = self._merge_split_bores(graph, found)
        self._absorb_orphan_fragments(graph, merged)
        # After the merge, because a modelled thread splits the bore it is cut
        # in: each fragment alone is not the hole, and the thread belongs to
        # the whole of it.
        for feature in merged:
            self._try_thread(graph, feature)
        for index, feature in enumerate(merged):
            feature.instance_id = self.instance_id(index)
        return merged

    # -- one cylinder -------------------------------------------------------

    def _recognize_one(
        self, graph: AttributedAdjacencyGraph, cylinder: AagNode, taken: set[int]
    ) -> Optional[FeatureInstance]:
        # A bore's cylindrical face has its outward normal pointing at the
        # axis, into the air inside the hole, so the face is stored reversed.
        # A boss or an external fillet is not. Without this every boss on the
        # part would be reported as a hole.
        if not cylinder.is_internal or cylinder.cyl_cone_axis is None:
            return None
        if self._is_blend_band(graph, cylinder):
            return None
        if self._is_corner_fillet(graph, cylinder):
            return None

        caps = self._collect_caps(graph, cylinder)
        if not (caps.planar or caps.cones or caps.freeform):
            return None  # nothing at either end: not a hole

        floors, openings = self._split_floors_and_openings(graph, cylinder, caps.planar)
        opening_count = len(openings) + self._count_freeform_openings(
            graph, cylinder, caps.freeform
        )

        has_floor = bool(floors) or bool(caps.cones)
        is_through = not has_floor and opening_count >= 2

        # A bore that stops on nothing recognizable -- no floor, no drill
        # cone -- ran into another cavity rather than being drilled to a
        # depth. Rules about tapping and flat bottoms make no sense on it.
        terminates_in_cavity = not is_through and not floors and not caps.cones

        # The opening is not part of the hole. It is the face the hole was
        # drilled into -- a face the size of the part, usually, shared with
        # every other hole on that side -- and listing it makes the feature
        # claim the whole wall: the viewport highlights it, and the resolver
        # cannot see a bore sitting inside the groove that interrupts it,
        # because the groove does not claim the wall too. Only the hole's own
        # ends belong to it.
        faces = [cylinder.face_id] + floors + caps.cones + caps.bridges
        parameters = {
            "diameter_mm": round(cylinder.cyl_radius * 2.0, 6),
            "depth_mm": round(self._depth(cylinder, graph, caps.bridges), 6),
            "is_through": is_through,
            # Only meaningful on a blind hole. A drill leaves a cone, so a
            # flat floor means something other than a drill made it.
            "flat_bottom": not caps.cones and not terminates_in_cavity,
            "axis": self._axis_tuple(cylinder),
        }
        if terminates_in_cavity:
            parameters["terminates_in_cavity"] = True
        if floors or caps.cones:
            # The hole has an end of its own -- a floor or a drill point --
            # so its axis can be signed toward the opening. Fragments of a
            # bore broken by a crossing cavity have no such end.
            parameters["axis_signed"] = True

        feature = FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.THROUGH_HOLE if is_through else FeatureType.BLIND_HOLE,
            faces=sorted(set(faces)),
            parameters=parameters,
        )

        if self._try_partial_bore(graph, cylinder, feature):
            return feature
        # One or the other, never both. A counterbored bolt hole usually has
        # a cone at its far end too -- the drill point, or a chamfer on the
        # exit -- and calling both in sequence let the countersink test
        # overwrite a counterbore that had already been recognized. The seat
        # is what the hole is for; the cone is how it was finished.
        if not self._try_counterbore(graph, cylinder, feature):
            self._try_countersink(graph, cylinder, feature, caps.cones)
        feature.parameters["hole_type"] = feature.type
        return feature

    # -- threads ------------------------------------------------------------

    def _try_thread(
        self, graph: AttributedAdjacencyGraph, feature: FeatureInstance
    ) -> bool:
        """Promote a bore to a tapped hole, on positive evidence only.

        A hole whose diameter merely matches a tap drill is not evidence of a
        thread. Most bores that size are clearance, reamed, dowel or pilot
        holes, and inferring from diameter alone turns a plate of standard
        drill sizes into a fully tapped part.

        So somebody has to have said so. Either the document states it -- a
        threaded ``PartDesign::Hole``, or a bore the user has confirmed --
        or the thread is actually cut in the solid as a helix, coaxial with
        the bore and at about its radius.

        A statement is taken first and taken whole. It comes with its own
        designation, pitch and tapped depth, and it is not subject to the
        geometric refusals below: those exist to stop the workbench guessing,
        and there is nothing left to guess at once the designer has written
        the callout into the feature.

        The tap-drill table is still what names a thread the helix found --
        the bore diameter is the tap drill, by definition.
        """
        if feature.type not in BORE_TYPES:
            return False
        diameter = feature.number("diameter_mm") or 0.0
        if diameter <= 0.0:
            return False
        cylinder = bore_wall(graph, feature)
        if cylinder is None:
            return False

        if self._apply_stated_thread(feature, cylinder, diameter):
            return True
        if not self._helices:
            return False

        # A bore that runs out into another cavity has no closed end to tap
        # into. It is a port or a cross-drilling.
        if feature.param("terminates_in_cavity"):
            return False

        # A bore interrupted by a *crossing* one picks up spline intersection
        # curves, and a fluid passage opening into a cross-bore is a port
        # rather than a tapped hole. Coaxial neighbours are exempt: a modelled
        # thread splits the very bore it is cut in, and those fragments are
        # the same hole rather than a crossing.
        axis = cylinder.cyl_cone_axis
        for neighbour, _ in neighbours(graph, cylinder.face_id):
            if neighbour.surface_type is not SurfaceType.CYLINDER:
                continue
            if neighbour.cyl_cone_axis is None or axis is None:
                return False
            if not axes_are_coaxial(neighbour.cyl_cone_axis, axis):
                return False

        helix = self._helix_for(cylinder)
        if helix is None:
            return False

        spec = match_tap_drill(diameter)
        if spec is None:
            # Without a standard size there is no designation, pitch or
            # nominal diameter to report, and a thread the rules cannot
            # reason about is worse than an honest plain bore.
            return False

        feature.type = FeatureType.THREADED_HOLE
        feature.parameters["thread_designation"] = spec.designation
        feature.parameters["thread_nominal_mm"] = spec.nominal_mm
        feature.parameters["thread_pitch_mm"] = spec.pitch_mm
        feature.parameters["thread_evidence"] = MODELLED_HELIX
        # The axial reach of the helix is the tapped length. Worst-casing to
        # the full hole depth would make the run-out rule fire on parts that
        # took the trouble to model the thread properly.
        if 0.0 < helix.axial_span <= (feature.number("depth_mm") or 0.0) + 0.5:
            feature.parameters["thread_depth_mm"] = round(helix.axial_span, 6)
        return True

    def _apply_stated_thread(
        self, feature: FeatureInstance, cylinder: AagNode, diameter: float
    ) -> bool:
        """Take a thread somebody has already declared for this bore.

        Matched on the bore's centreline and diameter, the same way a helix
        is matched to the bore it was cut in. There is no better handle to
        match on: FreeCAD keeps no usable record of which face on the final
        shape came from which feature, so a declared hole and a bore on the
        finished part have to be tied together by where they sit.
        """
        evidence = self.thread_evidence
        if evidence is None:
            return False
        line = centreline_of(cylinder)
        if line is None:
            return False
        fact = evidence.fact_for(diameter, line[0], line[1])
        if fact is None:
            return False
        fact.apply_to(feature, hole_depth_mm=feature.number("depth_mm"))
        return True

    def _helix_for(self, cylinder: AagNode):
        """The modelled thread cut on this bore, if there is one.

        Matched by axis and radius: the helix runs at the thread crest, which
        on an internal thread is a little inside the tap drill wall.
        """
        axis = cylinder.cyl_cone_axis
        if axis is None:
            return None
        for helix in self._helices:
            if not axes_are_coaxial(helix.axis, axis):
                continue
            if abs(helix.radius - cylinder.cyl_radius) <= 0.25 * cylinder.cyl_radius:
                return helix
        return None

    # -- guards -------------------------------------------------------------

    def _is_blend_band(self, graph: AttributedAdjacencyGraph, cylinder: AagNode) -> bool:
        """A fillet running along an edge rather than a bore.

        Such a band covers a fraction of a revolution and flows smoothly into
        its neighbours, so its lateral edges are tangent. A real bore meets
        its surroundings in sharp rims even when its entry is filleted,
        because that tangency is on the circular rim, not the sides.
        """
        if cylinder_wrap(cylinder) >= _BLEND_BAND_MAX_WRAP:
            return False
        tangent_lines = sum(
            1
            for edge in graph.edges_of(cylinder.face_id)
            if edge.edge_curve_type == "line" and edge.is_tangent
        )
        return tangent_lines >= 2

    def _is_corner_fillet(
        self, graph: AttributedAdjacencyGraph, cylinder: AagNode
    ) -> bool:
        """A vertical fillet in the corner of a pocket.

        Both a corner fillet and a hole drilled at a pocket corner have two
        planar walls alongside them. The difference is contact: the fillet is
        tangent to both walls, so its axis sits exactly one radius from each.
        A hole's axis does not.
        """
        axis = cylinder.cyl_cone_axis
        if axis is None:
            return False
        axis_dir = axis.Direction()

        walls = [
            node
            for node, _ in neighbours(graph, cylinder.face_id)
            if node.surface_type is SurfaceType.PLANE
            and node.plane_normal is not None
            and abs(node.plane_normal.Dot(axis_dir)) < 0.3
        ]
        if len(walls) < 2:
            return False

        tolerance = max(0.05, cylinder.cyl_radius * 0.05)
        for wall in walls:
            normal = wall.outward_normal
            if normal is None:
                return False
            distance = abs(gp_Vec(wall.centroid, axis.Location()).Dot(gp_Vec(normal)))
            if abs(distance - cylinder.cyl_radius) > tolerance:
                return False
        return True

    # -- caps ---------------------------------------------------------------

    class _Caps:
        def __init__(self) -> None:
            self.planar: list[int] = []
            self.cones: list[int] = []
            self.freeform: list[int] = []
            self.bridges: list[int] = []  # tangent tori crossed to reach a cap

    def _collect_caps(
        self, graph: AttributedAdjacencyGraph, cylinder: AagNode
    ) -> "HoleRecognizer._Caps":
        caps = self._Caps()
        axis = cylinder.cyl_cone_axis
        axis_dir = axis.Direction()
        cross = cross_section_area(cylinder)

        def consider(node: AagNode) -> None:
            if node.surface_type is SurfaceType.PLANE:
                if (
                    node.plane_normal is not None
                    and abs(node.plane_normal.Dot(axis_dir)) > _CAP_AXIS_ALIGNMENT
                    and node.face_id not in caps.planar
                ):
                    caps.planar.append(node.face_id)
            elif node.surface_type is SurfaceType.CONE:
                if (
                    node.cyl_cone_axis is not None
                    and abs(node.cyl_cone_axis.Direction().Dot(axis_dir)) > _COAXIAL_ALIGNMENT
                    and node.face_id not in caps.cones
                ):
                    caps.cones.append(node.face_id)
            elif node.surface_type.is_freeform:
                # Freeform faces are only ever evidence of an opening, never
                # of a floor: a drilled blind hole bottoms out on a plane or
                # a drill cone, not on a sculpted surface.
                pierces = shares_inner_wire_with(graph, node.face_id, cylinder.face_id)
                if (pierces or node.area >= cross * _OPENING_AREA_MULTIPLE) and (
                    node.face_id not in caps.freeform
                ):
                    caps.freeform.append(node.face_id)

        for node, edge in neighbours(graph, cylinder.face_id):
            consider(node)

            # A filleted rim or a bull-nosed floor puts a torus between the
            # bore wall and its real cap. Without hopping over it the bore
            # has no cap at all and disappears entirely. Only coaxial tori
            # are crossed: a rim fillet is always coaxial with its bore,
            # while a blend where two bores cross is not.
            if (
                node.surface_type is SurfaceType.TORUS
                and (edge.is_tangent or edge.concavity is Concavity.TANGENT)
                and node.torus_axis is not None
                and abs(node.torus_axis.Direction().Dot(axis_dir)) > _COAXIAL_ALIGNMENT
                and radial_distance(node.torus_axis.Location(), axis.Location(), axis_dir)
                < 0.5
            ):
                before = len(caps.planar) + len(caps.cones)
                for beyond, _ in neighbours(graph, node.face_id):
                    if beyond.face_id != cylinder.face_id:
                        consider(beyond)
                if len(caps.planar) + len(caps.cones) > before:
                    caps.bridges.append(node.face_id)

        return caps

    def _split_floors_and_openings(
        self, graph: AttributedAdjacencyGraph, cylinder: AagNode, planar_caps: list[int]
    ) -> tuple[list[int], list[int]]:
        """Sort planar caps into the bottom of the hole and its mouths."""
        axis = cylinder.cyl_cone_axis
        cross = cross_section_area(cylinder)
        floors: list[int] = []
        openings: list[int] = []

        for cap_id in planar_caps:
            cap = graph.node(cap_id)

            # If the bore's rim sits on this face's inner wire, the bore goes
            # through it, so it is a mouth however small it is.
            if shares_inner_wire_with(graph, cap_id, cylinder.face_id):
                openings.append(cap_id)
                continue

            if cap.area >= cross * _OPENING_AREA_MULTIPLE:
                openings.append(cap_id)
                continue

            # A real floor lies across the mouth, so its centroid is on the
            # axis. A stray fragment from a neighbouring feature can have the
            # right orientation and size but sits off to one side; without
            # this test it is mistaken for a floor and a through hole is
            # reported as blind.
            offset = radial_distance(cap.centroid, axis.Location(), axis.Direction())
            if offset <= cylinder.cyl_radius:
                floors.append(cap_id)

        return (floors, openings)

    def _count_freeform_openings(
        self, graph: AttributedAdjacencyGraph, cylinder: AagNode, freeform: list[int]
    ) -> int:
        """Count piercings rather than faces.

        A cross hole through a curved waist enters and leaves through the
        same connected face, so counting faces sees one opening where there
        are two. Grouping the shared rims by position along the axis
        separates the ends.
        """
        axis = cylinder.cyl_cone_axis
        origin, direction = axis.Location(), axis.Direction()
        total = 0

        for face_id in freeform:
            positions = sorted(
                axial_coordinate(edge.midpoint, origin, direction)
                for edge in graph.edges_of(cylinder.face_id)
                if edge.other_face(cylinder.face_id) == face_id and edge.midpoint is not None
            )
            if not positions:
                continue
            clusters = 1
            for previous, current in zip(positions, positions[1:]):
                if current - previous > cylinder.cyl_radius:
                    clusters += 1
            total += clusters
        return total

    # -- measurements -------------------------------------------------------

    def _depth(
        self, cylinder: AagNode, graph: AttributedAdjacencyGraph, bridges: list[int]
    ) -> float:
        """Axial extent of the bore, including any blend bands crossed.

        A rim fillet rolls through ninety degrees, so it adds its minor
        radius to the hole's real depth. Ignoring it reads an entry-filleted
        seat about a third shallow.
        """
        depth = cylinder_length(cylinder)
        for face_id in bridges:
            node = graph.node(face_id)
            depth += node.torus_minor_r
        return depth

    @staticmethod
    def _axis_tuple(cylinder: AagNode) -> tuple[float, float, float]:
        direction = cylinder.cyl_cone_axis.Direction()
        return (
            round(direction.X(), 6),
            round(direction.Y(), 6),
            round(direction.Z(), 6),
        )

    # -- refinements --------------------------------------------------------

    def _try_partial_bore(
        self, graph: AttributedAdjacencyGraph, cylinder: AagNode, feature: FeatureInstance
    ) -> bool:
        """A half-open cylindrical seat rather than a drillable bore.

        A bearing saddle, a handlebar cradle, a clamp jaw: half its
        circumference does not exist. It is machined open-side-up with an end
        mill or bored with the cap bolted on, so the rules that assume a drill
        going down a closed hole -- depth ratio, flat bottom, edge distance --
        have nothing to say about it.

        What separates a saddle from a bore is how much circle there is, and
        the honest way to measure that is the bounding box of the whole
        coaxial system rather than the arc of any one face. A real bore's
        cylinders union to a full circle, 2r across whichever way you measure;
        a saddle spans 2r one way and about r the other. Measuring the seed
        face alone gets a clamp bore split by a pinch slit wrong: each
        fragment is well under half a circle, but together they close, and
        that is a bore.

        The missing circumference has to open onto free space, not into
        another bore. Two parallel cylinders that intersect meet along
        generatrix lines, so a drilled hole grazed away by a crossing hole
        also unions "partial" -- but its straight boundary edges run against
        the crossing bore's curved wall, where a true saddle's run out onto
        the flats it was machined from.
        """
        radius = cylinder.cyl_radius
        if radius <= 0.0 or cylinder.cyl_cone_axis is None:
            return False
        axis = cylinder.cyl_cone_axis.Direction()
        origin = cylinder.cyl_cone_axis.Location()

        low = [None, None, None]
        high = [None, None, None]
        for node in graph.nodes:
            if node.surface_type is not SurfaceType.CYLINDER:
                continue
            if not node.is_reversed:
                continue
            if node.cyl_cone_axis is None:
                continue
            if abs(node.cyl_radius - radius) > _COAXIAL_RADIUS_TOL_MM:
                continue
            if abs(node.cyl_cone_axis.Direction().Dot(axis)) < _COAXIAL_PARALLEL_MIN_DOT:
                continue
            offset = gp_Vec(origin, node.cyl_cone_axis.Location())
            if offset.Crossed(gp_Vec(axis)).Magnitude() > _COAXIAL_OFFSET_TOL_MM:
                continue
            if node.bbox.IsVoid():
                continue
            bounds = node.bbox.Get()
            for i in range(3):
                if low[i] is None or bounds[i] < low[i]:
                    low[i] = bounds[i]
                if high[i] is None or bounds[i + 3] > high[i]:
                    high[i] = bounds[i + 3]
        if low[0] is None:
            return False

        spans = [high[i] - low[i] for i in range(3)]
        components = (abs(axis.X()), abs(axis.Y()), abs(axis.Z()))
        along = components.index(max(components))
        across = min(spans[i] for i in range(3) if i != along)
        if across >= _PARTIAL_BORE_LATERAL_MAX * radius:
            return False

        # The straight edges bounding the open side have to land on flats.
        for node, edge in neighbours(graph, cylinder.face_id):
            if edge.edge_curve_type != "line":
                continue
            if node.surface_type is not SurfaceType.PLANE:
                return False

        feature.type = FeatureType.PARTIAL_BORE
        feature.parameters["hole_type"] = FeatureType.PARTIAL_BORE
        feature.parameters["is_through"] = False
        feature.parameters["length_mm"] = round(cylinder_length(cylinder), 6)
        feature.parameters.pop("terminates_in_cavity", None)
        feature.parameters.pop("flat_bottom", None)
        return True

    def _try_counterbore(
        self,
        graph: AttributedAdjacencyGraph,
        cylinder: AagNode,
        feature: FeatureInstance,
    ) -> bool:
        """A larger coaxial bore stacked on top of this one, with a shoulder.

        A bore can carry a seat at each end. A through bolt gets a socket-head
        counterbore on one face and a nut seat on the other, and the two are
        one hole to drill and two to spot-face. The far seat is absorbed into
        the same feature rather than left to the main loop, which would see a
        wide short cylinder closed by a shoulder and call it a flat-bottomed
        blind hole -- and then a rule would ask why a flat-bottomed hole was
        drilled instead of milled, about a spot face.

        No attempt is made to tell a counterbore from a thread-relief groove
        on topology alone. They are the same shape: a wider coaxial band with
        a shoulder. What separates them is what the band is for, which is on
        the drawing and not in the solid, and the reference reads it from the
        tolerance data. Guessing from the shoulders instead -- a band with a
        step at each end is a groove -- costs more than it earns: it throws
        away every bolt hole counterbored from both faces of a flange, which
        has exactly that signature and is not a groove at all.
        """
        axis = cylinder.cyl_cone_axis
        axis_dir = axis.Direction()
        cross = cross_section_area(cylinder)

        found = False
        first_shoulder: Optional[int] = None

        for shoulder, _ in neighbours(graph, cylinder.face_id):
            if (
                shoulder.surface_type is not SurfaceType.PLANE
                or shoulder.plane_normal is None
                or abs(shoulder.plane_normal.Dot(axis_dir)) < _CAP_AXIS_ALIGNMENT
            ):
                continue
            if found and shoulder.face_id == first_shoulder:
                continue  # this end already absorbed

            for outer, _ in neighbours(graph, shoulder.face_id):
                if (
                    outer.face_id == cylinder.face_id
                    or outer.face_id in feature.faces
                    or outer.surface_type is not SurfaceType.CYLINDER
                    or not outer.is_internal
                    or outer.cyl_cone_axis is None
                    or outer.cyl_radius <= cylinder.cyl_radius + 1e-4
                    or not axes_are_coaxial(outer.cyl_cone_axis, axis)
                ):
                    continue

                if found:
                    # The far seat. Absorbing it claims its cylinder, which
                    # is the whole point.
                    feature.parameters["outer2_diameter_mm"] = round(
                        outer.cyl_radius * 2.0, 6
                    )
                    feature.parameters["counterbore2_depth_mm"] = round(
                        cylinder_length(outer), 6
                    )
                    feature.parameters["counterbore_double_ended"] = True
                    feature.faces = sorted(
                        set(feature.faces) | {shoulder.face_id, outer.face_id}
                    )
                    # Seats at both ends usually mean the bore runs through:
                    # each one is entered from its own face. Only say so when
                    # the far seat actually opens onto a face, because an
                    # undercut relief at the closed end of a blind bore has
                    # the same topology and terminates on an internal floor.
                    if self._opens_onto_a_face(
                        graph, outer, shoulder.face_id, axis_dir, cross
                    ):
                        feature.parameters["is_through"] = True
                        feature.parameters["flat_bottom"] = False
                    return True

                feature.type = FeatureType.COUNTERBORE
                feature.parameters["outer_diameter_mm"] = round(
                    outer.cyl_radius * 2.0, 6
                )
                feature.parameters["counterbore_depth_mm"] = round(
                    cylinder_length(outer), 6
                )
                feature.faces = sorted(
                    set(feature.faces) | {shoulder.face_id, outer.face_id}
                )

                # The bore was called blind because the annular shoulder was
                # its floor. With the outer cylinder merged on top of that
                # shoulder the shoulder is no longer an end, so ask again:
                # the bore is through if its far end opens onto a face.
                if self._opens_onto_a_face(
                    graph, cylinder, shoulder.face_id, axis_dir, cross
                ):
                    feature.parameters["is_through"] = True

                if not feature.param("axis_signed"):
                    self._sign_counterbore(
                        graph, feature, cylinder, outer, shoulder.face_id
                    )

                found = True
                first_shoulder = shoulder.face_id
                break

        return found

    def _sign_counterbore(
        self,
        graph: AttributedAdjacencyGraph,
        feature: FeatureInstance,
        inner: AagNode,
        outer: AagNode,
        shoulder_id: int,
    ) -> None:
        """Point the axis the way the tool came in.

        A through bore is left unsigned on the grounds that it can be drilled
        from either end. A counterbore cannot: the seat is spot-faced from
        whichever face it opens onto, and that end is a fact about the part
        rather than a fixturing choice. Usually it is the outer -- that is
        what a counterbore is -- but an undercut relief turned into the far
        end of a blind bore has exactly the same topology with the wide band
        buried inside, and there the tool comes in through the inner.

        Which of the two is settled by walking the bore stack out from each
        end and seeing whether it reaches the outside of the part. Saying so
        matters twice over: two counterbores machined into opposite faces of
        one block stay two setups instead of collapsing into one, and two
        bores in line with a relief band between them stay two bores instead
        of being merged into a single long hole.
        """
        axis = inner.cyl_cone_axis
        if axis is None:
            return
        axis_dir = axis.Direction()
        limits = self._axial_extremes(graph, axis_dir)

        inner_outside = self._reaches_the_outside(
            graph, inner, shoulder_id, axis_dir, limits
        )
        outer_outside = self._reaches_the_outside(
            graph, outer, shoulder_id, axis_dir, limits
        )
        if not inner_outside and not outer_outside:
            return  # buried at both ends: nothing to point at

        offset = _axial_midpoint(outer, axis_dir) - _axial_midpoint(inner, axis_dir)
        if abs(offset) <= 1e-6:
            return
        # Toward the outer when the seat is the way in, toward the inner when
        # the wide band is a relief buried at the far end.
        toward = offset if outer_outside else -offset
        signed = gp_Dir(axis_dir.XYZ())
        if toward < 0.0:
            signed.Reverse()
        feature.parameters["axis"] = (
            round(signed.X(), 6),
            round(signed.Y(), 6),
            round(signed.Z(), 6),
        )
        feature.parameters["axis_signed"] = True

    @staticmethod
    def _axial_extremes(
        graph: AttributedAdjacencyGraph, axis_dir
    ) -> tuple[float, float]:
        """How far the part reaches along an axis, either way."""
        low, high = math.inf, -math.inf
        for node in graph.nodes:
            if node.bbox.IsVoid():
                continue
            xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
            for x in (xmin, xmax):
                for y in (ymin, ymax):
                    for z in (zmin, zmax):
                        along = (
                            x * axis_dir.X() + y * axis_dir.Y() + z * axis_dir.Z()
                        )
                        low = min(low, along)
                        high = max(high, along)
        return (low, high)

    @staticmethod
    def _reaches_the_outside(
        graph: AttributedAdjacencyGraph,
        start: AagNode,
        shoulder_id: int,
        axis_dir,
        limits: tuple[float, float],
    ) -> bool:
        """Whether a tool sized for this bore could get to it from outside.

        Walks the stack of coaxial bores and shoulders out from one end,
        without crossing back over the shoulder that separates the two ends,
        and asks whether it comes out on a face at the end of the part. Only
        bores at least as wide as the one it started from are passable: a
        tool that fits this bore cannot come in through a narrower one.

        A gland groove splitting a counterbore into two bands, or an internal
        shoulder part way along, is a stepping stone rather than an answer --
        only where the walk stops does the question get asked.
        """
        low, high = limits
        if low is math.inf:
            return False
        minimum_radius = start.cyl_radius
        seen = {start.face_id}
        stack = [start.face_id]

        while stack:
            for node, _ in neighbours(graph, stack.pop()):
                if node.face_id == shoulder_id or node.face_id in seen:
                    continue
                if node.surface_type is SurfaceType.PLANE:
                    if node.plane_normal is None:
                        continue
                    if abs(node.plane_normal.Dot(axis_dir)) < _CAP_AXIS_ALIGNMENT:
                        continue
                    along = (
                        node.centroid.X() * axis_dir.X()
                        + node.centroid.Y() * axis_dir.Y()
                        + node.centroid.Z() * axis_dir.Z()
                    )
                    if (
                        abs(along - low) < _PART_EXTREME_TOL_MM
                        or abs(along - high) < _PART_EXTREME_TOL_MM
                    ):
                        return True
                    seen.add(node.face_id)
                    stack.append(node.face_id)
                elif node.surface_type is SurfaceType.CYLINDER:
                    if node.cyl_cone_axis is None:
                        continue
                    if abs(node.cyl_cone_axis.Direction().Dot(axis_dir)) < 0.98:
                        continue
                    if node.cyl_radius + 1e-4 < minimum_radius:
                        continue
                    seen.add(node.face_id)
                    stack.append(node.face_id)
        return False

    @staticmethod
    def _opens_onto_a_face(
        graph: AttributedAdjacencyGraph,
        bore: AagNode,
        skip_face: int,
        axis_dir,
        cross_section: float,
    ) -> bool:
        """Whether a bore's other end comes out somewhere.

        Somewhere means a plane square to the axis and broad enough to be a
        face of the part rather than another shoulder inside it. Three times
        the bore's own cross-section is the reference's line, and it is drawn
        low deliberately: a hub annulus is not much wider than the bore
        through it and is still the outside of the part.
        """
        for node, _ in neighbours(graph, bore.face_id):
            if node.face_id == skip_face:
                continue
            if node.surface_type is not SurfaceType.PLANE:
                continue
            if node.plane_normal is None:
                continue
            if abs(node.plane_normal.Dot(axis_dir)) < _CAP_AXIS_ALIGNMENT:
                continue
            if node.area >= cross_section * 3.0:
                return True
        return False

    def _try_countersink(
        self,
        graph: AttributedAdjacencyGraph,
        cylinder: AagNode,
        feature: FeatureInstance,
        cone_ids: list[int],
    ) -> bool:
        """A coaxial cone that widens away from the bore: a screw seat.

        A cone that narrows is the drill point at the bottom, not a
        countersink, and one that widens past any fastener head is a funnel
        or a chamber taper.
        """
        axis = cylinder.cyl_cone_axis
        eligible, is_through_bore = self._countersink_eligible(graph, cylinder)
        if not eligible:
            return False

        for cone_id in cone_ids:
            cone = graph.node(cone_id)
            if cone.cone_r0 <= 0.0 and cone.cone_r1 <= 0.0:
                continue
            if cone.cyl_cone_axis is None:
                continue
            if abs(axis.Direction().Dot(cone.cyl_cone_axis.Direction())) < 0.98:
                continue
            if not _widens_away_from(cone, cylinder.centroid):
                continue

            wide_radius = max(cone.cone_r0, cone.cone_r1)
            if wide_radius * 2.0 > _COUNTERSINK_MAX_RIM_DIAMETER:
                # Too wide for any fastener head, so not a seat -- but the
                # cone is still this bore's far opening. A big chamfered
                # bore stays a plain hole and gains an honest answer about
                # running through.
                if is_through_bore and not feature.param("flat_bottom"):
                    feature.type = FeatureType.THROUGH_HOLE
                    feature.parameters["is_through"] = True
                    feature.parameters["flat_bottom"] = False
                    feature.parameters.pop("terminates_in_cavity", None)
                continue

            feature.type = FeatureType.COUNTERSINK
            feature.parameters["included_angle"] = round(
                2.0 * abs(math.degrees(cone.cone_semi_angle)), 4
            )
            feature.parameters["rim_diameter_mm"] = round(wide_radius * 2.0, 6)
            if is_through_bore:
                # The first pass saw one planar opening and called the bore
                # blind, because the cone stands where the other opening
                # would be. It is the other opening.
                feature.parameters["is_through"] = True
                feature.parameters.pop("terminates_in_cavity", None)
            return True
        return False

    @staticmethod
    def _countersink_eligible(
        graph: AttributedAdjacencyGraph, cylinder: AagNode
    ) -> tuple[bool, bool]:
        """Whether this bore has an end for a countersink to be the other of.

        A cone on a bore means one of two things, and which it is depends on
        the rest of the bore rather than on the cone. If the bore already
        bottoms out on a floor, the cone at its mouth is a seat. If the bore
        runs through -- two openings, either of them planar or the widening
        cone itself -- the cone is the far one, and again a seat. A bore with
        neither has nothing at its far end but the cone, and a cone alone at
        the end of a bore is where the drill stopped.

        Returns whether to look at all, and whether the bore runs through.
        """
        axis_dir = cylinder.cyl_cone_axis.Direction()
        cross = cross_section_area(cylinder)
        has_floor = False
        large_openings = 0

        for node, _ in neighbours(graph, cylinder.face_id):
            if node.surface_type is SurfaceType.PLANE:
                if node.plane_normal is None:
                    continue
                if abs(node.plane_normal.Dot(axis_dir)) < 0.7:
                    continue
                if node.area < cross * 3.0:
                    has_floor = True
                else:
                    large_openings += 1
            elif node.surface_type is SurfaceType.CONE:
                if node.cyl_cone_axis is None:
                    continue
                if abs(axis_dir.Dot(node.cyl_cone_axis.Direction())) < 0.95:
                    continue
                if _widens_away_from(node, cylinder.centroid):
                    large_openings += 1

        is_through_bore = large_openings >= 2
        return (has_floor or is_through_bore, is_through_bore)

    # -- post pass ----------------------------------------------------------

    @staticmethod
    def _absorb_orphan_fragments(
        graph: AttributedAdjacencyGraph, features: list[FeatureInstance]
    ) -> bool:
        """Give a bore back the pieces of itself that nothing claimed.

        A cylinder needs a cap at one end to be read as a hole at all, and a
        middle fragment has none: a bore crossed by another bore has a piece
        with curved surfaces at both ends, and a modelled thread turns the
        bore it is cut in into a stack of flank cylinders with nothing planar
        anywhere near them. Those pieces are unmistakably part of a hole that
        was already found -- same radius, same axis line -- but no pass emits
        them, so they end up belonging to nothing.

        Nothing is created here and no classification changes; the fragments
        simply join the feature they are part of, which is what makes the
        viewport highlight the whole hole rather than its two ends, and what
        lets the thread pass see a thread that had been left in no man's
        land.
        """
        claimed = {face_id for feature in features for face_id in feature.faces}
        absorbed = False

        for feature in features:
            axis = _first_cylinder_axis(graph, feature)
            if axis is None:
                continue
            radius = (feature.number("diameter_mm") or 0.0) / 2.0
            if radius <= 0.0:
                continue
            direction = axis.Direction()
            origin = axis.Location()

            for node in graph.nodes:
                if node.surface_type is not SurfaceType.CYLINDER:
                    continue
                if node.face_id in claimed or not node.is_internal:
                    continue
                if node.cyl_cone_axis is None:
                    continue
                if abs(node.cyl_radius - radius) > 1e-3:
                    continue
                if abs(node.cyl_cone_axis.Direction().Dot(direction)) < 0.99:
                    continue
                offset = gp_Vec(origin, node.cyl_cone_axis.Location())
                if offset.Crossed(gp_Vec(direction)).Magnitude() > 0.01:
                    continue
                feature.faces = sorted(set(feature.faces) | {node.face_id})
                claimed.add(node.face_id)
                absorbed = True

        return absorbed

    def _merge_split_bores(
        self, graph: AttributedAdjacencyGraph, features: list[FeatureInstance]
    ) -> list[FeatureInstance]:
        """Rejoin one bore that the modeller split into several faces.

        A boolean or a seam can divide a single cylindrical wall into
        fragments. Left alone they would be reported as several holes of
        identical diameter stacked on the same axis.
        """
        merged: list[FeatureInstance] = []
        absorbed: set[int] = set()

        for index, feature in enumerate(features):
            if index in absorbed:
                continue
            fragments = [feature]
            for other_index in range(index + 1, len(features)):
                if other_index in absorbed:
                    continue
                other = features[other_index]
                if not self._same_bore(graph, feature, other):
                    continue
                absorbed.add(other_index)
                fragments.append(other)
                feature.faces = sorted(set(feature.faces + other.faces))
                # The more specific description of the two wins: a
                # counterbore that happens to be split is still a counterbore.
                if _specificity(other.type) > _specificity(feature.type):
                    feature.type = other.type
                    feature.parameters.update(
                        {
                            key: value
                            for key, value in other.parameters.items()
                            if key not in ("depth_mm", "diameter_mm", "is_through")
                        }
                    )

            if len(fragments) > 1:
                self._describe_interrupted_bore(graph, feature, fragments)
            merged.append(feature)
        return merged

    def _describe_interrupted_bore(
        self,
        graph: AttributedAdjacencyGraph,
        feature: FeatureInstance,
        fragments: list[FeatureInstance],
    ) -> None:
        """Describe a bore that a crossing cavity broke into pieces.

        Several coaxial fragments of one diameter are one hole with something
        cut across it. Whether it runs through is asked again of the pieces
        together: a fragment that looked blind because a cavity cut across it
        is not blind, but a bore genuinely stopped by a floor at one end is
        still stopped by it, and only ends that open onto a face of the part
        count. Two of those and no floor anywhere along the run, and the hole
        goes through.

        How it is drilled depends on the gap. A drill crosses a narrow void
        and carries on in one pass; a wide one leaves nothing to guide it, so
        the hole is drilled from both ends and what matters is the longest
        single run rather than the total.
        """
        axis = _first_cylinder_axis(graph, feature)
        if axis is None:
            return
        origin, direction = axis.Location(), axis.Direction()

        spans: list[tuple[float, float]] = []
        for fragment in fragments:
            for face_id in fragment.faces:
                node = graph.node(face_id)
                if node.surface_type is not SurfaceType.CYLINDER:
                    continue
                if node.cyl_p0 is None or node.cyl_p1 is None:
                    continue
                low = axial_coordinate(node.cyl_p0, origin, direction)
                high = axial_coordinate(node.cyl_p1, origin, direction)
                spans.append((min(low, high), max(low, high)))
        if not spans:
            return

        spans.sort()
        merged_spans = [list(spans[0])]
        for low, high in spans[1:]:
            if low <= merged_spans[-1][1] + 1e-6:
                merged_spans[-1][1] = max(merged_spans[-1][1], high)
            else:
                merged_spans.append([low, high])

        contiguous = max(high - low for low, high in merged_spans)
        voids = [
            nxt[0] - cur[1] for cur, nxt in zip(merged_spans, merged_spans[1:])
        ]
        total = merged_spans[-1][1] - merged_spans[0][0]

        if not self._runs_through(graph, feature, direction):
            # The pieces are one bore that stops somewhere. It is not the
            # cavity between them that stops it -- there is a floor or a
            # drill point at one end -- so the flag saying it runs out into
            # nothing is wrong, and the depth is the whole run rather than
            # the fragment this feature was seeded from.
            feature.parameters["depth_mm"] = round(total, 6)
            feature.parameters["max_contiguous_depth_mm"] = round(contiguous, 6)
            feature.parameters["max_void_mm"] = (
                round(max(voids), 6) if voids else 0.0
            )
            feature.parameters["fragment_count"] = len(merged_spans)
            feature.parameters.pop("terminates_in_cavity", None)
            return

        feature.type = FeatureType.THROUGH_HOLE
        feature.parameters["is_through"] = True
        feature.parameters["depth_mm"] = round(total, 6)
        feature.parameters["max_contiguous_depth_mm"] = round(contiguous, 6)
        feature.parameters["max_void_mm"] = round(max(voids), 6) if voids else 0.0
        feature.parameters["fragment_count"] = len(merged_spans)
        feature.parameters.pop("terminates_in_cavity", None)
        feature.parameters["flat_bottom"] = False

    @staticmethod
    def _runs_through(
        graph: AttributedAdjacencyGraph, feature: FeatureInstance, direction: gp_Dir
    ) -> bool:
        """Whether a bore in pieces comes out the other side.

        Asked of every piece at once, because the answer is a property of the
        run and not of any fragment: a piece stopped by a crossing cavity has
        no end of its own and says nothing either way. What settles it is
        what the pieces between them touch -- a floor or a drill cone
        anywhere along the run and the bore stops there; two ends opening
        onto faces of the part and it does not.
        """
        cylinders = [
            graph.node(face_id)
            for face_id in feature.faces
            if graph.node(face_id).surface_type is SurfaceType.CYLINDER
        ]
        if not cylinders:
            return False
        cross = cross_section_area(cylinders[0])

        openings: set[int] = set()
        for cylinder in cylinders:
            for node, _ in neighbours(graph, cylinder.face_id):
                if node.surface_type is SurfaceType.PLANE:
                    if node.plane_normal is None:
                        continue
                    if abs(node.plane_normal.Dot(direction)) <= 0.7:
                        continue
                    if node.area >= cross * 3.0:
                        openings.add(node.face_id)
                    else:
                        return False  # a floor: the bore stops here
                elif node.surface_type is SurfaceType.CONE:
                    if node.cyl_cone_axis is None:
                        continue
                    if abs(node.cyl_cone_axis.Direction().Dot(direction)) > 0.95:
                        return False  # a drill point
        return len(openings) >= 2

    @staticmethod
    def _same_bore(
        graph: AttributedAdjacencyGraph, a: FeatureInstance, b: FeatureInstance
    ) -> bool:
        """Whether two coaxial bores are fragments of one hole.

        Same axis and same diameter is not enough to decide. Two blind holes
        drilled from opposite faces of a part line up exactly and are still
        two holes. What separates the cases is the gap along the axis and
        whether each end is real:

        Both have an end of their own -- a floor or a drill point -- so both
        are complete holes. Only seam slop may separate them; a real gap of
        solid material between two floors means two drillings.

        At least one has no end of its own, so it was broken by something
        crossing it. Genuine fragments of one bore stay close together: the
        gap is at most the width of whatever interrupted them. Two holes
        drilled from opposite sides into a common cavity are separated by
        more than either fragment is long.
        """
        radius_a = (a.number("diameter_mm") or 0.0) / 2.0
        radius_b = (b.number("diameter_mm") or 0.0) / 2.0
        if abs(radius_a - radius_b) > 1e-3:
            return False

        axis_a = _first_cylinder_axis(graph, a)
        axis_b = _first_cylinder_axis(graph, b)
        if axis_a is None or axis_b is None:
            return False
        if not axes_are_coaxial(axis_a, axis_b, direction_dot=0.99, line_distance=0.01):
            return False

        direction = axis_a.Direction()
        span_a = _bbox_axial_range(graph, a, direction, radius_a)
        span_b = _bbox_axial_range(graph, b, direction, radius_a)
        if span_a is None or span_b is None:
            return True
        gap = max(0.0, span_a[0] - span_b[1], span_b[0] - span_a[1])

        if a.param("axis_signed") and b.param("axis_signed"):
            # Two finished holes: only bounding-box slop on a seam may
            # separate them.
            return gap <= 0.1

        shortest = min(span_a[1] - span_a[0], span_b[1] - span_b[0])
        return gap <= shortest


def _widens_away_from(cone: AagNode, origin) -> bool:
    """Whether a cone opens out as it recedes from a point.

    Which way a cone tapers is what separates a fastener seat from the mark
    a drill leaves: a countersink's wide rim is the end further from the
    bore, a drill point's is the end nearer it.
    """
    if cone.cone_p0 is None or cone.cone_p1 is None:
        return False
    if cone.cone_r0 >= cone.cone_r1:
        wide, narrow = cone.cone_p0, cone.cone_p1
    else:
        wide, narrow = cone.cone_p1, cone.cone_p0
    outward = gp_Vec(origin, cone.centroid)
    if outward.Magnitude() < 1e-9:
        return False
    return gp_Vec(narrow, wide).Dot(outward) > 0.0


def _axial_midpoint(node: AagNode, axis_dir) -> float:
    """Where a face sits along an axis, by the middle of its bounding box."""
    if node.bbox.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    return (
        (xmin + xmax) * 0.5 * axis_dir.X()
        + (ymin + ymax) * 0.5 * axis_dir.Y()
        + (zmin + zmax) * 0.5 * axis_dir.Z()
    )


def _bbox_axial_range(
    graph: AttributedAdjacencyGraph,
    feature: FeatureInstance,
    direction: gp_Dir,
    radius: float = 0.0,
) -> Optional[tuple[float, float]]:
    """How far a bore runs along its axis.

    Only the faces at the bore's own diameter count. A counterbore's seat is
    a wider band sitting on top of the bore rather than part of its run, and
    including it says the bore reaches places it does not -- two bolt holes
    drilled into opposite faces of a flange and sharing one seat between them
    would read as overlapping, and then as one hole.
    """
    low, high = math.inf, -math.inf
    for face_id in feature.faces:
        node = graph.node(face_id)
        if node.surface_type is not SurfaceType.CYLINDER or node.bbox.IsVoid():
            continue
        if radius > 0.0 and abs(node.cyl_radius - radius) > 1e-3:
            continue
        xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
        for x in (xmin, xmax):
            for y in (ymin, ymax):
                for z in (zmin, zmax):
                    along = x * direction.X() + y * direction.Y() + z * direction.Z()
                    low = min(low, along)
                    high = max(high, along)
    return None if low is math.inf else (low, high)


def _first_cylinder_axis(graph: AttributedAdjacencyGraph, feature: FeatureInstance):
    for face_id in feature.faces:
        node = graph.node(face_id)
        if node.surface_type is SurfaceType.CYLINDER and node.cyl_cone_axis is not None:
            return node.cyl_cone_axis
    return None


_SPECIFICITY = {
    FeatureType.COUNTERBORE: 4,
    FeatureType.COUNTERSINK: 3,
    FeatureType.THREADED_HOLE: 2,
}


def _specificity(feature_type: str) -> int:
    return _SPECIFICITY.get(feature_type, 1)
