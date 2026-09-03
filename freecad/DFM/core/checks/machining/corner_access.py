# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Whether a sharp inside corner is one anybody has to do anything about.

Almost every part has sharp concave corners, and almost none of them are a
problem. The base of a boss standing proud of a plate is a sharp concave
corner. So is the junction where a rib meets the deck it rises from, and the
line where a machined step drops to the surface below. A machinist looks at
those and puts a radius on them with the corner of the tool that is already
cutting the face, and thinks no more about it.

What makes a corner worth reporting is not its shape but whether anything can
get at it. The same 90-degree junction is trivial on the outside of a part and
genuinely awkward at the bottom of a canyon between two ribs -- the cutter has
to reach in with a long enough tool to clear its own holder, and the corner
either gets a bigger radius than the drawing asks for or an extra operation.

So this module answers one question: can a fillet cutter reach this corner
from open space?

It answers it the only way that is honest about geometry nobody has seen --
by firing rays and seeing what stops them. There are two tests, and they are
different questions:

*Cutter-formed* asks whether the tool that made the cavity already made this
corner on its way past. A square corner between a floor and a wall, on a
straight edge, with a cardinal approach that reaches open space, is produced
free by an end mill's own corner -- the side cuts the wall while the flat
bottom cuts the floor, in one pass. Nothing has to be done about it at all.
The one exception is an edge that runs into a planar wall at either end: that
is the floor line of a closed pocket, and the trihedral corner there can never
be sharp, so it stays reported.

*Accessible* is the weaker claim: not that the corner came for free, but that
a cutter could get to it if somebody wanted a radius there. Probed by standing
5 mm off the corner along the bisector of its two faces and casting six
cardinal rays. Foreign material within cutter reach means confined.

Both are sampled along the edge rather than at its midpoint. A hundred-
millimetre plinth line is open for most of its length and hemmed in where the
rib field crosses it, and a single probe at the middle generalises whichever
of those the midpoint happens to sit in.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRep import BRep_Tool
from OCP.BRepIntCurveSurface import BRepIntCurveSurface_Inter
from OCP.GeomAbs import GeomAbs_Plane
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.GeomLProp import GeomLProp_SLProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Face
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt, gp_Vec

from ...machining.aag import AagNode, SurfaceType


# A corner is confined by material within reach of a fillet cutter and its
# holder, not by anything anywhere on the part. Measuring against the part's
# own size made the open outer corners of a multi-boss part fail on clutter
# forty millimetres away, which no machinist would call an obstruction.
_CUTTER_REACH_MM = 25.0

# How far off the corner to stand before looking around. Far enough to be
# clear of the two faces forming it, close enough to still be in the corner.
_PROBE_STANDOFF_MM = 5.0

# Hits nearer than this are the probe grazing the geometry it started from.
_PROBE_MIN_MM = 0.5

# A face smaller than this in its second-smallest dimension cannot be reached
# by a standard fillet cutter whatever else is true, so the corner stays
# reported without asking.
_REACHABLE_FACE_MM = 3.0

# The floor a tool rides on has to be wide enough for the smallest end mill
# in anybody's library.
_MIN_FLOOR_MM = 1.0

# How closely a normal has to line up with an axis to be reachable in a
# three-axis setup.
_CARDINAL_DOT = 0.99

# Two faces are the same underlying surface when they agree this closely.
_SAME_SURFACE_DOT = 0.999
_SAME_SURFACE_MM = 1.0e-3

# The band a right angle falls in, in radians of deviation from flat.
_RIGHT_ANGLE_LOW = 1.3090
_RIGHT_ANGLE_HIGH = 1.8326

# A junction counts as uniform when the angle between its two normals varies
# by less than five degrees along the edge.
_UNIFORM_SPREAD_RAD = 0.0873

# How many points along an edge to test.
_SAMPLES = 5

# One confined sample is usually the probe near an end of the edge seeing the
# wall that terminates it. Two or more mark a real stretch of corner that has
# to be worked in a confined space.
_MIN_CONFINED_SAMPLES = 2

_CARDINALS = (
    gp_Dir(1.0, 0.0, 0.0),
    gp_Dir(-1.0, 0.0, 0.0),
    gp_Dir(0.0, 1.0, 0.0),
    gp_Dir(0.0, -1.0, 0.0),
    gp_Dir(0.0, 0.0, 1.0),
    gp_Dir(0.0, 0.0, -1.0),
)


