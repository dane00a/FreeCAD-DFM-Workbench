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

from OCP.Bnd import Bnd_Box, Bnd_OBB
from OCP.BRepBndLib import BRepBndLib
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp
from freecad.DFM.core.utils.occt import TopoDS
from freecad.DFM.core.utils.occt import TopTools_IndexedMapOfShape
from OCP.gp import gp_Vec

from ..aag import AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from ..face_extent import extent_of_face, span_of_face
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

# The least a pad may stand above what it rises from (bbox height).
_MIN_BOSS_HEIGHT_MM = 0.5


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
        self._shape = shape
        # Face handles by id, for measuring a top face's outline in its own
        # frame.
        self._face_map = None
        if shape is not None:
            self._face_map = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(shape, TopAbs_FACE, self._face_map)

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

        # Does anything actually stand proud? Enclosure is satisfied by the
        # floor of a cavity as well as by the top of a boss: a floor annulus
        # round a bore takes the bore as its "wall" and walks down it to the
        # far side of the part for a "base". What separates them is which
        # way each wall faces: a boss's walls wrap its material, a bore's
        # wall faces it. At least one wall must enclose, and the interior
        # ones (bores through the top) are kept out of the base search, the
        # height box and the cylinder parameters.
        interior: set[int] = set()
        has_enclosing_wall = False
        for wall_id in sorted(walls):
            orientation = _wall_orientation(graph.node(wall_id), top.centroid)
            if orientation < 0:
                interior.add(wall_id)
            elif orientation > 0 and wall_id not in edge_treatment:
                has_enclosing_wall = True
        if not has_enclosing_wall:
            return None

        if self._flush_with_rim(graph, top, walls):
            return None

        if envelope.touches_outer(graph, walls):
            return None
        if not self._has_base(graph, top, walls, interior):
            return None

        return self._describe(graph, top, walls, edge_treatment, interior)

    def _collect_walls(
        self, graph: AttributedAdjacencyGraph, top: AagNode, edges
    ) -> tuple[set[int], set[int]]:
        """The faces hanging off the top, and which of them are edge breaks.

        One hop out only. A boss's walls all meet its top; going further would
        walk down onto the base and take the part with it.
        """
        walls: set[int] = set()
        edge_treatment: set[int] = set()
        frontier: list[int] = []

        def wall_like(node: AagNode) -> bool:
            # The reference's test: anything not planar, or a planar face
            # smaller than twice the top. Area, not orientation -- which
            # means a square pad more than half as tall as it is wide has
            # walls larger than that, is left unenclosed, and is not a boss.
            # A standoff 6 x 6 x 13 inside an enclosure is not one in the
            # reference's output, and so it is not one here.
            if node.surface_type is SurfaceType.PLANE:
                return node.area < top.area * _BASE_MIN_AREA_RATIO
            return not node.has_freeform_curvature

        for face_id in sorted({edge.other_face(top.face_id) for edge in edges}):
            if not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            # A wall is anything not planar, or a planar face smaller than
            # twice the top (see wall_like).
            if node.surface_type is SurfaceType.PLANE and not wall_like(node):
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
            frontier.append(face_id)
            if _is_chamfer_cone(node):
                edge_treatment.add(face_id)

        # The second ring. A wall separated from the top by an edge break
        # (a chamfered pad edge) is two hops out, and the reference's
        # "BFS" never popped its queue, so it went unseen. One ring is all
        # that is justified: between a top and its wall there is at most one
        # edge treatment, and walking further across convex edges reaches the
        # part's whole envelope. Only a chamfer is crossed, measured as a
        # band 2 mm or less across whether it is a cone (a broken circular
        # edge) or a plane (a broken straight edge). Blends stay out: walking
        # the toroidal fillets lining a bore turned an annular ledge into a
        # phantom boss.
        for cur in frontier:
            if not graph.has_node(cur):
                continue
            cur_node = graph.node(cur)
            crossable = _is_chamfer_cone(cur_node) or (
                cur_node.surface_type is SurfaceType.PLANE and _is_narrow_band(cur_node)
            )
            if not crossable:
                continue
            for edge in graph.edges_of(cur):
                if edge.concavity not in (Concavity.CONVEX, Concavity.TANGENT):
                    continue
                nb_id = edge.other_face(cur)
                if nb_id == top.face_id or not graph.has_node(nb_id):
                    continue
                if nb_id in walls:
                    continue
                nb = graph.node(nb_id)
                # Collected, never re-queued: one ring.
                if not wall_like(nb):
                    continue
                walls.add(nb_id)
                if _is_chamfer_cone(nb):
                    edge_treatment.add(nb_id)

        return walls, edge_treatment

    @staticmethod
    def _flush_with_rim(
        graph: AttributedAdjacencyGraph, top: AagNode, walls: set[int]
    ) -> bool:
        """Whether the "boss" is only the land left between two cuts.

        A land between a bore and a gland passes every other guard -- its
        wall faces outward, it is enclosed, it stands above the gland floor
        -- but its top is the same plane as the face outside the gland.
        Nobody machined it.

        Walk top -> wall -> the floor it rises from (same facing, below the
        top) -> a wall on that floor facing back at the boss -> a plane level
        with the top. The floor is judged as a whole first: it must have no
        convex edge (a plate things stand on turns down over the part's
        edge; a cut's floor does not), and every non-boss wall round it must
        face back at the boss, looking one hop through blends. A neighbouring
        raised feature's far walls face away, so two features of one height
        on one plate never reject each other. Failing either keeps the boss.
        """
        top_n = top.outward_normal
        if top_n is None:
            return False
        top_v = gp_Vec(top_n.XYZ())
        tol = 1.0e-3
        boss_faces = walls | {top.face_id}

        def offset_from_top(node: AagNode) -> float:
            return gp_Vec(top.centroid, node.centroid).Dot(top_v)

        def same_facing_plane(node: AagNode) -> bool:
            if node.surface_type is not SurfaceType.PLANE:
                return False
            out = node.outward_normal
            return out is not None and out.Dot(top_n) > 0.999

        for wall_id in sorted(walls):
            for base_id in graph.neighbors_of(wall_id):
                if base_id in boss_faces or not graph.has_node(base_id):
                    continue
                base = graph.node(base_id)
                if not same_facing_plane(base):
                    continue
                if offset_from_top(base) > -tol:
                    continue  # not below the top

                enclosed_floor = not any(
                    edge.concavity is Concavity.CONVEX for edge in graph.edges_of(base_id)
                )
                if enclosed_floor:
                    for around_id in graph.neighbors_of(base_id):
                        if around_id in boss_faces or not graph.has_node(around_id):
                            continue
                        orientation = _wall_orientation(graph.node(around_id), top.centroid)
                        if orientation > 0:
                            enclosed_floor = False
                            break
                        if orientation == 0:
                            for beyond_id in graph.neighbors_of(around_id):
                                if beyond_id == base_id or beyond_id in boss_faces:
                                    continue
                                if not graph.has_node(beyond_id):
                                    continue
                                beyond = graph.node(beyond_id)
                                if _wall_orientation(
                                    beyond, top.centroid
                                ) > 0 and not same_facing_plane(beyond):
                                    enclosed_floor = False
                                    break
                            if not enclosed_floor:
                                break
                if not enclosed_floor:
                    continue

                for cut_id in graph.neighbors_of(base_id):
                    if cut_id in boss_faces or not graph.has_node(cut_id):
                        continue
                    cut = graph.node(cut_id)
                    if _wall_orientation(cut, top.centroid) >= 0:
                        continue
                    for rim_id in graph.neighbors_of(cut_id):
                        if rim_id in boss_faces or rim_id == base_id:
                            continue
                        if not graph.has_node(rim_id):
                            continue
                        rim = graph.node(rim_id)
                        if not same_facing_plane(rim):
                            continue
                        if abs(offset_from_top(rim)) > tol:
                            continue
                        return True
        return False

    @staticmethod
    def _has_base(
        graph: AttributedAdjacencyGraph,
        top: AagNode,
        walls: set[int],
        interior: frozenset[int] | set[int] = frozenset(),
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
            if face_id not in interior
        )
        top_normal = top.outward_normal

        for wall_id in sorted(walls):
            # Never reach the base through a bore: that is how a cavity
            # floor found the opposite face of the part down its own bore.
            if wall_id in interior:
                continue
            # Candidate bases: the wall's neighbours, plus the far neighbours
            # of a smoothly joined blend at its foot. A pad with a fillet all
            # round its foot meets its base only through the fillet, and a
            # one-hop search stopped at the torus and called it baseless.
            candidates: list[int] = []
            for edge in graph.edges_of(wall_id):
                beyond_id = edge.other_face(wall_id)
                if beyond_id in walls or beyond_id == top.face_id:
                    continue
                candidates.append(beyond_id)
                if not graph.has_node(beyond_id):
                    continue
                blend = graph.node(beyond_id)
                smooth = edge.concavity is Concavity.TANGENT or edge.is_tangent
                if smooth and blend.surface_type in (SurfaceType.TORUS, SurfaceType.CYLINDER):
                    for far_edge in graph.edges_of(beyond_id):
                        far_id = far_edge.other_face(beyond_id)
                        if far_id == wall_id or far_id in walls or far_id == top.face_id:
                            continue
                        candidates.append(far_id)
            for beyond_id in candidates:
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
                # The top must be smaller than the base it stands on. A top
                # that outreaches its base is the part's body over a
                # narrower flange, not something standing on anything.
                if max(top.bbox_dims()) >= max(beyond.bbox_dims()):
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
        interior: frozenset[int] | set[int] = frozenset(),
    ) -> Optional[FeatureInstance]:
        top_normal = top.outward_normal
        if top_normal is None:
            return None

        # A bore through the boss is not its outside diameter, and a tap
        # hole drilled through a pad reaches past the pad's far face, so
        # interior walls stay out of the measurement.
        solid_walls = walls - set(interior)
        spigot = self._tallest_cylinder_wall(graph, solid_walls)

        # The union bounding box projected onto the top's normal is only a
        # standout height when nothing else shares that box. For a round boss
        # the wall's own axial length is the exact answer, so prefer it.
        box = Bnd_Box()
        for face_id in sorted(solid_walls | {top.face_id}):
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
        # And measured properly when the faces will give it. The line above
        # projects an axis-aligned box diagonal onto the pad's own normal,
        # which answers about the box's corners and swings with the pad's
        # orientation: the reference measured 18.35 against 18.53 on one
        # solid with only the parametric seam moved. The box answer stays as
        # the fallback for faces with no sampleable boundary.
        if getattr(self, "_shape", None) is not None:
            lo, hi = 1.0e300, -1.0e300
            any_span = False
            for face_id in sorted(solid_walls | {top.face_id}):
                ok, lo, hi = span_of_face(self._shape, face_id, top_normal, lo, hi)
                any_span = any_span or ok
            if any_span and hi > lo:
                height = hi - lo
        # A pad has to stand somewhere. Below half a millimetre what is
        # raised is the land left between two cuts, and the cuts describe
        # it: the ridges between adjacent V-grooves read as an 18.5 x 4 mm
        # "boss" 0.107 mm tall, a height that is exactly the groove depth.
        # The smallest real boss in the corpus (2.54 mm) is far above this.
        # The resolver used to hide that ridge by accident; once a pad in a
        # cavity was allowed to coexist with it, the guard became necessary.
        if height < _MIN_BOSS_HEIGHT_MM:
            return None
        # Is the cylinder the boss's outline, or only a corner? Any
        # cylindrical wall used to make the whole boss cylindrical with that
        # cylinder's diameter, so a pad with R1 corners read as a 2 mm
        # cylinder and a racetrack as a cylinder of its width. Compared with
        # the top's outline, measured in the top's own frame:
        #   diameter ~ largest extent              -> cylindrical
        #   diameter ~ middle extent, and longer   -> obround (width = diameter)
        #   otherwise                              -> rectangular, and the
        #                                             cylinder is a corner radius
        is_obround = False
        corner_radius = 0.0
        if spigot is not None:
            extents = sorted(self._top_extents(top))
            diameter = spigot.cyl_radius * 2.0
            if abs(diameter - extents[2]) <= 0.1 * extents[2]:
                pass
            elif abs(diameter - extents[1]) <= 0.1 * extents[1] and extents[2] > 1.1 * extents[1]:
                is_obround = True
            else:
                corner_radius = spigot.cyl_radius
                spigot = None

        if spigot is not None:
            axial = cylinder_length(spigot)
            if axial > 1e-6:
                height = axial
                # The parameter span is only as stable as the face's UV box,
                # which on an obliquely cut rim moves with the seam. Each
                # cylindrical wall's own boundary measured along the axis
                # asks the geometry instead; the tallest wins.
                if getattr(self, "_shape", None) is not None and spigot.cyl_cone_axis is not None:
                    axis_dir = spigot.cyl_cone_axis.Direction()
                    for face_id in sorted(solid_walls):
                        if face_id == top.face_id:
                            continue
                        wall = graph.node(face_id)
                        if wall.surface_type is not SurfaceType.CYLINDER:
                            continue
                        measured = extent_of_face(self._shape, face_id, axis_dir)
                        if measured > height:
                            height = measured

        # A boss's walls rise from its base to its top: a cylindrical boss's
        # axis, and a pad's corner radii, run parallel to the top's normal. A
        # large cylinder whose axis lies along the top instead rolls over it
        # (a bar-clamp lobe machined in half read as two rectangular bosses,
        # corner radius 25). A foot fillet along a straight base edge is
        # exempt: radius well under half the boss height.
        for face_id in sorted(walls - set(interior) - edge_treatment):
            if face_id == top.face_id:
                continue
            wall = graph.node(face_id)
            if wall.surface_type is not SurfaceType.CYLINDER or wall.cyl_cone_axis is None:
                continue
            if abs(wall.cyl_cone_axis.Direction().Dot(top_normal)) > 0.3:
                continue
            if wall.cyl_radius < 0.5 * height:
                continue
            return None

        parameters: dict = {"height_mm": round(height, 6)}
        if spigot is not None:
            parameters["diameter_mm"] = round(spigot.cyl_radius * 2.0, 6)
            parameters["boss_type"] = "obround" if is_obround else "cylindrical"
            if is_obround:
                # The diameter stays the end-cap size, which is the width:
                # the boss rules read it as the characteristic thickness.
                parameters["width_mm"] = round(spigot.cyl_radius * 2.0, 6)
                parameters["length_mm"] = round(sorted(top.bbox_dims())[2], 6)
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
            if corner_radius > 0.0:
                parameters["corner_radius_mm"] = round(corner_radius, 6)

        # A boss owns its top and the walls that wrap it. What was cut into
        # it afterwards (a bore, a pocket, a groove) belongs to whatever cut
        # it, and `interior` is exactly that set. Those faces used to be kept
        # as padding against the resolver's face-count containment test,
        # which painted a richly machined pad's holes and grooves as boss;
        # the resolver now states the intent directly (a hole through a
        # boss, and a boss inside a cavity, coexist with it).
        faces = [top.face_id] + sorted(walls - edge_treatment - set(interior))
        faces += self._split_top_pieces(graph, top, set(faces), edge_treatment)
        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.BOSS,
            faces=faces,
            parameters=parameters,
        )

    @staticmethod
    def _split_top_pieces(
        graph: AttributedAdjacencyGraph,
        top: AagNode,
        owned: set[int],
        edge_treatment: set[int],
    ) -> list[int]:
        """The rest of the top, where a later cut divided it.

        A groove or pocket cut across a boss's top splits one surface into
        several faces: a gasket groove round a raised land leaves a ring
        outside it and an island inside, and the ring alone was the boss.
        Walk out from the top through whatever was cut into it and take back
        every face coplanar with it. The walk stays inside the top's own
        footprint (0.5 mm slack, free along the normal), so it can only
        recover surface that was already this pad. The same principle as a
        bend owning its split skin: a feature owns its whole surface,
        however the booleans divided it.
        """
        tn = top.outward_normal
        if tn is None or top.bbox.IsVoid():
            return []
        tnv = gp_Vec(tn.XYZ())
        t_off = gp_Vec(top.centroid.XYZ()).Dot(tnv)
        txn, tyn, tzn, txx, tyx, tzx = top.bbox.Get()
        lo = (txn - 0.5, tyn - 0.5, tzn - 0.5)
        hi = (txx + 0.5, tyx + 0.5, tzx + 0.5)
        axc = (abs(tn.X()), abs(tn.Y()), abs(tn.Z()))

        def in_footprint(node: AagNode) -> bool:
            if node.bbox.IsVoid():
                return False
            xn, yn, zn, xx, yx, zx = node.bbox.Get()
            a, b = (xn, yn, zn), (xx, yx, zx)
            for i in range(3):
                if axc[i] > 0.9:
                    continue  # free along the normal
                if a[i] < lo[i] or b[i] > hi[i]:
                    return False
            return True

        added: list[int] = []
        in_set = set(owned)
        seen = {top.face_id}
        stack = [top.face_id]
        while stack:
            cur = stack.pop()
            for nb_id in graph.neighbors_of(cur):
                if nb_id in seen:
                    continue
                seen.add(nb_id)
                if not graph.has_node(nb_id):
                    continue
                nb = graph.node(nb_id)
                if not in_footprint(nb):
                    continue
                if nb.surface_type is SurfaceType.PLANE:
                    nn = nb.outward_normal
                    off = gp_Vec(nb.centroid.XYZ()).Dot(tnv)
                    if nn is not None and nn.Dot(tn) > 0.999 and abs(off - t_off) < 0.05:
                        if nb_id not in in_set and nb_id not in edge_treatment:
                            added.append(nb_id)
                            in_set.add(nb_id)
                        stack.append(nb_id)
                        continue
                # Not the top surface: a route across what was cut into the pad.
                stack.append(nb_id)
        return added

    def _top_extents(self, top: AagNode) -> tuple[float, float, float]:
        """The top face's extents in its own frame (oriented box).

        World-axis extents show a tilted disc's full diameter only when the
        tilt is about a world axis; a compound-angle boss would read as not
        round. Falls back to the world box when the face is unavailable.
        """
        face_map = getattr(self, "_face_map", None)
        if face_map is not None and 1 <= top.face_id <= face_map.Extent():
            face = TopoDS.Face_s(face_map.FindKey(top.face_id))
            if not face.IsNull():
                obb = Bnd_OBB()
                BRepBndLib.AddOBB_s(face, obb, False, True)
                return (2.0 * obb.XHSize(), 2.0 * obb.YHSize(), 2.0 * obb.ZHSize())
        return top.bbox_dims()

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


