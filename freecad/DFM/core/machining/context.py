# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Everything a machining rule is allowed to look at.

Bundling the inputs into one object means the rule interface never has to
change as new data sources arrive -- feature recognition, tolerances, a
tessellation -- and it keeps rules honest about being stateless: everything
they know comes from here, nothing accumulates between parts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopoDS import TopoDS_Shape

from ..utils.geometry import FaceIndex
from .aag import AttributedAdjacencyGraph, SurfaceType
from .config import MachiningConfig
from .process_classifier import PartProcessResult, PartProcessType


@dataclass
class MachiningContext:
    """The shared result of one machining analysis pass."""

    shape: TopoDS_Shape
    graph: AttributedAdjacencyGraph
    face_index: FaceIndex
    config: MachiningConfig
    part_process: PartProcessResult

    # Populated once feature recognition lands.
    features: list = field(default_factory=list)

    _volume: Optional[float] = None
    _bbox_dims: Optional[tuple[float, float, float]] = None
    _plane_bbox_dims: Optional[tuple[float, float, float]] = None

    # -- convenience --------------------------------------------------------

    @property
    def process_type(self) -> PartProcessType:
        return self.part_process.type

    @property
    def is_turning_family(self) -> bool:
        return self.part_process.type.is_turning_family

    def volume_mm3(self) -> float:
        if self._volume is None:
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(self.shape, props)
            self._volume = abs(props.Mass())
        return self._volume

    def bbox_dims(self) -> tuple[float, float, float]:
        """Axis-aligned extent of the whole part."""
        if self._bbox_dims is None:
            box = Bnd_Box()
            BRepBndLib.Add_s(self.shape, box)
            if box.IsVoid():
                self._bbox_dims = (0.0, 0.0, 0.0)
            else:
                xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
                self._bbox_dims = (xmax - xmin, ymax - ymin, zmax - zmin)
        return self._bbox_dims

    def plane_bbox_dims(self) -> tuple[float, float, float]:
        """Extent measured from planar faces only.

        Deliberately distinct from :meth:`bbox_dims`. OpenCascade pads a curved
        face's bounding box after a boolean, overstating a dimension by a
        tenth of a millimetre or so depending on what curvature the part
        happens to carry. That is the same order as the tolerances these rules
        compare against, so a rule using the all-faces box would have an
        effective tolerance of "the configured value plus an unknowable
        amount".

        The trade is real and worth stating: this understates a part whose
        widest point falls on a curved face. Use it where a millimetre
        matters, and :meth:`bbox_dims` where it does not.
        """
        if self._plane_bbox_dims is None:
            box = Bnd_Box()
            found = False
            for node in self.graph.nodes:
                if node.surface_type is not SurfaceType.PLANE or node.bbox.IsVoid():
                    continue
                box.Add(node.bbox)
                found = True
            if not found or box.IsVoid():
                self._plane_bbox_dims = self.bbox_dims()
            else:
                xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
                self._plane_bbox_dims = (xmax - xmin, ymax - ymin, zmax - zmin)
        return self._plane_bbox_dims

    def bbox_diagonal(self) -> float:
        dx, dy, dz = self.bbox_dims()
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def sorted_bbox_dims(self) -> tuple[float, float, float]:
        """(shortest, middle, longest) of the part's extent."""
        ordered = sorted(self.bbox_dims())
        return (ordered[0], ordered[1], ordered[2])

    def external_planar_faces(self, min_area: float = 0.0) -> list:
        """Planar faces on the outside of the part, largest first.

        A face with at least one convex neighbour faces outward; a pocket
        floor, whose every junction is concave, does not.
        """
        faces = [
            node
            for node in self.graph.nodes
            if node.surface_type is SurfaceType.PLANE
            and node.convex_neighbor_count > 0
            and node.area >= min_area
        ]
        return sorted(faces, key=lambda n: (-n.area, n.face_id))
