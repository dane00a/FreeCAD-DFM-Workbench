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

from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.Bnd import Bnd_Box
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.GeomAbs import GeomAbs_BSplineCurve, GeomAbs_Cone, GeomAbs_Plane
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt, gp_Vec
from OCP.ProjLib import ProjLib
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_ON
from OCP.TopExp import TopExp, TopExp_Explorer
from freecad.DFM.core.utils.occt import TopoDS
from freecad.DFM.core.utils.occt import TopTools_IndexedMapOfShape

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..face_extent import extent_of_face, span_of_face
from ..features import BORE_TYPES, FeatureInstance, FeatureType
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
    cylinder_wraps_axis,
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
# A seat for a flat head grows about half a bore per side (genuine
# countersinks run 0.37-0.58 x bore); an edge break grows a fixed few tenths
# of a millimetre whatever the bore. A third of a bore separates them.
_MIN_HEAD_SEAT_GROWTH_PER_BORE = 0.30
# A seat truncated by thin material grows little but eats this share of the
# hole's axial extent, which no edge break does.
_MIN_SEAT_SHARE_OF_HOLE = 0.30

#: Minimum depth:diameter for a capless cylindrical band to count as a bore.
#: 0.10 sits in the measured gap between the deepest recess wall (0.082) and
#: the shallowest real breakthrough bore (0.111); a merged gland-split skirt
#: bore at 0.152 must keep clearing it.
_SHALLOW_BAND_MAX_ASPECT = 0.10

#: How many tangent coaxial blend faces the cap search may cross between a
#: bore wall and the face it opens onto. A rim rounded over and then
#: edge-broken needs two; three leaves room for one more segment without
#: letting the walk cross a fully blended part.
_MAX_BLEND_BRIDGE_HOPS = 3

#: A void wider than this many drill diameters leaves the drill unguided, so
#: the bore is drilled again from the far side.
_GUIDED_VOID_RATIO = 3.0

#: The bores the geometric thread check reads. Counterbores and countersinks
#: are promoted before it runs and never read as tapped from geometry.
_GEOMETRIC_THREAD_TYPES = frozenset(
    {FeatureType.BLIND_HOLE, FeatureType.THROUGH_HOLE, FeatureType.BREAKTHROUGH_HOLE}
)


