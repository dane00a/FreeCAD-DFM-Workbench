# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Semantic PMI out of a STEP file: the reference engine's XDE path, ported.

What the reference does on import, in its order:

1. Read the file through ``STEPCAFControl_Reader`` into an XCAF document
   with GD&T mode on (``XdeProcessor::load``).
2. Walk the document's dimension/tolerance tool for geometric tolerances,
   dimensions and datums, and its notes tool for free-text notes
   (``GdtParser::parse``), resolving every referenced face.
3. While the reader is still alive, recover from the raw STEP model what
   XCAF does not carry: per-face surface-texture requirements, datum faces
   bound through ``ITEM_IDENTIFIED_REPRESENTATION_USAGE`` or a composite
   aspect, datum targets the importer dropped, general drawing notes in
   ``'semantic text'`` properties, and dimension values the importer lost.
4. Fold those into the parsed result (``part_pipeline.cpp``).

Two differences between the OCCT 7.8 kernel this workbench runs and the 8.0
kernel the goldens were made on are bridged here, each from the STEP text and
each only where 7.8 demonstrably lost the number:

* A geometric tolerance whose magnitude is written as a complex measure
  entity -- ``(LENGTH_MEASURE_WITH_UNIT() MEASURE_REPRESENTATION_ITEM() ...)``
  -- reads back as zero on 7.8 (all of ``nist_ctc_01``, ``nist_ctc_02``,
  ``nist_ftc_08``). The value is read from the entity instead.
