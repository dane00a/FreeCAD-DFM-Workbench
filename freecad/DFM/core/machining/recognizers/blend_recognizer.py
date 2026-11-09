# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes fillets and chamfers.

A blend is the transition where two faces would otherwise meet at a sharp
edge: a fillet rolls the corner over an arc, a chamfer cuts it off flat. Both
are nearly free to draw and neither is free to cut. An internal fillet sets
the smallest cutter that can reach into the corner, and a chamfer on an
awkward face buys itself another setup, so both have to be found before any
of that can be judged.

What distinguishes them is the surface and the dihedral. A fillet is a
cylinder or a torus running tangent into a face on *each* side of it -- one
tangent edge is a cylinder that merely touches its neighbour, two is a blend.
A chamfer is a narrow planar strip meeting its neighbours obliquely, square
to neither and flat with neither, and long and thin the way a strip cut in
one pass of a tool is.

The kernel rarely leaves a blend as a single face. A fillet wrapping a part
top comes back as one face per edge it crosses; a chamfer run round a pocket
arrives as strips with mitred corners between them; a chamfer taken across an
arc is split into facets fanning round it. Every one of those is one tool and
one pass to the man running the machine, so sub-faces carrying the same size
are merged before anything is emitted -- by adjacency, and by lying on the
same underlying surface where an intervening face has cut between them.

Only the blend faces themselves are claimed, never the faces they transition
to. A chamfer that absorbed the pocket walls it breaks would overlap that
pocket almost entirely, and one of the two would then be thrown away as a
duplicate of the other.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from OCP.gp import gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import (
    FeatureRecognizer,
    axes_are_coaxial,
    cone_rim_mean_radii,
    cylinder_wraps_axis,
    face_map_of,
)


# A bounding box side below this is a flat face's own zero thickness rather
# than a real extent, so it is no candidate for the strip width.
_MIN_REAL_DIM_MM = 0.01

# A blend joins two distinct faces and carries a tangent edge into each of
# them. One tangent edge is a cylinder resting against a face it happens to
# touch -- a bore wall, whose only tangent edges are its own seams, has none.
_MIN_BLEND_EDGES = 2

# Two cylinders this close in radius, on a common axis line, are one surface
# the kernel split at a seam rather than two faces blended together.
_SEAM_RADIUS_TOL_MM = 0.01
_AXIS_PARALLEL_DOT = 0.999
_AXIS_COLLINEAR_TOL_MM = 0.05

# Sub-faces of one blend agree on size to within the accuracy of the
# measurement: tighter for a fillet radius, which is read off the surface
# itself, than for a chamfer distance, which is read off the bounding box.
_FILLET_RADIUS_TOL_MM = 0.05
_CHAMFER_DISTANCE_TOL_MM = 0.1

# The oblique dihedral window a chamfer lives in. 110 to 160 degrees of
# interior angle covers chamfer angles from 20 to 70 degrees off the face;
# square corners and tangent blends fall outside it either way.
_CHAMFER_DIHEDRAL_MIN_RAD = math.radians(110.0)
_CHAMFER_DIHEDRAL_MAX_RAD = math.radians(160.0)

# A chamfer breaks an edge, it is not a face in its own right: it stays small
# beside whatever it breaks, and it is long one way and narrow the other.
# Both guards refuse squarish oblique facets, which are draft faces or
# machined reliefs rather than edge breaks.
_CHAMFER_MAX_AREA_SHARE = 0.12
_CHAMFER_MIN_ASPECT = 5.0

# Two chamfer strips lie in one plane when their normals agree and neither
# stands off the other's plane.
_COPLANAR_DOT = 0.999
_COPLANAR_OFFSET_TOL_MM = 0.05

# A conical band wider than this along its slant is a taper or a seat being
# turned, not an edge being broken.
_MAX_CONICAL_CHAMFER_SLANT_MM = 2.0
# A wider cone is still a chamfer when it grows radially by at most this much
# (an oversized lead-in); past it the cone is a taper or a funnel.
_MAX_CHAMFER_LEG_MM = 3.0

_CURVED_TYPES = (SurfaceType.CYLINDER, SurfaceType.CONE, SurfaceType.TORUS)

# A rolling-ball blend no larger than this is a fillet in whatever surface the
# CAD system wrote it as. It mirrors the default blend-band radius the freeform
# rules and the FREEFORM_SURFACE emitter share.
_BLEND_MAX_RADIUS_MM = 10.0

