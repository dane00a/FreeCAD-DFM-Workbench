# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""OpenCascade names that moved between OCCT 7.x and 8.x, under one spelling.

FreeCAD 1.1 ships OCCT 7.8 and is moving to 8.0.1. OCP 8 replaced the
per-type collection classes (``TopTools_IndexedMapOfShape``,
``TColgp_Array1OfPnt``, ...) with templated ones in ``OCP.collections``,
and OCCT 8 dropped ``BRepTopAdaptor_FClass2d`` in favour of the
``IntTools_FClass2d`` it duplicated (same constructor, same ``Perform``).

Import these names from here rather than from OCP, and the code runs on
either kernel. Each alias is the class the kernel's own API takes, so a
map built here can be handed to ``TopExp.MapShapes_s`` and the like.
"""

try:  # OCCT 7.x
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d as FaceClassifier2d
    from OCP.TColgp import (
        TColgp_Array1OfPnt,
        TColgp_Array2OfPnt,
        TColgp_HArray1OfPnt,
    )
    from OCP.TDF import TDF_LabelSequence
    from OCP.TopTools import (
        TopTools_IndexedDataMapOfShapeListOfShape,
        TopTools_IndexedMapOfShape,
        TopTools_ListOfShape,
    )

    OCCT_MAJOR = 7
except ImportError:  # OCCT 8.x
    from OCP.collections import (
        Array1_gp_Pnt as TColgp_Array1OfPnt,
        Array2_gp_Pnt as TColgp_Array2OfPnt,
        HArray1_gp_Pnt as TColgp_HArray1OfPnt,
        IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as TopTools_IndexedDataMapOfShapeListOfShape,
        IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as TopTools_IndexedMapOfShape,
        List_TopoDS_Shape as TopTools_ListOfShape,
        Sequence_TDF_Label as TDF_LabelSequence,
    )
    from OCP.IntTools import IntTools_FClass2d as FaceClassifier2d

    OCCT_MAJOR = 8

#: The 7.x spelling, for code that keeps its original names.
BRepTopAdaptor_FClass2d = FaceClassifier2d

# TopoDS's downcasts: static methods of a class in OCP 7 (``TopoDS.Face_s``),
# free functions of a namespace in OCP 8 (``TopoDS.Face``). This offers the
# 7.x spelling on both.
if OCCT_MAJOR == 7:
    from OCP.TopoDS import TopoDS
else:
    from OCP.TopoDS import TopoDS as _TopoDSNamespace

    class TopoDS:  # noqa: N801 -- stands in for the OCP name
        """``TopoDS.Face_s(shape)`` and friends, over OCCT 8's namespace."""

        Vertex_s = staticmethod(_TopoDSNamespace.Vertex)
        Edge_s = staticmethod(_TopoDSNamespace.Edge)
        Wire_s = staticmethod(_TopoDSNamespace.Wire)
        Face_s = staticmethod(_TopoDSNamespace.Face)
        Shell_s = staticmethod(_TopoDSNamespace.Shell)
        Solid_s = staticmethod(_TopoDSNamespace.Solid)
        CompSolid_s = staticmethod(_TopoDSNamespace.CompSolid)
        Compound_s = staticmethod(_TopoDSNamespace.Compound)

__all__ = [
    "BRepTopAdaptor_FClass2d",
    "FaceClassifier2d",
    "OCCT_MAJOR",
    "TColgp_Array1OfPnt",
    "TColgp_Array2OfPnt",
    "TColgp_HArray1OfPnt",
    "TDF_LabelSequence",
    "TopTools_IndexedDataMapOfShapeListOfShape",
    "TopTools_IndexedMapOfShape",
    "TopTools_ListOfShape",
    "TopoDS",
]


def _repair_bnd_box_get() -> None:
    """Give ``Bnd_Box.Get()`` back its six numbers where the binding lost them.

    OCCT 8 returns the limits as a ``Bnd_Box::Limits`` struct, which OCP 8.0.1
    does not register, so the call raises instead of returning. Every caller
    unpacks the 7.x tuple ``(xmin, ymin, zmin, xmax, ymax, zmax)``; the
    corners carry the same values, gap included, so they stand in for it.
    Only patched when the binding is actually broken, and a void box still
    raises as it always did.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.gp import gp_Pnt

    probe = Bnd_Box()
    probe.Add(gp_Pnt(0.0, 0.0, 0.0))
    try:
        probe.Get()
        return
    except TypeError:
        pass

    def get(self):
        lo, hi = self.CornerMin(), self.CornerMax()
        return lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()

    Bnd_Box.Get = get


if OCCT_MAJOR >= 8:
    _repair_bnd_box_get()
