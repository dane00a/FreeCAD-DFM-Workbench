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
from ..threads import match_major_diameter
from .base import FeatureRecognizer, axes_are_coaxial




class ExternalThreadRecognizer(FeatureRecognizer):
    """Finds modelled external threads by their helices."""

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

        axes = candidate_axes(graph)
        if not axes:
            return []

        helices = find_helices(shape, axes)
        if not helices:
            return []

        found: list[FeatureInstance] = []
        for index, helix in enumerate(helices):
            feature = self._to_feature(helix, graph, shape)
            if feature is not None:
                feature.instance_id = self.instance_id(index)
                found.append(feature)
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
