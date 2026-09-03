# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Build the test corpus, and check that it is the corpus it should be.

The machining rules were developed against a couple of hundred parts, and
that corpus is what makes a change to a recognizer safe: run it over all of
them and see what moved. The parts are commercial work and are not
distributed, so what lives in this repository is the recipe rather than the
result -- each one built from boxes, cylinders and booleans.

`geometry_oracle.json` is what makes those the same parts rather than merely
similar ones. It records face, edge and solid counts, volume, area and
bounding box for each, measured from the originals. A builder that drifts by
a millimetre fails against the recorded volume, and one that fuses its tools
in a way that leaves a seam fails against the face count.

    python tests/fixtures/generate_fixtures.py --verify
    python tests/fixtures/generate_fixtures.py --out build/fixtures
    python tests/fixtures/generate_fixtures.py --verify simple_box deep_hole

Needs FreeCAD's Python, or any interpreter with cadquery-ocp installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))
sys.path.insert(0, _HERE)

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

from builders import load_all  # noqa: E402


ORACLE = os.path.join(_HERE, "geometry_oracle.json")

# How far a rebuilt part may differ from the recorded one. Counts must match
# exactly -- a different face count is a different topology, and the
# recognizers work on topology. The measurements carry a relative tolerance
# because a boolean is not bit-reproducible across OpenCascade versions.
_RELATIVE_TOLERANCE = 5e-4
_ABSOLUTE_TOLERANCE = 1e-3

#: Fixtures whose edge count differs from the recording for a reason that is
#: understood and is not a fault in the recipe.
#:
#: All five are lofted-hood louvers whose crest runs out flush into the deck.
#: That run-out is a near-tangential boolean, and it leaves slivers -- the
#: measured extras here are between 3e-5 and 8e-4 mm long, sitting exactly at
#: the run-outs. How many of those a kernel merges away is a version
#: question, and the recordings were made against a different OpenCascade
#: build from the one in FreeCAD 1.1.
#:
#: Everything that describes the part rather than its tessellation -- volume,
#: area, bounding box, face count, solid count -- matches to seven or eight
#: significant figures, which is what makes these the same parts. Listed
#: individually rather than relaxed globally, because an unexplained edge
#: count anywhere else is still a fault worth failing on.
_KNOWN_EDGE_DIFFERENCES = {
    "sm_louver_standard",
    "sm_louver_dome",
    "sm_louver_standard_sym",
    "sm_louver_bank_curved",
    "sm_hd_bracket",
}


def count(shape: TopoDS_Shape, kind) -> int:
    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, found)
    return found.Extent()


def measure(shape: TopoDS_Shape) -> dict:
    """The same handful of numbers the oracle records."""
    volume = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, volume)
    area = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, area)

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()

    return {
        "face_count": count(shape, TopAbs_FACE),
        "edge_count": count(shape, TopAbs_EDGE),
        "solid_count": count(shape, TopAbs_SOLID),
        "volume_mm3": round(volume.Mass(), 3),
        "area_mm2": round(area.Mass(), 4),
        "bbox_mm": [
            round(xmax - xmin, 4),
            round(ymax - ymin, 4),
            round(zmax - zmin, 4),
        ],
    }


def close_enough(built: float, recorded: float) -> bool:
    return abs(built - recorded) <= max(
        _ABSOLUTE_TOLERANCE, _RELATIVE_TOLERANCE * abs(recorded)
    )


def compare(built: dict, recorded: dict, name: str = "") -> tuple[list[str], list[str]]:
    """What differs between a rebuilt part and the recorded one.

    Returns the differences that are faults, and separately the ones already
    understood, so a known kernel artefact does not read as a broken recipe.
    """
    problems: list[str] = []
    known: list[str] = []
    for key in ("face_count", "edge_count", "solid_count"):
        if built[key] == recorded[key]:
            continue
        message = f"{key} {built[key]} vs {recorded[key]}"
        if key == "edge_count" and name in _KNOWN_EDGE_DIFFERENCES:
            known.append(message)
        else:
            problems.append(message)
    for key in ("volume_mm3", "area_mm2"):
        if not close_enough(built[key], recorded[key]):
            problems.append(f"{key} {built[key]:.3f} vs {recorded[key]:.3f}")
    for axis, (a, b) in enumerate(zip(built["bbox_mm"], recorded["bbox_mm"])):
        if not close_enough(a, b):
            problems.append(f"bbox[{'xyz'[axis]}] {a:.3f} vs {b:.3f}")
    return problems, known


def write_step(shape: TopoDS_Shape, path: str) -> None:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    writer.Write(path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "names", nargs="*", help="fixtures to build (default: all registered)"
    )
    parser.add_argument("--out", help="write STEP files to this directory")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check each built part against the recorded geometry",
    )
    parser.add_argument(
        "--list", action="store_true", help="list the registered fixtures and stop"
    )
    args = parser.parse_args(argv)

    registry = load_all()
    if args.list:
        for name in sorted(registry):
            print(name)
        return 0

    oracle = {}
    if args.verify:
        with open(ORACLE, encoding="utf-8") as handle:
            oracle = json.load(handle)

    names = args.names or sorted(registry)
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    built = failed = unverified = tolerated = 0
    for name in names:
        builder = registry.get(name)
        if builder is None:
            print(f"  ?  {name}: no builder registered")
            failed += 1
            continue
        try:
            shape = builder()
        except Exception as exc:  # a broken recipe must name itself
            print(f"  !  {name}: {type(exc).__name__}: {exc}")
            failed += 1
            continue

        built += 1
        if args.out:
            write_step(shape, os.path.join(args.out, name + ".step"))

        if not args.verify:
            continue
        recorded = oracle.get(name)
        if recorded is None:
            unverified += 1
            print(f"  -  {name}: not in the oracle")
            continue
        problems, known = compare(measure(shape), recorded, name)
        if problems:
            failed += 1
            print(f"  X  {name}: {'; '.join(problems)}")
        elif known:
            tolerated += 1
            print(f"  ~  {name}: {'; '.join(known)} (known kernel difference)")

    print(f"\nbuilt {built} of {len(names)}")
    if args.verify:
        matched = built - failed - unverified - tolerated
        print(f"matched the oracle: {matched}   differed: {failed}", end="")
        if tolerated:
            print(f"   known differences: {tolerated}", end="")
        print(f"   not recorded: {unverified}" if unverified else "")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
