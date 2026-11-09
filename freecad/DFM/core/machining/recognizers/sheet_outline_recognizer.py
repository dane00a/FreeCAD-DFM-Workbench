# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes tabs and notches on a sheet part's cut outline.

Both are features of the blank, put there by the laser or the punch before
anything went near the brake.

A tab is a peninsula: two sheared strips facing *away* from each other with
metal between them, joined at the far end by a third strip. A notch is the
same shape read the other way -- two strips facing *each other* across air,
joined at the bottom of the bite. That toward-or-away sign is the whole
discriminator, and it is the same one the rib-versus-slot and channel readings
turn on, applied here to the sheared edges of the blank.

The connecting strip is what keeps honest pairs from spurious ones. A real tab
end or notch bottom spans roughly the gap between its two sides, whereas the
front edge of a plain plate also "joins" the plate's left and right sides --
at many times their separation. The span gate is what tells those apart.

Notches double as the witness for relief rules: a bend relief or a corner
relief is a notch, and its absence is what the relief checks report.

Tabs and notches exist on sheet parts only. On a milled part a pair of thin
opposed walls is a rib or a slot, so the recognizer stands down entirely
unless the analyzer classified the part as sheet metal.
"""

from __future__ import annotations

from typing import Optional, Sequence

from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..face_extent import face_by_id
from ..features import SHEET_TYPES, FeatureInstance, FeatureType
from ..process_classifier import PartProcessType
from .base import FeatureRecognizer


# The feature types this recognizer emits. Spelled out here until
# `features.FeatureType` carries them: the strings are the contract, because
# they are what rules match on and what a saved analysis stores.
TAB = FeatureType.TAB
NOTCH = FeatureType.NOTCH

# Every sheet type, for reading back what the earlier sheet passes claimed.
_SHEET_TYPES = SHEET_TYPES


# A sheared edge is one gauge across. The window is wide because the cut face
# of a laser or punched edge is never exactly the nominal thickness -- there
# is taper, and there is the roll-over the punch leaves.
_STRIP_MIN_GAUGES = 0.5
_STRIP_MAX_GAUGES = 1.3

# A face perpendicular to the strip is one of the two skins rather than
# another sheared edge. Anything leaning further than this is not a skin
# direction at all.
_SKIN_PERPENDICULAR_MAX_DOT = 0.3

# The two skins are back to back, which is what makes the strip a genuine
# through-thickness cut rather than a fragment of one skin.
_SKIN_ANTI_PARALLEL_MAX_DOT = -0.9

# The two sides of a tab or a notch look straight at each other.
_SIDES_ANTI_PARALLEL_MAX_DOT = -0.9

# Narrower than this and the "gap" is a modelling seam; wider and the shape is
# a panel edge, not a tab or a bite out of one.
_MIN_GAP_MM = 0.2
_MAX_GAP_MM = 60.0

# The connector runs across the gap, not along it.
_CONNECTOR_PERPENDICULAR_MAX_DOT = 0.3

# How far past the gap the connecting strip may run and still be the end of
# this tab rather than an unrelated edge. The slack in gauges covers the
# corner reliefs at either end of the run.
_CONNECTOR_SPAN_SLACK = 1.5
_CONNECTOR_SPAN_SLACK_GAUGES = 2.0

# A tab is at most this share of its host panel's edge, with a floor so a
# small tongue on a small panel survives. Flanges are well above the floor.
_TAB_PANEL_FRACTION = 1.0 / 3.0
_TAB_FLOOR_MM = 10.0


class SheetOutlineRecognizer(FeatureRecognizer):
    """Recognizes tabs and notches on a sheet part's free outline."""

    prefix = "so"

    @property
    def name(self) -> str:
        return "Sheet Outline Recognizer"

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

        # Only what the earlier SHEET passes claimed counts as spoken for. The
        # machining recognizers read a sheared strip as a step and a bite as a
        # slot, and overruling those readings is the point of this pass -- so
        # honouring the general claimed set would silence it on every part.
        taken = _sheet_claimed(prior)

        self._shape = shape
        strips = self._sheared_strips(graph, gauge, taken)
        chains = self._arc_chains(graph, gauge, taken)
        candidates = self._pairs(graph, strips, chains, gauge)

        found: list[FeatureInstance] = []
        used: set[int] = set()
        for gap, _, _, first, second, connector, chain, is_notch in candidates:
            side_a = strips[first]
            side_b = strips[second]
            if side_a.node.face_id in used or side_b.node.face_id in used:
                continue
            used.add(side_a.node.face_id)
            used.add(side_b.node.face_id)

            faces = [side_a.node.face_id, side_b.node.face_id]
            if connector is not None:
                faces.append(connector.node.face_id)
                position = connector.node.centroid
            else:
                faces.extend(member.face_id for member in chain.members)
                position = chain.centroid

            run = min(side_a.length, side_b.length)
            found.append(
                FeatureInstance(
                    instance_id=self.instance_id(len(found)),
                    type=NOTCH if is_notch else TAB,
                    faces=faces,
                    parameters={
                        "width_mm": round(gap, 6),
                        # Tab length, or notch depth -- the same measurement
                        # read from the two sides of the same U.
                        "length_mm": round(run, 6),
                        "aspect": round(run / gap, 6) if gap > 1e-9 else 0.0,
                        "position": [position.X(), position.Y(), position.Z()],
                    },
                )
            )

        return found

    # -- gating ---------------------------------------------------------------

    def _sheet_gauge(self) -> float:
        """The classified sheet thickness, or zero when the part is not sheet."""
        process = getattr(self, "part_process", None)
        if process is None or process.type is not PartProcessType.SHEET_METAL:
            return 0.0
        return float(getattr(process, "sheet_thickness_mm", 0.0) or 0.0)

    # -- the strips -----------------------------------------------------------

    def _sheared_strips(
        self, graph: AttributedAdjacencyGraph, gauge: float, taken: set[int]
    ) -> list["_Strip"]:
        """Every planar face that is a cut edge of the blank.

        Gauge-thin is not enough on its own. A genuine sheared edge BRIDGES
        the thickness, so among its neighbours are faces on both skins of the
        sheet -- two roughly back-to-back planes standing perpendicular to it.
        A thin face that fails that is a fragment *of* a skin, such as the
        half-disc top of a round tab end that never merged into the panel, and
        skin fragments pairing across the thickness read as phantom tabs.
        """
        # The blank's silhouette, from planar faces only (a curved face's box
        # pads after booleans). It is what tells a tab from a corner: a tab
        # projects FROM the outline, so a strip lying on the silhouette is the
        # blank's own edge rather than one of a tab's flanks.
        #
        # Exact boxes (AddOptimal), not the graph's: BRepBndLib::Add pads a
        # plane bounded by arcs by an amount set by the arcs' parametrisation,
        # i.e. by where a seam fell, against a 0.05 mm tolerance below. Both
        # sides use them so an angled strip is judged consistently.
        exact: dict[int, Bnd_Box] = {}
        shape = getattr(self, "_shape", None)
        for node in graph.nodes_by_surface_type(SurfaceType.PLANE):
            box = Bnd_Box()
            face = face_by_id(shape, node.face_id) if shape is not None else None
            if face is not None:
                BRepBndLib.AddOptimal_s(face, box, False, False)
            exact[node.face_id] = box if not box.IsVoid() else node.bbox
        part_box = Bnd_Box()
        for node in graph.nodes_by_surface_type(SurfaceType.PLANE):
            part_box.Add(exact[node.face_id])

        strips: list[_Strip] = []
        for node in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if node.face_id in taken:
                continue
            thin = _second_smallest_extent(node)
            if thin > gauge * _STRIP_MAX_GAUGES or thin < gauge * _STRIP_MIN_GAUGES:
                continue
            normal = node.outward_normal
            if normal is None:
                continue
            if not _bridges_gauge(graph, node, normal):
                continue
            strips.append(
                _Strip(
                    node,
                    normal,
                    _largest_extent(node),
                    _on_silhouette(part_box, node, normal, exact.get(node.face_id)),
                )
            )
        return strips

    def _arc_chains(
        self, graph: AttributedAdjacencyGraph, gauge: float, taken: set[int]
    ) -> list["_ArcChain"]:
        """Curved sheared bands, grouped into the physical band they belong to.

        A rounded tab end and a round notch bottom are the laser-cut norm, and
        they arrive as cylindrical outline bands rather than flats. They act as
        CONNECTORS only -- the sides of a tab stay planar; the curve is just
        what closes the U.

        A cylinder's parametric seam and its tangencies split one physical band
        into fragments, so adjacent fragments are flooded into one chain and
        connect as a unit.
        """
        # A cut band through the blank has its AXIS along the gauge and spans
        # it. The second-smallest-extent test the flat strips use measures a
        # sheared strip's thickness, which only a narrow arc passes: a
        # keyhole's circle, 7 mm across in a 1.5 mm sheet, was invisible.
        arcs = [
            node
            for node in graph.nodes_by_surface_type(SurfaceType.CYLINDER)
            if node.face_id not in taken
            and not node.bbox.IsVoid()
            and node.cyl_cone_axis is not None
            and gauge * _STRIP_MIN_GAUGES
            <= _extent_along(node, node.cyl_cone_axis.Direction())
            <= gauge * _STRIP_MAX_GAUGES
        ]
        by_id = {node.face_id: node for node in arcs}

        chains: list[_ArcChain] = []
        seen: set[int] = set()
        for seed in arcs:
            if seed.face_id in seen:
                continue
            seen.add(seed.face_id)
            members: list[AagNode] = []
            queue = [seed]
            box = Bnd_Box()
            while queue:
                current = queue.pop()
                members.append(current)
                if not current.bbox.IsVoid():
                    box.Add(current.bbox)
                for neighbour_id in graph.neighbors_of(current.face_id):
                    if neighbour_id in seen or neighbour_id not in by_id:
                        continue
                    seen.add(neighbour_id)
                    queue.append(by_id[neighbour_id])

            members.sort(key=lambda node: node.face_id)
            length = 0.0
            if not box.IsVoid():
                xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
                length = max(xmax - xmin, ymax - ymin, zmax - zmin)
            count = float(len(members))
            centroid = gp_Pnt(
                sum(m.centroid.X() for m in members) / count,
                sum(m.centroid.Y() for m in members) / count,
                sum(m.centroid.Z() for m in members) / count,
            )
            chains.append(_ArcChain(members, length, centroid))
        return chains

    # -- the pairs ------------------------------------------------------------

    def _pairs(
        self,
        graph: AttributedAdjacencyGraph,
        strips: Sequence["_Strip"],
        chains: Sequence["_ArcChain"],
        gauge: float,
    ) -> list[tuple]:
        """Every candidate tab or notch, tightest gap first.

        Pairing greedily by ascending gap is what gets a comb right: a narrow
        finger's two walls have to pair as a TAB before the wide cuts on either
        side of it claim them into NOTCH pairs. The tighter geometric
        relationship is the real one.

        Ties break on face id so the same part always yields the same reading.
        """
        candidates: list[tuple] = []
        for i in range(len(strips)):
            for j in range(i + 1, len(strips)):
                side_a = strips[i]
                side_b = strips[j]
                if side_a.normal.Dot(side_b.normal) > _SIDES_ANTI_PARALLEL_MAX_DOT:
                    continue

                across = gp_Vec(side_a.node.centroid, side_b.node.centroid)
                reach = across.Dot(gp_Vec(side_a.normal))
                gap = abs(reach)
                if gap < _MIN_GAP_MM or gap > _MAX_GAP_MM:
                    continue

                connector, chain = self._connector(
                    graph, strips, chains, i, j, gap, gauge
                )
                if connector is None and chain is None:
                    continue

                # The first side's outward normal points toward the second, so
                # the gap between them is air: a NOTCH. Pointing away means
                # metal in between: a TAB.
                is_notch = reach > 0.0
                # A TAB projects from the outline, so both its flanks are
                # interior cuts and the outline is its tip. A leftover corner
                # where an under-fold cut stops has its faces ON the
                # silhouette, and it is nobody's tab. A NOTCH is not tested
                # this way: a relief cut in from the edge legitimately has its
                # far side on the outline.
                if not is_notch and (side_a.silhouette or side_b.silhouette):
                    continue
                # A tab is a tongue, and a tongue has shoulders: a projection
                # small against the edge it comes off, with that edge running
                # on past it on both sides. A FLANGE is the edge itself, the
                # full width of its panel, and its BEND already describes it.
                # Without this, which flanges read as tabs depended on whether
                # a relief cut happened to chop their end into a strip.
                if not is_notch and gap > _tab_ceiling(graph, side_a, side_b):
                    continue
                candidates.append(
                    (
                        gap,
                        side_a.node.face_id,
                        side_b.node.face_id,
                        i,
                        j,
                        connector,
                        chain,
                        is_notch,
                    )
                )

        candidates.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
        return candidates

    @staticmethod
    def _connector(
        graph: AttributedAdjacencyGraph,
        strips: Sequence["_Strip"],
        chains: Sequence["_ArcChain"],
        first: int,
        second: int,
        gap: float,
        gauge: float,
    ) -> tuple[Optional["_Strip"], Optional["_ArcChain"]]:
        """The strip that closes the U, flat or curved.

        Two length gates, and both kinds of connector face them. It spans
        roughly the gap, and it is strictly SHORTER than either side run --
        because a tab end and a notch bottom are the short side of the U. That
        second gate is what refuses a plate's front edge, which "joins" the
        plate's two long sides at their own full length, and what keeps a
        rectangular cutout reporting once rather than once per orientation.
        """
        side_a = strips[first]
        side_b = strips[second]
        span_limit = gap * _CONNECTOR_SPAN_SLACK + _CONNECTOR_SPAN_SLACK_GAUGES * gauge
        shortest_run = min(side_a.length, side_b.length)

        def spans_the_gap(length: float) -> bool:
            return length <= span_limit and length < shortest_run

        # Every connector is collected, both kinds, because the COUNT decides
        # whether this is a tab or notch at all. A tab or notch is a bite out
        # of an edge the blank already had, so exactly one end of the U is
        # closed: where the cut reaches the edge, the strip along it is
        # interrupted and touches only one wall. A U closed at both ends is
        # an enclosed aperture through the panel, and emitting nothing leaves
        # all its walls for the cavity recognizer to claim as one feature.
        # Stopping at the first hit also let a flat end win over the arc end
        # of an obround slot, leaving the arc to be called a stray fillet.
        connectors = 0
        flat: Optional["_Strip"] = None
        curved: Optional["_ArcChain"] = None
        for index, candidate in enumerate(strips):
            if index in (first, second):
                continue
            if (
                abs(candidate.normal.Dot(side_a.normal))
                > _CONNECTOR_PERPENDICULAR_MAX_DOT
            ):
                continue
            adjacent = graph.neighbors_of(candidate.node.face_id)
            if side_a.node.face_id not in adjacent:
                continue
            if side_b.node.face_id not in adjacent:
                continue
            if not spans_the_gap(candidate.length):
                continue
            connectors += 1
            if flat is None:
                flat = candidate

        for chain in chains:
            touches_a = False
            touches_b = False
            for member in chain.members:
                adjacent = graph.neighbors_of(member.face_id)
                touches_a = touches_a or side_a.node.face_id in adjacent
                touches_b = touches_b or side_b.node.face_id in adjacent
            if not touches_a or not touches_b:
                continue
            if not spans_the_gap(chain.length):
                continue
            connectors += 1
            if curved is None:
                curved = chain

        if connectors != 1:
            return (None, None)
        return (flat, curved)