# A freeform band's radius is a sampled-curvature estimate; an adjacent
# tangent sphere corner within this share of its own radius states it exactly.
_SPHERE_CORNER_RADIUS_SHARE = 0.15

# A patch closing a blend's corner matches a tangent fillet seed whose radius
# is within this share of the larger of the two.
_CORNER_MATCH_SHARE = 0.15

# An estimated (non-analytic) blend attaches to an analytic fillet whose
# radius it matches within this share.
_ATTACH_RADIUS_SHARE = 0.10

# Seed families. Seeds merge only within one family: analytic fillets
# (cylinders, tori) are family 0; freeform bands and sphere corners, whose
# radius is an estimate or which only fill the gaps of a blend network, are
# family 1.
_ANALYTIC = 0
_NON_ANALYTIC = 1


@dataclass
class _Seed:
    """One blend sub-face, with the size two of them must share to merge."""

    node: AagNode
    dim: float
    blend_neighbours: list[int] = field(default_factory=list)
    family: int = _ANALYTIC


class _UnionFind:
    """Joins blend sub-faces back into the operation they came from."""

    def __init__(self, count: int) -> None:
        self._parent = list(range(count))

    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def unite(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self._parent[a] = b


class BlendRecognizer(FeatureRecognizer):
    """Recognizes fillets and chamfers."""

    prefix = "b"

    @property
    def name(self) -> str:
        return "Blend Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored. A fillet in the corner of a
        # pocket belongs to that pocket's geometry as well as being a radius
        # in its own right, and the pocket pass has already taken it. Skipping
        # claimed faces would lose exactly the internal corners the rules most
        # need to see.
        found: list[FeatureInstance] = []
        self._face_map = face_map_of(shape)
        found.extend(self._fillets(graph))
        found.extend(self._chamfers(graph))
        found.extend(self._conical_chamfers(graph))
        _label_networks(graph, found)

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- fillets ------------------------------------------------------------

    def _fillets(self, graph: AttributedAdjacencyGraph) -> list[FeatureInstance]:
        """Cylinders and tori running tangent into a face on either side."""
        seeds: list[_Seed] = []
        for node in graph.nodes_by_surface_type(SurfaceType.CYLINDER):
            seed = self._fillet_seed(graph, node, node.cyl_radius)
            if seed is not None:
                seeds.append(seed)
        for node in graph.nodes_by_surface_type(SurfaceType.TORUS):
            seed = self._fillet_seed(graph, node, node.torus_minor_r)
            if seed is not None:
                seeds.append(seed)
        self._non_analytic_fillet_seeds(graph, seeds)

        return _emit_merged(
            graph,
            seeds,
            _FILLET_RADIUS_TOL_MM,
            _coaxial_seeds,
            FeatureType.FILLET,
            "radius_mm",
        )

    def _non_analytic_fillet_seeds(
        self, graph: AttributedAdjacencyGraph, seeds: list[_Seed]
    ) -> None:
        """Fillets that are neither cylinders nor tori.

        A rolling-ball blend between two non-parallel curved faces is written
        out as a B-spline patch, with small sphere patches where it turns a
        corner. Seeding only cylinders and tori left such a band as a freeform
        finding with no feature at all, and its corner balls were read as
        spherical pockets.

        A freeform face is a blend band on the terms the freeform rules and
        the FREEFORM_SURFACE emitter already share: blend-scale radius and
        tangent on at least two sides. Revolved faces stay out -- on a turned
        part they are profile passes. A sphere is a corner blend when it is
        small and tangent on at least two sides; a real spherical pocket
        meets its host with a sharp rim and never qualifies.
        """

        def tangent_count(node: AagNode) -> int:
            return sum(
                1
                for edge in graph.edges_of(node.face_id)
                if edge.is_tangent or edge.concavity is Concavity.TANGENT
            )

        # Seeds found so far by face, analytic first, growing as these are
        # added: a corner patch may close onto another corner patch.
        seed_dim = {seed.node.face_id: seed.dim for seed in seeds}

        def add(node: AagNode, radius: float) -> None:
            seed = self._fillet_seed(graph, node, radius, _NON_ANALYTIC)
            if seed is not None:
                seeds.append(seed)
                seed_dim[node.face_id] = radius

        # A corner of a blend is a blend, at any radius. The radius cap is a
        # guess for a patch judged alone; where a blend network turns a
        # corner, the patch closing it is tangent to the fillets it joins and
        # carries their radius, which is a measurement. Two matching tangent
        # fillet neighbours, not one: one tangency is how a sculpted wall
        # meets its edge break, two is a corner being closed.
        def matching_neighbour_radii(node: AagNode, radius: float) -> list[float]:
            found: list[float] = []
            for edge in graph.edges_of(node.face_id):
                if not (edge.is_tangent or edge.concavity is Concavity.TANGENT):
                    continue
                other = seed_dim.get(edge.other_face(node.face_id))
                if other is None:
                    continue
                if abs(other - radius) <= _CORNER_MATCH_SHARE * max(other, radius):
                    found.append(other)
            return found

        def closes_a_blend(node: AagNode, radius: float) -> bool:
            return radius > 0.0 and len(matching_neighbour_radii(node, radius)) >= 2

        for node in graph.nodes_by_surface_type(SurfaceType.SPHERE):
            if node.sphere_radius <= 0.0:
                continue
            if tangent_count(node) < 2:
                continue
            if node.sphere_radius > _BLEND_MAX_RADIUS_MM and not closes_a_blend(
                node, node.sphere_radius
            ):
                continue
            add(node, node.sphere_radius)

        for node in graph.nodes:
            if not node.has_freeform_curvature:
                continue
            if node.surface_type is SurfaceType.REVOLVED:
                continue
            if tangent_count(node) < 2:
                continue
            radius = 0.0
            if node.freeform_min_concave_radius_mm > 0.0:
                radius = node.freeform_min_concave_radius_mm
            elif node.freeform_max_convex_curvature > 0.0:
                radius = 1.0 / node.freeform_max_convex_curvature
            if radius <= 0.0:
                continue
            if radius > _BLEND_MAX_RADIUS_MM and not closes_a_blend(node, radius):
                continue
            # Snap a sampled radius onto the analytic one it matched, so the
            # corner and the band it closes merge instead of reporting 24.78
            # beside 25.
            matched = matching_neighbour_radii(node, radius)
            if matched:
                radius = matched[0]
            # Sampled B-spline curvature only approximates the ball radius
            # (0.94 for an R1 blend). A tangent analytic sphere corner states
            # it exactly, and matching radii is what lets the band and its
            # corners merge into one fillet.
            for edge in graph.edges_of(node.face_id):
                other_id = edge.other_face(node.face_id)
                if not graph.has_node(other_id):
                    continue
                other = graph.node(other_id)
                if (
                    other.surface_type is SurfaceType.SPHERE
                    and (edge.is_tangent or edge.concavity is Concavity.TANGENT)
                    and abs(other.sphere_radius - radius)
                    <= _SPHERE_CORNER_RADIUS_SHARE * other.sphere_radius
                ):
                    radius = other.sphere_radius
                    break
            add(node, radius)

    def _fillet_seed(
        self,
        graph: AttributedAdjacencyGraph,
        node: AagNode,
        radius: float,
        family: int = _ANALYTIC,
    ) -> Optional[_Seed]:
        tangent = graph.tangent_edges_of(node.face_id)

        # Count only the tangent edges reaching a genuinely different surface.
        # A full-turn bore wall comes back from the kernel as two coaxial
        # halves meeting at axis-parallel seams; those seams are smooth, but
        # they are an artefact of the parametrisation and not a blend into
        # anything. Requiring two real blend edges is what keeps bores,
        # counterbores and drilled holes out of the fillet list.
        #
        # A cylindrical fillet can only come from rounding a straight edge, so
        # it runs into the faces it blends along lines. A cylinder meeting
        # its tangent neighbours along circles is the opposite relationship:
        # a turned diameter whose rims have been rounded, where the rounds
        # are the fillets (tori) and the cylinder is the face being blended.
        # Left in, a flange's OD came back as a fillet of half its diameter.
        is_cylinder = node.surface_type is SurfaceType.CYLINDER

        # A cylindrical fillet rounds a straight edge, so it spans only the
        # turn of that edge's dihedral. A cylinder that closes on itself has
        # no edge to round: it is a bore or a boss, and the things tangent to
        # it are the fillets. The straight-edge test below misses it, because
        # a body of revolution's wall meets every vertical corner blend round
        # it along a straight generatrix, and a hub wall was reported as a
        # fillet of half its diameter.
        face_map = getattr(self, "_face_map", None)
        if is_cylinder and face_map is not None and cylinder_wraps_axis(
            graph, face_map, node
        ):
            return None
        blend_edges = 0
        neighbours: list[int] = []
        blended: list[AagNode] = []
        for edge in tangent:
            other_id = edge.other_face(node.face_id)
            neighbours.append(other_id)
            if not graph.has_node(other_id):
                continue
            if _is_seam_partner(node, graph.node(other_id)):
                continue
            if is_cylinder and edge.edge_curve_type != "line":
                continue
            blend_edges += 1
            blended.append(graph.node(other_id))

        # A fillet blends two faces that meet. The end radius of an obround
        # blends two faces that face each other and never meet: it closes a
        # channel, cut by the same tool in the same pass as the walls it
        # joins, and tangency along straight lines cannot tell it from a
        # fillet. Two planes with opposed outward normals cannot be joined by
        # a real fillet. The rounded end of a tab or a rib reads the same way.
        if is_cylinder and len(blended) == 2:
            if all(b.surface_type is SurfaceType.PLANE for b in blended):
                n0, n1 = blended[0].outward_normal, blended[1].outward_normal
                if n0 is not None and n1 is not None and n0.Dot(n1) < -0.9:
                    return None

        # A torus round whose second tangency a later cut consumed is still
        # the round: a gland cut through a rim round's footprint removes the
        # flat face it was tangent to and leaves a sharp edge in its place.
        # Admitted narrowly -- exactly one tangent edge onto a plane or
        # cylinder and every other edge sharp. A round whose other edges are
        # of unknown concavity is not one this can vouch for.
        if not is_cylinder and blend_edges == 1:
            all_edges = graph.edges_of(node.face_id)
            other_edges_sharp = True
            onto_prismatic = False
            for edge in all_edges:
                other_id = edge.other_face(node.face_id)
                if edge.concavity is Concavity.TANGENT:
                    if graph.has_node(other_id) and graph.node(other_id).surface_type in (
                        SurfaceType.PLANE,
                        SurfaceType.CYLINDER,
                    ):
                        onto_prismatic = True
                elif edge.concavity not in (Concavity.CONVEX, Concavity.CONCAVE):
                    other_edges_sharp = False
            has_sharp_edge = len(all_edges) > len(tangent)
            if other_edges_sharp and onto_prismatic and has_sharp_edge:
                blend_edges = 2  # admit as a seed

        if blend_edges < _MIN_BLEND_EDGES:
            return None

        return _Seed(node=node, dim=radius, blend_neighbours=neighbours, family=family)

    # -- chamfers -----------------------------------------------------------

    def _chamfers(self, graph: AttributedAdjacencyGraph) -> list[FeatureInstance]:
        """Narrow planar strips sitting at an oblique dihedral."""
        seeds: list[_Seed] = []
        # Runs that pass every gate but the aspect one, by face: distance.
        relaxed: dict[int, float] = {}

        for node in graph.nodes_by_surface_type(SurfaceType.PLANE):
            edges = graph.edges_of(node.face_id)
            if not edges:
                continue

            oblique = 0
            largest_neighbour_area = 0.0
            neighbours: list[int] = []
            for edge in edges:
                other_id = edge.other_face(node.face_id)
                if (
                    _CHAMFER_DIHEDRAL_MIN_RAD
                    <= edge.dihedral_angle
                    <= _CHAMFER_DIHEDRAL_MAX_RAD
                ):
                    oblique += 1
                    neighbours.append(other_id)
                if graph.has_node(other_id):
                    largest_neighbour_area = max(
                        largest_neighbour_area, graph.node(other_id).area
                    )

            if oblique < 1:
                continue
            if node.area >= largest_neighbour_area * _CHAMFER_MAX_AREA_SHARE:
                continue

            distance = _shortest_real_dim(node)
            if distance <= 0.0:
                continue
            if _longest_dim(node) < distance * _CHAMFER_MIN_ASPECT:
                # Everything but the aspect gate, which rejects isolated small
                # facets. A run flanked by confirmed chamfer is not isolated:
                # a pocket chamfer's top run 23.8 mm long on a 5 mm chamfer
                # (aspect 4.76) missed by a millimetre and took both its
                # corner cones down with it. Held aside for the corner pass.
                relaxed[node.face_id] = distance
                continue

            seeds.append(_Seed(node=node, dim=distance, blend_neighbours=neighbours))

        index_of = {seed.node.face_id: i for i, seed in enumerate(seeds)}

        def matches(a: float, b: float) -> bool:
            return abs(a - b) <= 0.15 * max(a, b)

        # A corner of a chamfer is a chamfer. Breaking the edge of a rounded
        # pocket leaves cones at the corners between the planar runs, and the
        # runs alone painted the chamfer in dashes. Same rule as a fillet's
        # corner balls: the corner carries the dimension of the chamfer it
        # joins (its own bounding box measures the corner's sweep, not the
        # band), and needs two matching neighbours -- one is an unrelated cone
        # touching a chamfer in passing, two is a corner being turned. A run
        # held back only by the aspect gate counts as the chamfer continuing,
        # or corner and run each wait on the other.
        for node in graph.nodes_by_surface_type(SurfaceType.CONE):
            if node.face_id in index_of:
                continue
            matched = 0
            distance = 0.0
            neighbours = []
            for edge in graph.edges_of(node.face_id):
                other_id = edge.other_face(node.face_id)
                neighbours.append(other_id)
                if other_id in index_of:
                    cd = seeds[index_of[other_id]].dim
                elif other_id in relaxed:
                    cd = relaxed[other_id]
                else:
                    continue
                if matched > 0 and not matches(cd, distance):
                    continue  # two different chamfers meeting
                distance = cd
                matched += 1
            if matched < 2 or distance <= 0.0:
                continue
            index_of[node.face_id] = len(seeds)
            seeds.append(_Seed(node=node, dim=distance, blend_neighbours=neighbours))

        # And the runs those corners connect: a face held back only by the
        # aspect gate is admitted once a corner of the same chamfer has been.
        # The reference walks these in hash order; ascending face id here.
        for face_id in sorted(relaxed):
            if face_id in index_of or not graph.has_node(face_id):
                continue
            distance = relaxed[face_id]
            neighbours = []
            joined = False
            for edge in graph.edges_of(face_id):
                other_id = edge.other_face(face_id)
                neighbours.append(other_id)
                if other_id in index_of and matches(seeds[index_of[other_id]].dim, distance):
                    joined = True
            if not joined:
                continue
            index_of[face_id] = len(seeds)
            seeds.append(
                _Seed(node=graph.node(face_id), dim=distance, blend_neighbours=neighbours)
            )

        def same_surface(a: _Seed, b: _Seed) -> bool:
            return _chamfer_same_surface(graph, a, b)

        return _emit_merged(
            graph,
            seeds,
            _CHAMFER_DISTANCE_TOL_MM,
            same_surface,
            FeatureType.CHAMFER,
            "distance_mm",
        )

    # -- conical chamfers ---------------------------------------------------

    def _conical_chamfers(
        self, graph: AttributedAdjacencyGraph
    ) -> list[FeatureInstance]:
        """Thin conical bands bevelling a circular edge.

        The knife edge on a vacuum flange is one of these: a half-millimetre
        band at 30 degrees off the sealing face, a chamfer in every sense that
        matters even though it is a cone rather than a strip.

        Reported liberally. A cone that really belongs to a hole -- a
        countersink mouth, a drill point -- is already claimed by that hole
        and the resolver drops the duplicate on overlap. A lead-in the hole
        recognizer judged an edge break is released from the hole, so this
        pass is what names it.
        """
        found: list[FeatureInstance] = []

        for node in graph.nodes_by_surface_type(SurfaceType.CONE):
            # Bevel scale, asked two ways; a cone is a chamfer if either
            # holds. (1) Absolutely thin: the slant (shortest bbox extent)
            # at hand-deburr scale, the same size whatever it breaks.
            # (2) An oversized lead-in: bigger than a deburr but still small,
            # because a chamfer is sized by the tool that cuts it. Measured
            # as radial growth, the number on the drawing: at most 3 mm of
            # leg (past that it is a taper or funnel) and under 0.30 x bore
            # of growth, the complement of the hole recognizer's head-seat
            # test, so every cone the hole releases as a lead-in is taken
            # here and no countersink is.
            slant = _shortest_real_dim(node)
            if slant <= 0.0:
                continue
            if slant > _MAX_CONICAL_CHAMFER_SLANT_MM:
                # The reference tunes these bounds on its graph's rim-mean
                # radii, not the face's extremes; see cone_rim_mean_radii.
                r0, r1 = cone_rim_mean_radii(
                    graph, getattr(self, "_face_map", None), node
                )
                r_min, r_max = min(r0, r1), max(r0, r1)
                # r_min near 0 is a drill point, not a bevel of anything.
                if r_min < 0.5:
                    continue
                growth = r_max - r_min
                if growth > _MAX_CHAMFER_LEG_MM:
                    continue
                if growth >= 0.60 * r_min:  # 0.30 x bore diameter
                    continue
                # And it has to actually bevel. A few-degree cone is a draft
                # face or a taper that clears the growth bounds only because
                # it barely grows; chamfer stock runs 30-60 degrees off the
                # face, and the band is wide enough to take an odd one.
                semi = abs(math.degrees(node.cone_semi_angle))
                if semi < 20.0 or semi > 70.0:
                    continue

            touches_plane = False
            for edge in graph.edges_of(node.face_id):
                other_id = edge.other_face(node.face_id)
                if not graph.has_node(other_id):
                    continue
                if graph.node(other_id).surface_type is SurfaceType.PLANE:
                    touches_plane = True
                    break
            if not touches_plane:
                continue

            # The machinist's convention: measured from the face the chamfer
            # bevels, not from the axis. A 45 degree chamfer has a semi-angle
            # of 45; a knife edge 30 degrees off its sealing face has a
            # semi-angle of 60.
            semi_deg = abs(math.degrees(node.cone_semi_angle))
            parameters = {
                "chamfer_angle_deg": round(90.0 - semi_deg, 1),
                "width_mm": round(slant, 6),
                "conical": True,
            }
            # The lead-in on a bore is not an operation of its own: the tool
            # that drills the hole spots the rim on the way in, or a chamfer
            # tool follows the drill in one cycle. A chamfer on a profile
            # edge keeps its operation; nothing else cuts that edge.
            if _is_bore_lead_in(graph, node):
                parameters["bore_lead_in"] = True
            found.append(
                FeatureInstance(
                    instance_id="",
                    type=FeatureType.CHAMFER,
                    faces=[node.face_id],
                    parameters=parameters,
                )
            )

        return found


def _is_bore_lead_in(graph: AttributedAdjacencyGraph, cone: AagNode) -> bool:
    """Whether a conical chamfer is coaxial with a cylinder it touches."""
    if cone.cyl_cone_axis is None:
        return False
    direction = cone.cyl_cone_axis.Direction()
    for edge in graph.edges_of(cone.face_id):
        other_id = edge.other_face(cone.face_id)
        if not graph.has_node(other_id):
            continue
        bore = graph.node(other_id)
        if bore.surface_type is not SurfaceType.CYLINDER or bore.cyl_cone_axis is None:
            continue
        if abs(direction.Dot(bore.cyl_cone_axis.Direction())) < 0.98:
            continue
        offset = gp_Vec(cone.cyl_cone_axis.Location(), bore.cyl_cone_axis.Location())
        if gp_Vec(direction.XYZ()).Crossed(offset).Magnitude() > 0.5:
            continue
        return True
    return False


# -- networks -----------------------------------------------------------------


def _label_networks(graph: AttributedAdjacencyGraph, found: list[FeatureInstance]) -> None:
    """Stamp each blend with the tangent-connected network it belongs to.

    A blend running continuously round a pocket is one pass of one cutter,
    however many faces the kernel split it into and however many features
    the merge rightly keeps apart; costing it per feature priced one
    perimeter as eighty operations. This is a label, not a merge: every
    feature stays its own. Connectivity is geometric, so two runs of one
    radius that never meet stay two, and it does not key on size, so a run
    that changes radius mid-path is one network holding two tool paths. A
    chamfer and a fillet meeting tangentially are two cutters.
    """
    owner: dict[int, int] = {}
    for i, feature in enumerate(found):
        for face_id in feature.faces:
            owner[face_id] = i
    networks = _UnionFind(len(found))
    for i, feature in enumerate(found):
        for face_id in feature.faces:
            if not graph.has_node(face_id):
                continue
            for edge in graph.edges_of(face_id):
                if not edge.is_tangent:
                    continue
                j = owner.get(edge.other_face(face_id))
                if j is None or found[j].type != feature.type:
                    continue
                networks.unite(i, j)
    label: dict[int, int] = {}
    for i, feature in enumerate(found):
        root = networks.find(i)
        if root not in label:
            label[root] = len(label)
        feature.parameters["blend_network"] = label[root]


# -- merging ------------------------------------------------------------------


def _emit_merged(
    graph: AttributedAdjacencyGraph,
    seeds: list[_Seed],
    dim_tol: float,
    same_surface: Callable[[_Seed, _Seed], bool],
    feature_type: str,
    dim_param: str,
) -> list[FeatureInstance]:
    """One feature per connected group of same-sized blend sub-faces."""
    if not seeds:
        return []

    index_of = {seed.node.face_id: i for i, seed in enumerate(seeds)}
    groups = _UnionFind(len(seeds))

    def same_dim(a: int, b: int) -> bool:
        return (
            seeds[a].family == seeds[b].family
            and abs(seeds[a].dim - seeds[b].dim) <= dim_tol
        )

    # Adjacency first. This is what joins the mitred corners of a chamfer run
    # round a pocket perimeter to the straight strips either side of them.
    for i, seed in enumerate(seeds):
        for edge in graph.edges_of(seed.node.face_id):
            other = index_of.get(edge.other_face(seed.node.face_id))
            if other is not None and same_dim(i, other):
                groups.unite(i, other)

    # Then the same underlying surface, which catches the pieces of one
    # operation that an intervening face has separated -- a chamfer strip
    # broken in two by the corner fillet it runs into. Note what this is
    # *not*: a shared-neighbour test. Sharing a source face is far too
    # permissive, since the top and bottom chamfers of a block both touch its
    # sides and are plainly two separate chamfers.
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            if same_dim(i, j) and same_surface(seeds[i], seeds[j]):
                groups.unite(i, j)

    # Non-analytic blends attach, they do not bridge. A group of freeform
    # bands or sphere corners with a same-sized analytic neighbour joins ONE
    # such fillet -- the largest -- so a corner ball becomes part of the blend
    # it rounds. Uniting with every neighbour welded separate fillet chains
    # into one; never attaching stranded each corner ball as its own fillet.
    # A band's radius is a curvature estimate (9.76 for an R10 blend), so it
    # matches within a share of the analytic radius, not the fixed tolerance.
    comp_area: dict[int, float] = {}
    for i, seed in enumerate(seeds):
        root = groups.find(i)
        comp_area[root] = comp_area.get(root, 0.0) + seed.node.area
    attach_to: dict[int, int] = {}
    for i, seed in enumerate(seeds):
        if seed.family != _NON_ANALYTIC:
            continue
        root_i = groups.find(i)
        for edge in graph.edges_of(seed.node.face_id):
            j = index_of.get(edge.other_face(seed.node.face_id))
            if j is None or seeds[j].family != _ANALYTIC:
                continue
            tolerance = max(dim_tol, _ATTACH_RADIUS_SHARE * seeds[j].dim)
            if abs(seed.dim - seeds[j].dim) > tolerance:
                continue
            root_j = groups.find(j)
            current = attach_to.get(root_i)
            if current is None or comp_area[root_j] > comp_area[current]:
                attach_to[root_i] = root_j
    for root_i, root_j in attach_to.items():
        groups.unite(root_i, root_j)

    # The size is reported from the largest member. A small corner patch
    # misreports it, because its bounding box does not line up with the
    # direction the chamfer was offset along; a long strip is reliable. An
    # analytic seed states its radius exactly where an attached freeform band
    # only estimates it, so it is canonical over one whatever the areas.
    canonical: dict[int, int] = {}
    for i, seed in enumerate(seeds):
        root = groups.find(i)
        best = canonical.get(root)
        if (
            best is None
            or seed.family < seeds[best].family
            or (
                seed.family == seeds[best].family
                and seed.node.area > seeds[best].node.area
            )
        ):
            canonical[root] = i

    found: list[FeatureInstance] = []
    feature_of_root: dict[int, FeatureInstance] = {}
    for i, seed in enumerate(seeds):
        root = groups.find(i)
        feature = feature_of_root.get(root)
        if feature is None:
            feature = FeatureInstance(
                instance_id="",
                type=feature_type,
                parameters={dim_param: round(seeds[canonical[root]].dim, 6)},
            )
            feature_of_root[root] = feature
            found.append(feature)
        if seed.node.face_id not in feature.faces:
            feature.faces.append(seed.node.face_id)

    for feature in found:
        feature.faces.sort()
    return found


# -- shared geometry ----------------------------------------------------------


def _shortest_real_dim(node: AagNode) -> float:
    """Shortest bounding box side that is not the face's own zero thickness.

    For a chamfer this is the offset the tool was set to, and it is the
    canonical width of any blend strip.
    """
    for dim in sorted(node.bbox_dims()):
        if dim > _MIN_REAL_DIM_MM:
            return dim
    return 0.0


def _longest_dim(node: AagNode) -> float:
    return max(node.bbox_dims())


def _is_seam_partner(node: AagNode, other: AagNode) -> bool:
    """Whether two cylinders are one surface the kernel split at a seam.

    Same axis line and same radius means a bore wall arriving as two halves,
    and the smooth edge between them is a parametrisation seam rather than a
    blend into another face.
    """
    if node.surface_type is not SurfaceType.CYLINDER:
        return False
    if other.surface_type is not SurfaceType.CYLINDER:
        return False
    if abs(node.cyl_radius - other.cyl_radius) > _SEAM_RADIUS_TOL_MM:
        return False
    return _coaxial(node, other)


def _coaxial(a: AagNode, b: AagNode) -> bool:
    """Whether two cylinders share an axis line, to blend tolerance."""
    if a.cyl_cone_axis is None or b.cyl_cone_axis is None:
        return False
    return axes_are_coaxial(
        a.cyl_cone_axis,
        b.cyl_cone_axis,
        direction_dot=_AXIS_PARALLEL_DOT,
        line_distance=_AXIS_COLLINEAR_TOL_MM,
    )


def _coaxial_seeds(a: _Seed, b: _Seed) -> bool:
    """Same-surface test for fillets.

    Only cylinders carry an axis, so two tori never merge this way. That is
    no loss: a torus corner blend already touches the cylindrical fillets
    either side of it across tangent edges, and merges by adjacency instead.
    """
    if a.node.surface_type is not SurfaceType.CYLINDER:
        return False
    if b.node.surface_type is not SurfaceType.CYLINDER:
        return False
    return _coaxial(a.node, b.node)


def _chamfer_same_surface(
    graph: AttributedAdjacencyGraph, a: _Seed, b: _Seed
) -> bool:
    """Same-surface test for chamfers.

    Two strips are pieces of one chamfer when they are coplanar -- a single
    flat chamfer the kernel cut in two, whose pieces stayed in its plane --
    or when both touch the same curved face. The second case is a chamfer
    taken across an arc: the kernel returns two or more facets with different
    normals fanning round the corner, and what they have in common is the
    curved feature they wrapped around. A block chamfered top and bottom has
    no curved faces at all, so that rule never fires there and its two
    chamfers stay apart even though both touch the same sides.

    Same surface and nearby: the pieces of one split chamfer sit a face
    apart, not a part apart. Unbounded, two separate pockets whose bottom
    runs share a plane 168 mm away were one chamfer. Bounded to five times
    the chamfer's own distance, here and not in the shared merge, where it
    re-split coaxial rim rounds.
    """
    if not a.node.bbox.IsVoid() and not b.node.bbox.IsVoid():
        alo = a.node.bbox.Get()
        blo = b.node.bbox.Get()
        gap2 = 0.0
        for k in range(3):
            d = max(0.0, blo[k] - alo[k + 3], alo[k] - blo[k + 3])
            gap2 += d * d
        reach = 5.0 * max(a.dim, b.dim)
        if gap2 > reach * reach:
            return False

    normal_a = a.node.outward_normal
    normal_b = b.node.outward_normal
    if normal_a is not None and normal_b is not None:
        if abs(normal_a.Dot(normal_b)) >= _COPLANAR_DOT:
            offset = gp_Vec(a.node.centroid, b.node.centroid)
            if abs(offset.Dot(gp_Vec(normal_a))) < _COPLANAR_OFFSET_TOL_MM:
                return True

    curved = _curved_neighbours(graph, a)
    if not curved:
        return False
    return bool(curved & _curved_neighbours(graph, b))


def _curved_neighbours(graph: AttributedAdjacencyGraph, seed: _Seed) -> set[int]:
    """Every curved face a strip touches, across a blend edge or any other."""
    found: set[int] = set()
    candidates = list(seed.blend_neighbours)
    candidates.extend(
        edge.other_face(seed.node.face_id)
        for edge in graph.edges_of(seed.node.face_id)
    )
    for face_id in candidates:
        if graph.has_node(face_id) and graph.node(face_id).surface_type in _CURVED_TYPES:
            found.add(face_id)
    return found