def _wall_orientation(node: AagNode, top_centroid) -> int:
    """+1 if a wall wraps the boss's material, -1 if it faces it, 0 unknown.

    A plane encloses when its outward normal points away from the top's
    centroid. A cylinder or cone encloses when its material is inside the
    surface (a stem), and faces the material when it is a bore. Tori,
    spheres and freeform faces carry no reliable answer.
    """
    if node.surface_type is SurfaceType.PLANE:
        out = node.outward_normal
        if out is None:
            return 0
        d = gp_Vec(top_centroid, node.centroid).Dot(gp_Vec(out.XYZ()))
        if d > 1.0e-6:
            return 1
        if d < -1.0e-6:
            return -1
        return 0
    if node.surface_type in (SurfaceType.CYLINDER, SurfaceType.CONE):
        return -1 if node.is_internal else 1
    return 0


def _is_narrow_band(node: AagNode) -> bool:
    """Whether a face is a band 2 mm or less across (its smallest real extent)."""
    if node.bbox.IsVoid():
        return False
    ordered = sorted(node.bbox_dims())
    for d in ordered:
        if d > 0.01:
            return d <= 2.0
    return False


def _is_chamfer_cone(node: AagNode) -> bool:
    """Whether a conical neighbour is a rim break rather than a boss wall."""
    if node.surface_type is not SurfaceType.CONE:
        return False
    ordered = sorted(node.bbox_dims())
    slant = next((d for d in ordered if d > _MIN_MEANINGFUL_DIM_MM), None)
    return slant is not None and slant <= _CHAMFER_MAX_SLANT_MM
