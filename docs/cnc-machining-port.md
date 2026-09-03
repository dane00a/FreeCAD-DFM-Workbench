<!-- SPDX-License-Identifier: LGPL-2.1-or-later -->
<!-- SPDX-FileNotice: Part of the DFM addon. -->

# CNC Machining Capability — Port Plan

Adding CNC milling and turning DFM to the workbench by porting a validated
C++/OpenCascade engine to Python.

**Branch:** `feat/cnc-machining`
**Reference:** `brooklineind-website/apps/geometry-service` — read-only source
material belonging to a separate project. It is a specification, never a repo
to edit.

---

## 1. What the reference engine is

~40,000 lines of C++ over OpenCascade, validated against 227 approved
regression baselines. Its layering:

| Layer | Content | Kernel-heavy? |
|---|---|---|
| AAG builder | Attributed Adjacency Graph over the B-rep: nodes = faces with surface type + analytic parameters; edges = shared edges with dihedral angle and CONCAVE/CONVEX/TANGENT classification | **Yes** — 102 OpenCascade references |
| Recognizers | 21 recognizers, seed-and-grow, fixed order → 34 feature types | **Almost none** |
| Resolver | Containment/intersection dedup across features | No |
| Process classifier | MILLED / TURNED / MILL_TURN / SHEET_METAL + axis of revolution | No |
| Rules engine | 89 rules in 15 categories, ~98 configurable thresholds | Partly |

### The structural finding that shapes everything

**Fifteen of the twenty machining recognizers make zero OpenCascade calls.**
They are pure graph algorithms over the AAG. Kernel work is confined to three
places: the AAG builder (once per part), tool reachability ray casts (a few
hundred per part), and five `occt_index → TopoDS_Face` lookups that should be
replaced by a dict during the port anyway.

Once the AAG exists in Python, the recognizer layer is ordinary
graph-and-vector code. **The port is a transcription problem, not a kernel
problem** — which is what makes it tractable at Python speed.

### Verified counts

The reference's own docs disagree with themselves (rule counts appear as 65,
75, and 89; fixtures as 164, 180, 190, 214, 227). The authoritative sources are
the code itself: `DFMRulesEngine::create_default()` for rules, the recognizer
sequence in `run_analyze()` for pipeline order. Counts quoted in this document
were verified against source, not docs.

---

## 2. Scope

### In

- AAG construction over a FreeCAD `Part::Feature` shape
- Feature recognition for milling and turning
- Part-process classification (MILLED / TURNED / MILL_TURN)
- The machining DFM rule families: hole, thread, pocket, slot, thin_feature,
  tool_access, setup, part, freeform, blend, boss, rib — 58 rules

### Out

| Excluded | Why |
|---|---|
| Sheet-metal recognizers + 24 `sheet` rules | Mutually exclusive with machining by classification; separate product area |
| GD&T / PMI extraction + 7 `gdt` rules | Requires AP242 semantic PMI from a STEP file. A FreeCAD document carries no PMI, so these rules can never fire |
| GLB export, GLB validator, edge extractor | Render output for a web viewer. FreeCAD draws B-rep edges natively |
| HTTP server, job queue, model upload | The task panel replaces it |
| `mapping.json` writer | Maps face IDs to GLB primitive indices for the web viewer |
| STEP import + shape healing | Geometry comes from the live document. See §6.3 |

Roughly 40% of the reference by line count is service plumbing or out-of-scope
families. The machining analysis core is closer to 20,000 lines.

---

## 3. How it maps onto this workbench

The existing analyzer/check split accommodates this better than expected.
`AnalysisRunner` caches analyzer results by ID and shares them across every
check that declares the same `required_analyzer_id`, and the result value is
opaque (`Any`). So:

```
MACHINING_ANALYZER  (one analyzer, runs once)
    │  builds AAG → runs recognizers → resolves → classifies process
    │  returns a MachiningContext
    ▼
MachiningCheck subclasses  (one per ported rule, all sharing that analyzer)
    │  emit CheckResult with failing_geometry = [("Face", idx), ...]
    ▼
existing _resolve_geometry_refs → GeometryRef → 3D highlighting, history, CSV
```

No changes to `AnalysisRunner` are required for this to work. Findings flow
into the existing results panel, history diffing, and viewport highlighting for
free.

### Proposed layout