# ---------------------------------------------------------------------------
# Small geometric helpers
# ---------------------------------------------------------------------------


def second_smallest_extent(node: AagNode) -> float:
    """How wide a face is, ignoring the dimension it has no thickness in.

    A planar face is flat, so its smallest bounding-box dimension is zero and
    says nothing. The next one up is the narrowest a tool has to fit into.
    """
    if node.bbox.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = node.bbox.Get()
    dims = sorted((xmax - xmin, ymax - ymin, zmax - zmin))
    return dims[1]


def is_cardinal(direction: Optional[gp_Dir]) -> bool:
    """Whether a direction lines up with a machine axis."""
    if direction is None:
        return False
    return (
        abs(direction.X()) > _CARDINAL_DOT
        or abs(direction.Y()) > _CARDINAL_DOT
        or abs(direction.Z()) > _CARDINAL_DOT
    )


def _cylinder_normal_at(node: AagNode, point: gp_Pnt) -> Optional[gp_Dir]:
    """A cylinder's outward normal at a point, taken radially from its axis.

    Constant along a generatrix, which is the only straight line a cylinder
    carries -- so for the plane-and-cylinder case this is exact rather than
    an approximation.
    """
    if node.cyl_cone_axis is None:
        return None
    origin = node.cyl_cone_axis.Location()
    axis = node.cyl_cone_axis.Direction()
    offset = gp_Vec(origin, point)
    radial = offset - gp_Vec(axis).Multiplied(offset.Dot(gp_Vec(axis)))
    if radial.Magnitude() <= 1.0e-6:
        return None
    normal = gp_Dir(radial)
    if node.is_reversed:
        normal.Reverse()
    return normal


def _outward_plane_normal(node: AagNode) -> Optional[gp_Dir]:
    if node.plane_normal is None:
        return None
    normal = gp_Dir(node.plane_normal.XYZ())
    if node.is_reversed:
        normal.Reverse()
    return normal


class ShapeProbe:
    """Ray casting against one solid, with the face lookups it needs.

    Built once per part and handed to every edge, because walking the shape's
    faces for each of a few hundred edges is the difference between a rule
    that runs and a rule nobody waits for.
    """

    def __init__(self, shape, graph):
        self.shape = shape
        self.graph = graph
        self._faces: list[TopoDS_Face] = []
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            self._faces.append(TopoDS.Face_s(explorer.Current()))
            explorer.Next()
        self._surfaces: dict[int, object] = {}

    # -- face access --------------------------------------------------------

    def face(self, face_id: int) -> Optional[TopoDS_Face]:
        """The B-rep face behind an adjacency-graph id.

        Graph ids count from one, the way OpenCascade's own shape map does;
        the explorer counts from zero.
        """
        index = face_id - 1
        if 0 <= index < len(self._faces):
            return self._faces[index]
        return None

    def surface(self, face_id: int):
        if face_id not in self._surfaces:
            face = self.face(face_id)
            self._surfaces[face_id] = None if face is None else BRep_Tool.Surface_s(face)
        return self._surfaces[face_id]

    def local_normal(self, node: AagNode, at: gp_Pnt) -> Optional[gp_Dir]:
        """The outward normal of a face at a particular point on it.

        A plane's normal is the same everywhere and a cylinder's is radial,
        so those are answered directly. Anything else is projected onto the
        surface and differentiated, which is what makes a drafted wall or a
        boss base circle answerable at all -- their normals turn as you go
        along the edge, and using the midpoint's everywhere puts the probe
        inside the material at the far end.
        """
        if node.surface_type is SurfaceType.PLANE:
            return _outward_plane_normal(node)
        if node.surface_type is SurfaceType.CYLINDER:
            radial = _cylinder_normal_at(node, at)
            if radial is not None:
                return radial

        surface = self.surface(node.face_id)
        face = self.face(node.face_id)
        if surface is None or face is None:
            return None
        try:
            projection = GeomAPI_ProjectPointOnSurf(at, surface)
            if not projection.IsDone() or projection.NbPoints() < 1:
                return None
            u, v = projection.LowerDistanceParameters()
            props = GeomLProp_SLProps(surface, u, v, 1, 1.0e-9)
            if not props.IsNormalDefined():
                return None
            normal = gp_Dir(props.Normal().XYZ())
        except Exception:
            return None
        if face.Orientation() == TopAbs_REVERSED:
            normal.Reverse()
        return normal

    # -- the shared edge ----------------------------------------------------

    def sample_points(self, face_id: int, midpoint: gp_Pnt) -> list[gp_Pnt]:
        """Points spread along the shared edge, or its midpoint alone.

        The adjacency edge carries only scalars, so the B-rep edge behind it
        is found the way the graph builder identified it: by its curve's
        midpoint. An edge that cannot be matched still gets tested, just at
        one point instead of five.
        """
        face = self.face(face_id)
        if face is None or midpoint is None:
            return [midpoint] if midpoint is not None else []

        explorer = TopExp_Explorer(face, TopAbs_EDGE)
        while explorer.More():
            edge = TopoDS.Edge_s(explorer.Current())
            explorer.Next()
            # Adapted rather than taken as a raw curve: in OCCT 7.8
            # `BRep_Tool::Curve` hands back only the curve and not its
            # parameter range, and an edge is exactly the range.
            curve = BRepAdaptor_Curve(edge)
            first, last = curve.FirstParameter(), curve.LastParameter()
            if not math.isfinite(first) or not math.isfinite(last):
                continue
            if curve.Value(0.5 * (first + last)).Distance(midpoint) > 1.0e-4:
                continue
            return [
                curve.Value(first + (last - first) * (k + 0.5) / _SAMPLES)
                for k in range(_SAMPLES)
            ]
        return [midpoint]

    # -- ray casting --------------------------------------------------------

    def first_hit(
        self, origin: gp_Pnt, direction: gp_Dir, maximum: float
    ) -> Optional[TopoDS_Face]:
        """The nearest face a ray meets, or None if it gets away.

        Hits closer than half a millimetre are the ray grazing whatever it
        started next to rather than meeting anything.
        """
        try:
            intersector = BRepIntCurveSurface_Inter()
            intersector.Init(self.shape, gp_Lin(origin, direction), 1.0e-6)
        except Exception:
            return None
        best_distance = math.inf
        best_face = None
        while intersector.More():
            distance = intersector.W()
            if _PROBE_MIN_MM < distance < maximum and distance < best_distance:
                best_distance = distance
                best_face = intersector.Face()
            intersector.Next()
        return best_face

    def escapes(self, origin: gp_Pnt, direction: gp_Dir, maximum: float) -> bool:
        return self.first_hit(origin, direction, maximum) is None


