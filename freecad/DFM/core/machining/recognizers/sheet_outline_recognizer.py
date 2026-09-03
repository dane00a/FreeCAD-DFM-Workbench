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

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
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
            strips.append(_Strip(node, normal, _largest_extent(node)))
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
        arcs = [
            node
            for node in graph.nodes_by_surface_type(SurfaceType.CYLINDER)
            if node.face_id not in taken
            and gauge * _STRIP_MIN_GAUGES
            <= _second_smallest_extent(node)
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
            return (candidate, None)

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
            return (None, chain)

        return (None, None)


# =============================================================================
# Strips
# =============================================================================


class _Strip:
    """A sheared edge of the blank, reduced to what the pairing needs."""

    __slots__ = ("node", "normal", "length")

    def __init__(self, node: AagNode, normal: gp_Dir, length: float):
        self.node = node
        self.normal = normal
        self.length = length  # the long dimension, running along the outline


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


def _second_smallest_extent(node: AagNode) -> float:
    """The middle of the three bounding-box dimensions.

    For a flat strip the smallest is zero -- the face has no thickness of its
    own -- so the middle one is how far it reaches across the sheet.
    """
    return sorted(node.bbox_dims())[1]


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
