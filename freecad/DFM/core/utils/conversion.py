# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

import os
import tempfile
import Part

from OCP.TopoDS import TopoDS_Shape
from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools


def ocp_to_freecad(ocp_shape: TopoDS_Shape) -> Part.Shape:
    """
    Converts an OCP TopoDS_Shape back to a FreeCAD Part.Shape via a temporary file.

    The reverse of freecad_to_ocp, and it goes through BRep for the same
    reason: FreeCAD is built against its own OpenCascade, so the two
    libraries' TopoDS_Shape are unrelated C++ types that cannot be handed
    across directly however alike they look. The serialised form is the only
    thing both of them agree on.

    Only the shapes built by the test fixtures need this today -- they are
    OCP all the way down, and putting one in the 3D view means putting it in
    a document first.
    """
    if ocp_shape is None or ocp_shape.IsNull():
        raise ValueError("Cannot convert an empty or Null OCP shape.")

    fd, temp_path = tempfile.mkstemp(suffix=".brep")

    try:
        BRepTools.Write_s(ocp_shape, temp_path)

        fc_shape = Part.Shape()
        fc_shape.importBrep(temp_path)

        if fc_shape.isNull():
            raise ValueError("FreeCAD failed to load the BRep file.")

        return fc_shape

    finally:
        os.close(fd)
        if os.path.exists(temp_path):
            os.remove(temp_path)


def freecad_to_ocp(fc_shape: Part.Shape) -> TopoDS_Shape:
    """
    Converts a FreeCAD Part.Shape to an OCP TopoDS_Shape via a temporary file.
    """
    if fc_shape is None or fc_shape.isNull():
        raise ValueError("Cannot convert an empty or Null FreeCAD shape.")

    fd, temp_path = tempfile.mkstemp(suffix=".brep")

    try:
        fc_shape.exportBrep(temp_path)

        ocp_shape = TopoDS_Shape()
        builder = BRep_Builder()

        BRepTools.Read_s(ocp_shape, temp_path, builder)

        if ocp_shape.IsNull():
            raise ValueError("OCP failed to load the BRep file.")

        return ocp_shape

    finally:
        os.close(fd)
        if os.path.exists(temp_path):
            os.remove(temp_path)