```
freecad/DFM/core/machining/
    aag.py                  AagNode, AagEdge, AttributedAdjacencyGraph
    aag_builder.py          the only kernel-heavy module
    features.py             FeatureType constants, FeatureInstance
    recognizers/            one module per recognizer, self-registering
    resolver.py             InteractingFeatureResolver
    process_classifier.py   MILLED / TURNED / MILL_TURN
    config.py               MachiningConfig, RuleThresholds, tool library
core/analyzers/machining_analyzer.py     @register_analyzer("MACHINING_ANALYZER")
core/checks/machining/                   one check per ported rule
core/processes/cnc_milling.yaml
core/processes/cnc_turning.yaml
```

### Four mismatches to resolve

**1. Rule shapes.** The workbench's `RuleShape` covers threshold comparisons.
Most ported rules fit (see §5), but several need shapes that do not exist:

| Needed shape | For | What it does |
|---|---|---|
| `SET_MEMBER` | `hole_nonstandard_diameter`, `slot_nonstandard_width`, `hole_countersink_angle`, `chamfer_nonstandard_angle` | Value must match a catalog entry (drill chart, end-mill diameters, standard angles) within a tolerance; the finding reports the *nearest* catalog value |
| `COMPOUND_AND` | `slot_overhang` (2 conjuncts), `thin_wall` aspect path (4) | Several ratios that must *all* trip before the rule fires |
| `REPORT` | `freeform_finishing`, `feature_complexity` | No limit; emits a computed plan (stepover, toolpath length, op count) |

**2. Thresholds.** The workbench models per-material `{target, limit}`. The
reference has ~98 thresholds, many read by rules that also take a limit derived
from the tool library rather than the material. Recommended split:

- **Material block** — the headline threshold a shop tunes per material
  (`hole_deep_warn_ratio`, `thin_wall_warn_mm`, …)
- **Process-level `machining:` block in the YAML** — the rest, plus the tool
  library and drill catalogs, which are shop capability rather than material
  property

**3. Process branching.** Six rules measure a *different quantity* against
*different thresholds* depending on MILLED vs TURNED. `part_aspect_ratio` is
the extreme: length/diameter against 4.0/8.0 on a turned part, bbox
longest/shortest against 8.0 on a milled one, then branches the message again
on plate-vs-bar. Either allow per-process threshold blocks inside a rule's
material entry, or split into separate rule IDs. Per-process blocks preserve
parity with the reference baselines; splitting reads better in the rule cards.

**4. No SUCCESS severity in the source.** The reference emits nothing when a
rule passes, and `return {}` means *both* "passed" and "not applicable" (wrong
process, missing parameter, suppressed by a sibling rule). The workbench
displays passed rules. Porting needs an explicit third state so a rule that
never ran is not reported as passing.

Also worth adding: an open `extra: dict` on `CheckResult`. The reference's
findings carry structured evidence (`occurrence_count`, `edm_candidate`,
`nearest_standard_mm`, `runout_basis`, per-face radius arrays) that the current
`value`/`limit`/`comparison`/`unit` fields cannot hold, and which would
otherwise degrade into message text.

### New process requirement

`ProcessRequirement` has no turning axis. The classifier derives an axis of
revolution automatically, so this is a manual *override* rather than a required
input — but lathe work needs it when the classifier is uncertain.

---

## 4. Phasing

Each phase is independently shippable and testable.

### Phase 0 — Foundation

The AAG builder, and nothing else. This is the gate on all subsequent work and
the only part where OpenCascade expertise matters.

Scope-cut aggressively: of `aag_builder.cpp`'s 1469 lines, roughly **250 are
DFM-relevant**. The rest builds silhouette data for a WebGL viewer — cylinder
rim point arrays, cone generatrix endpoints, torus meridian profiles,
multi-plane sphere clipping. All of it drops. The one exception is
`sphere_has_clip` / `sphere_clip_normal` / `sphere_clip_offset`, which the
spherical-pocket recognizer depends on.

Deliverable: an AAG over any solid, validated by the concavity census (§6.1).

### Phase 1 — AAG-only rules

Ten rules that need no feature recognition at all, giving a useful milling
*and* turning verdict before a single recognizer exists:

`part_aspect_ratio` · `no_datum_face` · `no_parallel_datum_pair` ·
`thin_clamping_dimension` · `small_part_holding` · `material_removal_high` ·
`thin_wall` (planar passes) · `sealed_void` (shell path) · `setup_count_high` ·
`feature_complexity`

Also in this phase: the process classifier, the config/threshold schema, the
tool library, and the engine loop (registration order, aggregation with
`Nx:` collapsing, severity-descending stable sort).

The classifier must come **before** any rule — a dozen rules branch on it, and
getting it wrong makes turning results actively wrong rather than merely
absent.

