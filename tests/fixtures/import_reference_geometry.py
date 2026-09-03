# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""One-time import of structural measurements from the reference fixture corpus.

Reads the STEP files the reference engine generates and records, per fixture,
the measurements that are independent of any rule engine: face count, edge
count, volume and bounding box. Those become a conformance oracle for the
ported Python fixture builders -- a builder that produces a different but
plausible solid is otherwise very hard to catch by reading code.

Usage::

    python tests/fixtures/import_reference_geometry.py <step_dir> [out.json]

The output JSON is committed; this script is not needed to run the tests, only
to regenerate the oracle if the corpus changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from OCP.BRep import BRep_Builder
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape


def read_step(path: Path):
    """Load a STEP file into a single shape, or None if it cannot be read."""
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        return None
    reader.TransferRoots()
    shape = reader.OneShape()
    return None if shape.IsNull() else shape


def measure(shape) -> dict:
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, faces)
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edges)
    solids = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)

    volume_props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, volume_props)
    area_props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, area_props)

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        bbox = None
    else:
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        bbox = [
            round(xmax - xmin, 4),
            round(ymax - ymin, 4),
            round(zmax - zmin, 4),
        ]

    return {
        "face_count": faces.Extent(),
        "edge_count": edges.Extent(),
        "solid_count": solids.Extent(),
        "volume_mm3": round(volume_props.Mass(), 4),
        "area_mm2": round(area_props.Mass(), 4),
        "bbox_mm": bbox,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    step_dir = Path(argv[1])
    out_path = Path(argv[2]) if len(argv) > 2 else Path(__file__).parent / "geometry_oracle.json"

    if not step_dir.is_dir():
        print(f"Not a directory: {step_dir}")
        return 1

    records: dict[str, dict] = {}
    failures: list[str] = []
    step_files = sorted(step_dir.glob("*.step"))

    for index, path in enumerate(step_files, start=1):
        name = path.stem
        try:
            shape = read_step(path)
            if shape is None:
                failures.append(name)
                continue
            records[name] = measure(shape)
        except Exception as exc:  # a malformed fixture must not stop the import
            failures.append(f"{name}: {exc}")
        if index % 25 == 0:
            print(f"  {index}/{len(step_files)}")

    out_path.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {len(records)} fixtures to {out_path}")
    if failures:
        print(f"{len(failures)} could not be read: {failures[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
