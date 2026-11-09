# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes external threads that have actually been modelled.

Most threads on a CAD model are not there. A tapped hole is drawn as a plain
bore at the tap drill diameter and the thread lives only on the drawing, which
is why the hole recognizer infers threads from standard drill sizes instead of
looking for them.

An external thread is different: when someone models one at all, they model it
properly, as a helical cut round the outside of a shaft. So the evidence here
is geometric and unambiguous -- an edge that winds more than a full turn about
an axis is a thread helix and nothing else. A fillet edge does not wind. A
seam does not wind. Nothing else on a machined part does.

Having found the helix, its pitch falls out of the geometry for free: the
axial distance covered divided by the number of turns. Pitch plus major
diameter is enough to name the thread against the standard tables, which is
what the relief-groove and run-out rules need.

Deliberately external only. A modelled internal thread belongs to the bore it
is cut in, and the hole recognizer owns that.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from ..aag import AagNode, AttributedAdjacencyGraph, SurfaceType
from ..features import FeatureInstance, FeatureType
from ..helix import Helix, candidate_axes, faces_touching, find_helices
from ..threads import find_by_designation, match_major_diameter
from ...utils.geometry import FaceIndex
from .base import FeatureRecognizer, axes_are_coaxial

# Same-radius tolerance for the fragments of one split cylinder.
_RADIUS_EPS = 1.0e-4


