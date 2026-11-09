# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Straight knurls on turned shafts.

A knurl is hundreds of tiny faces that mean one thing, and the recognizer
has to say that one thing rather than report every groove as a step. On a
cylinder a single family of axial grooves only counts as a knurl because the
grooves close the circumference, so how much of the circle they actually
cover is what these parts are about: one shaft whose bands wrap far enough,
one interrupted by a keyway and still wrapping far enough, and one with a
wrench flat that takes too much of the circle away.

Each groove is cut by a box rather than a cylinder or cone. A box leaves a
flat floor whose normal points radially and two flat flanks, which are the
only walls that carry a groove direction; it also keeps the hundreds of
booleans out of the curved-tool cases that lose faces silently. All the
cutters go into one multi-argument fuse and one cut, which is far cheaper
than cutting forty grooves one at a time.
"""

from __future__ import annotations

import math

from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_ListOfShape

from . import fixture
from .shapes import box_between, cut, cylinder, fuse


# -- private vocabulary -------------------------------------------------------


def _groove_cutter(
    r_floor: float, r_out: float, half_width: float, z0: float, z1: float, deg: float
) -> TopoDS_Shape:
    """One axial knurl groove's cutter, turned ``deg`` degrees about Z.

    A box from the groove floor out past the crest, ``2 * half_width`` wide
    and running ``z0`` to ``z1``, laid on +X and then rotated into place.
    """
    blank = box_between((r_floor, -half_width, z0), (r_out, half_width, z1))
    spin = gp_Trsf()
    spin.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(deg))
    return BRepBuilderAPI_Transform(blank, spin, True).Shape()


def _fuse_all(parts: list) -> TopoDS_Shape:
    """Every part fused in one multi-argument pass, the first as argument."""
    if len(parts) == 1:
        return parts[0]
    arguments = TopTools_ListOfShape()
    arguments.Append(parts[0])
    tools = TopTools_ListOfShape()
    for part in parts[1:]:
        tools.Append(part)
    operation = BRepAlgoAPI_Fuse()
    operation.SetArguments(arguments)
    operation.SetTools(tools)
    operation.Build()
    if not operation.IsDone():
        raise RuntimeError("knurl cutter fuse failed")
    return operation.Shape()


# -- parts --------------------------------------------------------------------


@fixture("knurled_grip_shaft")
def build_knurled_grip_shaft(seam_deg: float = 0.0) -> TopoDS_Shape:
    """A stepped grip with two straight knurl bands, one of them keyed.

    A 14 mm grip on a 10 mm shaft, 62 long, with two bands of twenty 0.5 mm
    grooves 0.3 deep on an 18 degree spacing, separated by a relief neck.
    The lower band is uninterrupted and wraps as far as twenty grooves can.
    The upper one is the same knurl with a 5 mm keyway through it, which
    swallows two grooves whole and still leaves enough of the circle for the
    knurl to be accepted -- an accept that is not at the ceiling, so a
    tightened wrap threshold shows up as this band dropping out on its own.

    The keyway's floor sits at R5.1: deep enough that its walls are too tall
    to be read as groove flanks and its floor too low to pass for a groove
    bottom, and just clear of the neck so the cut never grazes it.
    """
    crest = 7.0
    neck = 5.0
    floor = crest - 0.3
    reach = crest + 2.0
    seam = seam_deg

    body = cylinder(0.0, 0.0, 0.0, neck, 12.0, seam=seam)
    for z0, radius, height in (
        (12.0, crest, 18.0),  # band A, uninterrupted
        (30.0, neck, 4.0),  # relief neck between the bands
        (34.0, crest, 18.0),  # band C, interrupted by the keyway
        (52.0, neck, 10.0),  # shank
    ):
        body = fuse(body, cylinder(0.0, 0.0, z0, radius, height, seam=seam))

    tools = []
    # Band A. The grooves overrun both band ends by 1 mm so the crest is
    # cut right through rather than leaving a skin at either end.
    for k in range(20):
        tools.append(_groove_cutter(floor, reach, 0.25, 11.0, 31.0, k * 18.0))
    # Band C, offset half a pitch so the keyway takes exactly two grooves
    # whole instead of leaving a ragged half-groove at its edge.
    for k in range(20):
        tools.append(_groove_cutter(floor, reach, 0.25, 33.0, 53.0, 9.0 + k * 18.0))
    tools.append(box_between((5.1, -2.5, 33.0), (reach, 2.5, 53.0)))

    return cut(body, _fuse_all(tools))


@fixture("knurl_neg_flatted_shaft")
def build_knurl_neg_flatted_shaft(seam_deg: float = 0.0) -> TopoDS_Shape:
    """A knurled shaft with a wrench flat, which must not be claimed as a knurl.

    An ordinary part: an 18 mm band of twenty-four 0.4 mm grooves on a
    15 degree spacing, with one flat milled 5.4 mm off the axis. The flat
    takes about 79 degrees of the circle, leaving too little wrap for a
    single groove family to count as a knurl, so the knurl rule has to stay
    quiet and the surviving grooves fall to whatever else claims them.

    The 5.4 mm puts the flat's edge just clear of a groove flank, so six
    grooves vanish whole and eighteen survive whole, rather than one being
    left as a ragged half-groove.
    """
    crest = 7.0
    neck = 5.0
    floor = crest - 0.3
    reach = crest + 2.0
    flat = 5.4
    seam = seam_deg

    body = cylinder(0.0, 0.0, 0.0, neck, 6.0, seam=seam)
    body = fuse(body, cylinder(0.0, 0.0, 6.0, crest, 18.0, seam=seam))
    body = fuse(body, cylinder(0.0, 0.0, 24.0, neck, 10.0, seam=seam))

    tools = [
        _groove_cutter(floor, reach, 0.20, 5.0, 25.0, 7.5 + k * 15.0) for k in range(24)
    ]
    # The flat overruns the band at both ends, harmlessly: past the band the
    # shaft is only 5 mm in radius and the flat starts at 5.4.
    tools.append(box_between((flat, -12.0, 5.0), (12.0, 12.0, 25.0)))

    return cut(body, _fuse_all(tools))
