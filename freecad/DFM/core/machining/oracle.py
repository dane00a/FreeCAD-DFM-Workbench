# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""A physical oracle for edge concavity, used to validate the AAG.

The adjacency graph derives concavity analytically from surface normals and the
edge tangent, which is fast but easy to get subtly wrong -- a sign error shows
up as plausible-looking output rather than a crash. This module answers the
same question by a completely different route: walk a short distance into the
solid along the bisector of the two faces and ask the kernel whether that point
is inside material.

It is far too slow for production (a solid classification plus two face
classifications per edge) and is never called during analysis. Its job is to
prove the analytic implementation right across a corpus of shapes, which is the
cheapest insurance available against a whole class of silent errors.
"""

from __future__ import annotations

from typing import Optional

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.GeomLProp import GeomLProp_SLProps
from OCP.gp import gp_Dir, gp_Pnt, gp_Pnt2d, gp_Vec
from OCP.TopAbs import TopAbs_EDGE, TopAbs_IN, TopAbs_ON
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Face, TopoDS_Shape

from .aag import AttributedAdjacencyGraph, Concavity


# Returned when the probe cannot decide -- a near-tangent junction, a point the
# classifier puts on a boundary, or a projection that fails.
UNDECIDED = 0
CONVEX = 1
CONCAVE = -1


def physical_concavity(
    shape: TopoDS_Shape,
    edge_midpoint: gp_Pnt,
    face_a: TopoDS_Face,
    face_b: TopoDS_Face,
    probe_mm: float = 0.1,
    nudge_mm: float = 0.2,
) -> int:
    """Classify one edge by probing the solid. CONVEX, CONCAVE or UNDECIDED.

    `probe_mm` and `nudge_mm` are absolute distances, so both must be small
    against the part's features but large against its tolerances. The defaults
    suit millimetre-scale parts; :func:`concavity_census` scales them.
    """
    tangent = _shared_edge_tangent(face_a, edge_midpoint)
    if tangent is None:
        return UNDECIDED

    into_a = _into_face_direction(face_a, edge_midpoint, tangent, nudge_mm)
    into_b = _into_face_direction(face_b, edge_midpoint, tangent, nudge_mm)
    if into_a is None or into_b is None:
        return UNDECIDED

    bisector = gp_Vec(into_a).Added(gp_Vec(into_b))
    if bisector.Magnitude() <= 0.1:
        return UNDECIDED  # the faces are near-tangent; there is no wedge to probe

    direction = gp_Dir(bisector)
    probe = gp_Pnt(
        edge_midpoint.X() + direction.X() * probe_mm,
        edge_midpoint.Y() + direction.Y() * probe_mm,
        edge_midpoint.Z() + direction.Z() * probe_mm,
    )

    try:
        classifier = BRepClass3d_SolidClassifier(shape, probe, 1e-6)
    except Exception:
        return UNDECIDED

    state = classifier.State()
    if state == TopAbs_IN:
        return CONVEX  # bisector points into material: the wedge is solid
    if state == TopAbs_ON:
        return UNDECIDED
    return CONCAVE  # bisector points into air


def _shared_edge_tangent(face: TopoDS_Face, point: gp_Pnt) -> Optional[gp_Dir]:
    """Unit tangent of whichever of the face's edges passes through `point`."""
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        curve = BRep_Tool.Curve_s(edge, 0.0, 0.0)
        if curve is None:
            continue
        first, last = BRep_Tool.Range_s(edge)
        mid = (first + last) * 0.5
        candidate = gp_Pnt()
        derivative = gp_Vec()
        curve.D1(mid, candidate, derivative)
        if candidate.Distance(point) > 1e-4:
            continue
        if derivative.Magnitude() < 1e-12:
            continue
        return gp_Dir(derivative)
    return None


