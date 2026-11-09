# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""From a face the STEP reader produced to the face id the analysis uses.

Three shapes are in play, and keeping them apart is the whole job:

*The XCAF solids.* What ``STEPCAFControl_Reader`` put in the document, with
the free surface patches some exporters ship beside the part left out (the
reference keeps solids only, for the reason it gives: ``nist_ctc_04``'s
``Fill.1`` patch would otherwise land as a phantom face on a pocket floor).
Every PMI shape reference points into these.

*The healed shape.* The reference runs ``ShapeFix_Shape`` and a sewing pass
over the solids and analyses the result. Measured on the sixteen PMI
fixtures, healing keeps the faces themselves -- every one survives as the
same ``TShape`` except a single face of ``nist_ftc_11`` -- but sewing hands
them back in a different order. So the reference's own face ids are
positions in an order this workbench never sees; what carries over is
*which* faces a reference resolves, and that is decided by identity against
the healed faces, with a bounding-box fallback where identity fails. Both are
reproduced here against a healed copy built the reference's way.

*The analysis shape.* ``read_step`` returns the file through a plain
``STEPControl_Reader``, which is the XCAF solids face for face, in the same
order, followed by the surface patches. Nothing here changes that shape --
every other rule's numbers depend on it -- so a resolved face is carried
across by position, after checking that the two faces at that position agree
in type and extent.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from OCP.BRep import BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import GeomAbs_Cylinder
from OCP.ShapeFix import ShapeFix_Shape, ShapeFix_Solid
from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_FACE, TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape
from freecad.DFM.core.utils.occt import TopoDS
from freecad.DFM.core.utils.occt import TopTools_IndexedMapOfShape

from ...utils.geometry import FaceIndex


def heal(raw: TopoDS_Shape) -> TopoDS_Shape:
    """The reference's ``XdeProcessor::heal``, step for step."""
    fixer = ShapeFix_Shape()
    fixer.Init(raw)
    fixer.SetPrecision(1e-3)
    fixer.SetMaxTolerance(1e-1)
    fixer.Perform()
    fixed = fixer.Shape()
    if fixed is None or fixed.IsNull():
        fixed = raw

    if fixed.ShapeType() in (TopAbs_COMPOUND, TopAbs_SHELL):
        sewing = BRepBuilderAPI_Sewing(1e-3)
        sewing.Add(fixed)
        sewing.Perform()
        sewn = sewing.SewedShape()
        if sewn is not None and not sewn.IsNull():
            fixed = sewn

    if fixed.ShapeType() == TopAbs_SHELL:
        solid = ShapeFix_Solid().SolidFromShell(TopoDS.Shell_s(fixed))
        if solid is not None and not solid.IsNull():
            fixed = solid
    return fixed


def explore_faces(shape: TopoDS_Shape) -> List:
    """Faces in ``TopExp_Explorer`` order, repeats and all."""
    out = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        out.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return out


def _bounds(face) -> Optional[tuple]:
    box = Bnd_Box()
    BRepBndLib.Add_s(face, box)
    if box.IsVoid():
        return None
    return box.Get()


def _surface_type(face):
    return BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType()


def _same_extent(a: Optional[tuple], b: Optional[tuple], tol: float) -> bool:
    if a is None or b is None:
        return False
    return all(abs(p - q) < tol for p, q in zip(a, b))