# ---------------------------------------------------------------------------
# Cutter-formed corners
# ---------------------------------------------------------------------------


def is_cutter_formed(
    probe: ShapeProbe,
    edge,
    first: AagNode,
    second: AagNode,
    deviation: float,
    minimum_feature_mm: float,
) -> bool:
    """Whether an end mill made this corner on its way past.

    A square corner between two planes, on a straight edge, where a tool
    coming straight down one face's normal reaches it from open space, is
    produced free: the side of the cutter forms the wall while its flat
    bottom forms the floor, in the pass that made the cavity. Every pocket
    and slot floor line is made this way. There is nothing to report and no
    confinement test to apply -- lateral clutter is irrelevant when the tool
    doing the forming is the one that cut the cavity in the first place.

    Two things disqualify it. A floor narrower than the smallest end mill
    cannot be ridden on, and a wall below the minimum machinable size belongs
    to a feature no tool can make at all, so the corner is a symptom of that
    rather than free geometry.

    And the ends have to be formable too. An edge that runs into a planar
    wall at either end is the floor line of a closed pocket: the trihedral
    corner there can never be sharp, because the tool is round. An end that
    runs out to open space, or into a cylinder -- a radiused corner -- is
    formable, and the corner stays suppressed.
    """
    if not _RIGHT_ANGLE_LOW < deviation < _RIGHT_ANGLE_HIGH:
        return False
    if edge.edge_curve_type != "line":
        return False
    if first.surface_type is not SurfaceType.PLANE:
        return False
    if second.surface_type is not SurfaceType.PLANE:
        return False

    normal_a = _outward_plane_normal(first)
    normal_b = _outward_plane_normal(second)
    if normal_a is None or normal_b is None or edge.midpoint is None:
        return False

    bisector = gp_Vec(normal_a) + gp_Vec(normal_b)
    if bisector.Magnitude() <= 1.0e-6:
        return False
    bisector.Normalize()
    # Offset along the bisector rather than along the normal being tested: a
    # point moved only along the floor's normal still lies in the wall's
    # plane, and a ray running inside a plane reports degenerate hits that
    # read as blocked.
    origin = gp_Pnt(
        edge.midpoint.X() + 0.6 * bisector.X(),
        edge.midpoint.Y() + 0.6 * bisector.Y(),
        edge.midpoint.Z() + 0.6 * bisector.Z(),
    )

    reach = _CUTTER_REACH_MM * 40.0  # the spindle comes from outside the part
    formed = False
    for floor, wall, direction in (
        (first, second, normal_a),
        (second, first, normal_b),
    ):
        if not is_cardinal(direction):
            continue
        # The tool's bottom rides the face whose normal is the tool axis, so
        # that one has to take a real cutter. The other is formed by the
        # tool's side in passing and may be arbitrarily short -- a half-
        # millimetre marking pad rim is textbook cutter-formed.
        if second_smallest_extent(floor) < _MIN_FLOOR_MM:
            continue
        if second_smallest_extent(wall) < minimum_feature_mm:
            continue
        if probe.escapes(origin, direction, reach):
            formed = True
            break

    if not formed:
        return False

    # Both ends of the run have to open out. Not capped at this edge's own
    # length: OpenCascade splits a collinear floor line into fragments at
    # seam vertices, so the closed end may belong to a farther fragment of
    # the same physical run. The first hit along the run decides.
    along = gp_Vec(normal_a).Crossed(gp_Vec(normal_b))
    if along.Magnitude() <= 1.0e-6:
        return True
    along.Normalize()
    for sign in (-1.0, 1.0):
        direction = gp_Dir(sign * along.X(), sign * along.Y(), sign * along.Z())
        hit = probe.first_hit(origin, direction, reach)
        if hit is None:
            continue
        try:
            if BRepAdaptor_Surface(hit).GetType() == GeomAbs_Plane:
                return False
        except Exception:
            continue
    return True


