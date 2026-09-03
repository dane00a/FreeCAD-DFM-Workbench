# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes formed features: embosses, dimples, louvers and lances.

Formed geometry is pressed *out of* the panel rather than cut into it. The
punch draws the metal, the metal follows, and what comes back is a plateau
standing proud of the sheet with a matching hollow behind it.

That hollow is the whole discriminator. Drawn sheet keeps its thickness, so
every formed crest carries a back-side face exactly one gauge away with its
outward normal pointing back -- the same relationship the two skins of a flat
panel have. A machined boss sits on solid stock and has nothing behind it at
any distance.

Four crest shapes seed, one pass each: a flat plateau, a domed dimple, a
swept-arc hood, and anything with no analytic surface at all -- a lofted
louver scoop, a lens, a swage. The three analytic passes compare surface
parameters; the freeform pass has none to compare, so it walks the geometry
instead, marching one gauge inward from probes spread over the crest and
asking what it lands on.

What kind of formed feature it is comes from how many of its edges the punch
sheared open. None means the draw closed all the way round: an emboss. One
means the tool sheared a side and formed the hood over it: a louver. Two or
more means the metal was cut free at both ends and bridged: a lance.

Forming runs on sheet parts only. On a milled part a plateau with a hollow
behind it is a boss over a pocket, so the recognizer stands down entirely
unless the analyzer classified the part as sheet metal.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.BRep import BRep_Tool
from OCP.BRepTools import BRepTools
from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.gp import gp_Dir, gp_Pnt, gp_Pnt2d, gp_Vec
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_OUT

from ...utils.geometry import FaceIndex
from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import SHEET_TYPES, FeatureInstance, FeatureType
from ..process_classifier import PartProcessType
from .base import FeatureRecognizer


# The feature type this recognizer emits, with the forming operation carried
# as a `subtype` parameter. Spelled out here until `features.FeatureType`
# carries it: the string is the contract, because it is what rules match on
# and what a saved analysis stores.
SHEET_FORMED = FeatureType.SHEET_FORMED

EMBOSS = "emboss"
LOUVER = "louver"
LANCE = "lance"

# Every sheet type, for reading back what the earlier sheet passes claimed.
_SHEET_TYPES = SHEET_TYPES


# The two skins of drawn sheet are dead anti-parallel and exactly one gauge
# apart. The tolerance is what a modelled or imported skin pair costs, not
# what an exact offset costs.
_SKIN_ANTI_PARALLEL_MAX_DOT = -0.999
_SKIN_OFFSET_TOL_MM = 0.25

# The two skins are separated by the gauge, so a footprint test has to reach
# at least that far before it can find its own partner.
_SKIN_BBOX_SLOP_MM = 0.5

# A drawn dome and its back-side shell share a centre; a hood and its shell
# share an axis.
_CONCENTRIC_CENTRE_TOL_MM = 0.3
_COAXIAL_MIN_DOT = 0.999
_COAXIAL_LINE_TOL_MM = 0.3

# Coplanar to within this is the same physical crest, split by a Boolean seam.
_COPLANAR_MIN_DOT = 0.999
_COPLANAR_OFFSET_MM = 0.1

# Co-cylindrical to within this is the same physical hood, split by the
# cylinder's own parametric seam.
_CO_CYLINDRICAL_RADIUS_TOL_MM = 0.05
_CO_CYLINDRICAL_AXIS_TOL_MM = 0.05

# A sheared edge is one gauge across, the same window the outline pass uses.
_STRIP_MIN_GAUGES = 0.5
_STRIP_MAX_GAUGES = 1.3

# How many times longer than wide a face has to read before area over diagonal
# is allowed to answer "this is a band".
#
# The number does real separating work. A sheared lip runs the whole length of
# its hood and comes in around fifteen; the open end ring of a half-pipe rib
# wraps the hood's section instead, which is always the shorter way round, and
# comes in around eight. Corner patches and blend fragments sit at two or
# three. Without the gate, area over diagonal on a square of side s returns
# 0.707s, so every little patch a couple of millimetres across would read as a
# gauge-thin band and a louver would re-subtype itself into a lance.
_BAND_MIN_ASPECT = 10.0

# A sheared edge stands perpendicular to the metal it terminates.
_STRIP_PERPENDICULAR_MAX_DOT = 0.3

# Below this the "plateau" is a modelling seam, not something the press drew.
_MIN_HEIGHT_MM = 0.4

# The panel a flat crest was drawn out of is substantially bigger than the
# crest. Without it a plateau and its own deck swap roles.
_HOST_AREA_MULTIPLE = 2.0

# How far the crest may hang past the host's footprint. A formed feature's
# SIDE WALL also has a real skin pair, and some distant parallel panel will
# happily play host to it -- but the wall's footprint hangs outside that
# panel, and this is what catches it.
_FOOTPRINT_SLACK_MM = 0.75

# In-plane axes only: the host's normal axis carries the height, so testing
# containment along it would be testing the height twice.
_FOOTPRINT_NORMAL_AXIS_MIN = 0.7

# A hood arches OVER its panel with its axis lying IN it. When the axis points
# along the panel normal instead there is no distance from axis to plane worth
# speaking of: that face is a bore through the sheet, not a hood over it.
_AXIS_IN_PANEL_MAX_DOT = 0.1

# How far off the panel plane the seat of a dome or a hood may sit and still
# count as sitting ON it. This is also what separates a formed hood from a
# BEND, which shares its signature exactly: a bend's axis stands one inside
# radius above the panel it is tangent to, while a hood's lies in it.
_SEAT_TOL_MM = 0.5