* A dimension whose nominal measure also carries a precision qualifier --
  ``(LENGTH_MEASURE_WITH_UNIT() ... QUALIFIED_REPRESENTATION_ITEM((#q)) ...)``
  -- comes back unconverted, in the unit the measure is written in:
  ``nist_ftc_06``, ``nist_ftc_08`` and ``nist_ctc_05`` read 0.5 where 8.0
  reads 12.7. ``nist_ftc_09`` writes the same inches without the qualifier
  and 7.8 converts it correctly. The measure's unit is applied where 7.8
  skipped it.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDF import TDF_AttributeIterator, TDF_Label, TDF_LabelSequence
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Shape
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDimTolObjects import (
    XCAFDimTolObjects_DatumTargetType_Area,
    XCAFDimTolObjects_DatumTargetType_Circle,
    XCAFDimTolObjects_DatumTargetType_Line,
    XCAFDimTolObjects_DatumTargetType_Point,
    XCAFDimTolObjects_DatumTargetType_Rectangle,
    XCAFDimTolObjects_DimensionType_Location_Angular,
    XCAFDimTolObjects_DimensionType_Location_Oriented,
    XCAFDimTolObjects_DimensionType_Size_Angular,
    XCAFDimTolObjects_DimensionType_Size_Diameter,
    XCAFDimTolObjects_DimensionType_Size_Radius,
    XCAFDimTolObjects_GeomToleranceMatReqModif_L,
    XCAFDimTolObjects_GeomToleranceMatReqModif_M,
    XCAFDimTolObjects_GeomToleranceType_Angularity,
    XCAFDimTolObjects_GeomToleranceType_CircularityOrRoundness,
    XCAFDimTolObjects_GeomToleranceType_CircularRunout,
    XCAFDimTolObjects_GeomToleranceType_Concentricity,
    XCAFDimTolObjects_GeomToleranceType_Cylindricity,
    XCAFDimTolObjects_GeomToleranceType_Flatness,
    XCAFDimTolObjects_GeomToleranceType_Parallelism,
    XCAFDimTolObjects_GeomToleranceType_Perpendicularity,
    XCAFDimTolObjects_GeomToleranceType_Position,
    XCAFDimTolObjects_GeomToleranceType_ProfileOfLine,
    XCAFDimTolObjects_GeomToleranceType_ProfileOfSurface,
    XCAFDimTolObjects_GeomToleranceType_Straightness,
    XCAFDimTolObjects_GeomToleranceType_Symmetry,
    XCAFDimTolObjects_GeomToleranceType_TotalRunout,
    XCAFDimTolObjects_GeomToleranceTypeValue_Diameter,
    XCAFDimTolObjects_GeomToleranceTypeValue_SphericalDiameter,
    XCAFDimTolObjects_GeomToleranceZoneModif_Projected,
)
from OCP.XCAFDoc import (
    XCAFDoc,
    XCAFDoc_AssemblyItemRef,
    XCAFDoc_Datum,
    XCAFDoc_Dimension,
    XCAFDoc_DimTolTool,
    XCAFDoc_DocumentTool,
    XCAFDoc_GeomTolerance,
    XCAFDoc_GraphNode,
    XCAFDoc_NoteBalloon,
    XCAFDoc_NoteComment,
)

from ..annotations import AnnotationSet, ToleranceType
from .faces import FaceResolver, solids_compound
from .gdt_result import (
    GdtAnnotation,
    GdtDatum,
    GdtNote,
    GdtResult,
    GdtSurfaceFinish,
    category_of,
    to_annotation_set,
)
from .step_text import StepTextEntity, StepTextIndex


class StepPmiError(RuntimeError):
    """A STEP file the XDE reader could not open or transfer."""


# =============================================================================
# Small OCCT conveniences
# =============================================================================


def _attribute(label: TDF_Label, cls):
    """The label's first attribute of a class.

    ``TDF_Label.FindAttribute`` cannot hand an attribute back through the
    Python binding (the out-parameter is not rebound), so the attributes are
    walked instead; the binding returns each as its most derived type.
    """
    if label is None or label.IsNull():
        return None
    iterator = TDF_AttributeIterator(label)
    while iterator.More():
        value = iterator.Value()
        if isinstance(value, cls):
            return value
        iterator.Next()
    return None


def _ascii(handle) -> str:
    if handle is None:
        return ""
    try:
        return handle.ToCString() or ""
    except Exception:
        return ""


def _extended(text) -> str:
    """An ExtendedString as text, unmappable characters as '?' (the reference's)."""
    if text is None:
        return ""
    try:
        return "".join(
            chr(code) if code < 128 else "?"
            for code in (text.Value(i) for i in range(1, text.Length() + 1))
        )
    except Exception:
        try:
            return text.ToExtString()
        except Exception:
            return ""


def _labels(getter) -> List[TDF_Label]:
    sequence = TDF_LabelSequence()
    getter(sequence)
    return [sequence.Value(i) for i in range(1, sequence.Length() + 1)]


def _ref_shape_labels(label: TDF_Label) -> Tuple[List[TDF_Label], List[TDF_Label]]:
    first, second = TDF_LabelSequence(), TDF_LabelSequence()
    XCAFDoc_DimTolTool.GetRefShapeLabel_s(label, first, second)
    return (
        [first.Value(i) for i in range(1, first.Length() + 1)],
        [second.Value(i) for i in range(1, second.Length() + 1)],
    )


def _faces_of(shape: TopoDS_Shape) -> list:
    out = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out


# =============================================================================
# Reading the file
# =============================================================================


class XdeDocument:
    """One STEP file read the reference's way, and everything kept alive."""

    def __init__(self, path: str):
        self.path = path
        self.doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
        XCAFApp_Application.GetApplication_s().InitDocument(self.doc)

        reader = STEPCAFControl_Reader()
        # The reference also sets meta mode; 7.8's reader has no such switch
        # (property reading is on by default there).
        for switch in (
            "SetColorMode",
            "SetNameMode",
            "SetLayerMode",
            "SetGDTMode",
            "SetMetaMode",
            "SetPropsMode",
            "SetViewMode",
            "SetMatMode",
            "SetSHUOMode",
        ):
            setter = getattr(reader, switch, None)
            if setter is not None:
                setter(True)
        status = reader.ReadFile(path)
        if status != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise StepPmiError(f"{os.path.basename(path)}: STEP read failed ({status})")
        if not reader.Transfer(self.doc):
            raise StepPmiError(f"{os.path.basename(path)}: STEP transfer to XDE failed")
        self.reader = reader

        main = self.doc.Main()
        self.shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(main)
        self.dimtol_tool = XCAFDoc_DocumentTool.DimTolTool_s(main)
        self.notes_tool = XCAFDoc_DocumentTool.NotesTool_s(main)

        free = _labels(self.shape_tool.GetFreeShapes)
        self.solids = solids_compound([self.shape_tool.GetShape_s(lab) for lab in free])

        # The raw entity model, kept alive with the reader. Entities are held
        # in a list so that the binding hands back the same Python object for
        # the same entity everywhere, which is what lets them key a dict.
        work_session = reader.Reader().WS()
        self.model = work_session.Model()
        self.transfer_reader = work_session.TransferReader()
        self.entities = [self.model.Value(i) for i in range(1, self.model.NbEntities() + 1)]

        self.text = StepTextIndex()
        self.text.load(path)
        # The model numbers entities in file order, and so does the text
        # index: entity i of the one is entity i of the other. 7.8 clears the
        # model's own "#id" labels after transfer, so this is the bridge.
        text_entities = self.text.entities()
        self.text_ids: Dict[int, int] = {}
        self.by_text_id: Dict[int, object] = {}
        if len(text_entities) == len(self.entities):
            for entity, text_entity in zip(self.entities, text_entities):
                self.text_ids[id(entity)] = text_entity.id
                self.by_text_id[text_entity.id] = entity

    def shape_of(self, label: TDF_Label) -> Optional[TopoDS_Shape]:
        try:
            shape = self.shape_tool.GetShape_s(label)
        except Exception:
            return None
        return None if shape is None or shape.IsNull() else shape


class EntityFaces:
    """The reference's ``StepFaceResolver``: STEP entity to face ids.

    The reference asks ``TransferBRep::ShapeResult`` for each entity. That
    function is not in this workbench's binding, so the map is built the
    other way round: every face, shell and solid of the XCAF part is asked
    which entity it was transferred from. The faces an entity yields, and the
    identity test against the healed shape, are the reference's.
    """

    def __init__(self, xde: XdeDocument, resolver: FaceResolver):
        self._resolver = resolver
        self._faces: Dict[int, list] = {}
        reader = xde.transfer_reader
        if reader is None:
            return

        def record(shape, faces):
            try:
                entity = reader.EntityFromShapeResult(shape, 1)
            except Exception:
                entity = None
            if entity is None:
                return
            self._faces.setdefault(id(entity), [])
            for face in faces:
                self._faces[id(entity)].append(face)

        for face in resolver.xcaf_faces:
            record(face, [face])
        for kind in (TopAbs_SHELL, TopAbs_SOLID):
            explorer = TopExp_Explorer(xde.solids, kind)
            while explorer.More():
                record(explorer.Current(), _faces_of(explorer.Current()))
                explorer.Next()
        self._keepalive = xde.entities

    def faces_for(self, entity) -> List[int]:
        out: List[int] = []
        if entity is None:
            return out
        for face in self._faces.get(id(entity), []):
            face_id = self._resolver.identity_id(face)
            if face_id and face_id not in out:
                out.append(face_id)
        return out


# =============================================================================
# GdtParser
# =============================================================================

_TOLERANCE_TYPES = {
    XCAFDimTolObjects_GeomToleranceType_Flatness: ToleranceType.FLATNESS,
    XCAFDimTolObjects_GeomToleranceType_Straightness: ToleranceType.STRAIGHTNESS,
    XCAFDimTolObjects_GeomToleranceType_CircularityOrRoundness: ToleranceType.CIRCULARITY,
    XCAFDimTolObjects_GeomToleranceType_Cylindricity: ToleranceType.CYLINDRICITY,
    XCAFDimTolObjects_GeomToleranceType_Perpendicularity: ToleranceType.PERPENDICULARITY,
    XCAFDimTolObjects_GeomToleranceType_Angularity: ToleranceType.ANGULARITY,
    XCAFDimTolObjects_GeomToleranceType_Parallelism: ToleranceType.PARALLELISM,
    XCAFDimTolObjects_GeomToleranceType_Position: ToleranceType.POSITION,
    XCAFDimTolObjects_GeomToleranceType_Concentricity: ToleranceType.CONCENTRICITY,
    XCAFDimTolObjects_GeomToleranceType_Symmetry: ToleranceType.SYMMETRY,
    XCAFDimTolObjects_GeomToleranceType_CircularRunout: ToleranceType.CIRCULAR_RUNOUT,
    XCAFDimTolObjects_GeomToleranceType_TotalRunout: ToleranceType.TOTAL_RUNOUT,
    XCAFDimTolObjects_GeomToleranceType_ProfileOfLine: ToleranceType.PROFILE_OF_A_LINE,
    XCAFDimTolObjects_GeomToleranceType_ProfileOfSurface: ToleranceType.PROFILE_OF_A_SURFACE,
}

_TARGET_TYPES = {
    XCAFDimTolObjects_DatumTargetType_Point: "point",
    XCAFDimTolObjects_DatumTargetType_Line: "line",
    XCAFDimTolObjects_DatumTargetType_Rectangle: "rectangle",
    XCAFDimTolObjects_DatumTargetType_Circle: "circle",
    XCAFDimTolObjects_DatumTargetType_Area: "area",
}

_ANGULAR_DIMENSIONS = (
    XCAFDimTolObjects_DimensionType_Location_Oriented,
    XCAFDimTolObjects_DimensionType_Location_Angular,
)


def classify_pmi_kind(presentation_name: str) -> str:
    """What a presentation-only label's callout name says it is."""
    lower = presentation_name.lower()

    def has(word: str) -> bool:
        return word in lower

    if has("note") or has("text"):
        return "note"
    if has("cutting plane") or has("section"):
        return "section_symbol"
    if has("feature control") or has("fcf"):
        return "feature_control_frame"
    if has("datum"):
        return "datum_symbol"
    if has("weld"):
        return "weld_symbol"
    if has("surface") and (has("finish") or has("texture") or has("roughness")):
        return "surface_finish_symbol"
    if has("balloon") or has("callout") or has("flag"):
        return "callout"
    return "unclassified"


def _resolve_refs(xde: XdeDocument, resolver: FaceResolver, labels) -> List[int]:
    out: List[int] = []
    for label in labels:
        shape = xde.shape_of(label)
        if shape is None:
            continue
        if shape.ShapeType() == TopAbs_FACE:
            faces = [shape]
        else:
            faces = _faces_of(shape)
        for face in faces:
            face_id = resolver.parser_id(face)
            if face_id:
                out.append(face_id)
    return out


def parse_gdt(xde: XdeDocument, resolver: FaceResolver, kernel: "KernelBridge") -> GdtResult:
    """The reference's ``GdtParser::parse``."""
    result = GdtResult()
    tool = xde.dimtol_tool
    if tool is None:
        return result
    counter = 0

    # -- geometric tolerances ---------------------------------------------
    for label in _labels(tool.GetGeomToleranceLabels):
        result.has_pmi = True
        attribute = _attribute(label, XCAFDoc_GeomTolerance)
        if attribute is None:
            continue
        obj = attribute.GetObject()
        if obj is None:
            continue
        counter += 1
        type_ = _TOLERANCE_TYPES.get(obj.GetType(), ToleranceType.UNKNOWN)
        annotation = GdtAnnotation(
            annotation_id=f"gdt_{counter}", type=type_, category=category_of(type_)
        )
        if type_ == ToleranceType.UNKNOWN:
            annotation.details["occt_tolerance_type"] = int(obj.GetType())
        value = obj.GetValue()
        if value <= 0.0:
            value = kernel.tolerance_value(label, obj)
        annotation.tolerance_value = value
        value_type = obj.GetTypeOfValue()
        annotation.tolerance_zone = (
            "cylindrical"
            if value_type == XCAFDimTolObjects_GeomToleranceTypeValue_Diameter
            else "spherical"
            if value_type == XCAFDimTolObjects_GeomToleranceTypeValue_SphericalDiameter
            else "bilateral"
        )
        modifier = obj.GetMaterialRequirementModifier()
        annotation.material_condition = (
            "MMC"
            if modifier == XCAFDimTolObjects_GeomToleranceMatReqModif_M
            else "LMC"
            if modifier == XCAFDimTolObjects_GeomToleranceMatReqModif_L
            else "RFS"
        )
        if obj.GetZoneModifier() == XCAFDimTolObjects_GeomToleranceZoneModif_Projected:
            annotation.details["zone_modifier"] = "projected"

        first, _ = _ref_shape_labels(label)
        annotation.face_ids = _resolve_refs(xde, resolver, first)

        datum_labels = TDF_LabelSequence()
        XCAFDoc_DimTolTool.GetDatumOfTolerLabels_s(label, datum_labels)
        for d in range(1, datum_labels.Length() + 1):
            datum_attribute = _attribute(datum_labels.Value(d), XCAFDoc_Datum)
            if datum_attribute is None:
                continue
            datum_obj = datum_attribute.GetObject()
            if datum_obj is None:
                continue
            name = _ascii(datum_obj.GetName())
            # A datum set up by targets links one datum object per target,
            # all named with the parent letter: "A A A" reads wrong.
            if name and name not in annotation.datum_refs:
                annotation.datum_refs.append(name)
        result.annotations.append(annotation)

    # -- dimensions --------------------------------------------------------
    for label in _labels(tool.GetDimensionLabels):
        result.has_pmi = True
        attribute = _attribute(label, XCAFDoc_Dimension)
        if attribute is None:
            continue
        obj = attribute.GetObject()
        if obj is None:
            continue
        counter += 1
        kind = obj.GetType()
        if kind in _ANGULAR_DIMENSIONS:
            type_ = ToleranceType.DIMENSION_ANGULAR
        elif kind == XCAFDimTolObjects_DimensionType_Size_Diameter:
            type_ = ToleranceType.DIMENSION_DIAMETER
        elif kind == XCAFDimTolObjects_DimensionType_Size_Radius:
            type_ = ToleranceType.DIMENSION_RADIAL
        else:
            type_ = ToleranceType.DIMENSION_LINEAR
        annotation = GdtAnnotation(
            annotation_id=f"gdt_{counter}", type=type_, category=category_of(type_)
        )
        scale = kernel.dimension_scale(kind)
        value = obj.GetValue()
        upper = obj.GetUpperTolValue()
        lower = obj.GetLowerTolValue()
        annotation.tolerance_value = value * scale
        annotation.details["upper_limit"] = upper * scale
        annotation.details["lower_limit"] = lower * scale
        no_value = value <= 0.0 and upper == 0.0 and lower == 0.0
        if no_value:
            # Marked provisionally; the de-duplication pass below decides
            # whether this is a drawing-only callout or an artifact.
            annotation.details["presentation_only"] = True

        first, second = _ref_shape_labels(label)
        faces = _resolve_refs(xde, resolver, first)
        # The geometric fallback can map two instances onto one face.
        annotation.face_ids = list(dict.fromkeys(faces))

        # OCCT also files presentation annotations it cannot tie to a
        # semantic tolerance -- notes, section symbols -- in this bucket, with
        # no value and no geometry at all. Named from their callout instead.
        if no_value and not first and not second:
            annotation.type = ToleranceType.UNKNOWN
            annotation.category = category_of(ToleranceType.UNKNOWN)
            for key in ("presentation_only", "upper_limit", "lower_limit"):
                annotation.details.pop(key, None)
            presentation = _ascii(obj.GetPresentationName())
            semantic = _ascii(obj.GetSemanticName())
            if presentation:
                annotation.details["presentation_name"] = presentation
            if semantic:
                annotation.details["semantic_name"] = semantic
            annotation.details["pmi_kind"] = classify_pmi_kind(presentation)
        result.annotations.append(annotation)

    # -- datums --------------------------------------------------------------
    datum_counter = 0
    for label in _labels(tool.GetDatumLabels):
        result.has_pmi = True
        attribute = _attribute(label, XCAFDoc_Datum)
        if attribute is None:
            continue
        obj = attribute.GetObject()
        if obj is None:
            continue
        datum_counter += 1
        datum = GdtDatum(datum_id=f"datum_{datum_counter}", label=_ascii(obj.GetName()))
        if obj.IsDatumTarget():
            # Targets keep their number in the label (A1, A2, A3), so they
            # stay distinct records rather than merging into "A".
            datum.is_target = True
            number = obj.GetDatumTargetNumber()
            if number > 0 and datum.label:
                datum.label += str(number)
            datum.target_type = _TARGET_TYPES.get(obj.GetDatumTargetType(), "")

        first, _ = _ref_shape_labels(label)
        datum.face_ids = _resolve_refs(xde, resolver, first)

        if not datum.label and not datum.face_ids:
            continue
        # Exporters create one datum label per frame that cites it; merge
        # same-letter records, uniting their faces.
        if datum.label:
            same = next((d for d in result.datums if d.label == datum.label), None)
            if same is not None:
                for face_id in datum.face_ids:
                    if face_id not in same.face_ids:
                        same.face_ids.append(face_id)
                continue
        result.datums.append(datum)

    # -- notes -------------------------------------------------------------
    _parse_notes(xde, resolver, result)

    _deduplicate_dimensions(result)
    return result


def _parse_notes(xde: XdeDocument, resolver: FaceResolver, result: GdtResult) -> None:
    tool = xde.notes_tool
    if tool is None:
        return
    try:
        labels = _labels(tool.GetNotes)
    except Exception:
        return
    note_ref = XCAFDoc.NoteRefGUID_s()
    counter = 0
    for label in labels:
        comment = _attribute(label, XCAFDoc_NoteComment)
        if comment is not None:
            text, source = _extended(comment.Comment()), "comment"
        else:
            balloon = _attribute(label, XCAFDoc_NoteBalloon)
            if balloon is None:
                continue
            text, source = _extended(balloon.Comment()), "balloon"
        if not text:
            continue
        result.has_pmi = True
        counter += 1
        note = GdtNote(note_id=f"note_{counter}", text=text, source=source)

        graph = None
        iterator = TDF_AttributeIterator(label)
        while iterator.More():
            value = iterator.Value()
            if isinstance(value, XCAFDoc_GraphNode) and value.ID().IsSame(note_ref):
                graph = value
                break
            iterator.Next()
        if graph is not None:
            for c in range(1, graph.NbChildren() + 1):
                child = graph.GetChild(c)
                if child is None:
                    continue
                ref = _attribute(child.Label(), XCAFDoc_AssemblyItemRef)
                if ref is None:
                    continue
                index = ref.GetSubshapeIndex()
                if index <= 0:
                    continue  # a part-level note
                # The reference reads the subshape index as a position in
                # its own (healed) face order.
                face_id = resolver.id_of_healed_position(index - 1)
                if face_id:
                    note.face_ids.append(face_id)
        result.notes.append(note)


def _deduplicate_dimensions(result: GdtResult) -> None:
    """Drop the dimension labels OCCT adds beyond what the file declares.

    A valueless "dimension" on exactly a tolerance callout's faces is that
    callout's shadow; one on exactly a valued dimension's faces is its
    duplicate. What survives with no value is a genuine drawing-only
    dimension, kept and marked so.
    """

    def key(annotation: GdtAnnotation) -> frozenset:
        return frozenset(annotation.face_ids)

    tolerance_faces = set()
    valued_faces = set()
    for annotation in result.annotations:
        if not annotation.face_ids:
            continue
        if not annotation.is_dimension:
            tolerance_faces.add(key(annotation))
        elif annotation.tolerance_value > 0.0:
            valued_faces.add(key(annotation))

    kept = []
    for annotation in result.annotations:
        valueless = annotation.is_valueless_dimension
        if valueless and annotation.face_ids:
            if key(annotation) in tolerance_faces or key(annotation) in valued_faces:
                continue
        if valueless:
            annotation.details.pop("presentation_only", None)
            annotation.details["value_in_model"] = False
        kept.append(annotation)
    result.annotations = kept
    result.renumber_annotations()


def drop_phantom_dimensions(result: GdtResult, text: StepTextIndex) -> None:
    """Valueless dimensions are phantoms when the file has no drawing-only ones.

    Only when every real ``DIMENSIONAL_SIZE``/``_LOCATION``/``ANGULAR_*``
    entity carries a value can a valueless label be proven a phantom; one
    genuine drawing-only dimension anywhere and nothing is touched.
    """
    if not result.annotations or text.empty():
        return
    with_value = {
        dcr.refs[0]
        for dcr in text.of_type("DIMENSIONAL_CHARACTERISTIC_REPRESENTATION")
        if dcr.refs
    }
    for keyword in ("DIMENSIONAL_SIZE", "DIMENSIONAL_LOCATION", "ANGULAR_SIZE", "ANGULAR_LOCATION"):
        for entity in text.of_type(keyword):
            if entity.type != keyword:
                continue
            if entity.id not in with_value:
                return
    before = len(result.annotations)
    result.annotations = [
        a for a in result.annotations if not a.is_valueless_dimension
    ]
    if len(result.annotations) != before:
        result.renumber_annotations()


# =============================================================================
# The 7.8 kernel bridge
# =============================================================================


_SI_PREFIX_MM = {
    "": 1000.0,
    "MILLI": 1.0,
    "CENTI": 10.0,
    "DECI": 100.0,
    "MICRO": 1e-3,
    "KILO": 1e6,
}


def unit_to_mm(text: StepTextIndex, unit_id: int, depth: int = 0) -> float:
    """Millimetres per one of a STEP length unit, 0 when it is not one."""
    entity = text.get(unit_id)
    if entity is None or depth > 4:
        return 0.0
    raw = entity.raw.upper()
    if "CONVERSION_BASED_UNIT" in raw:
        # CONVERSION_BASED_UNIT(name, #measure): the measure states the
        # factor in another unit.
        for ref in entity.refs:
            measure = text.get(ref)
            if measure is None or not measure.reals:
                continue
            for inner in measure.refs:
                base = unit_to_mm(text, inner, depth + 1)
                if base > 0.0:
                    return measure.reals[0] * base
        return 0.0
    if "SI_UNIT" in raw and "METRE" in raw:
        match = re.search(r"SI_UNIT\s*\(\s*(?:\.(\w+)\.|\$)", raw)
        prefix = match.group(1) if match and match.group(1) else ""
        return _SI_PREFIX_MM.get(prefix, 0.0)
    return 0.0


def measure_value_mm(text: StepTextIndex, measure_id: int) -> float:
    """A length measure entity's value in millimetres, 0 when unreadable."""
    measure = text.get(measure_id)
    if measure is None or not measure.reals:
        return 0.0
    for ref in measure.refs:
        factor = unit_to_mm(text, ref)
        if factor > 0.0:
            return measure.reals[0] * factor
    return 0.0


class KernelBridge:
    """What 7.8's XDE reader loses that 8.0's does not, recovered from the text."""

    def __init__(self, xde: XdeDocument):
        self._xde = xde
        self._text = xde.text
        self._tolerances: Optional[List[Tuple[str, float]]] = None
        self._used: set = set()
        self._dimension_factor: Optional[float] = None

    def _tolerance_entities(self) -> List[Tuple[str, float]]:
        """(name, magnitude in mm) for every geometric tolerance, in file order."""
        if self._tolerances is None:
            from OCP.StepDimTol import StepDimTol_GeometricTolerance

            out = []
            for entity in self._xde.entities:
                if not isinstance(entity, StepDimTol_GeometricTolerance):
                    continue
                text_id = self._xde.text_ids.get(id(entity))
                text_entity = self._text.get(text_id) if text_id else None
                value = 0.0
                if text_entity is not None and text_entity.refs:
                    # GEOMETRIC_TOLERANCE(name, description, magnitude, aspect):
                    # the magnitude is the first reference.
                    value = measure_value_mm(self._text, text_entity.refs[0])
                out.append((_ascii(entity.Name()), value))
            self._tolerances = out
        return self._tolerances

    def tolerance_value(self, label: TDF_Label, obj) -> float:
        """The magnitude 7.8 read back as zero, by the label's name.

        Labels are created in model order and carry the entity's name, so
        the first unused entity of that name is the one.
        """
        name_attribute = _attribute(label, TDataStd_Name)
        name = _extended(name_attribute.Get()) if name_attribute is not None else ""
        for index, (entity_name, value) in enumerate(self._tolerance_entities()):
            if index in self._used or entity_name != name:
                continue
            self._used.add(index)
            return value if value > 0.0 else 0.0
        return 0.0

    def dimension_scale(self, kind) -> float:
        """The factor 7.8's dimension value needs to read as 8.0's.

        Taken over the file's dimension measures rather than per dimension,
        because nothing ties an XCAF dimension label back to its entity: the
        common case across the file's nominal values -- the measure's unit
        where it is qualified (7.8 left it unconverted), 1 where it is not.
        Angles are left alone.
        """
        if kind in _ANGULAR_DIMENSIONS or kind == XCAFDimTolObjects_DimensionType_Size_Angular:
            return 1.0
        if self._dimension_factor is None:
            self._dimension_factor = self._find_dimension_factor()
        return self._dimension_factor

    def _find_dimension_factor(self) -> float:
        factors: Dict[float, int] = {}
        for dcr in self._text.of_type("DIMENSIONAL_CHARACTERISTIC_REPRESENTATION"):
            for rep_id in dcr.refs[1:]:
                rep = self._text.get(rep_id)
                if rep is None or "SHAPE_DIMENSION_REPRESENTATION" not in rep.type:
                    continue
                for item_id in rep.refs:
                    item = self._text.get(item_id)
                    if item is None or not item.reals:
                        continue
                    raw = item.raw.upper()
                    if "LENGTH_MEASURE" not in raw:
                        continue
                    if "QUALIFIED_REPRESENTATION_ITEM" not in raw:
                        factors[1.0] = factors.get(1.0, 0) + 1
                        continue
                    for ref in item.refs:
                        factor = unit_to_mm(self._text, ref)
                        if factor > 0.0:
                            rounded = round(factor, 9)
                            factors[rounded] = factors.get(rounded, 0) + 1
                            break
        if not factors:
            return 1.0
        return max(factors.items(), key=lambda kv: kv[1])[0]


# =============================================================================
# Raw-model extractors
# =============================================================================


def _identification(datum) -> str:
    try:
        return _ascii(datum.Identification())
    except Exception:
        return ""


def _select_value(select):
    try:
        return select.Value()
    except Exception:
        return None


def _gisu_items(gisu) -> list:
    items = []
    try:
        count = gisu.NbIdentifiedItem()
    except Exception:
        return items
    for k in range(1, count + 1):
        try:
            item = gisu.IdentifiedItemValue(k)
        except Exception:
            item = None
        if item is not None:
            items.append(item)
    return items


def extract_datum_faces(xde: XdeDocument, faces: EntityFaces) -> Dict[str, List[int]]:
    """The reference's ``DatumFaceExtractor::extract``: multi-face datums.

    Three routes, for datums XCAF leaves without faces: an
    ``ITEM_IDENTIFIED_REPRESENTATION_USAGE`` listing the faces (read from
    the text, since the reader leaves its item list empty); the canonical
    datum feature -> composite aspect -> per-face GISU chain; and a GISU whose
    definition is the datum letter itself.
    """
    from OCP.StepAP242 import StepAP242_GeometricItemSpecificUsage
    from OCP.StepDimTol import StepDimTol_Datum
    from OCP.StepRepr import StepRepr_ShapeAspectRelationship

    out: Dict[str, List[int]] = {}

    def add(letter: str, face_ids: List[int]) -> None:
        if not face_ids:
            return
        acc = out.setdefault(letter, [])
        for face_id in face_ids:
            if face_id not in acc:
                acc.append(face_id)

    relationships = [e for e in xde.entities if isinstance(e, StepRepr_ShapeAspectRelationship)]
    gisus = [e for e in xde.entities if isinstance(e, StepAP242_GeometricItemSpecificUsage)]

    # datum feature -> letter, either orientation of the relationship.
    feature_letter: Dict[int, str] = {}
    feature_objects: Dict[int, object] = {}
    for rel in relationships:
        a, b = rel.RelatingShapeAspect(), rel.RelatedShapeAspect()
        if a is None or b is None:
            continue
        da, db = isinstance(a, StepDimTol_Datum), isinstance(b, StepDimTol_Datum)
        if da == db:
            continue
        datum, feature = (a, b) if da else (b, a)
        letter = _identification(datum)
        if not letter:
            continue
        feature_letter[id(feature)] = letter
        feature_objects[id(feature)] = feature
    if not feature_letter:
        return out

    # Route 2: feature -> related aspects (depth 2) -> GISU faces.
    adjacency: Dict[int, List[object]] = {}
    for rel in relationships:
        a, b = rel.RelatingShapeAspect(), rel.RelatedShapeAspect()
        if a is None or b is None:
            continue
        if isinstance(a, StepDimTol_Datum) or isinstance(b, StepDimTol_Datum):
            continue
        adjacency.setdefault(id(a), []).append(b)
        adjacency.setdefault(id(b), []).append(a)
    aspect_items: Dict[int, list] = {}
    for gisu in gisus:
        definition = _select_value(gisu.Definition())
        if definition is None:
            continue
        aspect_items.setdefault(id(definition), []).extend(_gisu_items(gisu))

    for feature_key, letter in feature_letter.items():
        frontier = [feature_objects[feature_key]]
        seen = {feature_key}
        face_ids: List[int] = []
        for _depth in range(2):
            following = []
            for node in frontier:
                for neighbour in adjacency.get(id(node), []):
                    if id(neighbour) in seen:
                        continue
                    seen.add(id(neighbour))
                    following.append(neighbour)
                    for item in aspect_items.get(id(neighbour), []):
                        face_ids.extend(faces.faces_for(item))
            frontier = following
            if not frontier:
                break
        add(letter, face_ids)

    # Route 3: a GISU whose definition is the datum letter itself.
    for gisu in gisus:
        definition = _select_value(gisu.Definition())
        if not isinstance(definition, StepDimTol_Datum):
            continue
        letter = _identification(definition)
        if not letter:
            continue
        face_ids: List[int] = []
        for item in _gisu_items(gisu):
            face_ids.extend(faces.faces_for(item))
        add(letter, face_ids)

    # Route 1: IIRU(name, description, definition, used_rep, items...) from
    # the text; definition -> letter, items -> faces.
    for iiru in xde.text.of_type("ITEM_IDENTIFIED_REPRESENTATION_USAGE"):
        if iiru.type != "ITEM_IDENTIFIED_REPRESENTATION_USAGE" or len(iiru.refs) < 3:
            continue
        definition = xde.by_text_id.get(iiru.refs[0])
        if definition is None or id(definition) not in feature_letter:
            continue
        face_ids = []
        for item_id in iiru.refs[2:]:
            face_ids.extend(faces.faces_for(xde.by_text_id.get(item_id)))
        add(feature_letter[id(definition)], face_ids)
    return out


def extract_datum_targets(text: StepTextIndex) -> Dict[str, str]:
    """Datum letters established by targets the XDE reader did not import."""
    out: Dict[str, str] = {}
    for target in text.of_type("PLACED_DATUM_TARGET_FEATURE"):
        kind = target.strings[1] if len(target.strings) >= 2 else ""
        for rid in text.referrers(target.id):
            rel = text.get(rid)
            if rel is None or rel.type != "SHAPE_ASPECT_RELATIONSHIP":
                continue
            for sid in rel.refs:
                if sid == target.id:
                    continue
                datum = text.get(sid)
                if datum is None or datum.type != "DATUM" or not datum.strings:
                    continue
                letter = datum.strings[-1]
                if letter and letter not in out:
                    out[letter] = kind
    return out


def apply_datum_target_fallback(result: GdtResult, targets: Dict[str, str]) -> None:
    for datum in result.datums:
        if datum.is_target:
            continue
        if datum.label in targets:
            datum.is_target = True
            if not datum.target_type:
                datum.target_type = targets[datum.label]


def apply_datum_face_fallback(result: GdtResult, fallback: Dict[str, List[int]]) -> None:
    for datum in result.datums:
        if datum.is_target or datum.face_ids:
            continue
        if datum.label in fallback:
            datum.face_ids = list(fallback[datum.label])


# -- general notes ----------------------------------------------------------


def decode_step_string(value: str) -> str:
    r"""Decode the ``\X2\...\X0\`` and ``\X\hh`` directives in a STEP literal."""
    out = []
    i, n = 0, len(value)
    while i < n:
        if value.startswith("\\X2\\", i):
            i += 4
            while i < n:
                if value.startswith("\\X0\\", i):
                    i += 4
                    break
                chunk = value[i : i + 4]
                try:
                    code = int(chunk, 16) if len(chunk) == 4 else -1
                except ValueError:
                    code = -1
                if code < 0:
                    out.append(value[i])
                    i += 1
                    continue
                i += 4
                if 0xD800 <= code <= 0xDBFF and i + 3 < n and value[i] != "\\":
                    try:
                        low = int(value[i : i + 4], 16)
                    except ValueError:
                        low = -1
                    if 0xDC00 <= low <= 0xDFFF:
                        code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                        i += 4
                out.append(chr(code))
        elif value.startswith("\\X\\", i):
            i += 3
            if i + 1 < n:
                try:
                    out.append(chr(int(value[i : i + 2], 16)))
                    i += 2
                except ValueError:
                    pass
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _tidy_block(joined: str) -> str:
    out: List[str] = []
    previous_blank = True
    for line in joined.split("\n"):
        line = line.rstrip(" \t\r\n")
        blank = not line
        if blank and previous_blank:
            continue
        out.append(line)
        previous_blank = blank
    return "\n".join(out).rstrip(" \t\r\n")


def extract_semantic_notes(text: StepTextIndex) -> Tuple[List[GdtNote], set]:
    """General drawing notes in ``'semantic text'`` properties.

    Returns the notes and every descriptive-item name consumed, which is what
    reconciles away the presentation labels the parser surfaced for the same
    callouts.
    """
    notes: List[GdtNote] = []
    names: set = set()
    for prop in text.of_type("PROPERTY_DEFINITION"):
        if prop.type != "PROPERTY_DEFINITION" or "semantic text" not in prop.strings:
            continue
        part_level = any(
            (text.get(r) is not None and text.get(r).type in ("PRODUCT_DEFINITION_SHAPE", "PRODUCT_DEFINITION"))
            for r in prop.refs
        )
        rep_id = 0
        for referrer in text.referrers(prop.id):
            pdr = text.get(referrer)
            if pdr is None or pdr.type != "PROPERTY_DEFINITION_REPRESENTATION":
                continue
            for ref in pdr.refs:
                rep = text.get(ref)
                if rep is not None and rep.type == "REPRESENTATION":
                    rep_id = ref
                    break
            if rep_id:
                break
        rep = text.get(rep_id) if rep_id else None
        if rep is None:
            continue

        item_names: List[str] = []
        texts: List[str] = []
        explicit_breaks = False
        for item_id in rep.refs:
            item = text.get(item_id)
            if item is None or item.type != "DESCRIPTIVE_REPRESENTATION_ITEM":
                continue
            if item.strings and item.strings[0] == "equivalent unicode string":
                continue
            if item.strings:
                item_names.append(item.strings[0])
            body = decode_step_string(item.strings[1] if len(item.strings) > 1 else "")
            if body.strip() in ("/n", "/N"):
                explicit_breaks = True
            texts.append(body)

        if not part_level and not any(name[:4].lower() == "text" for name in item_names):
            continue

        joined = ""
        for body in texts:
            if explicit_breaks:
                joined += "\n" if body.strip() in ("/n", "/N") else body
            else:
                if joined:
                    joined += "\n"
                joined += body
        block = _tidy_block(joined)
        names.update(item_names)
        if sum(1 for ch in block if ch.isascii() and ch.isalnum()) < 2:
            continue
        notes.append(GdtNote(note_id="note_sem", text=block, source="descriptive"))
    return notes, names


def fold_semantic_notes(result: GdtResult, notes: List[GdtNote], names: set) -> None:
    if not notes and not names:
        return
    if names:
        before = len(result.annotations)
        result.annotations = [
            a
            for a in result.annotations
            if not (
                a.type == ToleranceType.UNKNOWN
                and isinstance(a.details.get("presentation_name"), str)
                and a.details["presentation_name"] in names
            )
        ]
        if len(result.annotations) != before:
            result.renumber_annotations()
    result.notes.extend(notes)
    for n, note in enumerate(result.notes, start=1):
        note.note_id = f"note_{n}"
    result.has_pmi = True


# -- dimension values ---------------------------------------------------------


def extract_dimension_values(xde: XdeDocument, faces: EntityFaces) -> List[Tuple[float, List[int]]]:
    """(raw value, faces) for every dimension the file gives a length value."""
    text = xde.text
    out: List[Tuple[float, List[int]]] = []

    def value_of(dim_id: int) -> float:
        for referrer in text.referrers(dim_id):
            dcr = text.get(referrer)
            if dcr is None or "DIMENSIONAL_CHARACTERISTIC_REPRESENTATION" not in dcr.type:
                continue
            for rep_id in dcr.refs:
                if rep_id == dim_id:
                    continue
                rep = text.get(rep_id)
                if rep is None or "SHAPE_DIMENSION_REPRESENTATION" not in rep.type:
                    continue
                for item_id in rep.refs:
                    item = text.get(item_id)
                    if item is not None and item.reals and item.reals[0] > 0.0:
                        return item.reals[0]
        return 0.0

    def faces_of(dim: StepTextEntity) -> List[int]:
        found = set()
        for aspect_id in dim.refs:
            for g in text.referrers(aspect_id):
                gisu = text.get(g)
                if gisu is None or "GEOMETRIC_ITEM_SPECIFIC_USAGE" not in gisu.type:
                    continue
                for item_id in gisu.refs:
                    item = text.get(item_id)
                    if item is None or item.type != "ADVANCED_FACE":
                        continue
                    found.update(faces.faces_for(xde.by_text_id.get(item_id)))
        return sorted(found)

    dims = text.of_type("DIMENSIONAL_SIZE") + text.of_type("DIMENSIONAL_LOCATION")
    for dim in dims:
        value = value_of(dim.id)
        if value <= 0.0:
            continue
        out.append((value, faces_of(dim)))
    return out


def fold_dimension_values(result: GdtResult, recovered: List[Tuple[float, List[int]]]) -> None:
    """Fill recovered values into the valueless linear/angular dimensions."""
    if not recovered:
        return

    def is_dim(a: GdtAnnotation) -> bool:
        return a.type in (ToleranceType.DIMENSION_LINEAR, ToleranceType.DIMENSION_ANGULAR)

    factors = []
    for a in result.annotations:
        if not is_dim(a) or a.tolerance_value <= 0.0 or not a.face_ids:
            continue
        wanted = set(a.face_ids)
        for raw, face_ids in recovered:
            if raw > 0.0 and set(face_ids) == wanted:
                factors.append(a.tolerance_value / raw)
                break
    if not factors:
        return
    factors.sort()
    factor = factors[len(factors) // 2]

    def value_is_read(value_mm: float) -> bool:
        return any(
            is_dim(a) and a.tolerance_value > 0.0 and abs(a.tolerance_value - value_mm) < 0.01
            for a in result.annotations
        )

    orphans = [
        (raw, face_ids)
        for raw, face_ids in recovered
        if raw > 0.0 and face_ids and not value_is_read(raw * factor)
    ]
    index = 0
    for a in result.annotations:
        if not (is_dim(a) and a.tolerance_value <= 0.0 and a.details.get("value_in_model", True) is False):
            continue
        if index >= len(orphans):
            break
        raw, face_ids = orphans[index]
        index += 1
        a.tolerance_value = raw * factor
        a.face_ids = list(face_ids)
        a.details.pop("value_in_model", None)


# -- surface finish -------------------------------------------------------------


def _step_type(entity) -> str:
    from OCP.StepData import StepData_UndefinedEntity

    if entity is None:
        return ""
    try:
        if isinstance(entity, StepData_UndefinedEntity):
            return (entity.StepType() or "").upper()
        return (entity.DynamicType().Name() or "").upper()
    except Exception:
        return ""


def _is_requirement(kind: str) -> bool:
    return "SURFACE_TEXTURE_REQUIREMENT" in kind or "SURFACETEXTUREREQUIREMENT" in kind


def _is_condition(kind: str) -> bool:
    return "SURFACE_CONDITION" in kind or "SURFACECONDITION" in kind


def _unit_to_um(kind: str) -> float:
    if "MICROIN" in kind:
        return 0.0254
    if "MICROMETRE" in kind or "MICROMETER" in kind or "MICRON" in kind:
        return 1.0
    if "MILLIMETRE" in kind or "MILLIMETER" in kind:
        return 1000.0
    if "METRE" in kind or "METER" in kind:
        return 1e6
    return 0.0


def _undefined_params(entity):
    try:
        content = entity.UndefinedContent()
    except Exception:
        return []
    if content is None:
        return []
    params = []
    for i in range(1, content.NbParams() + 1):
        try:
            if content.IsParamEntity(i):
                params.append(("entity", content.ParamEntity(i)))
            else:
                params.append(("value", _ascii(content.ParamValue(i))))
        except Exception:
            continue
    return params


def _ra_um(requirement) -> float:
    from OCP.StepData import StepData_UndefinedEntity

    if not isinstance(requirement, StepData_UndefinedEntity):
        return 0.0
    for kind, ref in _undefined_params(requirement):
        if kind != "entity" or "ROUGHNESS_MEASURE_WITH_UNIT" not in _step_type(ref).replace(
            "ROUGHNESSMEASUREWITHUNIT", "ROUGHNESS_MEASURE_WITH_UNIT"
        ):
            continue
        if not isinstance(ref, StepData_UndefinedEntity):
            continue
        params = _undefined_params(ref)
        raw = 0.0
        for k, value in params:
            if k != "value" or not value:
                continue
            match = re.match(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", value)
            if match:
                raw = float(match.group(0))
                break
        if raw == 0.0:
            continue
        multiplier = 0.0
        for k, unit in params:
            if k != "entity":
                continue
            m = _unit_to_um(_step_type(unit))
            if m > 0.0:
                multiplier = m
                break
        if multiplier <= 0.0:
            multiplier = 1e6 if raw < 1e-3 else 1.0
        return raw * multiplier
    return 0.0


def extract_surface_finishes(xde: XdeDocument, faces: EntityFaces) -> List[GdtSurfaceFinish]:
    """AP242 surface-texture requirements, with the faces their GISUs name."""
    from OCP.StepAP242 import StepAP242_GeometricItemSpecificUsage

    requirements = [e for e in xde.entities if _is_requirement(_step_type(e))]
    if not requirements:
        return []
    ra = {id(r): _ra_um(r) for r in requirements}
    req_faces: Dict[int, List[int]] = {}
    for gisu in xde.entities:
        if not isinstance(gisu, StepAP242_GeometricItemSpecificUsage):
            continue
        matched = []
        try:
            representation = gisu.UsedRepresentation()
        except Exception:
            representation = None
        if representation is not None:
            try:
                for i in range(1, representation.NbItems() + 1):
                    item = representation.ItemsValue(i)
                    kind = _step_type(item)
                    if _is_requirement(kind) or _is_condition(kind):
                        matched.append(item)
            except Exception:
                pass
        definition = _select_value(gisu.Definition())
        if definition is not None:
            kind = _step_type(definition)
            if (_is_requirement(kind) or _is_condition(kind)) and not matched:
                matched.append(requirements[0])
        if not matched:
            continue
        face_ids: List[int] = []
        for item in _gisu_items(gisu):
            face_ids.extend(faces.faces_for(item))
        if not face_ids:
            continue
        for requirement in matched:
            req_faces.setdefault(id(requirement), []).extend(face_ids)

    out = []
    for n, requirement in enumerate(requirements, start=1):
        finish = GdtSurfaceFinish(
            surface_finish_id=f"sf_{n}",
            ra_um=ra.get(id(requirement), 0.0),
            source="ap242_surface_texture_requirement",
        )
        for face_id in req_faces.get(id(requirement), []):
            if face_id not in finish.face_ids:
                finish.face_ids.append(face_id)
        out.append(finish)
    return out


# -- thread callouts -----------------------------------------------------------

_METRIC_THREAD = re.compile(r"M(\d+(?:\.\d+)?)(?:\s*[xX]\s*(\d+(?:\.\d+)?))?")
_IMPERIAL_THREAD = re.compile(
    r"(\d+/\d+|#\d+|\d+)\s*-\s*(\d+)\s*(UNC|UNF|UNEF|UN)?", re.IGNORECASE
)


def _strip_trailing_zero(token: str) -> str:
    return token[:-2] if len(token) >= 2 and token.endswith(".0") else token


def detect_thread_designation(text: str) -> str:
    """A thread designation in callout text, canonicalized, or "" for none."""
    if not text:
        return ""
    match = _METRIC_THREAD.search(text)
    if match:
        out = "M" + _strip_trailing_zero(match.group(1))
        if match.group(2):
            out += "x" + _strip_trailing_zero(match.group(2))
        return out
    match = _IMPERIAL_THREAD.search(text)
    if match:
        out = f"{match.group(1)}-{match.group(2)}"
        if match.group(3):
            out += " " + match.group(3).upper()
        return out
    return ""


def extract_thread_callouts(xde: XdeDocument, resolver: FaceResolver) -> dict:
    """The reference's ``PmiThreadExtractor``: face id -> thread callout.

    Semantic name first, then presentation name, then the descriptions.
    Faces resolve by identity only, as in the reference's extractor.
    """
    from ..annotations import ThreadCallout
    from ..threads import find_by_designation

    out: dict = {}
    tool = xde.dimtol_tool
    if tool is None:
        return out
    for label in _labels(tool.GetDimensionLabels):
        attribute = _attribute(label, XCAFDoc_Dimension)
        obj = attribute.GetObject() if attribute is not None else None
        if obj is None:
            continue
        designation, source = "", ""
        for text, tag in (
            (_ascii(obj.GetSemanticName()), "semantic_name"),
            (_ascii(obj.GetPresentationName()), "presentation_name"),
        ):
            designation = detect_thread_designation(text)
            if designation:
                source = tag
                break
        if not designation:
            try:
                count = obj.NbDescriptions()
            except Exception:
                count = 0
            for i in range(count):
                try:
                    designation = detect_thread_designation(_ascii(obj.GetDescription(i)))
                except Exception:
                    designation = ""
                if designation:
                    source = "description"
                    break
        if not designation:
            continue
        spec = find_by_designation(designation)
        callout = ThreadCallout(
            designation=designation,
            nominal_mm=spec.nominal_mm if spec else 0.0,
            pitch_mm=spec.pitch_mm if spec else 0.0,
            source=source,
        )
        first, _ = _ref_shape_labels(label)
        for ref in first:
            shape = xde.shape_of(ref)
            if shape is None:
                continue
            faces = [shape] if shape.ShapeType() == TopAbs_FACE else _faces_of(shape)
            for face in faces:
                face_id = resolver.identity_id(face)
                if face_id:
                    out[face_id] = callout
    return out


# =============================================================================
# The whole import
# =============================================================================


def read_gdt(path: str, analysis_shape: TopoDS_Shape) -> GdtResult:
    """Every PMI element of a STEP file, against the analysis shape's face ids."""
    xde = XdeDocument(path)
    return _read(xde, FaceResolver(xde.solids, analysis_shape))


def _read(xde: XdeDocument, resolver: FaceResolver) -> GdtResult:
    entity_faces = EntityFaces(xde, resolver)
    kernel = KernelBridge(xde)

    def guarded(step, default):
        # Every raw-model extractor is best-effort in the reference, and a
        # malformed file must not cost the rest of the PMI.
        try:
            return step()
        except Exception:
            return default

    finishes = guarded(lambda: extract_surface_finishes(xde, entity_faces), [])
    datum_faces = guarded(lambda: extract_datum_faces(xde, entity_faces), {})
    datum_targets = guarded(lambda: extract_datum_targets(xde.text), {})
    notes, names = guarded(lambda: extract_semantic_notes(xde.text), ([], set()))
    dimension_values = guarded(lambda: extract_dimension_values(xde, entity_faces), [])

    result = guarded(lambda: parse_gdt(xde, resolver, kernel), GdtResult())
    if finishes:
        result.surface_finishes = finishes
        result.has_pmi = True
    apply_datum_target_fallback(result, datum_targets)
    apply_datum_face_fallback(result, datum_faces)
    drop_phantom_dimensions(result, xde.text)
    fold_semantic_notes(result, notes, names)
    fold_dimension_values(result, dimension_values)
    return result


def read_step_pmi(path: str, analysis_shape: TopoDS_Shape) -> AnnotationSet:
    """The PMI of a STEP file as the annotation set the rules read.

    ``analysis_shape`` is the shape the analysis will run on -- the face ids
    in the result are its :class:`~...utils.geometry.FaceIndex` ids. Thread
    callouts ride along in :attr:`~..annotations.AnnotationSet.threads`.
    """
    xde = XdeDocument(path)
    resolver = FaceResolver(xde.solids, analysis_shape)
    callouts = to_annotation_set(_read(xde, resolver))
    try:
        semantic = extract_thread_callouts(xde, resolver)
    except Exception:
        semantic = {}
    # Callouts the XCAF reader cannot see -- presentation text and shape
    # aspect names -- merged under the semantic ones, as the reference does.
    try:
        from .thread_text import extract_thread_text_callouts, merge_text_callouts

        text = extract_thread_text_callouts(xde, resolver, EntityFaces(xde, resolver))
    except Exception:
        text = {}
    callouts.threads = merge_text_callouts(semantic, text) if text else semantic
    return callouts