def _into_face_direction(
    face: TopoDS_Face, point: gp_Pnt, tangent: gp_Dir, nudge_mm: float
) -> Optional[gp_Dir]:
    """Unit vector lying in `face`, perpendicular to the edge, pointing inward.

    The inward sense is decided by nudging along both candidates and asking the
    trimmed-face classifier which one lands on the face. Deliberately not
    decided by aiming at the face centroid: on a face with a hole the centroid
    can sit over the void, and the probe then walks across the opening.
    """
    surface = BRep_Tool.Surface_s(face)
    if surface is None:
        return None

    projector = GeomAPI_ProjectPointOnSurf(point, surface)
    if not projector.IsDone() or projector.NbPoints() == 0:
        return None
    u, v = projector.LowerDistanceParameters()

    props = GeomLProp_SLProps(surface, u, v, 1, 1e-9)
    if not props.IsNormalDefined():
        return None

    # Only used as a frame; its sign does not matter.
    in_plane = gp_Vec(props.Normal()).Crossed(gp_Vec(tangent))
    if in_plane.Magnitude() < 1e-9:
        return None
    candidate = gp_Dir(in_plane)

    positive = _lands_on_face(face, surface, point, candidate, nudge_mm)
    negative = _lands_on_face(face, surface, point, candidate.Reversed(), nudge_mm)
    if positive == negative:
        return None  # both or neither: cannot tell which way is inward
    return candidate if positive else gp_Dir(candidate.Reversed().XYZ())


def _lands_on_face(
    face: TopoDS_Face, surface, point: gp_Pnt, direction: gp_Dir, nudge_mm: float
) -> bool:
    """True when stepping `nudge_mm` along `direction` stays on the trimmed face."""
    stepped = gp_Pnt(
        point.X() + direction.X() * nudge_mm,
        point.Y() + direction.Y() * nudge_mm,
        point.Z() + direction.Z() * nudge_mm,
    )
    projector = GeomAPI_ProjectPointOnSurf(stepped, surface)
    if not projector.IsDone() or projector.NbPoints() == 0:
        return False
    u, v = projector.LowerDistanceParameters()
    try:
        classifier = BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7)
    except Exception:
        return False
    return classifier.State() == TopAbs_IN


class CensusResult:
    """Tally of an AAG's concavity signs against the physical oracle."""

    def __init__(self) -> None:
        self.agreed = 0
        self.disagreed = 0
        self.undecided = 0
        self.skipped_tangent = 0
        self.disagreements: list[tuple[int, int, str, str]] = []

    @property
    def compared(self) -> int:
        return self.agreed + self.disagreed

    @property
    def agreement(self) -> float:
        return self.agreed / self.compared if self.compared else 1.0

    def __repr__(self) -> str:
        return (
            f"<CensusResult {self.agreed}/{self.compared} agreed "
            f"({self.agreement:.4%}), {self.undecided} undecided, "
            f"{self.skipped_tangent} tangent>"
        )


def concavity_census(
    shape: TopoDS_Shape,
    graph: AttributedAdjacencyGraph,
    face_index,
    scale_probes: bool = True,
) -> CensusResult:
    """Compare every AAG edge's concavity against the physical oracle.

    Tangent edges are skipped: the oracle has no wedge to probe there, which is
    exactly the case the analytic tolerance exists to catch.
    """
    result = CensusResult()

    probe_mm, nudge_mm = 0.1, 0.2
    if scale_probes:
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        if not box.IsVoid():
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            diagonal = gp_Pnt(xmin, ymin, zmin).Distance(gp_Pnt(xmax, ymax, zmax))
            # The reference's constants suit a ~100 mm part; hold that ratio so
            # very small or very large parts get proportionate probes.
            scale = max(0.05, min(10.0, diagonal / 100.0))
            probe_mm, nudge_mm = 0.1 * scale, 0.2 * scale

    for edge in graph.edges:
        if edge.concavity is Concavity.TANGENT:
            result.skipped_tangent += 1
            continue
        if edge.concavity is Concavity.UNKNOWN or edge.midpoint is None:
            result.undecided += 1
            continue

        verdict = physical_concavity(
            shape,
            edge.midpoint,
            face_index.face_at(edge.face_id_a),
            face_index.face_at(edge.face_id_b),
            probe_mm=probe_mm,
            nudge_mm=nudge_mm,
        )
        if verdict == UNDECIDED:
            result.undecided += 1
            continue

        expected = Concavity.CONVEX if verdict == CONVEX else Concavity.CONCAVE
        if expected is edge.concavity:
            result.agreed += 1
        else:
            result.disagreed += 1
            result.disagreements.append(
                (edge.face_id_a, edge.face_id_b, edge.concavity.name, expected.name)
            )

    return result