# =============================================================================
# Strips
# =============================================================================


class _Strip:
    """A sheared edge of the blank, reduced to what the pairing needs."""

    __slots__ = ("node", "normal", "length", "silhouette")

    def __init__(
        self, node: AagNode, normal: gp_Dir, length: float, silhouette: bool = False
    ):
        self.node = node
        self.normal = normal
        self.length = length  # the long dimension, running along the outline
        self.silhouette = silhouette  # lies on the blank's own outline


class _ArcChain:
    """One physical curved band, however many faces the kernel split it into."""

    __slots__ = ("members", "length", "centroid")

    def __init__(self, members: list[AagNode], length: float, centroid: gp_Pnt):
        self.members = members
        self.length = length
        self.centroid = centroid


# =============================================================================
# Geometry helpers
# =============================================================================


def _sheet_claimed(prior: Optional[Sequence[FeatureInstance]]) -> set[int]:
    """Faces the earlier sheet passes already spoke for.

    Small-radius bend and hem cylinders are gauge-thin by bounding box, so
    without this a hem's own fold would offer itself as a connector strip.
    """
    return {
        face_id
        for feature in prior or ()
        if feature.type in _SHEET_TYPES
        for face_id in feature.faces
    }


def _tab_ceiling(
    graph: AttributedAdjacencyGraph, side_a: "_Strip", side_b: "_Strip"
) -> float:
    """The widest a tab between these flanks may be: a share of its host panel.

    The host panel's edge is measured along the gap, on the skins beside
    either flank. The floor keeps a genuinely small tongue on a small panel
    and sits well under flange scale, so it can never re-admit a flange.
    """
    gap_dir = side_a.normal
    panel = 0.0
    for side in (side_a, side_b):
        for neighbour_id in graph.neighbors_of(side.node.face_id):
            if not graph.has_node(neighbour_id):
                continue
            neighbour = graph.node(neighbour_id)
            if neighbour.surface_type is not SurfaceType.PLANE:
                continue
            normal = neighbour.outward_normal
            # The panel skins stand square to the strip's own normal.
            if normal is None or abs(normal.Dot(side.normal)) > _SKIN_PERPENDICULAR_MAX_DOT:
                continue
            if neighbour.bbox.IsVoid():
                continue
            xn, yn, zn, xx, yx, zx = neighbour.bbox.Get()
            extent = (
                abs(gap_dir.X()) * (xx - xn)
                + abs(gap_dir.Y()) * (yx - yn)
                + abs(gap_dir.Z()) * (zx - zn)
            )
            panel = max(panel, extent)
    if panel <= 0.0:
        return float("inf")
    return max(panel * _TAB_PANEL_FRACTION, _TAB_FLOOR_MM)