class HoleRecognizer(FeatureRecognizer):
    """Finds through holes, blind holes, counterbores and countersinks."""

    prefix = "h"

    #: What the document and the user have already said about this part's
    #: threads, set by the analyzer before the run. Nothing when the analysis
    #: has no document behind it, which is the headless case, and then the
    #: bore's own spline edges are the only source there is.
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

        self._shape = shape
        # Face handles by id, for the few questions only the B-rep can
        # answer (does a cap actually cover the bore axis).
        self._face_map = None
        if shape is not None:
            self._face_map = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(shape, TopAbs_FACE, self._face_map)

        # Smallest bore first, so a counterbore is always seeded from its
        # inner cylinder and can absorb the larger coaxial one. Seeded the
        # other way round, the outer cylinder would be emitted as a blind
        # hole before the inner got a chance to claim it.
        cylinders = sorted(
            graph.nodes_by_surface_type(SurfaceType.CYLINDER),
            key=lambda n: (n.cyl_radius, n.face_id),
        )

        # Thread-relief grooves the counterbore test found, emitted after the
        # holes are merged: they are not bores and take no part in that.
        self._pending_reliefs: list[FeatureInstance] = []
        for cylinder in cylinders:
            if cylinder.face_id in taken:
                continue
            self._pending_relief_bands = []
            feature = self._recognize_one(graph, cylinder, taken)
            if feature is not None:
                found.append(feature)
                taken.update(feature.faces)
            # Buried relief bands the counterbore test refused: nothing else
            # claims them, and left free the loop would seed a phantom bore
            # of the relief's diameter on each.
            taken.update(self._pending_relief_bands)
            self._pending_relief_bands = []

        merged = self._merge_split_bores(graph, found)
        self._absorb_orphan_fragments(graph, merged)
        merged = self._fold_seam_halves(graph, merged)
        merged = self._join_across_relief(graph, merged)
        for feature in merged:
            self._record_overshoot_scar(graph, feature)
        for feature in merged:
            self._end_in_relief(graph, feature)
        # After the merge, because a modelled thread splits the bore it is cut
        # in: each fragment alone is not the hole, and the thread belongs to
        # the whole of it.
        for feature in merged:
            self._try_thread(graph, feature)
        merged.extend(self._pending_reliefs)
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

        self._scar_candidates = []
        floors, openings = self._split_floors_and_openings(
            graph, cylinder, caps.planar, list(caps.chain)
        )
        opening_count = len(openings) + self._count_freeform_openings(
            graph, cylinder, caps.freeform
        )

        # A cone that does not wrap the axis is not a drill point. A real
        # drill point or countersink wraps round the axis; a twist drill
        # breaking out beside a wall clips it with its point and cuts a
        # conical crescent off to one side, which counted as a floor made a
        # port open at both ends read blind. Only asked when the bore has no
        # planar floor and two openings already.
        self._cone_scar_candidates = []
        if not floors and opening_count >= 2:
            kept = []
            for cone_id in caps.cones:
                if self._is_crescent_cone(graph.node(cone_id), cylinder):
                    self._cone_scar_candidates.append(cone_id)
                else:
                    kept.append(cone_id)
            caps.cones[:] = kept

        self._note_bridges(graph, caps, floors + openings + caps.cones)

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
            "depth_mm": round(self._depth(cylinder, graph, caps.depth_links), 6),
            "is_through": is_through,
            # Only meaningful on a blind hole. A drill leaves a cone, so a
            # flat floor means something other than a drill made it.
            "flat_bottom": not caps.cones and not terminates_in_cavity,
            "axis": self._axis_tuple(cylinder),
        }
        if terminates_in_cavity:
            parameters["terminates_in_cavity"] = True
        if caps.entry_fillet_radius > 0.0:
            parameters["entry_fillet_radius_mm"] = round(caps.entry_fillet_radius, 6)
        if floors or caps.cones:
            # The hole has an end of its own -- a floor or a drill point --
            # so its axis can be signed toward the opening. Fragments of a
            # bore broken by a crossing cavity have no such end.
            #
            # And actually signed: pointed from the closed end toward the
            # opening, which is the way the tool comes in. The kernel
            # canonicalises a cylinder's axis whichever way the hole was
            # drilled, so a hole from +Z and one from -Z both report +Z and
            # the setup count could not tell them apart. As the reference
            # does it: with a floor, along the floor's outward normal (it
            # points into the bore); with only a drill cone, from the cone's
            # centroid toward the cylinder's.
            direction = cylinder.cyl_cone_axis.Direction()
            flip = False
            if floors:
                floor_normal = graph.node(floors[0]).outward_normal
                if floor_normal is not None and direction.Dot(floor_normal) < 0.0:
                    flip = True
            else:
                to_opening = gp_Vec(graph.node(caps.cones[0]).centroid, cylinder.centroid)
                if to_opening.Magnitude() > 1.0e-6 and gp_Vec(direction).Dot(to_opening) < 0.0:
                    flip = True
            if flip:
                parameters["axis"] = tuple(
                    round(-c, 6) + 0.0 for c in (direction.X(), direction.Y(), direction.Z())
                )
            parameters["axis_signed"] = True

        feature = FeatureInstance(
            instance_id=self.instance_id(0),
            # Three-way, not two. A bore with no floor of its own is not
            # blind just because it failed the through test.
            type=(
                FeatureType.THROUGH_HOLE
                if is_through
                else FeatureType.BREAKTHROUGH_HOLE
                if terminates_in_cavity
                else FeatureType.BLIND_HOLE
            ),
            faces=sorted(set(faces)),
            parameters=parameters,
        )

        if is_through and len(openings) >= 2 and (
            self._scar_candidates or self._cone_scar_candidates
        ):
            self._record_breakout_scar(graph, cylinder, feature, openings)

        # A wide shallow capless band is not a bore. A cylindrical face with
        # no floor of its own and almost no axial extent for its diameter
        # was never made by a tool travelling down that axis: it is the
        # retaining wall of a turned face recess (the gasket capture wall of
        # a ConFlat seat). The floor is on aspect, not size -- a wide bore
        # through thin plate has the same ratio but is through and never
        # reaches here. A band that plainly is a counterbore (a smaller
        # coaxial bore directly beneath, joined by one planar annulus square
        # to the axis) is kept as one; anything else is dropped and its face
        # left unclaimed for the groove and pocket passes.
        #
        # Before that, an annular channel. An open ring with material
        # standing in the middle -- outer wall, ring floor, and a coaxial
        # convex wall rising back up alongside the outer one -- has an outer
        # wall that looks exactly like a wide bore whose floor misses the
        # axis. Aspect does not separate them (a web relief on a disk sat
        # just above the shallow-band gate); the island does. Scoped to a
        # breakthrough hole: a bore with a real floor, or one that goes
        # through, is not in doubt.
        if feature.type == FeatureType.BREAKTHROUGH_HOLE and self._is_annular_channel(
            graph, cylinder
        ):
            return None
        if feature.type == FeatureType.BREAKTHROUGH_HOLE:
            diameter = feature.number("diameter_mm") or 0.0
            depth = feature.number("depth_mm") or 0.0
            if diameter > 1.0e-9 and depth / diameter < _SHALLOW_BAND_MAX_ASPECT:
                if not self._shallow_band_as_counterbore(graph, cylinder, feature):
                    return None

        if self._try_partial_bore(graph, cylinder, feature):
            return feature
        # One or the other, never both. A counterbored bolt hole usually has
        # a cone at its far end too -- the drill point, or a chamfer on the
        # exit -- and calling both in sequence let the countersink test
        # overwrite a counterbore that had already been recognized. The seat
        # is what the hole is for; the cone is how it was finished.
        if not self._try_counterbore(graph, cylinder, feature):
            self._try_countersink(graph, cylinder, feature, caps.cones)
        # The counterbore test proved the bore runs on past the cap the
        # first pass took for a floor: that cap is the mouth of an internal
        # relief groove, not the bottom of the hole.
        if feature.param("bore_continues_past_floor"):
            feature.parameters["flat_bottom"] = False
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
        or the thread is actually cut in the solid, which leaves its spline
        edges on the bore wall (see :meth:`_modelled_thread`).

        A statement is taken first and taken whole. It comes with its own
        designation, pitch and tapped depth, and it is not subject to the
        geometric refusals below: those exist to stop the workbench guessing,
        and there is nothing left to guess at once the designer has written
        the callout into the feature.

        The tap-drill table is still what names a modelled thread -- the bore
        diameter is the tap drill, by definition.
        """
        if feature.type == FeatureType.COUNTERBORE:
            # A counterbore keeps its type, but a PMI callout on its inner
            # bore gives it the thread parameters, so the thread rules fire
            # on it without a separate threaded-counterbore type.
            callout = self._pmi_thread_for(graph, feature, bore_wall(graph, feature))
            if callout is not None:
                _apply_pmi_thread(feature, callout)
                return False
        if feature.type not in BORE_TYPES:
            return False
        diameter = feature.number("diameter_mm") or 0.0
        if diameter <= 0.0:
            return False
        cylinder = bore_wall(graph, feature)
        if cylinder is None:
            return False

        # A thread callout the PMI links to this bore wins over every other
        # check, through bores and cavity-terminating ones included: the
        # file says this is threaded, whatever the geometry leaves open.
        callout = self._pmi_thread_for(graph, feature, cylinder)
        if callout is not None:
            feature.type = FeatureType.THREADED_HOLE
            feature.parameters["hole_type"] = FeatureType.THREADED_HOLE
            _apply_pmi_thread(feature, callout)
            return True

        if self._apply_stated_thread(feature, cylinder, diameter):
            return True
        # The geometric check is the reference's, and it only ever looks at
        # plain bores: counterbores and countersinks were promoted before it
        # ran there and are never read as tapped from their geometry.
        if feature.type not in _GEOMETRIC_THREAD_TYPES:
            return False

        # A bore that runs out into another cavity has no closed end to tap
        # into. It is a port or a cross-drilling.
        if feature.param("terminates_in_cavity"):
            return False

        # A bore that touches any other cylinder is interrupted by it. The
        # intersection curves are splines that mimic a partial turn of a
        # thread, and a passage opening into a cross-bore is a port rather
        # than a tapped hole. Counterbore pairs never get here.
        for neighbour, _ in neighbours(graph, cylinder.face_id):
            if neighbour.surface_type is SurfaceType.CYLINDER:
                return False

        spec, thread_depth = self._modelled_thread(cylinder, diameter)
        if spec is None:
            return False

        feature.type = FeatureType.THREADED_HOLE
        feature.parameters["hole_type"] = FeatureType.THREADED_HOLE
        feature.parameters["thread_confidence"] = "definite"
        feature.parameters["thread_designation"] = spec.designation
        feature.parameters["thread_pitch_mm"] = spec.pitch_mm
        feature.parameters["thread_nominal_mm"] = spec.nominal_mm
        feature.parameters["thread_evidence"] = MODELLED_HELIX
        if thread_depth is not None:
            feature.parameters["thread_depth_mm"] = thread_depth
        return True

    def _modelled_thread(self, cylinder: AagNode, diameter: float):
        """The thread cut in this bore's wall, read off its spline edges.

        A single spline edge on a bore proves nothing -- a keyway slot through
        it leaves one as a trim curve. A modelled thread leaves several, one
        per start and end, and between them they run at least most of a turn,
        so it takes two or more spline edges whose summed length reaches 80%
        of the circumference. Each length is estimated from three points, as
        the reference does.

        Spline edges whose axial midpoint sits in the outer 15% at either end
        of the bore are left out. A bore punched through a freeform face gets
        a spline rim there, split at the seam into two edges that together
        span the whole circumference, which is this test's signature exactly;
        a thread advances through the bore's interior instead.

        Nothing counts without a tap-drill size to name it: a "thread" with
        no designation, pitch or nominal diameter is one the rules cannot
        reason about. The tapped depth is the axial reach of the edges that
        actually advance (a fair fraction of a pitch), when that is a sane
        share of the bore.

        Returns ``(spec, thread_depth_mm)``, or ``(None, None)``.
        """
        face_map = self._face_map
        if face_map is None or not 1 <= cylinder.face_id <= face_map.Extent():
            return None, None
        if cylinder.cyl_cone_axis is None or cylinder.cyl_p0 is None or cylinder.cyl_p1 is None:
            return None, None
        face = TopoDS.Face_s(face_map.FindKey(cylinder.face_id))
        ax_p0 = cylinder.cyl_p0
        ax_len = cylinder.cyl_p0.Distance(cylinder.cyl_p1)
        ax_dir = gp_Vec(cylinder.cyl_cone_axis.Direction().XYZ())

        count = 0
        total = 0.0
        explorer = TopExp_Explorer(face, TopAbs_EDGE)
        while explorer.More():
            curve = BRepAdaptor_Curve(TopoDS.Edge_s(explorer.Current()))
            explorer.Next()
            if curve.GetType() != GeomAbs_BSplineCurve:
                continue
            u0, u1 = curve.FirstParameter(), curve.LastParameter()
            p0 = curve.Value(u0)
            p1 = curve.Value((u0 + u1) * 0.5)
            p2 = curve.Value(u1)
            if ax_len > 1e-9:
                t_mid = sum(gp_Vec(ax_p0, p).Dot(ax_dir) / ax_len for p in (p0, p1, p2)) / 3.0
                if t_mid < 0.15 or t_mid > 0.85:
                    continue
            count += 1
            total += p0.Distance(p1) + p1.Distance(p2)
        if count < 2 or total < math.pi * diameter * 0.8:
            return None, None
        spec = match_tap_drill(diameter)
        if spec is None:
            return None, None

        t_lo, t_hi = 1e30, -1e30
        have_helix = False
        explorer = TopExp_Explorer(face, TopAbs_EDGE)
        while explorer.More():
            curve = BRepAdaptor_Curve(TopoDS.Edge_s(explorer.Current()))
            explorer.Next()
            if curve.GetType() != GeomAbs_BSplineCurve:
                continue
            h0, h1 = curve.FirstParameter(), curve.LastParameter()
            values = [
                gp_Vec(ax_p0, curve.Value(h0 + (h1 - h0) * s)).Dot(ax_dir)
                for s in (0.0, 0.25, 0.5, 0.75, 1.0)
            ]
            if max(values) - min(values) < 0.3 * spec.pitch_mm:
                continue
            have_helix = True
            t_lo = min(t_lo, min(values))
            t_hi = max(t_hi, max(values))
        depth = None
        if have_helix and t_hi > t_lo:
            modelled = t_hi - t_lo
            if 0.5 < modelled <= ax_len + 0.5:
                depth = modelled
        return spec, depth

    def _pmi_callout_on(self, cylinder: AagNode):
        """The PMI thread callout linked to exactly this cylinder, if any."""
        evidence = self.thread_evidence
        pmi = getattr(evidence, "pmi", None) if evidence is not None else None
        if not pmi:
            return None
        return pmi.get(cylinder.face_id)

    def _pmi_thread_for(
        self,
        graph: AttributedAdjacencyGraph,
        feature: FeatureInstance,
        cylinder: Optional[AagNode],
    ):
        """The PMI thread callout linked to this hole's bore wall, if any.

        The reference looks up its seed cylinder. Ours is the bore wall
        (smallest cylinder, lowest id), and a seam-split bore's other half
        of the same radius is asked too, since which half seeds is an
        ordering accident the callout does not care about.
        """
        evidence = self.thread_evidence
        pmi = getattr(evidence, "pmi", None) if evidence is not None else None
        if not pmi or cylinder is None:
            return None
        if cylinder.face_id in pmi:
            return pmi[cylinder.face_id]
        for face_id in sorted(feature.faces):
            if face_id not in pmi or not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            if (
                node.surface_type is SurfaceType.CYLINDER
                and abs(node.cyl_radius - cylinder.cyl_radius) <= 1.0e-3
            ):
                return pmi[face_id]
        return None

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
            # The bore's blend chain: tangent coaxial tori walked out from
            # the bore wall, each mapped to the link it was reached from.
            self.chain: dict[int, Optional[int]] = {}
            # cap -> the chain link it was reached through
            self.via: dict[int, int] = {}
            self.entry_fillet_radius = 0.0
            self.depth_links: list[int] = []

    def _collect_caps(
        self, graph: AttributedAdjacencyGraph, cylinder: AagNode
    ) -> "HoleRecognizer._Caps":
        caps = self._Caps()
        axis = cylinder.cyl_cone_axis
        axis_dir = axis.Direction()
        cross = cross_section_area(cylinder)

        chain = self._blend_chain(graph, cylinder)
        caps.chain = chain

        def consider(node: AagNode, via: Optional[int]) -> None:
            if node.surface_type is SurfaceType.PLANE:
                if (
                    node.plane_normal is not None
                    and abs(node.plane_normal.Dot(axis_dir)) > _CAP_AXIS_ALIGNMENT
                    and node.face_id not in caps.planar
                ):
                    caps.planar.append(node.face_id)
                    if via is not None:
                        caps.via[node.face_id] = via
            elif node.surface_type is SurfaceType.CONE:
                # Parallel is not coaxial. A part-axis rim chamfer a bolt
                # bore breaks out through is parallel to every bore on the
                # circle and centred on none of them; a hole's own
                # countersink, chamfer or drill point shares its axis line.
                if (
                    node.cyl_cone_axis is not None
                    and abs(node.cyl_cone_axis.Direction().Dot(axis_dir)) > _COAXIAL_ALIGNMENT
                    and radial_distance(node.cyl_cone_axis.Location(), axis.Location(), axis_dir)
                    <= 0.5
                    and node.face_id not in caps.cones
                ):
                    caps.cones.append(node.face_id)
                    if via is not None:
                        caps.via[node.face_id] = via
            elif node.surface_type.is_freeform:
                # Freeform faces are only ever evidence of an opening, never
                # of a floor: a drilled blind hole bottoms out on a plane or
                # a drill cone, not on a sculpted surface. Pierced through
                # the bore's blend chain counts as pierced by the bore.
                pierces = any(
                    shares_inner_wire_with(graph, node.face_id, partner)
                    for partner in [cylinder.face_id, *chain]
                )
                if (pierces or node.area >= cross * _OPENING_AREA_MULTIPLE) and (
                    node.face_id not in caps.freeform
                ):
                    caps.freeform.append(node.face_id)

        for node, _ in neighbours(graph, cylinder.face_id):
            consider(node, None)

        # A filleted rim or a bull-nosed floor puts a torus -- or a chain of
        # them, a round then an edge break -- between the bore wall and its
        # real cap. Without crossing it the bore has no cap at that end and
        # can disappear entirely. Caps are looked for beyond every link.
        for link in sorted(chain):
            for beyond, _ in neighbours(graph, link):
                if beyond.face_id == cylinder.face_id or beyond.face_id in chain:
                    continue
                consider(beyond, link)

        return caps

    def _blend_chain(
        self, graph: AttributedAdjacencyGraph, cylinder: AagNode
    ) -> dict[int, Optional[int]]:
        """Tangent coaxial tori walked out from the bore wall.

        Every link is a torus reached across a tangent edge -- what a blend
        is, and what separates it from a counterbore step, a chamfer's cone,
        a groove wall or a cavity rim, all of which meet their neighbours
        sharply. Every link is coaxial with the bore, not merely with its
        predecessor, so the chain cannot drift off axis a tolerance at a
        time; and the walk is bounded, so a fully blended part cannot be
        crossed end to end. Maps each link to the link it was reached from
        (None beside the bore).
        """
        axis = cylinder.cyl_cone_axis
        axis_dir = axis.Direction()

        def is_bore_blend(node: AagNode, edge) -> bool:
            return (
                node.surface_type is SurfaceType.TORUS
                and (edge.is_tangent or edge.concavity is Concavity.TANGENT)
                and node.torus_axis is not None
                and abs(node.torus_axis.Direction().Dot(axis_dir)) > _COAXIAL_ALIGNMENT
                and radial_distance(node.torus_axis.Location(), axis.Location(), axis_dir)
                < 0.5
            )

        chain: dict[int, Optional[int]] = {}
        frontier: list[int] = []
        for node, edge in neighbours(graph, cylinder.face_id):
            if is_bore_blend(node, edge) and node.face_id not in chain:
                chain[node.face_id] = None
                frontier.append(node.face_id)
        for _ in range(1, _MAX_BLEND_BRIDGE_HOPS):
            if not frontier:
                break
            following: list[int] = []
            for link in frontier:
                for node, edge in neighbours(graph, link):
                    if node.face_id == cylinder.face_id or node.face_id in chain:
                        continue
                    if not is_bore_blend(node, edge):
                        continue
                    chain[node.face_id] = link
                    following.append(node.face_id)
            frontier = following
        return chain

    @staticmethod
    def _note_bridges(
        graph: AttributedAdjacencyGraph, caps: "HoleRecognizer._Caps", used: list[int]
    ) -> None:
        """The blend links that carried a used cap belong to the hole.

        Depth comes only from the link a cap was reached through, because
        minor radius equals axial span only for the ninety-degree roll that
        lands on the cap; a link further up can bulge and come back. The
        fillet the part was designed with is the largest radius on the path
        back to the bore, and the whole path is absorbed. A dead-end branch
        of the walk carried no cap and is not part of the hole.
        """
        through: list[int] = []
        members: list[int] = []
        for cap_id in used:
            link = caps.via.get(cap_id)
            if link is None or link in members:
                continue
            through.append(link)
            while link is not None:
                if link not in members:
                    members.append(link)
                caps.entry_fillet_radius = max(
                    caps.entry_fillet_radius, graph.node(link).torus_minor_r
                )
                link = caps.chain.get(link)
        caps.bridges = members
        caps.depth_links = through

    def _split_floors_and_openings(
        self,
        graph: AttributedAdjacencyGraph,
        cylinder: AagNode,
        planar_caps: list[int],
        chain: Sequence[int] = (),
    ) -> tuple[list[int], list[int]]:
        """Sort planar caps into the bottom of the hole and its mouths."""
        axis = cylinder.cyl_cone_axis
        cross = cross_section_area(cylinder)
        floors: list[int] = []
        openings: list[int] = []

        for cap_id in planar_caps:
            cap = graph.node(cap_id)

            # If the bore's rim sits on this face's inner wire, the bore goes
            # through it, so it is a mouth however small it is. A blended
            # mouth shares that inner wire with its blend, not with the bore,
            # so the bore's blend chain counts as the bore here: only faces
            # reached through the chain are eligible, and only on an inner
            # wire, and a bull-nose floor disc has none.
            if any(
                shares_inner_wire_with(graph, cap_id, partner)
                for partner in [cylinder.face_id, *chain]
            ):
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
            #
            # Near the axis is not enough on its own: the annulus round a
            # bore's opening has a hole exactly where the bore is, and a
            # fragment of it (the ring split by a fuse) can still have its
            # centroid near the axis. A complete annulus is caught above by
            # the inner-wire test; a fragment carries the rim on its outer
            # wire instead, so ask directly whether the cap covers the axis.
            offset = radial_distance(cap.centroid, axis.Location(), axis.Direction())
            near_axis = offset <= cylinder.cyl_radius
            if near_axis and self._cap_covers_axis(cap, axis.Location(), axis.Direction()):
                floors.append(cap_id)
            elif near_axis:
                # Inside the bore's footprint but not covering its axis: not
                # a floor, but if the bore turns out to break out, one lying
                # beyond the breakout is the notch its tool left.
                self._scar_candidates.append(cap_id)

        return (floors, openings)

    def _is_crescent_cone(self, cone: AagNode, cylinder: AagNode) -> bool:
        """A coaxial cone the bore axis misses and that narrows away.

        The axis line missing the face's box means the cone does not wrap
        the axis. Narrowing away from the bore (apex beyond the face) is
        what a drill point does; an entry chamfer half-removed by a slot
        also misses the axis but widens outward, apex back inside the bore.
        """
        if cone.bbox.IsVoid():
            return False
        axis = cylinder.cyl_cone_axis
        origin, axis_dir = axis.Location(), axis.Direction()
        if not cone.bbox.IsOut(gp_Lin(origin, axis_dir)):
            return False
        face_map = getattr(self, "_face_map", None)
        if face_map is None or not 1 <= cone.face_id <= face_map.Extent():
            return False
        face = TopoDS.Face_s(face_map.FindKey(cone.face_id))
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Cone:
            return False
        axis_v = gp_Vec(axis_dir.XYZ())
        t_bore = gp_Vec(origin, cylinder.centroid).Dot(axis_v)
        t_face = gp_Vec(origin, cone.centroid).Dot(axis_v)
        t_apex = gp_Vec(origin, surface.Cone().Apex()).Dot(axis_v)
        return (t_apex - t_face) * (t_face - t_bore) > 0.0

    def _record_breakout_scar(
        self,
        graph: AttributedAdjacencyGraph,
        cylinder: AagNode,
        feature: FeatureInstance,
        openings: list[int],
    ) -> None:
        """Record the notch a through bore's tool left in a wall at breakout.

        A bore that breaks out into a cavity beside a wall can clip that wall
        and leave a flat crescent of floor beyond the breakout plane, off to
        one side of the axis: a burr trap nothing else reports, because the
        cap test rightly refuses it as a floor. A scar is square to the axis
        (the flat end of the tool), lies beyond every opening, and lies
        beyond an opening that is an internal wall -- a step beside a mouth
        on the part's outer skin is a partial spot face, often deliberate.

        Positions along the axis are where the axis pierces each face's
        plane rather than projected centroids, which are only right for
        faces square to the bore.
        """
        axis = cylinder.cyl_cone_axis
        origin, axis_dir = axis.Location(), axis.Direction()
        axis_v = gp_Vec(axis_dir.XYZ())

        def pierce_t(node: AagNode) -> Optional[float]:
            if node.plane_normal is None:
                return None
            normal_v = gp_Vec(node.plane_normal.XYZ())
            denom = axis_v.Dot(normal_v)
            if abs(denom) <= 1e-6:
                return None
            return gp_Vec(origin, node.centroid).Dot(normal_v) / denom

        t_lo, t_hi = 1e18, -1e18
        open_lo = open_hi = None
        for opening_id in openings:
            t = pierce_t(graph.node(opening_id))
            if t is None:
                return
            if t < t_lo:
                t_lo, open_lo = t, opening_id
            if t > t_hi:
                t_hi, open_hi = t, opening_id

        def is_internal_wall(face_id) -> bool:
            return any(
                edge.concavity is Concavity.CONCAVE for edge in graph.edges_of(face_id)
            )

        scar_faces: list[int] = []
        scar_area = 0.0
        overshoot = 0.0
        for candidate_id in self._scar_candidates:
            node = graph.node(candidate_id)
            if node.plane_normal is None or abs(node.plane_normal.Dot(axis_dir)) < 0.99:
                continue
            t = pierce_t(node)
            if t is None:
                continue
            beyond = max(t - t_hi, t_lo - t)
            if beyond <= 0.05:
                continue
            if not is_internal_wall(open_hi if t > t_hi else open_lo):
                continue
            scar_faces.append(candidate_id)
            scar_area += node.area
            overshoot = max(overshoot, beyond)

        # Conical crescents: the drill point clipped the wall. Coaxial with
        # the bore, so the projected centroid is a fair position, and the
        # notch depth is the deepest box corner past the breakout.
        for cone_id in self._cone_scar_candidates:
            node = graph.node(cone_id)
            t = gp_Vec(origin, node.centroid).Dot(axis_v)
            if max(t - t_hi, t_lo - t) <= 0.05:
                continue
            past_hi = t > t_hi
            if not is_internal_wall(open_hi if past_hi else open_lo):
                continue
            xn, yn, zn, xx, yx, zx = node.bbox.Get()
            deepest = 0.0
            for x in (xn, xx):
                for y in (yn, yx):
                    for z in (zn, zx):
                        tc = gp_Vec(origin, gp_Pnt(x, y, z)).Dot(axis_v)
                        deepest = max(deepest, tc - t_hi if past_hi else t_lo - tc)
            scar_faces.append(cone_id)
            scar_area += node.area
            overshoot = max(overshoot, deepest)
            feature.parameters["breakout_scar_conical"] = True

        if scar_faces:
            feature.parameters["breakout_scar_faces"] = scar_faces
            feature.parameters["breakout_scar_area_mm2"] = scar_area
            feature.parameters["breakout_overshoot_mm"] = overshoot

    @staticmethod
    def _shallow_band_as_counterbore(
        graph: AttributedAdjacencyGraph, cylinder: AagNode, feature: FeatureInstance
    ) -> bool:
        """Reclassify a shallow capless band as a counterbore, if it is one."""
        axis = cylinder.cyl_cone_axis
        axis_dir = axis.Direction()
        for annulus_id in graph.neighbors_of(cylinder.face_id):
            if not graph.has_node(annulus_id):
                continue
            annulus = graph.node(annulus_id)
            if annulus.surface_type is not SurfaceType.PLANE or annulus.plane_normal is None:
                continue
            # Square to the axis: a seat floor, not a side wall.
            if abs(annulus.plane_normal.Dot(axis_dir)) < 0.99:
                continue
            for inner_id in graph.neighbors_of(annulus_id):
                if inner_id == cylinder.face_id or not graph.has_node(inner_id):
                    continue
                inner = graph.node(inner_id)
                if inner.surface_type is not SurfaceType.CYLINDER:
                    continue
                if inner.cyl_cone_axis is None:
                    continue
                if inner.cyl_radius >= cylinder.cyl_radius - 1.0e-6:
                    continue
                # A counterbore sits over a bore, and a bore wall is concave.
                # A face gland has the same three faces -- outer wall, ring
                # floor, smaller coaxial wall -- but its inner wall is the
                # convex sealing land standing back up to the face.
                if not inner.is_internal:
                    continue
                # And a bore beneath the seat lies past the annulus, not
                # beside it: a groove's two walls straddle the same axial
                # band as each other.
                axis_vec = gp_Vec(axis_dir.XYZ())
                origin = axis.Location()
                t_annulus = gp_Vec(origin, annulus.centroid).Dot(axis_vec)
                t_inner = gp_Vec(origin, inner.centroid).Dot(axis_vec)
                t_band = gp_Vec(origin, cylinder.centroid).Dot(axis_vec)
                if (t_inner - t_annulus) * (t_band - t_annulus) > 0.0:
                    continue
                if abs(inner.cyl_cone_axis.Direction().Dot(axis_dir)) < 0.99:
                    continue
                offset = gp_Vec(axis.Location(), inner.cyl_cone_axis.Location())
                if offset.Crossed(gp_Vec(axis_dir.XYZ())).Magnitude() > 0.5:
                    continue
                outer_diameter = feature.number("diameter_mm") or 0.0
                feature.type = FeatureType.COUNTERBORE
                feature.parameters["outer_diameter_mm"] = outer_diameter
                feature.parameters["diameter_mm"] = round(inner.cyl_radius * 2.0, 6)
                feature.parameters["counterbore_depth_mm"] = feature.number("depth_mm")
                feature.parameters.pop("terminates_in_cavity", None)
                feature.faces = sorted(set(feature.faces) | {annulus_id})
                return True
        return False

    def _is_annular_channel(self, graph: AttributedAdjacencyGraph, cylinder: AagNode) -> bool:
        """Whether a wide wall is the outside of an open ring, not a bore."""
        axis = cylinder.cyl_cone_axis
        axis_dir = axis.Direction()
        origin = axis.Location()
        axis_vec = gp_Vec(axis_dir.XYZ())
        seed_span = _node_axial_span(cylinder, origin, axis_dir)
        for annulus_id in graph.neighbors_of(cylinder.face_id):
            if not graph.has_node(annulus_id):
                continue
            annulus = graph.node(annulus_id)
            if annulus.surface_type is not SurfaceType.PLANE or annulus.plane_normal is None:
                continue
            if abs(annulus.plane_normal.Dot(axis_dir)) < 0.99:
                continue
            # A ring floor misses the axis; a real bore floor covers it.
            if self._cap_covers_axis(annulus, origin, axis_dir):
                continue
            for island_id in graph.neighbors_of(annulus_id):
                if island_id == cylinder.face_id or not graph.has_node(island_id):
                    continue
                island = graph.node(island_id)
                if island.surface_type is not SurfaceType.CYLINDER:
                    continue
                if island.is_internal:
                    continue  # convex: an island
                if island.cyl_cone_axis is None:
                    continue
                if island.cyl_radius >= cylinder.cyl_radius - 1.0e-6:
                    continue
                if abs(island.cyl_cone_axis.Direction().Dot(axis_dir)) < 0.99:
                    continue
                offset = gp_Vec(origin, island.cyl_cone_axis.Location())
                if offset.Crossed(axis_vec).Magnitude() > 0.5:
                    continue
                # Alongside, not beyond: the island shares the outer wall's
                # axial band. One past the floor would be a boss at the
                # bottom of a genuine bore.
                island_span = _node_axial_span(island, origin, axis_dir)
                overlap = min(seed_span[1], island_span[1]) - max(seed_span[0], island_span[0])
                if overlap < 0.5 * (seed_span[1] - seed_span[0]):
                    continue
                return True
        return False

    def _cap_covers_axis(self, cap: AagNode, axis_origin, axis_dir) -> bool:
        """Whether the point where the bore axis pierces this cap is on it.

        A floor fills the bore's circle; a fragment of the opening annulus
        does not. When the face cannot be examined the old answer (a floor)
        stands.
        """
        normal = cap.plane_normal
        if normal is None:
            return True
        denom = gp_Vec(axis_dir.XYZ()).Dot(gp_Vec(normal.XYZ()))
        if abs(denom) < 1e-9:
            return False  # the bore runs along the plane
        t = gp_Vec(axis_origin, cap.centroid).Dot(gp_Vec(normal.XYZ())) / denom
        pierce = gp_Pnt(
            axis_origin.X() + axis_dir.X() * t,
            axis_origin.Y() + axis_dir.Y() * t,
            axis_origin.Z() + axis_dir.Z() * t,
        )

        face_map = getattr(self, "_face_map", None)
        if face_map is None or not 1 <= cap.face_id <= face_map.Extent():
            return True
        face = TopoDS.Face_s(face_map.FindKey(cap.face_id))
        if face.IsNull():
            return True
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Plane:
            return True
        uv = ProjLib.Project_s(surface.Plane(), pierce)
        state = BRepClass_FaceClassifier(face, uv, 1.0e-4).State()
        return state in (TopAbs_IN, TopAbs_ON)

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
        depth = self._cylinder_depth(cylinder)
        for face_id in bridges:
            node = graph.node(face_id)
            depth += node.torus_minor_r
        return depth

    def _cylinder_depth(self, cylinder: AagNode) -> float:
        """A bore's depth, measured along its axis from its own boundary.

        Not the V-parameter span: for a cylinder V is axial, so on a cleanly
        trimmed bore that is the depth, but the V bounds are the UV bounding
        box of the trimmed face, and which U the face begins at is decided by
        the parametric seam -- so a bore cut by oblique faces read 77.54 or
        77.70 on one solid depending on where the seam fell. The boundary is
        sampled, not just its vertices: on a filleted or obliquely cut rim the
        extreme axial point sits in the middle of a rim curve. Falls back to
        the parameter span when the face cannot be measured.
        """
        span = cylinder_length(cylinder)
        shape = getattr(self, "_shape", None)
        if shape is None or cylinder.surface_type is not SurfaceType.CYLINDER:
            return span
        if cylinder.cyl_cone_axis is None:
            return span
        measured = extent_of_face(shape, cylinder.face_id, cylinder.cyl_cone_axis.Direction())
        return measured if measured > 0.0 else span

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
        feature.parameters["length_mm"] = round(self._cylinder_depth(cylinder), 6)
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
                # A counterbore's outer wall is a bore: it wraps the axis. The
                # rounded end of an obround slot is a half-cylinder coaxial
                # with a hole drilled at its centre, because that is where the
                # end radius is struck, and it read as a counterbore on that
                # hole while an identical hole in the same floor stayed plain.
                if self._face_map is not None and not cylinder_wraps_axis(
                    graph, self._face_map, outer
                ):
                    continue

                if found:
                    # The far seat. Absorbing it claims its cylinder, which
                    # is the whole point.
                    feature.parameters["outer2_diameter_mm"] = round(
                        outer.cyl_radius * 2.0, 6
                    )
                    feature.parameters["counterbore2_depth_mm"] = round(
                        self._cylinder_depth(outer), 6
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

                # A buried band is an internal relief groove, not a
                # counterbore. A counterbore's band is open: past its far end
                # is air, because that is where the cutter entered. A relief
                # undercut is buried mid-bore, and past its far shoulder the
                # same bore carries on at the inner radius. "Opens onto a
                # large face" is a positive signal only -- plenty of real
                # counterbores open onto a small or fragmented face -- so the
                # band is refused only on the geometric proof that the bore
                # re-enters beyond it.
                #
                # A PMI thread callout on the bore is the other proof: the
                # band is the relief the thread runs out into. Only the
                # callout makes the band a THREAD_RELIEF_GROOVE, since emitting
                # one asserts the bore is tapped, which no diameter alone may
                # imply; without it the band is just claimed, and the groove
                # recognizer's GROOVE is what describes it.
                opens_large, re_enters = self._band_ends(
                    graph, outer, shoulder.face_id, cylinder, axis, cross
                )
                callout = self._pmi_callout_on(cylinder)
                if not opens_large and (callout is not None or re_enters):
                    self._pending_relief_bands.append(outer.face_id)
                    if re_enters:
                        # Set on the geometric proof only, never on the
                        # callout alone: the shoulder walked through is the
                        # relief's mouth, not the bottom of the bore.
                        feature.parameters["bore_continues_past_floor"] = True
                        feature.parameters["flat_bottom"] = False
                    if callout is not None:
                        relief = {
                            "width_mm": round(self._cylinder_depth(outer), 6),
                            "depth_mm": round(outer.cyl_radius - cylinder.cyl_radius, 6),
                            "diameter_mm": round(outer.cyl_radius * 2.0, 6),
                            "adjacent_thread_designation": callout.designation,
                        }
                        if callout.pitch_mm > 0.0:
                            relief["adjacent_thread_pitch_mm"] = callout.pitch_mm
                        self._pending_reliefs.append(
                            FeatureInstance(
                                instance_id="",
                                type=FeatureType.THREAD_RELIEF_GROOVE,
                                faces=[outer.face_id, shoulder.face_id],
                                parameters=relief,
                            )
                        )
                    # Keep scanning: a true entry-side counterbore may still
                    # sit on the other shoulder.
                    continue

                feature.type = FeatureType.COUNTERBORE
                feature.parameters["outer_diameter_mm"] = round(
                    outer.cyl_radius * 2.0, 6
                )
                feature.parameters["counterbore_depth_mm"] = round(
                    self._cylinder_depth(outer), 6
                )
                feature.faces = sorted(
                    set(feature.faces) | {shoulder.face_id, outer.face_id}
                )
                self._walk_stacked_seats(graph, feature, outer, axis)

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

    def _walk_stacked_seats(
        self,
        graph: AttributedAdjacencyGraph,
        feature: FeatureInstance,
        seat: AagNode,
        axis,
    ) -> None:
        """Follow a counterbore that is itself counterbored, outward.

        The second seat is reachable only across the first seat's own
        shoulder, never from the bore, so left alone the main loop meets it
        as an unclaimed capless band and makes a second feature of it. Each
        further level is recorded in `counterbore_stack`, outward;
        `outer_diameter_mm` keeps meaning the seat directly over the bore.

        A seat is open and a groove is buried: past a relief or gland the
        band below continues at its own diameter, the same size-independent
        test the first level uses. Without it the walk took an O-ring gland
        cut in a seat wall for a second seat.
        """
        axis_dir = axis.Direction()
        origin = axis.Location()
        axis_vec = gp_Vec(axis_dir.XYZ())

        def coaxial(node: AagNode) -> bool:
            if node.cyl_cone_axis is None:
                return False
            if abs(axis_dir.Dot(node.cyl_cone_axis.Direction())) < 0.98:
                return False
            offset = gp_Vec(origin, node.cyl_cone_axis.Location())
            return axis_vec.Crossed(offset).Magnitude() <= 0.5

        def square_plane(node: AagNode) -> bool:
            return (
                node.surface_type is SurfaceType.PLANE
                and node.plane_normal is not None
                and abs(node.plane_normal.Dot(axis_dir)) >= 0.7
            )

        step = seat
        stack = [
            {
                "diameter_mm": round(seat.cyl_radius * 2.0, 6),
                "depth_mm": round(self._cylinder_depth(seat), 6),
            }
        ]
        for _ in range(4):
            found_next: Optional[tuple[AagNode, int]] = None
            for edge in graph.edges_of(step.face_id):
                if found_next is not None:
                    break
                cap_id = edge.other_face(step.face_id)
                if not graph.has_node(cap_id) or cap_id in feature.faces:
                    continue
                cap = graph.node(cap_id)
                if not square_plane(cap):
                    continue
                for edge_w in graph.edges_of(cap_id):
                    wall_id = edge_w.other_face(cap_id)
                    if wall_id == step.face_id or not graph.has_node(wall_id):
                        continue
                    wall = graph.node(wall_id)
                    if wall.surface_type is not SurfaceType.CYLINDER:
                        continue
                    if not wall.is_internal:
                        continue  # a recessed wall
                    if wall.cyl_radius <= step.cyl_radius + 1.0e-4:
                        continue
                    if not coaxial(wall):
                        continue
                    re_enters = False
                    for edge_f in graph.edges_of(wall_id):
                        far_id = edge_f.other_face(wall_id)
                        if far_id == cap_id or not graph.has_node(far_id):
                            continue
                        far = graph.node(far_id)
                        if not square_plane(far):
                            continue
                        for edge_b in graph.edges_of(far_id):
                            beyond_id = edge_b.other_face(far_id)
                            if beyond_id == wall_id or not graph.has_node(beyond_id):
                                continue
                            beyond = graph.node(beyond_id)
                            if beyond.surface_type is not SurfaceType.CYLINDER:
                                continue
                            if abs(beyond.cyl_radius - step.cyl_radius) > 0.05:
                                continue
                            if not coaxial(beyond):
                                continue
                            re_enters = True
                            break
                        if re_enters:
                            break
                    if re_enters:
                        continue
                    found_next = (wall, cap_id)
                    break
            if found_next is None:
                break
            wall, cap_id = found_next
            feature.faces = sorted(set(feature.faces) | {wall.face_id, cap_id})
            stack.append(
                {
                    "diameter_mm": round(wall.cyl_radius * 2.0, 6),
                    "depth_mm": round(self._cylinder_depth(wall), 6),
                }
            )
            step = wall
        if len(stack) > 1:
            feature.parameters["counterbore_stack"] = stack
            feature.parameters["counterbore_levels"] = len(stack)

    @staticmethod
    def _band_ends(
        graph: AttributedAdjacencyGraph,
        band: AagNode,
        shoulder_id: int,
        cylinder: AagNode,
        axis,
        cross: float,
    ) -> tuple[bool, bool]:
        """How a wider coaxial band ends past its far shoulder.

        Returns whether it opens onto a planar face much bigger than the
        bore, and whether a coaxial cylinder at the bore's own radius
        continues beyond that shoulder.
        """
        axis_dir = axis.Direction()
        origin = axis.Location()
        opens_large = False
        re_enters = False
        for far, _ in neighbours(graph, band.face_id):
            if far.face_id == shoulder_id:
                continue
            if far.surface_type is not SurfaceType.PLANE or far.plane_normal is None:
                continue
            if abs(far.plane_normal.Dot(axis_dir)) < _CAP_AXIS_ALIGNMENT:
                continue
            if far.area >= cross * _OPENING_AREA_MULTIPLE:
                opens_large = True
            for beyond, _ in neighbours(graph, far.face_id):
                if beyond.face_id == band.face_id:
                    continue
                if beyond.surface_type is not SurfaceType.CYLINDER:
                    continue
                if beyond.cyl_cone_axis is None:
                    continue
                if abs(beyond.cyl_radius - cylinder.cyl_radius) > 0.05:
                    continue
                if abs(axis_dir.Dot(beyond.cyl_cone_axis.Direction())) < 0.98:
                    continue
                offset = gp_Vec(origin, beyond.cyl_cone_axis.Location())
                if gp_Vec(axis_dir.XYZ()).Crossed(offset).Magnitude() > 0.5:
                    continue
                re_enters = True
                break
        return opens_large, re_enters

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
        countersink. A countersink and a lead-in chamfer are the same cone,
        and what separates them is what set its size: a chamfer is an edge
        break, the same absolute size whatever bore it breaks, while a
        countersink is sized by the head it swallows and grows about half a
        bore per side. A cone that widens past any fastener head is a funnel
        or a chamber taper.
        """
        axis = cylinder.cyl_cone_axis
        scan = self._countersink_eligible(graph, cylinder)
        if not scan.eligible:
            return False
        is_through_bore = scan.is_through
        bore_diameter = cylinder.cyl_radius * 2.0

        for cone_id in cone_ids:
            cone = graph.node(cone_id)
            if cone.cone_r0 <= 0.0 and cone.cone_r1 <= 0.0:
                continue
            if cone.cyl_cone_axis is None:
                continue
            if abs(axis.Direction().Dot(cone.cyl_cone_axis.Direction())) < 0.98:
                continue
            # Direction and axis line: a cone merely parallel to the bore is
            # not this hole's countersink.
            offset = gp_Vec(axis.Location(), cone.cyl_cone_axis.Location())
            if gp_Vec(axis.Direction().XYZ()).Crossed(offset).Magnitude() > 0.5:
                continue
            if not _widens_away_from(cone, cylinder.centroid):
                continue

            # Measure the face, not a centre-of-mass estimate of it.
            _, wide_radius = _cone_true_radii(cone)

            # Does the cone's wide end open out of the part, or into a bigger
            # bore? A large bore drilled with a twist drill leaves a conical
            # bottom; put a smaller passage through its apex and, from the
            # passage's side, that cone is a flawless coaxial widening cone
            # meeting the bore exactly. What gives it away is where it ends:
            # a countersink opens to the surface of the part, a drill-point
            # remnant opens to the wall of the bore it bottoms -- a coaxial
            # cylinder whose radius is the cone's own wide rim. Matched to
            # 2 %, so a countersink inside a counterbore (rim smaller than
            # the counterbore) is not caught by it.
            if self._bottoms_a_bigger_bore(graph, cylinder, cone, wide_radius):
                continue
            is_head_seat = True
            is_edge_break = False

            # (a) Absolute ceiling: real fastener seats top out around 30 mm
            # of rim. Wider is a chamber taper or funnel.
            if wide_radius * 2.0 > _COUNTERSINK_MAX_RIM_DIAMETER:
                is_head_seat = False

            # (b) Does the cone scale with the bore, or with the edge? A third
            # of a bore of radial growth keeps every genuine countersink
            # (0.37-0.58 x bore) and rejects the fixed edge break and the
            # oversized lead-in too small to seat the head its bore implies.
            # (c) ...unless the cone ran out of material before it ran out of
            # fastener: a seat truncated by thin sheet grows little but eats
            # a third or more of the hole's axial extent, which no edge break
            # does. (a) is checked independently, so this cannot rescue a
            # funnel.
            growth = wide_radius - cylinder.cyl_radius
            grows_like_a_head = growth >= _MIN_HEAD_SEAT_GROWTH_PER_BORE * bore_diameter
            eats_the_material = False
            if not grows_like_a_head:
                cone_axial = _axial_extent(cone)
                bore_axial = sum(
                    _axial_extent(graph.node(face_id))
                    for face_id in feature.faces
                    if graph.has_node(face_id)
                    and graph.node(face_id).surface_type is SurfaceType.CYLINDER
                )
                total = bore_axial + cone_axial
                if total > 1.0e-6:
                    eats_the_material = cone_axial >= _MIN_SEAT_SHARE_OF_HOLE * total
            if not grows_like_a_head and not eats_the_material:
                is_head_seat = False
                is_edge_break = True

            if not is_head_seat:
                # Not a seat, but still this bore's far opening: a big
                # chamfered bore stays a plain hole and gains an honest
                # answer about running through. An edge break is released
                # from the hole's faces so the blend recognizer can call it
                # the chamfer it is; a cone rejected only by the ceiling is a
                # mouth or taper of the bore itself and stays with it.
                if (
                    is_edge_break
                    and cone_id in feature.faces
                    and cone_id != cylinder.face_id
                ):
                    feature.faces.remove(cone_id)
                if is_through_bore and not scan.has_planar_floor:
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
            # Claiming the entry cone says nothing about the far end: a blind
            # hole can be countersunk at the mouth and pointed at the bottom.
            # State the far end from what the scan found.
            feature.parameters["drill_point_bottom"] = scan.has_drill_point
            feature.parameters["flat_bottom"] = (
                scan.has_planar_floor and not scan.has_drill_point
            )
            if cone_id not in feature.faces:
                feature.faces.append(cone_id)
                feature.faces.sort()
            if is_through_bore:
                # The first pass saw one planar opening and called the bore
                # blind, because the cone stands where the other opening
                # would be. It is the other opening.
                feature.parameters["is_through"] = True
                feature.parameters["flat_bottom"] = True  # unused on a through bore
                feature.parameters.pop("terminates_in_cavity", None)
                # And its depth is the cylinder and the cone together, not the
                # cylinder alone. Summed from each face's analytic V range: a
                # cylinder's V runs along the axis, a cone's along the slant,
                # so the cone's is scaled by the cosine of its half angle.
                full_depth = self._bore_and_cone_depth(graph, feature)
                if full_depth > 0.0:
                    feature.parameters["depth_mm"] = round(full_depth, 6)
            return True
        return False

    def _bore_and_cone_depth(
        self, graph: AttributedAdjacencyGraph, feature: FeatureInstance
    ) -> float:
        """Axial length of a feature's cylinders and cones, summed."""
        face_map = self._face_map
        if face_map is None:
            return 0.0
        total = 0.0
        for face_id in feature.faces:
            if not graph.has_node(face_id) or not 1 <= face_id <= face_map.Extent():
                continue
            node = graph.node(face_id)
            if node.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
                continue
            surface = BRepAdaptor_Surface(TopoDS.Face_s(face_map.FindKey(face_id)), True)
            extent = abs(surface.LastVParameter() - surface.FirstVParameter())
            if node.surface_type is SurfaceType.CONE:
                extent *= abs(math.cos(node.cone_semi_angle))
            total += extent
        return total

    @staticmethod
    def _bottoms_a_bigger_bore(
        graph: AttributedAdjacencyGraph,
        cylinder: AagNode,
        cone: AagNode,
        wide_radius: float,
    ) -> bool:
        """Whether the cone's wide rim meets a wider coaxial bore's wall."""
        axis = cylinder.cyl_cone_axis
        axis_dir = axis.Direction()
        for wall, _ in neighbours(graph, cone.face_id):
            if wall.surface_type is not SurfaceType.CYLINDER:
                continue
            if wall.cyl_cone_axis is None:
                continue
            if abs(axis_dir.Dot(wall.cyl_cone_axis.Direction())) < 0.98:
                continue
            offset = gp_Vec(axis.Location(), wall.cyl_cone_axis.Location())
            if gp_Vec(axis_dir.XYZ()).Crossed(offset).Magnitude() > 0.5:
                continue
            if wall.cyl_radius <= cylinder.cyl_radius * 1.05:
                continue
            if abs(wall.cyl_radius - wide_radius) <= 0.02 * wide_radius + 0.05:
                return True
        return False

    @staticmethod
    def _countersink_eligible(
        graph: AttributedAdjacencyGraph, cylinder: AagNode
    ) -> "_CountersinkScan":
        """Whether this bore has an end for a countersink to be the other of.

        A cone on a bore means one of two things. If the bore bottoms out on
        a floor, a cone at its mouth is a seat; if it runs through -- two
        openings, either planar or the widening cone itself -- the cone is
        the far one. And a cone that widens away from the bore cannot be a
        bottom whatever the other end does, because a drill point narrows to
        its apex: a blind hole countersunk at the mouth and pointed at the
        bottom has neither a floor nor two openings, and is still eligible.
        """
        axis_dir = cylinder.cyl_cone_axis.Direction()
        cross = cross_section_area(cylinder)
        scan = _CountersinkScan()
        large_openings = 0

        for node, _ in neighbours(graph, cylinder.face_id):
            if node.surface_type is SurfaceType.PLANE:
                if node.plane_normal is None:
                    continue
                if abs(node.plane_normal.Dot(axis_dir)) < 0.7:
                    continue
                if node.area < cross * 3.0:
                    scan.has_planar_floor = True
                else:
                    large_openings += 1
            elif node.surface_type is SurfaceType.CONE:
                if node.cyl_cone_axis is None:
                    continue
                if abs(axis_dir.Dot(node.cyl_cone_axis.Direction())) < 0.95:
                    continue
                if node.centroid.Distance(cylinder.centroid) < 1.0e-6:
                    continue
                if _widens_away_from(node, cylinder.centroid):
                    large_openings += 1
                    scan.has_widening_cone = True
                else:
                    # Narrowing away from the bore: a drill point. A hole can
                    # have both, and the far end's flat_bottom needs to know.
                    scan.has_drill_point = True

        scan.is_through = large_openings >= 2
        scan.eligible = (
            scan.has_planar_floor or scan.is_through or scan.has_widening_cone
        )
        return scan

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

    def _fold_seam_halves(
        self, graph: AttributedAdjacencyGraph, features: list[FeatureInstance]
    ) -> list[FeatureInstance]:
        """Fold a seam half into the feature that owns its partner.

        The kernel splits a full cylinder into two half faces. The
        counterbore pass takes the first matching outer half, so the partner
        half is seeded on its own as a hole of the seat's diameter -- and
        those orphans then form patterns of nothing, or get welded across a
        narrower section into a bore that does not exist. A feature folds
        into another when every cylinder it has is a seam partner of one the
        other owns: same radius, coaxial, and sharing an edge, which is what
        keeps two genuinely separate coaxial holes of one diameter apart.
        Only a smaller feature folds into a larger one, so a counterbore is
        never folded into its own half.
        """

        def cylinders_of(feature: FeatureInstance) -> list[AagNode]:
            return [
                graph.node(face_id)
                for face_id in feature.faces
                if graph.has_node(face_id)
                and graph.node(face_id).surface_type is SurfaceType.CYLINDER
            ]

        def seam_partners(a: AagNode, b: AagNode) -> bool:
            if a.face_id == b.face_id:
                return False
            if a.cyl_cone_axis is None or b.cyl_cone_axis is None:
                return False
            if abs(a.cyl_radius - b.cyl_radius) > 1.0e-3:
                return False
            da = a.cyl_cone_axis.Direction()
            if abs(da.Dot(b.cyl_cone_axis.Direction())) < 0.999:
                return False
            offset = gp_Vec(a.cyl_cone_axis.Location(), b.cyl_cone_axis.Location())
            if gp_Vec(da.XYZ()).Crossed(offset).Magnitude() > 0.05:
                return False
            return any(
                edge.other_face(a.face_id) == b.face_id for edge in graph.edges_of(a.face_id)
            )

        folded = [False] * len(features)
        receivers: list[int] = []
        for fi, feature in enumerate(features):
            f_cyls = cylinders_of(feature)
            if not f_cyls:
                continue
            for gi, other in enumerate(features):
                if gi == fi or folded[gi]:
                    continue
                g_cyls = cylinders_of(other)
                if len(g_cyls) <= len(f_cyls):
                    continue
                if not all(any(seam_partners(fc, gc) for gc in g_cyls) for fc in f_cyls):
                    continue
                other.faces = sorted(set(other.faces) | set(feature.faces))
                folded[fi] = True
                if gi not in receivers:
                    receivers.append(gi)
                break

        # The receiver has more cylinders than it had, so its through
        # question is asked again of the whole of it.
        for gi in receivers:
            if not folded[gi]:
                self._describe_interrupted_bore(graph, features[gi], [features[gi]])
        return [feature for index, feature in enumerate(features) if not folded[index]]

    def _join_across_relief(
        self, graph: AttributedAdjacencyGraph, features: list[FeatureInstance]
    ) -> list[FeatureInstance]:
        """Join one bore that its own relief groove cut in two.

        A tap drill with a relief undercut part-way down is one hole. The
        undercut divides the bore wall into two coaxial fragments of one
        radius, and each is seeded on its own: the entry fragment with its
        chamfer as a countersink, the far one with its drill point as a blind
        hole. Left so, a bolt circle of such ports makes two patterns where
        there is one, and the resolver's coaxial merge neither considers
        countersinks nor runs before patterns, so it has to happen here.

        Two features join when a cylinder of each is the same radius and
        coaxial with the other, and both touch a shoulder of ONE larger
        coaxial band -- the relief between them. That last condition is what
        separates a relief-interrupted bore from two holes that merely share
        an axis. The joined hole keeps the more specific type (the entry's),
        spans both fragments and takes how it finishes from the far one.
        """

        def coaxial(a: AagNode, b: AagNode, same_radius: bool) -> bool:
            if a.cyl_cone_axis is None or b.cyl_cone_axis is None:
                return False
            if same_radius and abs(a.cyl_radius - b.cyl_radius) > 1.0e-3:
                return False
            da = a.cyl_cone_axis.Direction()
            if abs(da.Dot(b.cyl_cone_axis.Direction())) < 0.999:
                return False
            offset = gp_Vec(a.cyl_cone_axis.Location(), b.cyl_cone_axis.Location())
            return gp_Vec(da.XYZ()).Crossed(offset).Magnitude() <= 0.05

        def relief_bands(cylinder: AagNode) -> set[int]:
            # Larger coaxial cylinders sharing a planar shoulder with it.
            bands: set[int] = set()
            axis_dir = cylinder.cyl_cone_axis.Direction()
            for edge in graph.edges_of(cylinder.face_id):
                shoulder_id = edge.other_face(cylinder.face_id)
                if not graph.has_node(shoulder_id):
                    continue
                shoulder = graph.node(shoulder_id)
                if shoulder.surface_type is not SurfaceType.PLANE:
                    continue
                if shoulder.plane_normal is None:
                    continue
                if abs(shoulder.plane_normal.Dot(axis_dir)) < 0.99:
                    continue
                for edge2 in graph.edges_of(shoulder_id):
                    band_id = edge2.other_face(shoulder_id)
                    if band_id == cylinder.face_id or not graph.has_node(band_id):
                        continue
                    band = graph.node(band_id)
                    if band.surface_type is not SurfaceType.CYLINDER:
                        continue
                    if band.cyl_radius <= cylinder.cyl_radius + 1.0e-3:
                        continue
                    if not coaxial(cylinder, band, False):
                        continue
                    bands.add(band_id)
            return bands

        def cylinders_of(feature: FeatureInstance) -> list[AagNode]:
            return [
                graph.node(face_id)
                for face_id in feature.faces
                if graph.has_node(face_id)
                and graph.node(face_id).surface_type is SurfaceType.CYLINDER
            ]

        def axial_range(feature: FeatureInstance, radius: float, origin, direction):
            low, high = math.inf, -math.inf
            for node in cylinders_of(feature):
                if abs(node.cyl_radius - radius) > 1.0e-3:
                    continue
                for point in (node.cyl_p0, node.cyl_p1):
                    if point is None:
                        continue
                    t = gp_Vec(origin, point).Dot(gp_Vec(direction.XYZ()))
                    low, high = min(low, t), max(high, t)
            return low, high

        # Repeat until nothing joins: a join gives a feature new fragments,
        # and those can put it next to a relief it was not next to when the
        # scan passed its partner. A single pass left a spool bore crossed
        # by several glands in two pieces.
        receivers: list[FeatureInstance] = []
        any_joined = True
        while any_joined:
            any_joined = False
            joined = [False] * len(features)
            for i in range(len(features)):
                if joined[i]:
                    continue
                for j in range(i + 1, len(features)):
                    if joined[j]:
                        continue
                    pair = None
                    for x in cylinders_of(features[i]):
                        for y in cylinders_of(features[j]):
                            if not coaxial(x, y, True):
                                continue
                            bands_x = relief_bands(x)
                            if not bands_x:
                                continue
                            if not bands_x & relief_bands(y):
                                continue
                            pair = x
                            break
                        if pair is not None:
                            break
                    if pair is None:
                        continue

                    # The more specific fragment carries the entry (its
                    # countersink or counterbore); the other is the far end.
                    # On a tie the first is kept.
                    i_is_far = _specificity(features[j].type) > _specificity(
                        features[i].type
                    )
                    keep = features[j] if i_is_far else features[i]
                    other = features[i] if i_is_far else features[j]

                    origin = pair.cyl_cone_axis.Location()
                    direction = pair.cyl_cone_axis.Direction()
                    keep_lo, keep_hi = axial_range(keep, pair.cyl_radius, origin, direction)
                    other_lo, other_hi = axial_range(other, pair.cyl_radius, origin, direction)
                    t_lo, t_hi = min(keep_lo, other_lo), max(keep_hi, other_hi)
                    # Measured before the faces merge. Only fragments on
                    # opposite sides of the band are a bore interrupted by a
                    # relief; two over the same span are the halves of one
                    # bore touching the same shoulder. Joining those still
                    # removes a duplicate, but it is not a relief interruption.
                    disjoint = min(keep_hi, other_hi) - max(keep_lo, other_lo) < 0.05

                    for face_id in other.faces:
                        if face_id not in keep.faces:
                            keep.faces.append(face_id)
                    if t_hi > t_lo:
                        keep.parameters["depth_mm"] = round(t_hi - t_lo, 6)
                    through = bool(keep.parameters.get("is_through", False)) or bool(
                        other.parameters.get("is_through", False)
                    )
                    keep.parameters["is_through"] = through
                    keep.parameters["flat_bottom"] = (
                        True if through else bool(other.parameters.get("flat_bottom", False))
                    )
                    keep.parameters.pop("bore_continues_past_floor", None)
                    if disjoint:
                        keep.parameters["relief_interrupted"] = True

                    other_index = i if i_is_far else j
                    keep_index = j if i_is_far else i
                    joined[other_index] = True
                    any_joined = True
                    if not any(features[keep_index] is r for r in receivers):
                        receivers.append(features[keep_index])
                    if other_index == i:
                        break
            features = [f for index, f in enumerate(features) if not joined[index]]

        # A joined hole has more cylinders than either fragment, so the
        # whole-bore through question is asked of it, as it is of any
        # multi-cylinder hole.
        for feature in receivers:
            if any(feature is kept for kept in features):
                feature.faces.sort()
                self._describe_interrupted_bore(graph, feature, [feature])
        return features

    @staticmethod
    def _record_overshoot_scar(
        graph: AttributedAdjacencyGraph, feature: FeatureInstance
    ) -> None:
        """Report a bore that ran past its exit and scored a wall.

        The crescent a bore leaves when it overshoots its exit into an
        adjacent wall is folded into the bore -- it is one drilling -- but
        folding it in silently would lose the thing worth reporting: a burr
        trap in a place nobody can reach.

        A fragment is an overshoot when it is a partial arc of the bore lying
        mostly beyond the bore proper: the full-circle walls plus the bore's
        own cones. Walls are grouped by axial band and the full-circle test
        runs on each group's union, because the kernel seam-splits a bore
        into halves neither of which spans the diameter alone; judged a face
        at a time a countersunk bore's whole wall read as an overshoot.
        """
        if feature.param("breakout_scar_faces"):
            return  # a planar scar is already recorded
        cylinders = [
            graph.node(face_id)
            for face_id in feature.faces
            if graph.has_node(face_id)
            and graph.node(face_id).surface_type is SurfaceType.CYLINDER
            and graph.node(face_id).cyl_cone_axis is not None
        ]
        if not cylinders:
            return
        seed = min(cylinders, key=lambda n: (n.cyl_radius, n.face_id))
        axis = seed.cyl_cone_axis.Direction()
        origin = seed.cyl_cone_axis.Location()

        # Bands: [low, high, box, face ids].
        bands: list[list] = []
        main_lo, main_hi = math.inf, -math.inf
        for face_id in feature.faces:
            if not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            if node.bbox.IsVoid():
                continue
            low, high = _node_axial_span(node, origin, axis)
            if node.surface_type is SurfaceType.CONE:
                main_lo, main_hi = min(main_lo, low), max(main_hi, high)
                continue
            if node.surface_type is not SurfaceType.CYLINDER:
                continue
            if abs(node.cyl_radius - seed.cyl_radius) > 1.0e-3:
                continue
            into = None
            for band in bands:
                if low < band[1] - 0.1 and high > band[0] + 0.1:
                    into = band
                    break
            if into is None:
                into = [low, high, Bnd_Box(), []]
                bands.append(into)
            into[0] = min(into[0], low)
            into[1] = max(into[1], high)
            into[2].Add(node.bbox)
            into[3].append(face_id)

        arc_bands = []
        for band in bands:
            if _spans_full_circle(band[2], axis, seed.cyl_radius):
                main_lo, main_hi = min(main_lo, band[0]), max(main_hi, band[1])
            else:
                arc_bands.append(band)
        if not arc_bands or main_hi < main_lo:
            return

        scars: list[int] = []
        scar_area = 0.0
        overshoot = 0.0
        for low, high, _, ids in arc_bands:
            # Mostly beyond the bore's own band, judged against the band's
            # own extent: the kernel pads curved-face boxes, so a crescent
            # sitting past the bore formally overlaps it.
            extent = high - low
            beyond = max(high - main_hi, main_lo - low)
            if extent <= 1.0e-9 or beyond < 0.5 * extent:
                continue
            # An overshoot is a sliver past the end of the bore. An arc as
            # long as the bore itself is the bore -- a wall that broke out
            # through the side of the part.
            if extent > 0.5 * (main_hi - main_lo):
                continue
            for face_id in ids:
                scars.append(face_id)
                scar_area += graph.node(face_id).area
            overshoot = max(overshoot, beyond)
        if not scars:
            return
        feature.parameters["breakout_scar_faces"] = scars
        feature.parameters["breakout_scar_area_mm2"] = scar_area
        feature.parameters["breakout_overshoot_mm"] = overshoot
        feature.parameters["breakout_scar_kind"] = "overshoot_arc"

    def _end_in_relief(self, graph: AttributedAdjacencyGraph, feature: FeatureInstance) -> None:
        """A bore that ends in a relief undercut does not go through.

        The first pass counts a relief's shoulder ring as a second opening:
        the bore does pass through that ring -- into the relief, not to air.
        So an end cap that opens into a wider coaxial internal band, whose
        own far end is no opening, and past which the bore does not carry
        on, is where the bore ends. Depth runs from the open rim to the
        band's far end.

        Neither the band nor its shoulders are claimed: that band is often a
        groove already, and claiming it here would put one face under a hole
        and a groove at once. Its size is recorded as evidence instead.
        """
        if not feature.param("is_through"):
            return  # only false throughs are corrected
        cylinders = [
            graph.node(face_id)
            for face_id in feature.faces
            if graph.has_node(face_id)
            and graph.node(face_id).surface_type is SurfaceType.CYLINDER
            and graph.node(face_id).cyl_cone_axis is not None
        ]
        if not cylinders:
            return
        # The seed: the hole was seeded from its narrowest cylinder.
        seed = min(cylinders, key=lambda n: (n.cyl_radius, n.face_id))
        axis = seed.cyl_cone_axis.Direction()
        origin = seed.cyl_cone_axis.Location()
        axis_vec = gp_Vec(axis.XYZ())

        def t_of(point) -> float:
            return gp_Vec(origin, point).Dot(axis_vec)

        def coaxial(node: AagNode) -> bool:
            if node.cyl_cone_axis is None:
                return False
            if abs(node.cyl_cone_axis.Direction().Dot(axis)) < 0.99:
                return False
            offset = gp_Vec(origin, node.cyl_cone_axis.Location())
            return axis_vec.Crossed(offset).Magnitude() <= 0.5

        def axial_plane(node: AagNode) -> bool:
            return (
                node.surface_type is SurfaceType.PLANE
                and node.plane_normal is not None
                and abs(node.plane_normal.Dot(axis)) >= 0.99
            )

        own = set(feature.faces)
        for cylinder in cylinders:
            if abs(cylinder.cyl_radius - seed.cyl_radius) > 1.0e-3:
                continue
            if cylinder.cyl_p0 is None or cylinder.cyl_p1 is None:
                continue
            cross = math.pi * cylinder.cyl_radius**2

            def same_bore(node: AagNode) -> bool:
                return (
                    node.surface_type is SurfaceType.CYLINDER
                    and node.is_internal
                    and abs(node.cyl_radius - cylinder.cyl_radius) < 1.0e-3
                    and coaxial(node)
                )

            for edge in graph.edges_of(cylinder.face_id):
                cap_id = edge.other_face(cylinder.face_id)
                if not graph.has_node(cap_id):
                    continue
                cap = graph.node(cap_id)
                if not axial_plane(cap):
                    continue
                if cap.area >= 3.0 * cross:
                    continue  # a real opening
                if self._cap_covers_axis(cap, origin, axis):
                    continue  # a real floor
                for edge2 in graph.edges_of(cap_id):
                    band_id = edge2.other_face(cap_id)
                    if not graph.has_node(band_id):
                        continue
                    # A counterbore's band is one of the hole's own faces; a
                    # relief band never is -- it is unclaimed, or a groove.
                    # Without this a through bore's own seat, split by a
                    # gland, reads as a relief and the bore comes back blind.
                    if band_id in own:
                        continue
                    band = graph.node(band_id)
                    if band.surface_type is not SurfaceType.CYLINDER:
                        continue
                    if band.cyl_radius <= cylinder.cyl_radius + 1.0e-3:
                        continue  # wider
                    if not band.is_internal:
                        continue  # internal
                    if not coaxial(band):
                        continue
                    # An entry seat, not a relief, when the band's far end
                    # opens.
                    band_cross = math.pi * band.cyl_radius**2
                    band_opens = False
                    for edge3 in graph.edges_of(band_id):
                        other_id = edge3.other_face(band_id)
                        if other_id == cap_id or not graph.has_node(other_id):
                            continue
                        other = graph.node(other_id)
                        if not axial_plane(other):
                            continue
                        if other.area >= 3.0 * band_cross:
                            band_opens = True
                            break
                    if band_opens:
                        continue

                    # The bore must actually end here. A bore that runs on
                    # through a land relief has the same wall past the band.
                    # The relief's own lip is the same radius but convex --
                    # metal on the other side -- so it does not count.
                    bore_continues = False
                    for edge3 in graph.edges_of(band_id):
                        other_id = edge3.other_face(band_id)
                        if other_id == cap_id or not graph.has_node(other_id):
                            continue
                        other = graph.node(other_id)
                        if same_bore(other):
                            bore_continues = True
                            break
                        if other.surface_type is not SurfaceType.PLANE:
                            continue
                        for edge4 in graph.edges_of(other_id):
                            beyond_id = edge4.other_face(other_id)
                            if beyond_id == band_id or not graph.has_node(beyond_id):
                                continue
                            if same_bore(graph.node(beyond_id)):
                                bore_continues = True
                                break
                        if bore_continues:
                            break
                    if bore_continues:
                        continue
                    if band.cyl_p0 is None or band.cyl_p1 is None:
                        continue

                    # Depth: the bore's open rim to the band's far end.
                    t_cap = t_of(cap.centroid)
                    t_rim = t_of(cylinder.cyl_p0)
                    if abs(t_of(cylinder.cyl_p1) - t_cap) > abs(t_rim - t_cap):
                        t_rim = t_of(cylinder.cyl_p1)
                    t_band = t_of(band.cyl_p0)
                    if abs(t_of(band.cyl_p1) - t_rim) > abs(t_band - t_rim):
                        t_band = t_of(band.cyl_p1)
                    depth = abs(t_rim - t_band)
                    if depth <= 0.0:
                        continue

                    params = feature.parameters
                    params["is_through"] = False
                    params["flat_bottom"] = True  # a bored floor past the relief
                    params["depth_mm"] = round(depth, 6)
                    params["ends_in_relief"] = True
                    # A blind bore is reached from one end, so its approach
                    # is signed and points at the opening; left unsigned, a
                    # back recess clusters with the front-face drilling and a
                    # two-setup part reads as one.
                    open_dir = gp_Dir(axis.XYZ())
                    if t_rim < t_band:
                        open_dir.Reverse()
                    params["axis"] = (
                        round(open_dir.X(), 6),
                        round(open_dir.Y(), 6),
                        round(open_dir.Z(), 6),
                    )
                    params["axis_signed"] = True
                    params["relief_diameter_mm"] = round(band.cyl_radius * 2.0, 6)
                    params["relief_width_mm"] = round(self._cylinder_depth(band), 6)
                    params.pop("terminates_in_cavity", None)
                    if feature.type in (
                        FeatureType.THROUGH_HOLE,
                        FeatureType.BREAKTHROUGH_HOLE,
                    ):
                        feature.type = FeatureType.BLIND_HOLE
                        params["hole_type"] = feature.type
                    return

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
                # The more specific type of the two wins: a counterbore that
                # happens to be split is still a counterbore. Only the type:
                # the reference keeps the first fragment's parameters, so a
                # seed that proved its bore runs on past a relief keeps
                # saying so, and keeps its axis unsigned.
                if _specificity(other.type) > _specificity(feature.type):
                    feature.type = other.type
                    feature.parameters["hole_type"] = other.type

            # Asked of anything with more than one cylinder to its name,
            # not only of what this pass just joined together. A counterbore
            # has two cylinders from the start -- the bore and its seat --
            # and the first pass called it blind because the annular
            # shoulder stood at one end of the bore. Seen as a whole, that
            # shoulder is wide enough to be a face the tool came in through,
            # and the bore carries on past it and out the other side.
            cylinders = sum(
                1
                for face_id in feature.faces
                if graph.node(face_id).surface_type is SurfaceType.CYLINDER
            )
            if cylinders > 1:
                self._describe_interrupted_bore(graph, feature, [feature])
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
                # Each face's span along the axis, measured from the face
                # (face_extent), not from its parameter box: on a face
                # bounded by an intersection curve that box follows the
                # curve's parametrisation, i.e. the seam (the reference
                # measured shaft_endcap 30.04 / 30.05 on one solid). The
                # parameter span stays as the fallback.
                shape = getattr(self, "_shape", None)
                if shape is not None:
                    ok, low, high = span_of_face(shape, face_id, direction, 1.0e300, -1.0e300)
                    if ok and high > low:
                        offset = (
                            origin.X() * direction.X()
                            + origin.Y() * direction.Y()
                            + origin.Z() * direction.Z()
                        )
                        spans.append((low - offset, high - offset))
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
            # The pieces are one bore that stops somewhere, and the reference
            # leaves such a bore exactly as its first pass described it: the
            # run's depth, segments and voids are only written for a bore
            # that comes out the other side.
            return

        # Going through does not stop a counterbore being one. Only the base
        # bore types are relabelled; a counterbore, countersink or threaded
        # hole keeps its own name and just gains is_through.
        if _specificity(feature.type) <= 1:
            feature.type = FeatureType.THROUGH_HOLE
            feature.parameters["hole_type"] = FeatureType.THROUGH_HOLE
        feature.parameters["is_through"] = True
        feature.parameters["depth_mm"] = round(total, 6)
        feature.parameters["max_contiguous_depth_mm"] = round(contiguous, 6)
        feature.parameters["max_void_mm"] = round(max(voids), 6) if voids else 0.0
        feature.parameters["fragment_count"] = len(merged_spans)
        # Drilling passes: one, plus one for every void too wide for the
        # drill to cross while still guided (over three diameters of the
        # first cylinder). Segments either side of a narrow void drill
        # together; a wide one means flipping the part and starting again.
        first = next(
            graph.node(face_id)
            for face_id in feature.faces
            if graph.node(face_id).surface_type is SurfaceType.CYLINDER
        )
        guided = _GUIDED_VOID_RATIO * 2.0 * first.cyl_radius
        feature.parameters["drilling_passes"] = 1 + sum(1 for gap in voids if gap > guided)
        feature.parameters.pop("terminates_in_cavity", None)
        feature.parameters["flat_bottom"] = False

    def _runs_through(
        self, graph: AttributedAdjacencyGraph, feature: FeatureInstance, direction: gp_Dir
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
        # Measured against the bore rather than against whatever the widest
        # cylinder here happens to be. On a counterbore the seat's annular
        # shoulder is small next to the seat and wide next to the bore, and
        # it is the bore the tool came down: the shoulder is a face it
        # entered through, not a floor it stopped on.
        bore = min(cylinders, key=lambda n: n.cyl_radius)
        cross = cross_section_area(bore)

        # Only a full-circle run may have a small cap promoted to an opening
        # below. A half-pipe left open at both ends is a formed hood, not a
        # bore, and opening its end annuli invents a through hole spanning
        # it. Same lateral-box test the partial-bore check uses: a real bore
        # unions to 2r across, a saddle to about r.
        full_circle = True
        lateral = Bnd_Box()
        for node in cylinders:
            if not node.bbox.IsVoid():
                lateral.Add(node.bbox)
        if not lateral.IsVoid():
            bounds = lateral.Get()
            spans = [bounds[i + 3] - bounds[i] for i in range(3)]
            components = (abs(direction.X()), abs(direction.Y()), abs(direction.Z()))
            along = 0
            if components[1] > components[along]:
                along = 1
            if components[2] > components[along]:
                along = 2
            lateral_min = min(spans[i] for i in range(3) if i != along)
            full_circle = lateral_min >= 2.0 * bore.cyl_radius * 0.9

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
                        continue
                    # A small cap. The area test fails in one direction
                    # only: a bore large for its stock cannot have an end
                    # face three times its own section, so real openings get
                    # filed as floors, never the reverse. So a small cap is
                    # asked the two questions that do separate them: does it
                    # cover the axis (a blind bottom), and does a smaller
                    # coaxial bore carry on beyond it (a counterbore step,
                    # whose shoulder the axis misses just as it misses a
                    # rim). Neither, and it is an opening.
                    if not full_circle or self._cap_covers_axis(
                        node, bore.cyl_cone_axis.Location(), direction
                    ):
                        return False  # a floor: the bore stops here
                    if self._narrower_bore_beyond(graph, node, cylinder, direction):
                        return False
                    openings.add(node.face_id)
                elif node.surface_type is SurfaceType.CONE:
                    if node.cyl_cone_axis is None:
                        continue
                    # A crescent the drill point cut beside the bore is not
                    # its drill point -- same test as the first pass, against
                    # this fragment.
                    if self._is_crescent_cone(node, cylinder):
                        continue
                    if abs(node.cyl_cone_axis.Direction().Dot(direction)) > 0.95:
                        return False  # a drill point
        return len(openings) >= 2

    @staticmethod
    def _narrower_bore_beyond(
        graph: AttributedAdjacencyGraph,
        cap: AagNode,
        cylinder: AagNode,
        direction: gp_Dir,
    ) -> bool:
        """Whether a smaller coaxial bore continues past this cap.

        Judged against this fragment's own radius: from the pilot's side of a
        counterbore the seat is the larger cylinder, and comparing against
        some other member would make the answer depend on face order.
        """
        for beyond_id in graph.neighbors_of(cap.face_id):
            if not graph.has_node(beyond_id):
                continue
            beyond = graph.node(beyond_id)
            if beyond.surface_type is not SurfaceType.CYLINDER:
                continue
            if beyond.cyl_cone_axis is None or cylinder.cyl_cone_axis is None:
                continue
            if beyond.cyl_radius >= cylinder.cyl_radius - 1.0e-6:
                continue
            if abs(beyond.cyl_cone_axis.Direction().Dot(direction)) < 0.99:
                continue
            offset = gp_Vec(
                cylinder.cyl_cone_axis.Location(), beyond.cyl_cone_axis.Location()
            )
            if offset.Crossed(gp_Vec(direction.XYZ())).Magnitude() > 0.5:
                continue
            return True
        return False

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
        if gap <= shortest:
            return True

        # ...unless the gap is spanned by a face the bore already owns. A
        # bore that runs on past its own countersink cone leaves a crescent
        # of wall beyond it; measured against that sliver's own extent the
        # cone reads as a gap between two bores. A coaxial face of either
        # feature covering the gap says plainly that this is one drilling.
        gap_lo = min(span_a[1], span_b[1])
        gap_hi = max(span_a[0], span_b[0])
        origin = axis_a.Location()
        for feature in (a, b):
            for face_id in feature.faces:
                if not graph.has_node(face_id):
                    continue
                node = graph.node(face_id)
                if node.surface_type not in (SurfaceType.CYLINDER, SurfaceType.CONE):
                    continue
                if node.cyl_cone_axis is None or node.bbox.IsVoid():
                    continue
                if abs(node.cyl_cone_axis.Direction().Dot(direction)) < 0.98:
                    continue
                offset = gp_Vec(origin, node.cyl_cone_axis.Location())
                if gp_Vec(direction.XYZ()).Crossed(offset).Magnitude() > 0.5:
                    continue
                xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
                along = [
                    x * direction.X() + y * direction.Y() + z * direction.Z()
                    for x in (xmin, xmax)
                    for y in (ymin, ymax)
                    for z in (zmin, zmax)
                ]
                if min(along) <= gap_lo + 0.1 and max(along) >= gap_hi - 0.1:
                    return True
        return False


def _apply_pmi_thread(feature: FeatureInstance, callout) -> None:
    """Write a PMI thread callout onto a hole, as the reference does."""
    feature.parameters["thread_confidence"] = "definite"
    feature.parameters["thread_evidence"] = (
        "user_confirmed" if getattr(callout, "source", "") == "user_confirmed" else "pmi"
    )
    feature.parameters["thread_designation"] = callout.designation
    if callout.nominal_mm > 0.0:
        feature.parameters["thread_nominal_mm"] = callout.nominal_mm
    if callout.pitch_mm > 0.0:
        feature.parameters["thread_pitch_mm"] = callout.pitch_mm
    if getattr(callout, "thread_depth_mm", 0.0) > 0.0:
        feature.parameters["thread_depth_mm"] = callout.thread_depth_mm


class _CountersinkScan:
    """What a bore's neighbours say about its ends."""

    __slots__ = (
        "eligible",
        "is_through",
        "has_planar_floor",
        "has_widening_cone",
        "has_drill_point",
    )

    def __init__(self) -> None:
        self.eligible = False
        self.is_through = False
        self.has_planar_floor = False
        self.has_widening_cone = False
        self.has_drill_point = False


def _cone_true_radii(cone: AagNode) -> tuple[float, float]:
    """The radial extent of a cone face, smallest and largest.

    The reference measures this from the rim points it sampled, because its
    graph's cone radii are taken at the mean axial position of each rim and
    on a face a boolean has cut they describe a wedge's centre of mass. Our
    graph takes the radii at the face's own V bounds, which already are the
    extremes of the face, so they serve directly.
    """
    return min(cone.cone_r0, cone.cone_r1), max(cone.cone_r0, cone.cone_r1)


def _axial_extent(node: AagNode) -> float:
    """A cylinder's or cone's extent along its own axis (analytic V range)."""
    if node.surface_type is SurfaceType.CYLINDER and node.cyl_p0 is not None:
        return node.cyl_p0.Distance(node.cyl_p1)
    if node.surface_type is SurfaceType.CONE and node.cone_p0 is not None:
        return node.cone_p0.Distance(node.cone_p1)
    return 0.0


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


def _spans_full_circle(box, axis, radius: float) -> bool:
    """Whether walls whose union box this is go all the way round the axis."""
    if box.IsVoid():
        return False
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    spans = (xmax - xmin, ymax - ymin, zmax - zmin)
    components = (abs(axis.X()), abs(axis.Y()), abs(axis.Z()))
    axial = components.index(max(components))
    return all(spans[i] >= 1.9 * radius for i in range(3) if i != axial)


def _node_axial_span(node: AagNode, origin, direction) -> tuple[float, float]:
    """A face's bounding box projected onto an axis through `origin`."""
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    axis_vec = gp_Vec(direction.XYZ())
    along = [
        gp_Vec(origin, gp_Pnt(x, y, z)).Dot(axis_vec)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
        for z in (zmin, zmax)
    ]
    return min(along), max(along)


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
    FeatureType.BLIND_HOLE: 1,
    FeatureType.THROUGH_HOLE: 1,
    FeatureType.BREAKTHROUGH_HOLE: 1,
}


def _specificity(feature_type: str) -> int:
    # Anything else (a partial bore) ranks below the plain hole types.
    return _SPECIFICITY.get(feature_type, 0)