# The back-side void belongs to the feature: the recess ceiling's small
# neighbours are its inner walls and seam fragments. Anything larger than this
# multiple of the crest is the surrounding panel and stays out.
_RECESS_NEIGHBOUR_AREA_MULTIPLE = 2.0

# -- the sampled point-offset test ------------------------------------------

# Up to thirty-six probes: enough to span a hood in both directions, cheap
# enough that the pre-stage below keeps the whole thing off the profile.
_PROBE_GRID = 6

# Fewer probes than this and the face is too small a patch to judge.
_PROBE_MIN = 8

# How much of the crest the partner -- often several faces -- has to cover.
# A real offset pair covers nearly all of it while everything else covers
# none, failing on the first probe rather than marginally, so the number only
# has to leave headroom for two skins trimmed differently at a sheared edge.
_PROBE_COVERAGE = 0.80

# On a true offset pair the two outward normals are opposite at the landing:
# an offset surface inherits its parent's normal field and the solid's
# orientation flips one of the two.
_PROBE_NORMAL_MAX_DOT = -0.80

# What a modelled or imported skin pair costs. An exact offset lands orders of
# magnitude closer than this; a hand-built or CAD-exported one will not.
_PROBE_TOL_MM = 0.05

# A freeform crest faces away from the panel it was drawn out of. Its inner
# skin faces down into the air path, and that is what stops the pair seeding
# twice -- the sampled test itself is symmetric.
_FREEFORM_FACING_MIN_DOT = 0.2