### Phase 2 — Holes and threads

The core of CNC DFM. `HoleRecognizer` is the single largest port (1789 lines,
six output types, three merge passes) but drives six of the fifteen
most-frequently-firing rules.

Unlocks: `hole_deep_risk` (including the turned boring-bar branch),
`hole_edge_distance`, `hole_web_thickness`, `hole_flat_bottom`,
`hole_intersecting`, `hole_nonstandard_diameter`, plus `thin_wall`'s
hole-to-plane and hole-to-hole passes.

**Threads need a decision.** The reference recognizes a thread only from AP242
PMI, modeled helical geometry, or explicit user confirmation — never from
diameter. That was a deliberate product ruling after false positives on
clearance, reamed, and dowel holes. With no PMI in FreeCAD, threads come from
modeled helices or a user override. The override is cheap to build and worth
having; without it the entire thread family is dead.

### Phase 3 — Pockets, slots, blends

`PocketRecognizer` (961 lines, BFS + floor re-selection + ~20 guards),
`SlotRecognizer`, `BlendRecognizer`, `UndercutRecognizer`, and the
`InteractingFeatureResolver`.

Unlocks `pocket_square_corner` (the third most-fired rule), `pocket_deep_risk`,
`pocket_aspect_ratio`, `pocket_narrow_opening`, `slot_*`, `cutter_radius_*`,
`undercut_present`, `tool_access_*`.

At the end of this phase a milled part gets a genuinely useful analysis.

### Phase 4 — Turning completion

`GrooveRecognizer` (O-ring glands, retaining-ring grooves, thread relief —
this *is* lathe DFM), `ExternalThreadRecognizer` (116 lines),
`TurnedProfileRecognizer` (165 lines), and
`refine_part_process_with_features`.

Three of those four are among the smallest files in the reference. Turning DFM
is essentially: classify the part correctly, find the bores, find the grooves,
find the threads, know where the profile is.

### Phase 5 — Breadth

`MarkingRecognizer` first, and before showing anyone a part with a serial
number on it — its purpose is defensive. Without it, every engraved character
is claimed as a slot, undercut, or boss and the results panel drowns. Then
Rib / Boss / Step (thin-feature and setup rules), Slit / ThroughCavity
(misclassification fixes), and finally SphericalPocket / Channel / Draft /
Pattern.

### Deliberately deferred

**`sharp_internal_edge`** — ~1150 lines with a five-sample × six-ray
accessibility vote and a dozen suppression paths. It is the second
most-fired rule and the single biggest porting cost in the engine, and it
emits WARN only. Port it after Phases 1–3 are stable; without its suppression
logic it buries every other finding.

---

## 5. Rule shape mapping

Of the 58 machining rules:

| Fit | Count | Examples |
|---|---|---|
| `TARGET_AND_LIMIT` | 8 | `hole_deep_risk`, `pocket_deep_risk`, `thin_wall` (absolute), `material_removal_high` |
| `LIMIT_ONLY` | 18 | `hole_edge_distance`, `slot_deep_risk`, `rib_height_aspect`, `minimum_feature_size` |
| `BINARY` | 19 | `undercut_present`, `sealed_void`, `tool_access_blocked`, `hole_flat_bottom` |
| Needs a new shape | 13 | see §3 |

Four rules of the "derived limit" kind (`pocket_aspect_ratio`,
`cutter_radius_infeasible`, `freeform_internal_radius`,
`turned_profile_radius`) compute their limit from the tool library rather than
the material. These need the limit to be resolvable at evaluation time, with
the material block able to override.

---

## 6. Landmines

Collected from the reference's own documentation and code comments. Each of
these cost the original authors real debugging time.

### 6.1 The concavity sign convention

The dihedral formula is `interior = π − atan2(e·(na × nb), na·nb)`, where `e`
is the edge tangent taken from face A's **pcurve**, flipped only when the
edge's orientation within its wire is `REVERSED`.

Ground truth to test against:

- A hole or pocket **opening rim** is **CONVEX** — the edge you deburr
- A boss or rib **base junction** is **CONCAVE** (≈270°)
- A pocket wall-to-floor junction is **CONCAVE**
- Box outer edges are **CONVEX**

Four alternative formulations were tried and rejected in the reference; the
rejected variants are documented so they are not reinvented. In particular, do
**not** additionally XOR the face orientation — `TopExp_Explorer` already
composes it into the sub-shapes it returns, and doing it twice inverts
everything.

