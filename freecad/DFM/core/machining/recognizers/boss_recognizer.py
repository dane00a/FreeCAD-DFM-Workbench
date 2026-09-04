# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes bosses standing proud of a face.

A boss is a pocket turned inside out: a pad or a spigot left standing while the
material around it comes away. Round ones carry bearings and seals, square ones
carry mounting pads, and either way the tool has to walk around the outside of
it rather than clear the inside.

The seed is a flat face every one of whose edges is convex -- nothing rims it,
so nothing is above it. That test alone would take the top and bottom of the
raw billet as well, so three things have to follow.

It must be *enclosed*: every edge of the top has to land on a face collected as
a wall. A fragment of the outer skin has edges escaping to the big faces beyond
it, and those escapes are the tell.

Its walls must be *freestanding*: a wall sitting on the part's outer envelope is
part of the original stock silhouette, so the candidate is a corner or a step
rather than something standing up from a floor.

And it must sit on a *base* -- a much larger planar face one hop out past a
wall. That is what separates a boss from a rib top or from any small face that
happens to be surrounded by faces its own size.
"""

from __future__ import annotations

from typing import Optional, Sequence

from OCP.Bnd import Bnd_Box

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..features import FeatureInstance, FeatureType
from .base import FeatureRecognizer, cylinder_length


# A boss top is roughly square, round or polygonal. Longer and thinner than
# this and the face is a plate side or a rib top, whose walls and base would
# otherwise satisfy every downstream guard.
_TOP_MAX_ASPECT = 5.0

# The surface a boss stands on is larger than its top by at least this much,
# as well as facing the same way. Size alone is not enough: the walls of a
# tall boss outgrow its top without becoming its base.
_BASE_MIN_AREA_RATIO = 2.0

# A cone this shallow across its slant is edge treatment -- a lead-in chamfer
# on the boss rim -- rather than a wall. It still proves the boss is enclosed,
# but is kept out of the emitted face set so the chamfer survives as a feature
# of its own.
_CHAMFER_MAX_SLANT_MM = 2.0

# Below this a bounding-box dimension is numerical noise, not an extent.
_MIN_MEANINGFUL_DIM_MM = 0.01

# Faces standing this close to the part's outer envelope are on it.
_OUTER_ENVELOPE_TOLERANCE_MM = 1.0e-3

# A wall has to point along one world axis this strongly before its position
# can meaningfully be compared with a flat outer extent.
_AXIS_ALIGNED_MIN_DOT = 0.9

# Flat-walled bosses must stand parallel to their base. Cylindrical ones need
# not -- a spigot tilted off the plate it grows from is still unambiguously a
# boss, because the cylinder says so.
_BASE_PARALLEL_MIN_DOT = 0.98

# Below this a top-face bounding-box dimension is the face's flat direction.
_MIN_TOP_DIM_MM = 0.1


class BossRecognizer(FeatureRecognizer):
    """Recognizes bosses standing proud of a face."""

    prefix = "bo"

    @property
    def name(self) -> str:
        return "Boss Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        # `claimed` is deliberately ignored. A boss inside a pocket is part of
        # that pocket's geometry as well as a boss in its own right; the guards
        # below are what keep this honest, not the claim set. The internal
        # `taken` set still stops one boss being reported twice.
        envelope = _PlanarEnvelope(graph)
        found: list[FeatureInstance] = []
        taken: set[int] = set()

        for top in graph.nodes_by_surface_type(SurfaceType.PLANE):
            if top.face_id in taken:
                continue
            feature = self._recognize_one(graph, top, envelope)
            if feature is None:
                continue
            found.append(feature)
            taken.update(feature.faces)

        for index, feature in enumerate(found):
            feature.instance_id = self.instance_id(index)
        return found

    # -- seeding ------------------------------------------------------------

    def _recognize_one(
        self,
        graph: AttributedAdjacencyGraph,
        top: AagNode,
        envelope: "_PlanarEnvelope",
    ) -> Optional[FeatureInstance]:
        edges = graph.edges_of(top.face_id)
        if not edges:
            return None
        if any(edge.concavity is not Concavity.CONVEX for edge in edges):
            return None
        if _aspect_ratio(top.bbox_dims(), _MIN_TOP_DIM_MM) > _TOP_MAX_ASPECT:
            return None

        walls, edge_treatment = self._collect_walls(graph, top, edges)
        if not walls:
            return None  # a top with no wall is not standing on anything

        # Enclosure: every edge of the top must land on a collected wall.
        collected = walls | {top.face_id}
        if any(edge.other_face(top.face_id) not in collected for edge in edges):
            return None

        if envelope.touches_outer(graph, walls):
            return None
        if not self._has_base(graph, top, walls):
            return None

        return self._describe(graph, top, walls, edge_treatment)

    def _collect_walls(
        self, graph: AttributedAdjacencyGraph, top: AagNode, edges
    ) -> tuple[set[int], set[int]]:
        """The faces hanging off the top, and which of them are edge breaks.

        One hop out only. A boss's walls all meet its top; going further would
        walk down onto the base and take the part with it.
        """
        walls: set[int] = set()
        edge_treatment: set[int] = set()

        for face_id in sorted({edge.other_face(top.face_id) for edge in edges}):
            if not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            # A wall is anything not planar, or a planar face that stands
            # across the top rather than continuing in the same plane as it.
            #
            # Orientation rather than area, because area is a proxy that
            # fails on exactly the bosses the height rule cares about: the
            # walls of a pad taller than twice its width are bigger than its
            # top, so an area test called them the base, dropped them, and
            # left the top unenclosed. A pad then vanished above 2x width --
            # while the rule warns at 4x, so it could never fire on one.
            # Cylindrical bosses were unaffected, which is what hid it.
            if node.surface_type is SurfaceType.PLANE and self._is_base_plane(
                node, top
            ):
                continue
            # A shaped surface is not the side of a pad. A boss is prismatic
            # or round -- that is what makes it a boss rather than a form --
            # and a face carrying real curvature is a surface somebody set
            # out to make. Absorbed as walls, three impeller blades and the
            # hub they stand on became one boss, and the blades stopped
            # existing: no freeform finding, no undercut, no report that the
            # part cannot be reached from any cardinal direction at all.
            if node.has_freeform_curvature:
                continue
            walls.add(face_id)
            if _is_chamfer_cone(node):
                edge_treatment.add(face_id)

        return walls, edge_treatment

    @staticmethod
    def _is_base_plane(node: AagNode, top: AagNode) -> bool:
        """Whether a planar neighbour of the top is what the boss stands on.

        The surface a boss rises from faces the same way the top does, and is
        larger. A wall stands across it. Both conditions are needed: without
        the orientation test a tall boss's own wall reads as its base, and
        without the size test a shelf reads as a boss standing on nothing.
        """
        top_normal = top.outward_normal
        normal = node.outward_normal
        if top_normal is None or normal is None:
            return node.area >= top.area * _BASE_MIN_AREA_RATIO
        if abs(top_normal.Dot(normal)) < _BASE_PARALLEL_MIN_DOT:
            return False
        return node.area >= top.area * _BASE_MIN_AREA_RATIO

    @staticmethod
    def _has_base(
        graph: AttributedAdjacencyGraph, top: AagNode, walls: set[int]
    ) -> bool:
        """Whether a much larger planar face lies one hop past a wall.

        Flat-walled bosses additionally require that face to be parallel to the
        top, which is what stops a slanted face on a step reading as a base. A
        cylindrical boss is exempt: the cylinder already fixes the shape, and a
        spigot is allowed to lean off the plate it rises from.
        """
        has_cylinder_wall = any(
            graph.node(face_id).surface_type is SurfaceType.CYLINDER
            for face_id in walls
        )
        top_normal = top.outward_normal

        for wall_id in sorted(walls):
            for edge in graph.edges_of(wall_id):
                beyond_id = edge.other_face(wall_id)
                if beyond_id in walls or beyond_id == top.face_id:
                    continue
                if not graph.has_node(beyond_id):
                    continue
                beyond = graph.node(beyond_id)
                if beyond.surface_type is not SurfaceType.PLANE:
                    continue
                if beyond.area < top.area * _BASE_MIN_AREA_RATIO:
                    continue
                if not has_cylinder_wall:
                    base_normal = beyond.outward_normal
                    if top_normal is None or base_normal is None:
                        continue
                    if abs(top_normal.Dot(base_normal)) < _BASE_PARALLEL_MIN_DOT:
                        continue
                return True
        return False

    # -- measurement --------------------------------------------------------

    def _describe(
        self,
        graph: AttributedAdjacencyGraph,
        top: AagNode,
        walls: set[int],
        edge_treatment: set[int],
    ) -> Optional[FeatureInstance]:
        top_normal = top.outward_normal
        if top_normal is None:
            return None

        spigot = self._tallest_cylinder_wall(graph, walls)

        # The union bounding box projected onto the top's normal is only a
        # standout height when nothing else shares that box. For a round boss
        # the wall's own axial length is the exact answer, so prefer it.
        box = Bnd_Box()
        for face_id in sorted(walls | {top.face_id}):
            node = graph.node(face_id)
            if not node.bbox.IsVoid():
                box.Add(node.bbox)
        height = 0.0
        if not box.IsVoid():
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            height = abs(
                (xmax - xmin) * top_normal.X()
                + (ymax - ymin) * top_normal.Y()
                + (zmax - zmin) * top_normal.Z()
            )
        if spigot is not None:
            axial = cylinder_length(spigot)
            if axial > 1e-6:
                height = axial

        parameters: dict = {"height_mm": round(height, 6)}
        if spigot is not None:
            parameters["diameter_mm"] = round(spigot.cyl_radius * 2.0, 6)
            parameters["boss_type"] = "cylindrical"
            if spigot.cyl_cone_axis is not None:
                axis = spigot.cyl_cone_axis.Direction()
                parameters["axis"] = (
                    round(axis.X(), 6),
                    round(axis.Y(), 6),
                    round(axis.Z(), 6),
                )
                # A point on the axis line, not the bounding-box centre: a
                # tilted cylinder does not fill its axis-aligned box evenly,
                # so a box-derived position puts the drawn silhouette beside
                # the boss instead of tangent to it.
                location = spigot.cyl_cone_axis.Location()
                parameters["axis_location"] = (
                    round(location.X(), 6),
                    round(location.Y(), 6),
                    round(location.Z(), 6),
                )
        else:
            dims = sorted(top.bbox_dims())
            parameters["width_mm"] = round(
                dims[0] if dims[0] > _MIN_TOP_DIM_MM else dims[1], 6
            )
            parameters["length_mm"] = round(dims[2], 6)
            parameters["diameter_mm"] = 0.0
            parameters["boss_type"] = "rectangular"

        faces = [top.face_id] + sorted(walls - edge_treatment)
        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.BOSS,
            faces=faces,
            parameters=parameters,
        )

    @staticmethod
    def _tallest_cylinder_wall(
        graph: AttributedAdjacencyGraph, walls: set[int]
    ) -> Optional[AagNode]:
        """The longest cylindrical wall, which is the spigot itself.

        Longest rather than first: a boss with a relief undercut at its root
        has two coaxial cylinders, and the tall one is the boss.
        """
        best: Optional[AagNode] = None
        best_length = -1.0
        for face_id in sorted(walls):
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.CYLINDER:
                continue
            length = cylinder_length(node)
            if length > best_length:
                best = node
                best_length = length
        return best


# =============================================================================
# Helpers
# =============================================================================


class _PlanarEnvelope:
    """The part's outer extent, and whether a wall sits on it.

    Built from planar faces only. A plane's bounding box is exact to the
    kernel's tolerance, while a cylinder or torus can come out of a boolean
    with a box padded by tens of microns -- enough to inflate the envelope and
    let a wall that really is on the outside slip through the guard.
    """

    def __init__(self, graph: AttributedAdjacencyGraph) -> None:
        box = Bnd_Box()
        found = False
        for node in graph.nodes:
            if node.surface_type is not SurfaceType.PLANE:
                continue
            if not node.bbox.IsVoid():
                box.Add(node.bbox)
                found = True
        self._extent: Optional[tuple[float, ...]] = (
            box.Get() if found and not box.IsVoid() else None
        )

    def touches_outer(
        self, graph: AttributedAdjacencyGraph, walls: set[int]
    ) -> bool:
        """Whether any planar wall lies on the part's outer envelope.

        One such wall is enough to reject the candidate: a genuine boss has
        every wall separating raised material from a floor around it, so a wall
        flush with the stock silhouette means the candidate is a corner of the
        billet rather than something standing on a face.
        """
        if self._extent is None:
            return False

        for face_id in sorted(walls):
            wall = graph.node(face_id)
            if wall.surface_type is not SurfaceType.PLANE:
                continue  # a curved wall cannot coincide with a flat extent
            normal = wall.outward_normal
            if normal is None or wall.bbox.IsVoid():
                continue
            components = (abs(normal.X()), abs(normal.Y()), abs(normal.Z()))
            axis = components.index(max(components))
            if components[axis] < _AXIS_ALIGNED_MIN_DOT:
                continue

            bounds = wall.bbox.Get()
            wall_min, wall_max = bounds[axis], bounds[axis + 3]
            part_min, part_max = self._extent[axis], self._extent[axis + 3]
            # A wall square to this axis has min ~ max, but the kernel gives a
            # flat face a box of non-zero thickness, so both ends are compared.
            for limit in (part_min, part_max):
                if (
                    abs(wall_min - limit) < _OUTER_ENVELOPE_TOLERANCE_MM
                    and abs(wall_max - limit) < _OUTER_ENVELOPE_TOLERANCE_MM
                ):
                    return True
        return False


def _aspect_ratio(dims: tuple[float, float, float], floor_mm: float) -> float:
    """Longest extent over shortest real one; zero when there is no real one."""
    ordered = sorted(dims)
    shortest = next((d for d in ordered if d > floor_mm), 0.0)
    return ordered[2] / shortest if shortest > 0.0 else 0.0


def _is_chamfer_cone(node: AagNode) -> bool:
    """Whether a conical neighbour is a rim break rather than a boss wall."""
    if node.surface_type is not SurfaceType.CONE:
        return False
    ordered = sorted(node.bbox_dims())
    slant = next((d for d in ordered if d > _MIN_MEANINGFUL_DIM_MM), None)
    return slant is not None and slant <= _CHAMFER_MAX_SLANT_MM