class SheetFormedRecognizer(FeatureRecognizer):
    """Recognizes features pressed out of a sheet-metal panel."""

    prefix = "sf"

    @property
    def name(self) -> str:
        return "Sheet Formed Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        gauge = self._sheet_gauge()
        if gauge <= 0.0:
            return []

        # Only what the earlier SHEET passes claimed counts. A hem's return lip
        # passes every plateau gate there is -- skin pair, raised above the
        # base, grounded -- and the bend cylinders beside it are the only thing
        # that says it is a fold rather than a hood. The machining passes'
        # claims say nothing: their reading of an emboss as a boss over a
        # pocket is exactly what this pass exists to overrule.
        folded = _sheet_claimed(prior)
        taken = set(folded)
        faces = _FaceCache(shape)

        found: list[FeatureInstance] = []
        self._flat_crests(graph, gauge, folded, taken, found, faces)
        self._domed_crests(graph, gauge, folded, taken, found, faces)
        self._swept_crests(graph, gauge, folded, taken, found, faces)
        self._freeform_crests(graph, gauge, folded, taken, found, faces)
        return found

    # -- gating ---------------------------------------------------------------

    def _sheet_gauge(self) -> float:
        """The classified sheet thickness, or zero when the part is not sheet."""
        process = getattr(self, "part_process", None)
        if process is None or process.type is not PartProcessType.SHEET_METAL:
            return 0.0
        return float(getattr(process, "sheet_thickness_mm", 0.0) or 0.0)

    # -- pass 1: flat crests --------------------------------------------------

    def _flat_crests(
        self,
        graph: AttributedAdjacencyGraph,
        gauge: float,
        folded: set[int],
        taken: set[int],
        found: list[FeatureInstance],
        faces: "_FaceCache",
    ) -> None:
        """Flat plateaus, whatever shape their walls are.

        Round, cross and freeform-outline embosses with a flat top all land
        here, which is why this pass runs first: it lets a hood claim its own
        walls and blends before the curved passes go looking for crests.
        """
        for crest in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if crest.face_id in taken or _is_a_fold(graph, crest, folded):
                continue
            skin = _gauge_skin_partner(graph, crest, gauge)
            if skin is None:
                continue
            up = crest.outward_normal
            if up is None:
                continue

            # A Boolean seam splits one physical crest -- the overlapping arms
            # of a cross emboss, say -- into several coplanar faces. Flood them
            # into one feature rather than seeding once per fragment.
            patch = _coplanar_patch(graph, crest, up)

            host, height = self._flat_host(graph, crest, up)
            if host is None:
                continue

            hood = _merge_hoods(graph, patch, host, up, gauge, faces)
            if not hood.grounded:
                continue

            found.append(
                self._emit(
                    graph, crest, skin, hood, height, taken, len(found), patch, gauge
                )
            )

    @staticmethod
    def _flat_host(
        graph: AttributedAdjacencyGraph, crest: AagNode, up: gp_Dir
    ) -> tuple[Optional[AagNode], float]:
        """The panel a flat plateau was drawn out of.

        A larger panel facing the same way, with the plateau standing OUTWARD
        of it and its footprint contained. The complementary recess on the far
        side of the sheet fails the sign, which is what makes each formed
        feature seed exactly once.
        """
        host: Optional[AagNode] = None
        height = math.inf
        for candidate in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if candidate.face_id == crest.face_id:
                continue
            normal = candidate.outward_normal
            if normal is None or normal.Dot(up) < _COPLANAR_MIN_DOT:
                continue
            if candidate.area < crest.area * _HOST_AREA_MULTIPLE:
                continue
            offset = gp_Vec(candidate.centroid, crest.centroid).Dot(gp_Vec(up))
            if offset < _MIN_HEIGHT_MM:
                continue
            if not _footprint_inside(crest, candidate, up):
                continue
            if offset < height:
                height = offset
                host = candidate
        return (host, height)

    # -- pass 2: domed crests -------------------------------------------------

    def _domed_crests(
        self,
        graph: AttributedAdjacencyGraph,
        gauge: float,
        folded: set[int],
        taken: set[int],
        found: list[FeatureInstance],
        faces: "_FaceCache",
    ) -> None:
        """Drawn dimples: a convex cap over a concentric shell one gauge in.

        Bends cannot reach this pass -- their skins are cylinders -- so the
        only thing to refuse is the shell itself, which bulges the wrong way.
        """
        for crest in graph.nodes_by_surface_type(SurfaceType.SPHERE):
            if crest.face_id in taken or crest.is_internal:
                continue
            if _is_a_fold(graph, crest, folded):
                continue
            if crest.sphere_center is None:
                continue
            skin = _gauge_skin_partner(graph, crest, gauge)
            if skin is None:
                continue

            host: Optional[AagNode] = None
            up = gp_Dir(0.0, 0.0, 1.0)
            height = math.inf
            for candidate in graph.nodes_by_surface_type(SurfaceType.PLANE):
                normal = candidate.outward_normal
                if normal is None:
                    continue
                seat = gp_Vec(candidate.centroid, crest.sphere_center).Dot(
                    gp_Vec(normal)
                )
                # The dome has to SIT on the panel: its centre lies at or just
                # under the panel plane. Floating above it means some other
                # feature's far skin, and sitting behind it means the far side
                # of the sheet, which would report the dome at an understated
                # height.
                if seat > gauge + _SEAT_TOL_MM or seat < -_SEAT_TOL_MM:
                    continue
                apex = seat + crest.sphere_radius
                if apex < _MIN_HEIGHT_MM:
                    continue
                if candidate.area < crest.area:
                    continue
                if not _footprint_inside(crest, candidate, normal):
                    continue
                if apex < height:
                    height = apex
                    host = candidate
                    up = normal
            if host is None:
                continue

            hood = _scan_hood(graph, crest, host, up, gauge, faces)
            if not hood.grounded:
                continue
            found.append(
                self._emit(graph, crest, skin, hood, height, taken, len(found), (), gauge)
            )

    # -- pass 3: swept-arc crests ---------------------------------------------

    def _swept_crests(
        self,
        graph: AttributedAdjacencyGraph,
        gauge: float,
        folded: set[int],
        taken: set[int],
        found: list[FeatureInstance],
        faces: "_FaceCache",
    ) -> None:
        """Constant-section hoods: half-pipe ribs, rolled beads, boxy louvers.

        This pass shares its signature with the bend recognizer exactly -- a
        cylinder with a coaxial partner one gauge out is what both seed on --
        and order settles most of it: bends run first and arrive here claimed.
        What separates the two geometrically is where the axis sits. A bend's
        axis stands one inside radius ABOVE the panel it is tangent to; a
        hood's lies IN the panel it arches over. The seat window below is that
        test, and it is the same window the dome pass uses.
        """
        for crest in graph.nodes_by_surface_type(SurfaceType.CYLINDER):
            if crest.face_id in taken or crest.is_internal:
                continue
            if _is_a_fold(graph, crest, folded):
                continue
            if crest.cyl_cone_axis is None:
                continue
            skin = _gauge_skin_partner(graph, crest, gauge)
            if skin is None:
                continue

            axis = crest.cyl_cone_axis
            direction = axis.Direction()
            origin = axis.Location()

            # A cylinder carries a parametric seam, and any hood whose arc
            # crosses it comes back as two faces. Absorbing the fragments is
            # not optional: the seam can put the deck contact on one fragment
            # and the sheared lip on the other, and the hood then censuses as a
            # grounded emboss with no open edges -- the exact silent
            # mis-subtype this pass exists to prevent.
            patch = _co_cylindrical_patch(graph, crest)

            host: Optional[AagNode] = None
            up = gp_Dir(0.0, 0.0, 1.0)
            height = math.inf
            for candidate in graph.nodes_by_surface_type(SurfaceType.PLANE):
                normal = candidate.outward_normal
                if normal is None:
                    continue
                if abs(normal.Dot(direction)) > _AXIS_IN_PANEL_MAX_DOT:
                    continue
                seat = gp_Vec(candidate.centroid, origin).Dot(gp_Vec(normal))
                if seat > gauge + _SEAT_TOL_MM or seat < -_SEAT_TOL_MM:
                    continue
                # The apex is the analytic top of the cylinder, clamped to how
                # far the crest actually reaches. A hood swept past the top of
                # its arc hits the analytic value exactly; one that stops short
                # would otherwise over-report its height.
                reach = max(
                    _bbox_reach(fragment, candidate.centroid, normal)
                    for fragment in patch
                )
                apex = min(seat + crest.cyl_radius, reach)
                if apex < _MIN_HEIGHT_MM:
                    continue
                if candidate.area < sum(fragment.area for fragment in patch):
                    continue
                if not _footprint_inside(crest, candidate, normal):
                    continue
                if apex < height:
                    height = apex
                    host = candidate
                    up = normal
            if host is None:
                continue

            hood = _merge_hoods(graph, patch, host, up, gauge, faces)
            if not hood.grounded:
                continue
            found.append(
                self._emit(
                    graph, crest, skin, hood, height, taken, len(found), patch, gauge
                )
            )

    # -- pass 4: freeform crests ----------------------------------------------

    def _freeform_crests(
        self,
        graph: AttributedAdjacencyGraph,
        gauge: float,
        folded: set[int],
        taken: set[int],
        found: list[FeatureInstance],
        faces: "_FaceCache",
    ) -> None:
        """Hoods with no analytic surface at all.

        A real louver punch draws a TAPERED scoop: the section's height and
        radius both grow along the run and the nose closes over, so the hood is
        a lofted spline and its inner skin is that surface's offset. Neither
        has a radius, an axis or a centre to compare, which is why the three
        passes above cannot see it.

        Nothing here is louver-shaped. The pass asks only whether this is a
        freeform face carrying a constant-gauge partner, arching out of a flat
        panel it reaches back down to -- a scoop, a lens, a swage, a drawn boss
        with a shaped top all answer yes. The subtype still comes from the
        count of sheared strips.
        """
        if not faces.usable:
            return
        for crest in graph.nodes:
            if not crest.surface_type.is_freeform:
                continue
            if crest.face_id in taken or _is_a_fold(graph, crest, folded):
                continue
            if not crest.has_freeform_curvature or crest.freeform_mean_normal is None:
                continue
            skin = _gauge_skin_partner(graph, crest, gauge, faces)
            if skin is None:
                continue

            host: Optional[AagNode] = None
            up = gp_Dir(0.0, 0.0, 1.0)
            height = math.inf
            for candidate in graph.nodes_by_surface_type(SurfaceType.PLANE):
                normal = candidate.outward_normal
                if normal is None:
                    continue
                # The hood has to FACE OUT of this panel. The sampled skin test
                # is symmetric -- march in from either skin and you land on the
                # other -- and this is what stops the inner skin seeding a
                # second feature.
                if crest.freeform_mean_normal.Dot(normal) < _FREEFORM_FACING_MIN_DOT:
                    continue
                apex = _bbox_reach(crest, candidate.centroid, normal)
                if apex < _MIN_HEIGHT_MM:
                    continue
                # It has to sit ON the panel, give or take a gauge. A crest
                # floating above it is some other feature's far skin; one
                # buried below it is on the wrong side of the sheet.
                low = -_bbox_reach(crest, candidate.centroid, normal.Reversed())
                if abs(low) > gauge + _SEAT_TOL_MM:
                    continue
                if candidate.area < crest.area:
                    continue
                if not _footprint_inside(crest, candidate, normal):
                    continue
                if apex < height:
                    height = apex
                    host = candidate
                    up = normal
            if host is None:
                continue

            hood = _scan_hood(graph, crest, host, up, gauge, faces)
            if not hood.grounded:
                continue
            found.append(
                self._emit(graph, crest, skin, hood, height, taken, len(found), (), gauge)
            )

    # -- emitting -------------------------------------------------------------

    def _emit(
        self,
        graph: AttributedAdjacencyGraph,
        crest: AagNode,
        skin: AagNode,
        hood: "_HoodScan",
        height: float,
        taken: set[int],
        index: int,
        patch: Sequence[AagNode],
        gauge: float,
    ) -> FeatureInstance:
        subtype = (
            EMBOSS
            if hood.open_strips == 0
            else LOUVER
            if hood.open_strips == 1
            else LANCE
        )

        faces = [crest.face_id]
        taken.add(crest.face_id)
        # The recess behind the feature belongs to it. It is also what lets the
        # pipeline dedup the machining reading of the same void, seeded from
        # underneath as a pocket or a blind hole.
        faces.append(skin.face_id)
        taken.add(skin.face_id)
        for neighbour_id in graph.neighbors_of(skin.face_id):
            if neighbour_id in taken or not graph.has_node(neighbour_id):
                continue
            neighbour = graph.node(neighbour_id)
            if neighbour.area > crest.area * _RECESS_NEIGHBOUR_AREA_MULTIPLE:
                continue  # the surrounding panel, not part of the feature
            faces.append(neighbour_id)
            taken.add(neighbour_id)
        for member in hood.cluster:
            if member.face_id in taken:
                continue
            faces.append(member.face_id)
            taken.add(member.face_id)
        for fragment in patch:
            if fragment.face_id not in taken:
                faces.append(fragment.face_id)
                taken.add(fragment.face_id)
            fragment_skin = _gauge_skin_partner(graph, fragment, gauge)
            if fragment_skin is not None and fragment_skin.face_id not in taken:
                faces.append(fragment_skin.face_id)
                taken.add(fragment_skin.face_id)

        dims = sorted(crest.bbox_dims())
        return FeatureInstance(
            instance_id=self.instance_id(index),
            type=SHEET_FORMED,
            faces=faces,
            parameters={
                "subtype": subtype,
                "height_mm": round(height, 6),
                "width_mm": round(dims[1], 6),
                "length_mm": round(dims[2], 6),
                "open_edges": hood.open_strips,
                "position": [
                    crest.centroid.X(),
                    crest.centroid.Y(),
                    crest.centroid.Z(),
                ],
            },
        )