**Port the concavity census as a test harness.** The reference has a physical
oracle (`physicalConcavity`, a solid-classifier probe) that agreed with the AAG
on 13,419 of 13,706 edges across its corpus. It is far too slow for production
but it is the cheapest possible proof that a Python AAG is correct.

### 6.2 The inner-wire bug is fixed — do not port a compensator

`aag.h` still documents an inner-wire dihedral sign inversion, and several
recognizers contain comments about working around it. **The bug was fixed at
source in July 2026 by deleting the offending flip**; the header comments are
stale by over a year. Porting the (correct) formula *and* a comment-inspired
compensator double-flips and inverts every inner-wire edge.

`is_inner_wire_edge` itself is still used, but only for genuine *topological*
questions — "does this bore pass through a cap", "is this edge on a window's
inner boundary".

### 6.3 Shape healing changes face order

The reference runs `ShapeFix_Shape` → `BRepBuilderAPI_Sewing` →
`ShapeFix_Solid` on import. Any of those can split, merge, or reorder faces.
Geometry from a live FreeCAD document is valid by construction, so healing is
unnecessary — but skipping it means face indices differ from the reference's,
and no C++ artifact is index-comparable. That is fine for a standalone
workbench. Validate and warn on shells/compounds instead: every recognizer
assumes a closed solid.

### 6.4 `BRepLProp_SLProps` holds a reference to its adaptor

```python
adaptor = BRepAdaptor_Surface(face, True)   # named local — MUST outlive props
props = BRepLProp_SLProps(adaptor, u, v, 1, 1e-6)
```

Inlining the adaptor into the constructor lets it be garbage-collected and
segfaults. This is the highest-severity porting hazard and it caused crashes in
the original.

### 6.5 Ordered containers make decisions

The C++ uses `std::set<std::string>` and `std::map` whose lexicographic
iteration order determines *outcomes*, not just cosmetics — which face becomes
`faces[0]`, which candidate wins a greedy claim on a tie. Twelve files iterate
`unordered_map`/`unordered_set` where C++ bucket order decides the winner.
**Use `sorted()` everywhere the C++ used an ordered container to make a
choice**, and audit every unordered iteration.

### 6.6 Performance

The reference's own roadmap marks optimization as **open**, with named O(N²)
hot spots: pocket BFS, pattern pairwise distances, tool-access feature×face
iteration, thin-wall edge pairs. Their < 30 s target is untested above ~1000
faces *in C++*.

Highest-payoff fixes available during the port, in order:

1. **Replace the `occt_index → face` anti-pattern.** Five recognizers re-run a
   full `TopExp_Explorer` over the entire shape counting to a target index,
   inside loops that are already O(F). Build one dict during AAG construction.
2. **Eliminate `ShapeAnalysis_Surface::ValueOfUV`** from the dihedral
   computation — it does a global surface inverse-projection twice per edge.
   The exact UV is already available from the pcurve for face A, and obtainable
   the same way for face B. Faster *and* more accurate.
3. **Precompute inner-wire edge membership** once instead of walking both
   faces' wires per edge.
4. **Vectorize the O(n²) face-pair loops** (rib, slit pass 2, channel) with
   numpy dot-product matrices before pairing.
5. **Bucket rib candidates by normal direction** before the all-planar-pairs
   loop — the worst offender.

### 6.7 Known contract bugs in the reference

Port these as-is for parity, then fix deliberately:

- **Pocket** never writes `max_width_mm` and hardcodes `is_open = False`,
  despite its header documenting both. A pocket rule branches on `is_open`, so
  that branch is dead.
- **Groove** O-ring ratio band: the header says `[1.4, 2.0]`, the code uses
  `[1.4, 2.2]`. The code is authoritative.
- **Step** uses the raw plane normal without the `is_reversed` correction,
  inconsistently with every other recognizer.
- The HTTP path omits `drop_undercut_dominated_pockets`; the CLI path is the
  golden reference.
- `ADJACENT` is declared in the relationship enum and never emitted.

### 6.8 Aggregation rounding

Findings are grouped by a key containing details rounded to 4 decimal places.
C's `round()` is half-away-from-zero; Python's `round()` is banker's rounding
and disagrees on exact ties. Use `math.floor(v * 10000 + 0.5) / 10000`. The key
also embeds JSON number formatting, which differs between the two languages.

---

## 7. Validation strategy

### Headless test loop

The geometry core imports no FreeCAD, so it tests in plain Python:

```
.venv311/  — FreeCAD's Python 3.11 + cadquery-ocp 7.8.1 (matches bundled OCCT)
.venv/     — system Python 3.13 + cadquery-ocp 7.9.3
tests/stubs/FreeCAD.py  — Console, Vector, ParamGet, getUserAppDataDir
```