def _box_reach(box: Bnd_Box, direction: gp_Vec) -> float:
    """The furthest a box's corners reach along ``direction``, from the origin."""
    xn, yn, zn, xx, yx, zx = box.Get()
    return max(
        x * direction.X() + y * direction.Y() + z * direction.Z()
        for x in (xn, xx)
        for y in (yn, yx)
        for z in (zn, zx)
    )


def _on_silhouette(
    part_box: Bnd_Box, node: AagNode, normal: gp_Dir, box: Optional[Bnd_Box] = None
) -> bool:
    """Whether the face reaches as far out along its normal as the blank does."""
    if part_box.IsVoid():
        return False
    direction = gp_Vec(normal)
    own = box if box is not None else node.bbox
    return abs(_box_reach(own, direction) - _box_reach(part_box, direction)) < 0.05


def _second_smallest_extent(node: AagNode) -> float:
    """The middle of the three bounding-box dimensions.

    For a flat strip the smallest is zero -- the face has no thickness of its
    own -- so the middle one is how far it reaches across the sheet.
    """
    return sorted(node.bbox_dims())[1]


def _extent_along(node: AagNode, direction: gp_Dir) -> float:
    """The face's bounding-box extent projected onto ``direction``."""
    xn, yn, zn, xx, yx, zx = node.bbox.Get()
    return (
        abs(direction.X()) * (xx - xn)
        + abs(direction.Y()) * (yx - yn)
        + abs(direction.Z()) * (zx - zn)
    )


