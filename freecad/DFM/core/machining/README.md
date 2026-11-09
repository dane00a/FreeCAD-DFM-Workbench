# core/machining: feature recognition for machined and sheet-metal parts

Pure OCP (cadquery-ocp). Nothing here imports FreeCAD, Part or Qt at module
scope, which `tests/test_core_imports.py` enforces; the few functions in
`thread_sources` that write to a FreeCAD document import it inside the
function. It takes a `TopoDS_Shape` and finds holes, threads,
pockets, slots, grooves, steps, bosses, ribs, blends, turned profiles,
markings, knurls and sheet-metal bends and forms, and classifies the part as
milled, turned, mill-turn or sheet metal.

It is a port of a C++ reference engine and is held to that engine's approved
results: `tests/fixtures/check_golden.py` runs all 280 reference fixtures and
compares them with the reference's own goldens. `docs/resync-ledger.md`
records how each reference change was carried over, and
`docs/parity-tasks.md` the known differences. Where it and
`core/recognition` (moulding and 3D printing) both recognise something, they
do so independently; neither reads the other's features.

```python
from freecad.DFM.core.machining.aag_builder import AagBuilder
from freecad.DFM.core.machining.config import MachiningConfig
from freecad.DFM.core.utils.geometry import FaceIndex

face_index = FaceIndex(shape)
graph = AagBuilder(shape, face_index).build()
```

In the workbench the whole pass runs as `MACHINING_ANALYZER`
(`core/analyzers/machining_analyzer.py`), which returns one
`MachiningContext`: the face graph, the configuration, the process
classification and the recognised features. Every machining and sheet rule
reads that one context.

* **Face and edge numbers** are 1-based, from `core.utils.geometry.FaceIndex` /
  `EdgeIndex`, the same as FreeCAD's `Face<n>` / `Edge<n>` and as
  `core/recognition`.
* **Caching:** `cache.MACHINING_CACHE` keys a context by the shape's face
  fingerprint (shared with `core/recognition`), an edge fingerprint, the
  configuration, what the part states about itself (threads, PMI) and
  `ENGINE_VERSION`. A changed shape, threshold, confirmed thread or engine
  all miss.
* **Engine version:** `_version.ENGINE_VERSION` is the reference engine's
  version this one matches. It is part of every cache key and every recorded
  baseline (`core/baselines`), so bump it with the reference.
* **Thresholds:** `config.MachiningConfig.from_preferences(prefs)` reads the
  shop's preferences: tool library, drill sizes, machine mode, unit system,
  blank form and every rule threshold.
* **Inputs the part states:** a FreeCAD document can declare tapped holes and
  the stock a part is cut from (`thread_sources`, `blank_declaration`); a STEP
  file's PMI carries GD&T and thread callouts (`pmi/`). Absent either, the
  geometry decides.

## Layout

| Module | What it does |
|---|---|
| `aag.py`, `aag_builder.py` | The attributed adjacency graph: faces, shared edges, convexity, surface parameters |
| `process_classifier.py` | Milled, turned, mill-turn or sheet metal |
| `recognizers/` | One recognizer per feature family, run in `RECOGNIZER_PIPELINE` order |
| `resolver.py` | Settles faces two recognizers both claim |
| `sheet_folds.py`, `sheet_ftg.py` | Fold families and the fold census for sheet parts |
| `pmi/` | GD&T and thread callouts read from STEP files |
| `features.py`, `context.py` | The feature types and the context every rule reads |
| `config.py` | Thresholds, tools and preferences |
| `cache.py`, `_version.py` | The per-shape cache and the engine version |

## Tests

```sh
# unit tests (plain Python + cadquery-ocp)
python -m pytest -q tests
# every reference fixture against the reference's approved goldens
python tests/fixtures/check_golden.py --jobs 6 --quiet
```