def _axial_extent(faces: FaceIndex, face_id: int) -> float:
    """A cylindrical face's length along its axis: its V parameter range."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    face = faces.face_at(face_id)
    if face is None or face.IsNull():
        return 0.0
    surface = BRepAdaptor_Surface(face, True)
    return abs(surface.LastVParameter() - surface.FirstVParameter())



class ExternalThreadRecognizer(FeatureRecognizer):
    """External threads a STEP callout names, then modelled ones by their helices.

    The callout channel is the reference's whole recognizer; the helix
    search is this workbench's own and only speaks for bands no callout
    already named.
    """

    prefix = "xt"

    @property
    def name(self) -> str:
        return "External Thread Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        if shape is None:
            return []

        found = self._from_pmi(graph, shape)
        offset = len(found)
        stated = {face_id for feature in found for face_id in feature.faces}

        axes = candidate_axes(graph)
        if not axes:
            return found

        helices = find_helices(shape, axes)
        for index, helix in enumerate(helices):
            feature = self._to_feature(helix, graph, shape)
            if feature is None:
                continue
            # A band the callout already named is that thread; its helix
            # says nothing more.
            if stated.intersection(feature.faces):
                continue
            feature.instance_id = self.instance_id(offset + index)
            found.append(feature)
        return found

    # -- PMI ------------------------------------------------------------------

    def _from_pmi(self, graph: AttributedAdjacencyGraph, shape) -> list[FeatureInstance]:
        """External threads a STEP file's thread callouts name.

        The reference's whole recognizer: a callout linked to the outside of
        a shaft makes that band, with its coaxial same-radius fragments, an
        external thread. Bores are the hole pass's to thread.
        """
        evidence = getattr(self, "thread_evidence", None)
        pmi = getattr(evidence, "pmi", None) if evidence is not None else None
        if not pmi:
            return []

        found: list[FeatureInstance] = []
        claimed: set[int] = set()
        cylinders = graph.nodes_by_surface_type(SurfaceType.CYLINDER)
        faces = None
        for seed in cylinders:
            if seed.is_internal or seed.face_id in claimed:
                continue
            callout = pmi.get(seed.face_id)
            if callout is None or not callout.designation:
                continue
            if seed.cyl_cone_axis is None:
                continue

            # The seam or a tangent line splits a full cylinder in two.
            band = [seed]
            for other in cylinders:
                if other is seed or other.is_internal or other.face_id in claimed:
                    continue
                if other.cyl_cone_axis is None:
                    continue
                if abs(other.cyl_radius - seed.cyl_radius) > _RADIUS_EPS:
                    continue
                if not axes_are_coaxial(other.cyl_cone_axis, seed.cyl_cone_axis):
                    continue
                band.append(other)

            nominal, pitch = callout.nominal_mm, callout.pitch_mm
            if nominal <= 0.0 or pitch <= 0.0:
                spec = find_by_designation(callout.designation)
                if spec is not None:
                    nominal = nominal if nominal > 0.0 else spec.nominal_mm
                    pitch = pitch if pitch > 0.0 else spec.pitch_mm

            # A callout names a diameter and the face has to be it. An
            # external thread's major diameter is nominal less a small
            # allowance, so a cylinder more than a tenth off nominal is not
            # the thread described -- nist_ctc_04's M12 callout also lands on
            # the two halves of a Ø60 pocket wall.
            major = 2.0 * seed.cyl_radius
            if nominal > 0.0 and abs(major - nominal) > 0.10 * nominal:
                continue

            if faces is None:
                faces = FaceIndex(shape)
            length = 0.0
            for node in band:
                claimed.add(node.face_id)
                length = max(length, _axial_extent(faces, node.face_id))

            direction = seed.cyl_cone_axis.Direction()
            parameters: dict = {
                "axis": (direction.X(), direction.Y(), direction.Z()),
                "major_diameter_mm": major,
                "length_mm": length,
                "thread_designation": callout.designation,
            }
            if nominal > 0.0:
                parameters["thread_nominal_mm"] = nominal
            if pitch > 0.0:
                parameters["thread_pitch_mm"] = pitch
            parameters["thread_evidence"] = (
                "user_confirmed" if callout.source == "user_confirmed" else "pmi"
            )
            found.append(
                FeatureInstance(
                    instance_id=self.instance_id(len(found)),
                    type=FeatureType.EXTERNAL_THREAD,
                    faces=[node.face_id for node in band],
                    parameters=parameters,
                )
            )
        return found

    # -- features -----------------------------------------------------------

    def _to_feature(
        self, helix: Helix, graph: AttributedAdjacencyGraph, shape
    ) -> Optional[FeatureInstance]:
        pitch = helix.pitch()
        if pitch is None or pitch <= 0.0:
            return None

        faces = faces_touching(shape, helix.edges, graph)
        if not faces:
            return None

        # Which side the material is on cannot be read off the helix's own
        # faces. The flanks of a thread groove look into that groove, so they
        # are internal in exactly the way a pocket wall is, whether the thread
        # is cut on a shaft or in a bore. The question is settled by the
        # surface the thread is cut *on* -- the crest.
        crest = _crest_cylinder(graph, helix)
        if crest is None or crest.is_internal:
            return None
        if crest.face_id not in {node.face_id for node in faces}:
            faces = sorted(faces + [crest], key=lambda node: node.face_id)

        major_diameter = 2.0 * crest.cyl_radius
        spec = match_major_diameter(major_diameter)
        # Diameter alone is a weak match: an M8 and a 5/16 differ by a tenth
        # of a millimetre. The measured pitch is what settles it, so a spec
        # whose pitch disagrees with the geometry is the wrong spec.
        if spec is not None and abs(spec.pitch_mm - pitch) > 0.15:
            spec = None

        axis_dir = helix.axis.Direction()
        parameters: dict = {
            "axis": (
                round(axis_dir.X(), 6),
                round(axis_dir.Y(), 6),
                round(axis_dir.Z(), 6),
            ),
            "major_diameter_mm": round(major_diameter, 6),
            "length_mm": round(helix.axial_span, 6),
            "thread_pitch_mm": round(pitch, 6),
            "turns": round(helix.turns, 3),
            "thread_evidence": "modelled_helix",
        }
        if spec is not None:
            parameters["thread_designation"] = spec.designation
            parameters["thread_nominal_mm"] = spec.nominal_mm
            parameters["thread_system"] = spec.system

        return FeatureInstance(
            instance_id=self.instance_id(0),
            type=FeatureType.EXTERNAL_THREAD,
            faces=sorted(node.face_id for node in faces),
            parameters=parameters,
        )


def _crest_cylinder(
    graph: AttributedAdjacencyGraph, helix: Helix
) -> Optional[AagNode]:
    """The turned surface the thread is cut on.

    The helix runs at the thread root, so the crest is a coaxial cylinder a
    thread depth away -- the shank on an external thread, the bore wall on an
    internal one. It carries the major diameter the thread is named for, and
    its own inside/outside sense is the thread's.

    Nearest by radius among the coaxial candidates, which is unambiguous
    because a thread depth is small next to the diameter it is cut on.
    """
    best: Optional[AagNode] = None
    best_delta = float("inf")
    for node in graph.nodes:
        if node.surface_type is not SurfaceType.CYLINDER:
            continue
        if node.cyl_cone_axis is None or not node.cyl_radius:
            continue
        if not axes_are_coaxial(node.cyl_cone_axis, helix.axis):
            continue
        delta = abs(node.cyl_radius - helix.radius)
        # A thread never cuts anything like a quarter of its own radius away.
        # Anything further off is a different diameter on the same centreline.
        if delta > 0.25 * helix.radius:
            continue
        if delta < best_delta:
            best_delta, best = delta, node
    return best