# =============================================================================
# The hood
# =============================================================================


class _HoodScan:
    """One crest's surroundings: its walls, its open edges, and its footing."""

    __slots__ = ("cluster", "open_strips", "grounded")

    def __init__(self) -> None:
        self.cluster: list[AagNode] = []
        self.open_strips = 0
        self.grounded = False


def _scan_hood(
    graph: AttributedAdjacencyGraph,
    crest: AagNode,
    host: AagNode,
    up: gp_Dir,
    gauge: float,
    faces: "_FaceCache",
) -> _HoodScan:
    """Read the crest's one-hop neighbourhood.

    Walls join the cluster. Gauge-thin strips standing perpendicular to the
    crest are sheared open edges, which is what a lance leaves behind.
    Grounding means some wall -- or the crest itself, as with a dome's rim --
    reaches a face coplanar with the host panel.
    """
    scan = _HoodScan()
    seen: set[int] = set()
    for neighbour_id in graph.neighbors_of(crest.face_id):
        if neighbour_id in seen or not graph.has_node(neighbour_id):
            continue
        neighbour = graph.node(neighbour_id)

        if neighbour.surface_type is SurfaceType.PLANE:
            normal = neighbour.outward_normal
            if normal is not None:
                if normal.Dot(up) > _COPLANAR_MIN_DOT and _on_the_host_plane(
                    host, neighbour, up
                ):
                    scan.grounded = True  # the crest meets the panel directly
                    continue  # a dome's rim, not a wall
                if normal.Dot(up) < -_COPLANAR_MIN_DOT:
                    continue  # the back-side skin

        seen.add(neighbour_id)
        scan.cluster.append(neighbour)

        # An open edge is a through-thickness cut face: a gauge-thin strip
        # whose own outward direction stands perpendicular to the CREST's
        # outward direction where the two meet. Measuring against the crest
        # rather than against the panel is what lets a curved hood be read at
        # all -- a formed lip's normal is tangential to the hood, so it can
        # point any which way relative to the panel while still being dead
        # perpendicular to the metal it terminates. On a flat crest the two
        # directions coincide.
        crest_out = _face_outward_at(crest, neighbour.centroid, faces)
        if crest_out is None:
            crest_out = up
        strip_out = _face_outward_at(neighbour, neighbour.centroid, faces)
        if (
            strip_out is not None
            and abs(strip_out.Dot(crest_out)) < _STRIP_PERPENDICULAR_MAX_DOT
            and _is_gauge_thin_strip(neighbour, gauge)
        ):
            scan.open_strips += 1
            continue

        # Grounding: the wall touches the host plane. Any coplanar fragment
        # will do -- fuse seams split panels.
        for wall_neighbour_id in graph.neighbors_of(neighbour_id):
            if not graph.has_node(wall_neighbour_id):
                continue
            wall_neighbour = graph.node(wall_neighbour_id)
            if wall_neighbour.surface_type is not SurfaceType.PLANE:
                continue
            normal = wall_neighbour.outward_normal
            if normal is None or normal.Dot(up) < _COPLANAR_MIN_DOT:
                continue
            if _on_the_host_plane(host, wall_neighbour, up):
                scan.grounded = True
                break

    return scan