With `PYTHONPATH=tests/stubs`, the existing 27 tests run in ~1.2 s with no
FreeCAD process. The same loop will carry the machining tests.

### The fixture corpus

211 of the 227 fixtures are built programmatically from OpenCascade primitives
in a C++ fixture generator — verified: no file loads, no randomness, fully
deterministic output. The remaining 16 are committed AP242 STEP files (PMI, out
of scope). Construction is the same style as this repo's
`tests/test_bridge_span_analyzer.py`, where builder functions state the expected
value in the docstring, so they port near-mechanically. **About 172 of the 227
are relevant to milling and turning**, and all 172 are pure-OpenCascade.

Highest-value subset to port first: the **18 threshold-boundary pairs**
(`threshold_hole_5_9x` / `6_1x`, `threshold_wall_1_6mm` / `1_4mm`, …). Each is
a box plus one cut whose single dimension straddles a rule limit — the cheapest,
highest-signal fixtures in the corpus, and they transfer to any rule engine.

### The baselines are not a conformance oracle

Worth stating plainly, because it is the intuitive assumption and it is wrong:
**the 227 approved baselines cannot be diffed against Python output.** The
schema ports fine (~120 lines); the *vocabulary* does not overlap.

The reference emits 83 distinct rule IDs across 15 categories over a recognized
feature graph. This workbench's `Rulebook` has 8 generic rules and no feature
recognition. Conceptual overlap is four rules (`thin_wall` ↔
`MIN_WALL_THICKNESS`, `undercut_present` ↔ `NO_UNDERCUTS`,
`sharp_internal_edge` ↔ `SHARP_INTERNAL_CORNERS`, the draft rules ↔
`MIN_DRAFT_ANGLE`) — and even those disagree on counts, because the reference
counts per recognized feature while this workbench counts per face. Beyond that,
`feature_count` is meaningless without recognizers, `part_process_type` needs
the classifier, and `feature_complexity` appears in 181 of the 227 baselines,
so almost nothing is comparable even in principle.

**Use the fixture geometry and the harness design; generate fresh Python
baselines.** Read the C++ baselines as documentation of *what each shape is
designed to trip*, which is the fastest way to know what a ported fixture is
for.

### The one cross-implementation oracle that does work

Run the C++ binary once to dump `(fixture name → face count, volume, bounding
box)` for all 211 fixtures. That validates ported *geometry* without depending
on either rule engine, catching transliteration errors that produce a different
but plausible solid. Build this first — it is cheap and it is the only
implementation-independent check available.

The fixture generator has six ordering constraints that are semantically
load-bearing and will silently produce wrong-but-plausible geometry if missed:
external blends go on the pristine solid before any cuts; countersinks are cut
into the bare plate first (cone booleans against accumulated shapes silently
drop the cone face); text glyphs must be fused before returning; and three
more documented in its comments.

### Port the drift gate itself

`expected_checker.cpp` is 444 lines of pure JSON logic with no OpenCascade
dependency, and its unit test ports just as easily. Worth porting verbatim,
preserving three design decisions:

- **Bidirectional map comparison** — a baseline is a *complete* specification.
  Any rule that fires but is not listed is drift. This makes an explicit
  "must not fire" list redundant.
- **Opt-in via key presence** — absent means "not asserted", present-but-empty
  means "assert this stays empty". This is what lets a growing engine adopt new
  assertion blocks without re-approving every baseline at once.
- **Counts only — never face IDs, never floats.** Face indices reindex on
  unrelated geometry edits and floats churn on every tweak. This applies
  identically here: `GeometryRef.index` is exactly as unstable as a C++ face ID.

### The guard suite

135 test cases in the reference carry a `false_positive` tag, each pinning one
historical false positive with a comment naming the regression. Their standing
rule is *never delete these*. Port the subset whose rules exist here, and adopt
the convention: every new rule gets a positive fixture, a false-positive guard,
and threshold boundary pairs.

---

## 8. Open decisions

1. **Thread evidence.** Build a user-confirmation channel for threads, or leave
   the family dead? Recommendation: build it — it is cheap and unlocks four
   rules that matter.
2. **Process branching.** Per-process threshold blocks (parity with the
   reference) or split rule IDs (clearer rule cards)?
3. **Rule-card scale.** 58 rules is a lot of cards in the process library. Does
   that UI need grouping or filtering work before the rules land?
4. **Parity vs. correctness** on the four known contract bugs in §6.7 — match
   the reference so baselines line up, or fix on the way through?