class FaceResolver:
    """Places a face from the STEP reader in the analysis shape.

    ``xcaf_shape`` is the solids compound from the XCAF document;
    ``analysis_shape`` is the shape the rules run on. The healed copy is
    built here, because it decides what resolves.
    """

    #: The reference's fallback tolerance: OCCT pads a curved face's box by a
    #: few hundredths after healing, and distinct features sit millimetres
    #: apart, so a tenth pins the face without colliding.
    BOX_TOLERANCE = 0.1

    def __init__(self, xcaf_shape: TopoDS_Shape, analysis_shape: TopoDS_Shape):
        self.xcaf_faces = explore_faces(xcaf_shape)
        self._xcaf_map = TopTools_IndexedMapOfShape()
        for face in self.xcaf_faces:
            self._xcaf_map.Add(face)

        self.healed_shape = heal(xcaf_shape)
        self.healed_faces = explore_faces(self.healed_shape)
        self._healed_map = TopTools_IndexedMapOfShape()
        for face in self.healed_faces:
            self._healed_map.Add(face)

        self._analysis_index = FaceIndex(analysis_shape)
        self._xcaf_to_ours = self._align(self.xcaf_faces, self._analysis_index)
        self._healed_bounds: Dict[int, Optional[tuple]] = {}
        self._healed_types: Dict[int, object] = {}
        self._healed_first_position: Optional[Dict[int, int]] = None

    # -- the analysis-shape side ------------------------------------------

    @staticmethod
    def _align(xcaf_faces: Sequence, index: FaceIndex) -> List[int]:
        """Our one-based id for each XCAF face, or 0 where none matches.

        Position first: the plain reader produces the same faces in the same
        order. Each pairing is checked on surface type and extent, and a face
        that fails the check is looked up by geometry instead, so a file the
        two readers order differently still resolves.
        """
        count = len(index)
        ours: List[int] = []
        lookup: Optional[Dict[tuple, List[int]]] = None
        for position, face in enumerate(xcaf_faces, start=1):
            if position <= count:
                candidate = index.face_at(position)
                if _surface_type(candidate) == _surface_type(face) and _same_extent(
                    _bounds(candidate), _bounds(face), 1e-6
                ):
                    ours.append(position)
                    continue
            if lookup is None:
                lookup = {}
                for i in range(1, count + 1):
                    other = index.face_at(i)
                    bounds = _bounds(other)
                    if bounds is None:
                        continue
                    key = (int(_surface_type(other)),) + tuple(round(v, 4) for v in bounds)
                    lookup.setdefault(key, []).append(i)
            bounds = _bounds(face)
            key = (
                (int(_surface_type(face)),) + tuple(round(v, 4) for v in bounds)
                if bounds is not None
                else None
            )
            hits = lookup.get(key, []) if key is not None else []
            ours.append(hits[0] if hits else 0)
        return ours

    def id_of_xcaf_face(self, face) -> int:
        """Our id for a face of the XCAF solids, 0 if it is not one."""
        position = self._xcaf_map.FindIndex(face)
        if position <= 0:
            return 0
        return self._xcaf_to_ours[position - 1]

    def id_of_healed_position(self, position: int) -> int:
        """Our id for the healed face at a zero-based explorer position."""
        if position < 0 or position >= len(self.healed_faces):
            return 0
        face = self.healed_faces[position]
        found = self.id_of_xcaf_face(face)
        if found:
            return found
        # A face healing rebuilt: carry it across by geometry.
        target_type = _surface_type(face)
        target_bounds = _bounds(face)
        for i, candidate in enumerate(self.xcaf_faces):
            if _surface_type(candidate) != target_type:
                continue
            if _same_extent(_bounds(candidate), target_bounds, self.BOX_TOLERANCE):
                return self._xcaf_to_ours[i]
        return 0

    def id_of_healed_face(self, face) -> int:
        """Our id for a face of the healed shape (``StepFaceResolver::face_id``).

        The reference names a healed face by the first explorer position
        that holds it, so a face the explorer visits twice takes its first.
        """
        if face is None or face.IsNull():
            return 0
        if self._healed_first_position is None:
            self._healed_first_position = {}
            for position, candidate in enumerate(self.healed_faces):
                key = self._healed_map.FindIndex(candidate)
                self._healed_first_position.setdefault(key, position)
        key = self._healed_map.FindIndex(face)
        if key <= 0:
            return 0
        return self.id_of_healed_position(self._healed_first_position.get(key, -1))

    # -- the reference's two resolution routes ------------------------------

    def survives_heal(self, face) -> bool:
        return self._healed_map.Contains(face)

    def identity_id(self, face) -> int:
        """``StepFaceResolver``: identity against the healed shape, no fallback."""
        if face is None or face.IsNull() or not self._healed_map.Contains(face):
            return 0
        return self.id_of_xcaf_face(face)

    def parser_id(self, face) -> int:
        """``GdtParser``'s ``find_face_index``: identity, then a matching box.

        The fallback walks the healed faces in the healed order and takes the
        first of the same surface type (and radius, for a cylinder) whose box
        agrees to a tenth of a millimetre on every side.
        """
        if face is None or face.IsNull():
            return 0
        found = self.identity_id(face)
        if found:
            return found
        if face.ShapeType() != TopAbs_FACE:
            return 0
        face = TopoDS.Face_s(face)
        target = BRepAdaptor_Surface(face)
        target_type = target.GetType()
        target_radius = target.Cylinder().Radius() if target_type == GeomAbs_Cylinder else 0.0
        target_bounds = _bounds(face)
        if target_bounds is None:
            return 0
        for position, candidate in enumerate(self.healed_faces):
            surface = BRepAdaptor_Surface(candidate)
            if surface.GetType() != target_type:
                continue
            if (
                target_type == GeomAbs_Cylinder
                and abs(target_radius - surface.Cylinder().Radius()) > self.BOX_TOLERANCE
            ):
                continue
            if position not in self._healed_bounds:
                self._healed_bounds[position] = _bounds(candidate)
            if _same_extent(self._healed_bounds[position], target_bounds, self.BOX_TOLERANCE):
                return self.id_of_healed_position(position)
        return 0


def solids_compound(shapes: Sequence[TopoDS_Shape]) -> TopoDS_Compound:
    """The free shapes' solids in one compound -- or, failing any, the shapes."""
    from OCP.TopAbs import TopAbs_SOLID

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    solids = 0
    for shape in shapes:
        if shape is None or shape.IsNull():
            continue
        explorer = TopExp_Explorer(shape, TopAbs_SOLID)
        while explorer.More():
            builder.Add(compound, explorer.Current())
            solids += 1
            explorer.Next()
    if solids == 0:
        for shape in shapes:
            if shape is not None and not shape.IsNull():
                builder.Add(compound, shape)
    return compound