# ---------------------------------------------------------------------------
# The accessibility vote
# ---------------------------------------------------------------------------


def _same_surface(node: AagNode, reference: AagNode) -> bool:
    """Whether two faces are pieces of one underlying surface.

    OpenCascade splits a cylinder at its seam, so the wall of a boss is
    usually two faces where the corner only names one. A ray stopped by the
    other half has been stopped by the boss itself, and counting that as
    obstruction would report every boss base as confined.
    """
    if node.surface_type is not reference.surface_type:
        return False
    if reference.surface_type is SurfaceType.PLANE:
        a, b = node.plane_normal, reference.plane_normal
        if a is None or b is None or abs(a.Dot(b)) < _SAME_SURFACE_DOT:
            return False
        offset = gp_Vec(reference.centroid, node.centroid)
        return abs(offset.Dot(gp_Vec(b))) < _SAME_SURFACE_MM
    if reference.surface_type is SurfaceType.CYLINDER:
        if abs(node.cyl_radius - reference.cyl_radius) > 1.0e-4:
            return False
        a, b = node.cyl_cone_axis, reference.cyl_cone_axis
        if a is None or b is None:
            return False
        if abs(a.Direction().Dot(b.Direction())) < _SAME_SURFACE_DOT:
            return False
        offset = gp_Vec(b.Location(), a.Location())
        return offset.Crossed(gp_Vec(b.Direction())).Magnitude() < _SAME_SURFACE_MM
    return False


def _eligible_normals(probe, edge, first, second):
    """The two normals to bisect, if this junction can be voted on at all.

    Three shapes qualify. A straight corner between two cardinal planes is
    the mounting-foot case. A straight corner between a cardinal plane and a
    cylinder is a generatrix -- the only straight line a cylinder has, so the
    angle is constant along it and the same reasoning applies.

    The third is the general one, and it is why a boss base circle and a
    drafted wall can be judged: any junction whose two normals keep the same
    angle to each other all the way along. That admits a circle and a taper
    while still excluding a tilted boss's ellipse, whose angle varies and
    which really does need specialty tooling however open it looks.
    """
    if second_smallest_extent(first) <= _REACHABLE_FACE_MM:
        return None
    if second_smallest_extent(second) <= _REACHABLE_FACE_MM:
        return None

    straight = edge.edge_curve_type == "line"
    both_planar = (
        first.surface_type is SurfaceType.PLANE
        and second.surface_type is SurfaceType.PLANE
    )
    plane = cylinder = None
    if (
        first.surface_type is SurfaceType.PLANE
        and second.surface_type is SurfaceType.CYLINDER
    ):
        plane, cylinder = first, second
    elif (
        second.surface_type is SurfaceType.PLANE
        and first.surface_type is SurfaceType.CYLINDER
    ):
        plane, cylinder = second, first

    if straight and both_planar:
        normal_a = _outward_plane_normal(first)
        normal_b = _outward_plane_normal(second)
        if is_cardinal(normal_a) and is_cardinal(normal_b):
            return normal_a, normal_b

    if straight and plane is not None and edge.midpoint is not None:
        plane_normal = _outward_plane_normal(plane)
        axis = cylinder.cyl_cone_axis
        if (
            is_cardinal(plane_normal)
            and axis is not None
            and is_cardinal(axis.Direction())
        ):
            radial = _cylinder_normal_at(cylinder, edge.midpoint)
            if radial is not None:
                return plane_normal, radial

    # The general case only applies where the straight tests do not.
    if straight and (both_planar or plane is not None):
        return None
    return _uniform_junction_normals(probe, edge, first, second)