def _merge_hoods(
    graph: AttributedAdjacencyGraph,
    patch: Sequence[AagNode],
    host: AagNode,
    up: gp_Dir,
    gauge: float,
    faces: "_FaceCache",
) -> _HoodScan:
    """One scan for a crest that arrived in fragments.

    Fragments are each other's neighbours, so a sibling would otherwise be
    counted as a wall of its own crest.
    """
    fragment_ids = {fragment.face_id for fragment in patch}
    merged = _HoodScan()
    seen: set[int] = set()
    for fragment in patch:
        scan = _scan_hood(graph, fragment, host, up, gauge, faces)
        merged.grounded = merged.grounded or scan.grounded
        merged.open_strips += scan.open_strips
        for member in scan.cluster:
            if member.face_id in fragment_ids or member.face_id in seen:
                continue
            seen.add(member.face_id)
            merged.cluster.append(member)
    return merged


def _on_the_host_plane(host: AagNode, node: AagNode, up: gp_Dir) -> bool:
    return abs(gp_Vec(host.centroid, node.centroid).Dot(gp_Vec(up))) < _COPLANAR_OFFSET_MM


# =============================================================================
# The skin pair
# =============================================================================


def _gauge_skin_partner(
    graph: AttributedAdjacencyGraph,
    node: AagNode,
    gauge: float,
    faces: Optional["_FaceCache"] = None,
) -> Optional[AagNode]:
    """The face one gauge behind this one, or nothing.

    Each surface asks the question in its own terms, because one gauge behind
    means something different for each. A plane steps along its normal to an
    anti-parallel partner. A sphere is concentric with its shell, a cylinder
    coaxial with its own. Anything freeform has no parameters to compare and
    falls through to the sampled test.

    The process classifier carries a boolean form of the same question for its
    solid-stock veto; this returns the partner, because the feature owns it.
    """
    if node.surface_type is SurfaceType.PLANE:
        normal = node.outward_normal
        if normal is None:
            return None
        for other in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if other.face_id == node.face_id:
                continue
            other_normal = other.outward_normal
            if other_normal is None:
                continue
            if other_normal.Dot(normal) > _SKIN_ANTI_PARALLEL_MAX_DOT:
                continue
            offset = abs(gp_Vec(node.centroid, other.centroid).Dot(gp_Vec(normal)))
            if abs(offset - gauge) > _SKIN_OFFSET_TOL_MM:
                continue
            if not _bboxes_touch(node, other, gauge + _SKIN_BBOX_SLOP_MM):
                continue
            return other
        return None

    if node.surface_type is SurfaceType.SPHERE:
        if node.sphere_center is None:
            return None
        for other in graph.nodes_by_surface_type(SurfaceType.SPHERE):
            if other.face_id == node.face_id or other.sphere_center is None:
                continue
            if (
                node.sphere_center.Distance(other.sphere_center)
                > _CONCENTRIC_CENTRE_TOL_MM
            ):
                continue
            gap = abs(abs(node.sphere_radius - other.sphere_radius) - gauge)
            if gap > _SKIN_OFFSET_TOL_MM:
                continue
            if not _bboxes_touch(node, other, gauge + _SKIN_BBOX_SLOP_MM):
                continue
            return other
        return None

    if node.surface_type is SurfaceType.CYLINDER:
        axis = node.cyl_cone_axis
        if axis is None:
            return None
        for other in graph.nodes_by_surface_type(SurfaceType.CYLINDER):
            if other.face_id == node.face_id or other.cyl_cone_axis is None:
                continue
            if not _axes_coincide(axis, other.cyl_cone_axis):
                continue
            gap = abs(abs(node.cyl_radius - other.cyl_radius) - gauge)
            if gap > _SKIN_OFFSET_TOL_MM:
                continue
            if not _bboxes_touch(node, other, gauge + _SKIN_BBOX_SLOP_MM):
                continue
            return other
        return None

    if faces is None or not faces.usable or not node.surface_type.is_freeform:
        return None
    return _sampled_skin_partner(graph, node, gauge, faces)