def _largest_extent(node: AagNode) -> float:
    return max(node.bbox_dims())


def _bridges_gauge(
    graph: AttributedAdjacencyGraph, node: AagNode, normal: gp_Dir
) -> bool:
    """Whether the face has both skins of the sheet among its neighbours."""
    seen: list[gp_Dir] = []
    for neighbour_id in graph.neighbors_of(node.face_id):
        if not graph.has_node(neighbour_id):
            continue
        neighbour = graph.node(neighbour_id)
        if neighbour.surface_type is not SurfaceType.PLANE:
            continue
        other = neighbour.outward_normal
        if other is None:
            continue
        if abs(other.Dot(normal)) > _SKIN_PERPENDICULAR_MAX_DOT:
            continue  # leaning too far to be a skin of this strip
        for previous in seen:
            if previous.Dot(other) < _SKIN_ANTI_PARALLEL_MAX_DOT:
                return True  # both skins accounted for
        seen.append(other)
    return False


def is_sheet_profile_edge(
    graph: AttributedAdjacencyGraph, face_id: int, gauge: float
) -> bool:
    """A wall cut through the blank: one gauge deep, both skins beside it.

    The one definition of "a wall cut through this blank" the sheet passes
    share -- the bend recognizer's shear test, the blank purge and this
    recognizer's own strips all ask it -- so they cannot drift apart.
    """
    if gauge <= 0.0 or not graph.has_node(face_id):
        return False
    node = graph.node(face_id)
    if node.surface_type is not SurfaceType.PLANE:
        return False
    thin = _second_smallest_extent(node)
    if thin > gauge * _STRIP_MAX_GAUGES or thin < gauge * _STRIP_MIN_GAUGES:
        return False
    normal = node.outward_normal
    if normal is None:
        return False
    return _bridges_gauge(graph, node, normal)