def _uniform_junction_normals(probe, edge, first, second):
    """The mid-edge normals, if the junction keeps its angle along the edge."""
    if edge.midpoint is None:
        return None
    samples = probe.sample_points(first.face_id, edge.midpoint)
    if len(samples) < 2:
        return None

    lowest, highest = math.inf, -math.inf
    middle = None
    for index, point in enumerate(samples):
        normal_a = probe.local_normal(first, point)
        normal_b = probe.local_normal(second, point)
        if normal_a is None or normal_b is None:
            return None
        angle = normal_a.Angle(normal_b)
        lowest = min(lowest, angle)
        highest = max(highest, angle)
        if index == len(samples) // 2:
            middle = (normal_a, normal_b)
    if middle is None or (highest - lowest) >= _UNIFORM_SPREAD_RAD:
        return None
    return middle


def is_reachable(probe: ShapeProbe, edge, first: AagNode, second: AagNode) -> bool:
    """Whether a fillet cutter could get to this corner from open space.

    Stand five millimetres off the corner along the bisector of its two
    faces, and cast six rays down the machine axes. Anything they hit within
    cutter reach is either the corner's own two faces -- which every corner
    has, and which obstruct nothing -- or foreign material, which is what
    confinement means.

    The exception is a corner whose own faces surround the probe. Outside a
    boss, the wall can eat two lateral rays and the floor a third; from
    inside a blind recess whose wall and floor are the corner's only faces,
    they close over it, and four own-face blocks means enclosed rather than
    open.

    Sampled along the edge, and confined only if at least two samples are.
    One is usually the probe near an end seeing the wall that terminates the
    edge, which is a corner of the part rather than a property of this one.
    """
    normals = _eligible_normals(probe, edge, first, second)
    if normals is None:
        return False  # not a shape this test can speak about; report it

    own_faces = {first.face_id, second.face_id}
    for node in probe.graph.nodes:
        if node.face_id in own_faces:
            continue
        if _same_surface(node, first) or _same_surface(node, second):
            own_faces.add(node.face_id)
    own_shapes = [probe.face(face_id) for face_id in sorted(own_faces)]
    own_shapes = [face for face in own_shapes if face is not None]

    fallback = gp_Vec(normals[0]) + gp_Vec(normals[1])
    if fallback.Magnitude() <= 1.0e-6:
        return False
    fallback.Normalize()

    samples = probe.sample_points(first.face_id, edge.midpoint)
    if not samples:
        return False

    confined = 0
    for point in samples:
        bisector = gp_Vec(fallback.X(), fallback.Y(), fallback.Z())
        local_a = probe.local_normal(first, point)
        local_b = probe.local_normal(second, point)
        if local_a is not None and local_b is not None:
            local = gp_Vec(local_a) + gp_Vec(local_b)
            if local.Magnitude() > 1.0e-6:
                local.Normalize()
                bisector = local

        origin = gp_Pnt(
            point.X() + _PROBE_STANDOFF_MM * bisector.X(),
            point.Y() + _PROBE_STANDOFF_MM * bisector.Y(),
            point.Z() + _PROBE_STANDOFF_MM * bisector.Z(),
        )

        foreign = own = 0
        for direction in _CARDINALS:
            hit = probe.first_hit(origin, direction, _CUTTER_REACH_MM)
            if hit is None:
                continue
            if any(hit.IsSame(face) for face in own_shapes):
                own += 1
            else:
                foreign += 1
        if foreign >= 1 or own >= 4:
            confined += 1

    needed = _MIN_CONFINED_SAMPLES if len(samples) >= 2 else 1
    return confined < needed