def _axes_coincide(first, second) -> bool:
    """Whether two axes are the same line, either sense.

    The cross product of the inter-origin offset with the shared unit
    direction IS the perpendicular distance between the two lines, so one test
    covers both origins sliding along the axis.
    """
    if abs(first.Direction().Dot(second.Direction())) < _COAXIAL_MIN_DOT:
        return False
    offset = gp_Vec(first.Location(), second.Location())
    return (
        offset.Crossed(gp_Vec(first.Direction())).Magnitude() <= _COAXIAL_LINE_TOL_MM
    )


# =============================================================================
# The sampled point-offset test
# =============================================================================


class _FaceCache:
    """face_id to TopoDS_Face, built once per part.

    A node carries analytic parameters but not its face, and nothing can
    evaluate a spline without one.
    """

    __slots__ = ("_index",)

    def __init__(self, shape) -> None:
        self._index = None
        if shape is not None:
            try:
                index = FaceIndex(shape)
            except Exception:  # a shape the kernel will not map
                return
            if len(index):
                self._index = index

    @property
    def usable(self) -> bool:
        return self._index is not None

    def get(self, node: AagNode):
        if self._index is None or not 1 <= node.face_id <= len(self._index):
            return None
        return self._index.face_at(node.face_id)


class _Probe:
    """A point on a face's trimmed domain, with the outward normal there."""

    __slots__ = ("point", "normal")

    def __init__(self, point: gp_Pnt, normal: gp_Dir):
        self.point = point
        self.normal = normal


def _probe_face(face, reversed_face: bool, grid: int) -> list[_Probe]:
    """Points spread over a face's TRIMMED domain.

    A UV grid is classified and points outside the trim are dropped. Without
    that the test would happily march onto the untrimmed EXTENSION of a
    surface, which is precisely how a sampled-offset check false-positives.
    """
    probes: list[_Probe] = []
    if face is None:
        return probes
    surface = BRep_Tool.Surface_s(face)
    if surface is None:
        return probes
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    if not (u1 > u0) or not (v1 > v0):
        return probes

    classifier = BRepTopAdaptor_FClass2d(face, Precision.Confusion_s())
    for i in range(grid):
        for j in range(grid):
            u = u0 + (u1 - u0) * (i + 0.5) / grid
            v = v0 + (v1 - v0) * (j + 0.5) / grid
            if classifier.Perform(gp_Pnt2d(u, v)) == TopAbs_OUT:
                continue
            point = gp_Pnt()
            du = gp_Vec()
            dv = gp_Vec()
            surface.D1(u, v, point, du, dv)
            cross = du.Crossed(dv)
            if cross.Magnitude() < 1e-9:
                continue  # a singular pole
            normal = gp_Dir(cross.XYZ())
            if reversed_face:
                normal.Reverse()
            probes.append(_Probe(point, normal))
    return probes


class _OffsetTarget:
    """One candidate partner, set up once rather than per probe."""

    __slots__ = ("_surface", "_reversed", "_classifier", "_projector")

    def __init__(self, face, reversed_face: bool):
        self._surface = BRep_Tool.Surface_s(face)
        self._reversed = reversed_face
        self._classifier = None
        self._projector = None
        if self._surface is None:
            return
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        self._classifier = BRepTopAdaptor_FClass2d(face, Precision.Confusion_s())
        self._projector = GeomAPI_ProjectPointOnSurf()
        self._projector.Init(self._surface, u0, u1, v0, v1, Precision.Confusion_s())

    @property
    def usable(self) -> bool:
        return self._projector is not None

    def lands(self, probe: _Probe, gauge: float) -> bool:
        """Whether marching one gauge inward from the probe lands on this face."""
        target = probe.point.Translated(gp_Vec(probe.normal) * (-gauge))
        self._projector.Perform(target)
        if not self._projector.IsDone() or self._projector.NbPoints() == 0:
            return False
        if self._projector.LowerDistance() > _PROBE_TOL_MM:
            return False
        u, v = self._projector.LowerDistanceParameters()
        # The landing has to be inside the face's TRIM, not merely on the
        # surface it happens to lie on. Projection alone would answer with a
        # point on the unbounded extension, and a small patch of some large
        # curved wall could then stand in for a whole hood.
        if self._classifier.Perform(gp_Pnt2d(u, v)) == TopAbs_OUT:
            return False
        point = gp_Pnt()
        du = gp_Vec()
        dv = gp_Vec()
        self._surface.D1(u, v, point, du, dv)
        cross = du.Crossed(dv)
        if cross.Magnitude() < 1e-9:
            return False
        normal = gp_Dir(cross.XYZ())
        if self._reversed:
            normal.Reverse()
        return normal.Dot(probe.normal) <= _PROBE_NORMAL_MAX_DOT


def _sampled_skin_partner(
    graph: AttributedAdjacencyGraph,
    crest: AagNode,
    gauge: float,
    faces: _FaceCache,
) -> Optional[AagNode]:
    """The freeform skin partner of a freeform face, found by walking it.

    Four things together stop this matching a face that merely happens to run
    parallel. The march is DIRECTIONAL, so being within one gauge in some
    other direction never scores. The landing has to be inside the candidate's
    TRIM. The normals have to be ANTI-PARALLEL where it lands. And nearly the
    whole crest has to be covered, by probes spread over its entire domain --
    a face overlapping only part of the crest fails on the probes outside the
    overlap, and that is the case no bounding box or centroid test can tell
    apart from a real skin.

    The partner is often SEVERAL faces, so coverage is measured over their
    union and the one returned is whichever took the most probes. That is the
    normal case rather than a concession: an inner skin routinely arrives
    split along the curve where it crosses the deck's own plane, and either
    half on its own covers only part of the hood.
    """
    crest_face = faces.get(crest)
    if crest_face is None:
        return None
    probes = _probe_face(crest_face, crest.is_reversed, _PROBE_GRID)
    count = len(probes)
    if count < _PROBE_MIN:
        return None

    diagonal = _bbox_diagonal(crest)
    # A fragment covering as little as a quarter of the crest still hits one
    # of these, while a candidate that hits none of the four is not worth
    # thirty-six projections.
    pre_stage = (0, count // 3, (2 * count) // 3, count - 1)

    covered = [False] * count
    best: Optional[AagNode] = None
    best_hits = 0
    covered_count = 0

    for candidate in graph.nodes:
        if candidate.face_id == crest.face_id:
            continue
        if not candidate.surface_type.is_freeform:
            continue
        # Cheap locality gates. These are a cost control, not the correctness
        # gate -- the coverage test is. Deliberately no area ratio: a skin that
        # arrives in fragments has no predictable area relative to the crest.
        if not _bboxes_touch(crest, candidate, gauge + _SKIN_BBOX_SLOP_MM):
            continue
        if crest.centroid.Distance(candidate.centroid) > 4.0 * gauge + 0.5 * diagonal:
            continue
        candidate_face = faces.get(candidate)
        if candidate_face is None:
            continue

        target = _OffsetTarget(candidate_face, candidate.is_reversed)
        if not target.usable:
            continue
        if not any(target.lands(probes[i], gauge) for i in pre_stage):
            continue

        hits = 0
        for i in range(count):
            if not target.lands(probes[i], gauge):
                continue
            hits += 1
            if not covered[i]:
                covered[i] = True
                covered_count += 1
        if hits > best_hits:
            best_hits = hits
            best = candidate

    needed = math.ceil(_PROBE_COVERAGE * count)
    return best if covered_count >= needed else None


# =============================================================================
# Geometry helpers
# =============================================================================


def _sheet_claimed(prior: Optional[Sequence[FeatureInstance]]) -> set[int]:
    """Faces the earlier sheet passes already spoke for."""
    return {
        face_id
        for feature in prior or ()
        if feature.type in _SHEET_TYPES
        for face_id in feature.faces
    }


def _is_a_fold(
    graph: AttributedAdjacencyGraph, crest: AagNode, folded: set[int]
) -> bool:
    """Whether this plateau is really a bent lip rather than a formed hood.

    A hem's return lip passes every plateau gate there is. What identifies it
    as a fold is the bend cylinders sitting right beside it, already claimed
    by the bend pass.
    """
    if not folded:
        return False
    return any(
        neighbour_id in folded for neighbour_id in graph.neighbors_of(crest.face_id)
    )


def _coplanar_patch(
    graph: AttributedAdjacencyGraph, seed: AagNode, up: gp_Dir
) -> list[AagNode]:
    """The seed and every coplanar face reachable from it."""
    patch = [seed]
    seen = {seed.face_id}
    index = 0
    while index < len(patch):
        current = patch[index]
        index += 1
        for neighbour_id in graph.neighbors_of(current.face_id):
            if neighbour_id in seen or not graph.has_node(neighbour_id):
                continue
            neighbour = graph.node(neighbour_id)
            if neighbour.surface_type is not SurfaceType.PLANE:
                continue
            normal = neighbour.outward_normal
            if normal is None or normal.Dot(up) < _COPLANAR_MIN_DOT:
                continue
            if not _on_the_host_plane(seed, neighbour, up):
                continue
            seen.add(neighbour_id)
            patch.append(neighbour)
    return patch


def _co_cylindrical_patch(
    graph: AttributedAdjacencyGraph, seed: AagNode
) -> list[AagNode]:
    """The seed and every face on the same cylinder reachable from it."""
    axis = seed.cyl_cone_axis
    patch = [seed]
    seen = {seed.face_id}
    index = 0
    while index < len(patch):
        current = patch[index]
        index += 1
        for neighbour_id in graph.neighbors_of(current.face_id):
            if neighbour_id in seen or not graph.has_node(neighbour_id):
                continue
            neighbour = graph.node(neighbour_id)
            if neighbour.surface_type is not SurfaceType.CYLINDER:
                continue
            if neighbour.cyl_cone_axis is None:
                continue
            if neighbour.is_internal != seed.is_internal:
                continue
            if abs(neighbour.cyl_radius - seed.cyl_radius) > _CO_CYLINDRICAL_RADIUS_TOL_MM:
                continue
            direction = axis.Direction()
            if abs(neighbour.cyl_cone_axis.Direction().Dot(direction)) < _COAXIAL_MIN_DOT:
                continue
            offset = gp_Vec(axis.Location(), neighbour.cyl_cone_axis.Location())
            if offset.Crossed(gp_Vec(direction)).Magnitude() > _CO_CYLINDRICAL_AXIS_TOL_MM:
                continue
            seen.add(neighbour_id)
            patch.append(neighbour)
    return patch


def _face_outward_at(
    node: AagNode, at: gp_Pnt, faces: Optional[_FaceCache] = None
) -> Optional[gp_Dir]:
    """Which way is out of the metal, here on this face.

    Constant for a plane, radial for a sphere or a cylinder, and the real
    surface normal at the nearest point for anything freeform. This is what
    makes the open-strip test curvature-agnostic: on a flat crest it is the
    host normal, and on a hood it rotates with the hood.
    """
    if node.surface_type is SurfaceType.PLANE:
        return node.outward_normal

    if node.surface_type is SurfaceType.SPHERE and node.sphere_center is not None:
        radial = gp_Vec(node.sphere_center, at)
        if radial.Magnitude() < 1e-7:
            return None
        direction = gp_Dir(radial.XYZ())
        if node.is_internal:
            direction.Reverse()
        return direction

    if node.surface_type is SurfaceType.CYLINDER and node.cyl_cone_axis is not None:
        axis = gp_Vec(node.cyl_cone_axis.Direction())
        radial = gp_Vec(node.cyl_cone_axis.Location(), at)
        radial = radial - axis * radial.Dot(axis)
        if radial.Magnitude() < 1e-7:
            return None
        direction = gp_Dir(radial.XYZ())
        if node.is_internal:
            direction.Reverse()
        return direction

    if faces is None:
        return None
    face = faces.get(node)
    if face is None:
        return None
    surface = BRep_Tool.Surface_s(face)
    if surface is None:
        return None
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    projector = GeomAPI_ProjectPointOnSurf()
    projector.Init(surface, u0, u1, v0, v1, Precision.Confusion_s())
    projector.Perform(at)
    if not projector.IsDone() or projector.NbPoints() == 0:
        return None
    u, v = projector.LowerDistanceParameters()
    point = gp_Pnt()
    du = gp_Vec()
    dv = gp_Vec()
    surface.D1(u, v, point, du, dv)
    cross = du.Crossed(dv)
    if cross.Magnitude() < 1e-9:
        return None
    direction = gp_Dir(cross.XYZ())
    if node.is_reversed:
        direction.Reverse()
    return direction


def _footprint_inside(crest: AagNode, host: AagNode, up: gp_Dir) -> bool:
    """Whether the crest sits inside the host panel's own footprint."""
    if crest.bbox.IsVoid() or host.bbox.IsVoid():
        return False
    crest_box = crest.bbox.Get()
    host_box = host.bbox.Get()
    components = (abs(up.X()), abs(up.Y()), abs(up.Z()))
    for axis in range(3):
        if components[axis] > _FOOTPRINT_NORMAL_AXIS_MIN:
            continue
        if crest_box[axis] < host_box[axis] - _FOOTPRINT_SLACK_MM:
            return False
        if crest_box[axis + 3] > host_box[axis + 3] + _FOOTPRINT_SLACK_MM:
            return False
    return True


def _bboxes_touch(first: AagNode, second: AagNode, slop: float) -> bool:
    """Whether the two footprints overlap once one is grown by the slop.

    The slop has to bridge the gauge-sized offset between two skins, plus a
    little, or a skin pair could never find each other.
    """
    if first.bbox.IsVoid() or second.bbox.IsVoid():
        return False
    a = first.bbox.Get()
    b = second.bbox.Get()
    for axis in range(3):
        if a[axis] - slop > b[axis + 3] or b[axis] > a[axis + 3] + slop:
            return False
    return True


def _bbox_reach(node: AagNode, origin: gp_Pnt, direction: gp_Dir) -> float:
    """How far the face's bounding box reaches along a direction.

    Used to CLAMP the analytic apex of a swept crest: an arc that stops short
    of the top of its own cylinder never reaches its radius above the axis.
    """
    if node.bbox.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    vector = gp_Vec(direction)
    return max(
        gp_Vec(origin, gp_Pnt(x, y, z)).Dot(vector)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
        for z in (zmin, zmax)
    )


def _bbox_diagonal(node: AagNode) -> float:
    dx, dy, dz = node.bbox_dims()
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _second_smallest_extent(node: AagNode) -> float:
    return sorted(node.bbox_dims())[1]


def _band_width(node: AagNode) -> float:
    """How wide the face is when read as a band: its area over its diagonal.

    For a long thin strip the diagonal approximates the strip's length, so
    area over length IS its width -- and unlike the bounding box that stays
    true when the strip is curved or lies askew to the world axes.

    The distinction is not academic. A boxy louver's sheared lip is a flat
    axis-aligned rectangle and both measures return the gauge. A tapered
    hood's lip climbs from the deck to the apex as it runs, so its box picks
    up the whole hood height and the box measure misses it -- and the hood
    then censuses as an emboss.
    """
    diagonal = _bbox_diagonal(node)
    if diagonal < 1e-9 or node.area <= 0.0:
        return math.inf
    return node.area / diagonal


def _is_gauge_thin_strip(node: AagNode, gauge: float) -> bool:
    """Whether the face is one gauge across, by either measure.

    Both have to land in the same window; taking whichever fits is what makes
    the test curvature-agnostic without moving the window itself.
    """
    low = _STRIP_MIN_GAUGES * gauge
    high = _STRIP_MAX_GAUGES * gauge
    if low <= _second_smallest_extent(node) <= high:
        return True
    width = _band_width(node)
    if width <= 0.0 or _bbox_diagonal(node) < _BAND_MIN_ASPECT * width:
        return False
    return low <= width <= high
