<!--
SPDX-License-Identifier: LGPL-2.1-or-later
SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
-->

# Parity with the reference engine

**Status, 2026-11-09: all 280 reference goldens match.** Measured with
`tests/fixtures/check_golden.py`, which analyses every fixture and holds it to
the reference's approved `expected/<fixture>.json` at `fa18e7fa` with the
reference's own drift semantics -- every finding count, category, severity,
recognized-feature signature, PMI tally and process classification. 280 of
280, from 98 when the resync began; 182 fixed, none broken along the way.
Every one of the 27 seam variants analyses identically to its control.

How it got there is in `docs/resync-ledger.md` (the reference's history,
replayed commit by commit) and in "Residuals after the fa18e7fa resync"
below (the fixtures that needed more than a replay, with the cause of each).

**Run it after every change:**

    python tests/fixtures/check_golden.py --jobs 4 --save after.json --compare before.json

The reference is a live repository; when it moves, refresh the goldens'
snapshot (`fixture_processes.json`, `geometry_oracle.json`, the rule list in
`core/baselines/parity.py`) and replay its new commits the same way.

**Kernel bridges.** Three places where OpenCascade 7.8 (FreeCAD) and 8.0 (the
reference's build) differ are bridged rather than left as drift, each
documented where it lives: the XDE PMI reads in `core/machining/pmi/`
(`KernelBridge`), plane normals on left-handed frames in the AAG builder
(§F below), and the reference's pre-analysis mesh in the machining analyzer.
Seven fixture recipes carry sub-micron sliver-edge differences from the
reference's STEP files (`_KNOWN_EDGE_DIFFERENCES` in
`tests/fixtures/generate_fixtures.py`); none affects analysis.

**Known divergences that do not show in the goldens.** The external-thread
recognizer keeps a modelled-helix route the reference does not have (no
golden contains EXTERNAL_THREAD). A few suspected reference quirks are ported
as they are, and named in the ledger and the tables below.

Everything from §0a to §6 is the September 2026 record, measured against
`26e76d14` over 211 parts. It is kept for the reasoning it records; every
difference it names is closed at `fa18e7fa`.

## 0a. The shipped sheet limits override the rules

**Fixed 2026-09-09.** The limits are blanked, so each rule answers from
the gauge as the reference does, and all seven are exact. The
factor-style rules keep their configured multipliers. Recorded here
because the shape of the mistake is worth remembering: a settings file
can silently disable a correct rule, and a comparison run at the code
defaults will not show it.

Seven sheet rules disagree, fourteen findings between them, and none of it is
rule logic. Every one of those rules computes a gauge-relative answer -- a
web of 2×t, a flange of 4×t + r, a brake ceiling interpolated between 125° at
11 gauge and 180° at 14 -- and then lets a configured limit replace it:

```python
ceiling = self.safe_float(rule_config.limit)
if ceiling is None:
    ceiling = max_std_bend_deg(gauge, context)
```

`sheet_metal.yaml` supplies a figure for each, so the fallback never runs.
`SHEET_BEND_ANGLE_EXTREME` ships `150.0`, a flat angle standing in for the
gauge band. `SHEET_FORMED_PITCH` ships `6.0` mm where the rule wants 2×t. The
others ship the factor's numeral as though it were millimetres — `2.5`,
`4.0`, `2.0` — which is right only at exactly 1 mm gauge.

Blanking those numeric limits, and keeping the severities, takes the corpus
from L1 131 to 117 and from 56 rules exact to 63. The fix is in the settings
file, not in any check: either remove the limits so the gauge-relative
default stands, or write them as the factors they are.

This was invisible until the comparison started reading the process YAML.
Measured with every limit left at `N/A`, all seven rules fall back to the
correct logic and appear to agree — which is what the run of 2026-09-08
reported, and why its 114 was optimistic.

## 0b. Severity

**Fixed 2026-09-09.** Every severity now matches. Four were declarations
in the process files, one was a threshold entered in the error band
where the check's own default puts it in the warning band, one was a
severity nailed into a check so the process file could not reach it, and
the two complexity rules were being graded when they should not be --
an operation count is a pricing signal, and marking a part ERROR for
being complicated pushes the real errors below it.

Thirty-nine findings carry a different severity from the reference's, on
rules whose counts agree exactly. Four are one-line declarations in the
process YAML; three are banding inside the rule.

| rule | ours | reference | where |
| --- | --- | --- | --- |
| `tool_access_special_setup` | WARNING ×20 | INFO ×20 | `cnc_milling.yaml`, `cnc_turning.yaml` |
| `slot_nonstandard_width` | INFO ×7 | WARNING ×7 | `cnc_milling.yaml` |
| `hole_nonstandard_diameter` | INFO ×4 | WARNING ×4 | `cnc_milling.yaml`, `cnc_turning.yaml` |
| `hole_countersink_angle` | INFO ×2 | WARNING ×2 | `cnc_milling.yaml`, `cnc_turning.yaml` |
| `boss_wall_thickness` | ERROR ×2 | WARNING ×2 | threshold placement, not a declaration |
| `feature_complexity` | 1 ERROR + 2 WARNING of 166 | all INFO | banding in the check |
| `sheet_feature_complexity` | 1 WARNING of 45 | all INFO | banding in the check |

Severity is what colours a finding and orders the report, so this is more
visible to a reader than several of the count differences above.

Recognition is closer still. Against the reference's `features.json`, total
L1 error is 21 and 198 of 211 fixtures match exactly, from 479 and 99 before
the 2026-09-04 work. Fifteen feature types agree exactly, including every
hole type. What is left:

| type | ours | reference | where |
| --- | --- | --- | --- |
| `THROUGH_CAVITY` | 29 | 25 | phantom windows on three formed sheet parts and the turbo housing flange |
| `PATTERN` | 79 | 83 | four fixtures, one group each |
| `FREEFORM_SURFACE` | 29 | 26 | `sm_emboss_freeform`, `torture_freeform_leviathan` |
| `GROOVE` | 16 | 14 | `sm_hd_bracket`, `sm_relief_shapes` |
| `BOSS` | 21 | 22 | three fixtures, one each way |
| `STEP` | 52 | 54 | `sm_torture_curved_outline` |
| `POCKET` | 68 | 70 | `sm_formed_shapes`, `torture_freeform_leviathan` |
| `FILLET` | 42 | 43 | `sm_emboss_gallery` |

The turbo housing case is worth reading first because it is one disagreement
counted twice: the reference calls faces 1-6 and 10-13 a `BOSS` and we call
almost the same set a `THROUGH_CAVITY`. Our boss pass declines it, and the
reason is worth finding before the phantom cavity is chased separately.

## Measuring this correctly

Three things have to be right before a rule comparison means anything, and
each of them has produced a wrong answer here at least once.

**Sum the occurrences, not the rows.** The reference collapses identical
findings into one published row and records how many it stood for in
`details.occurrence_count`. Its 846 rows are 1218 occurrences. Counting rows
made `thin_wall` look like 46 against 40 when it was 46 against 99.

**Translate the six aliased rule ids.** These are the same rule under two
spellings, and compared literally each shows up as a total disagreement in
both directions at once — inflating the apparent difference by roughly 300.

| ours | reference |
| --- | --- |
| `boss_height_ratio` | `boss_height_risk` |
| `hole_depth_ratio` | `hole_deep_risk` |
| `material_removal` | `material_removal_high` |
| `pocket_corner_radius` | `pocket_square_corner` |
| `pocket_depth_ratio` | `pocket_deep_risk` |
| `slot_depth_ratio` | `slot_deep_risk` |

**Pass the same declared inputs.** The reference's batch harness declares
customer intent for four fixtures, in `fixture_analysis_options` in its
`main.cpp`. These are declarations, not anything either engine works out from
the geometry, so running them on defaults asks the two engines different
questions. It accounted for five findings across three rules.

| fixture | declared |
| --- | --- |
| `torture_casting_ribfield` | blank `as_cast` |
| `as_cast_no_draft` | blank `as_cast` |
| `torture_freeform_leviathan` | precision mode |
| `sm_gauge_steel_ceiling` | material family `steel` |

Every other fixture runs on defaults on both sides.

**And run each part under its own process's configuration.** Take
`active_rules`, `rule_limits` and `rule_feedback` from the YAML of the
process the classifier chose for that part -- `cnc_milling`, `cnc_turning` or
`sheet_metal` -- and not from a merge of all three. Three things go wrong
otherwise. Running every registered check against a machining context also
runs the moulding and printing ones, which expect a different context and
raise, and a rule that raises reads as a rule that found nothing. Passing a
flat severity reports every INFO and every ERROR the reference publishes as a
disagreement. And passing `N/A` limits lets every rule fall back to its code
default, which hides §0a completely: measured that way the sheet rules all
appear to agree.

## How to work one of these

1. Run the rule over the fixture named and read what it produces.
2. Read the reference's `dfm.json` for the same fixture.
3. **Compare the recognition before comparing the rule.** Most of what is
   left is not a rule difference at all. The reference's `feature_complexity`
   message prints its own census (`"12 holes, 4 pockets, 0 slots…"`), and
   `sheet_feature_complexity` does the same for sheet parts — that line is
   usually the whole answer.
4. Only then read both rule bodies.

The reference is read-only. Nothing in this list is a licence to edit it.

---

## 1. Recognizer scope, not rule logic

The rule bodies are equivalent; the recognizers disagree about what is on the
part. Fixing these means changing recognition, which moves several rules at
once — so measure the whole corpus after each, not just the fixture.

### 1a. `slot_overhang` +18 — the slot recognizer seeds where the reference's does not

**Resolved 2026-09-04.** The pocket pass was retyping long cavities as slots
and the slot pass stood down wherever the pocket pass had claimed a face, so
which vocabulary a channel got was decided by running order. Both passes now
emit their own reading and the resolver arbitrates on the aspect ratio, and
the floored-slot pass re-selects its floor, rejects a lidded opening and
measures width across the narrowest facing pair. `SLOT` is exact against the
reference. The rule count below has not been re-measured.

The rules are the same computation: `length/width > 6` and `depth/width >=
gate`, same thresholds, same order. All eighteen findings of difference are
features that exist here and not there.

| fixture | ours | reference | reference sees |
|---|---|---|---|
| `micro_fluidic_channels` | 16 | 0 | 4 pockets, **0 slots** |
| `torture_casting_ribfield` | 1 | 0 | 19 "other", **0 slots** |
| `machine_base_angle_plate` | 1 | 0 | 1 "other", **0 slots** |

**Traced, and the resolver is not the cause.** Measured on
`micro_fluidic_channels`:

- Our POCKET recognizer finds the 16 channels: faces `[9, 12, 60]` and so
  on, width 1.0, length 25.0, depth 2.5.
- Our SLOT recognizer finds the same channels with the same parameters.
- Our resolver then prefers SLOT, **correctly by the reference's own rule** —
  `interacting_feature_resolver.cpp` overrides its POCKET > SLOT priority
  when the two cover the same face set, the slot's length/width is at least
  2, and its length is at least its depth. Here that is 25:1 and 25 ≥ 2.5.
  We already implement that override, at the same threshold.

So the difference is entirely that the reference's **slot recognizer never
seeds on these channels**, and the override therefore never runs. Its seed
constraints are documented in `slot_recognizer.cpp` around line 25: the floor
must be small relative to the part, at least two thirds of the floor's edges
must be concave, and there must be two anti-parallel planar walls.

Two things to establish before changing anything:

1. Which of those three constraints rejects a 1 mm × 25 mm channel floor in
   the reference. That is the difference to port.
2. Why the reference reports **4** pockets for 16 channels. Either the part
   has four channel groups, or its pocket recognizer grows across a connected
   network where ours stops at each channel. If the latter, that is a second
   difference and the more consequential one.

### 1b. `impeller_blade_hub` — three of five done

The cause was one miss, as expected. The boss recognizer took the three
B-spline blades as walls of a boss, so the blades and the hub they stand on
became a single protrusion and the blades stopped existing. A shaped face is
not the side of a pad, and the wall test now says so.

Fixed by that: `freeform_finishing` 4→5 (exact), `freeform_internal_radius`
0→1 (exact), `boss_height_ratio` 7→6 (exact). `sharp_internal_edge` moved
54→60, toward the reference.

**Still open, and it is a real port rather than a tweak:**

| rule | ours | reference |
|---|---|---|
| `tool_access_blocked` | 0 | 3 |
| `undercut_present` | 0 | 3 |

Both need UNDERCUT features on the blades, which we do not produce. Our
undercut recognizer has a planar pass and a cylinder/torus pass, and so does
the reference's — but the reference has a **third path in its draft
recognizer** (`draft_recognizer.cpp`, around line 150): for a face with
reverse draft it samples the overhanging region, takes each sample's surface
normal, and ray-tests it against all six cardinal directions. When a majority
of the overhang is unreachable it emits an UNDERCUT with
`surface_type = "FREEFORM"`.

The majority gate is the load-bearing part and its comment says why: a
wrapped impeller channel buries 86–100% of its overhang, while a machinable
exterior fillet is open and only ray-grazes on a few edge samples (33% and
4–8% on two named fixtures). An earlier normal-coherence proxy could not tell
them apart and called machinable fillets unmachinable.

`tool_access_blocked` needs nothing of its own once those exist: it fires on
any feature all of whose faces are undercut.

### 1c. `torture_casting_ribfield` — `rib_draft_angle` 0 vs 3

**Resolved 2026-09-08, and it was never a rule difference.** The reference's
batch harness declares this fixture's blank as `as_cast`; ours was running it
on the default billet, and the rule correctly stands down on a part cut from
solid. Declaring the same thing makes both engines report three. See
*Measuring this correctly* above -- `casting_draft_angle` on `as_cast_no_draft`
and `no_orthogonal_datum_trio` on `torture_freeform_leviathan` were the same
mistake.

We recognize 4 RIB and 4 BOSS; the reference recognizes 19 "other". Our rib
recognizer is finding ribs, and the draft rule then says nothing about them.
Check whether `rib_draft_angle` is reading a parameter our rib recognizer
does not set.

Same fixture: `slot_depth_ratio` 1 vs 3, `slot_overhang` 1 vs 0.

---

## 2. Rules that stop short

### 2a. `feature_complexity` −7 — silent on a part with nothing on it

**Resolved.** `feature_complexity` and `sheet_feature_complexity` both agree
exactly as of 2026-09-08.

Every one of the seven is a part where we report 0 and the reference reports
1: `simple_box`, `small_part`, `thin_sheet`, `sealed_void`,
`single_datum_dome`, `thin_threshold_3_1mm`, `drafted_fin_channel`.

Our rule returns early when the feature count is zero. The reference still
emits its census — "0 features, ~0 operations" is a true and useful statement
about a part somebody is quoting.

**Smallest fix in this document.** One early return in
`part_detail_checks.py`.

### 2b. `setup_count_high` −16 — every approach direction is treated as reversible

**Partly done.** The signed-direction work landed and took this from −16 to
−6, now spread one apiece over four fixtures rather than concentrated. What is
left is no longer the reversibility premise.

Eighteen parts differ. Fifteen are 0 vs 1 and three are 1 vs 0, and the cause
is the same in both directions: our clustering compares approach directions
**without sign**, and the reference's does not always.

The reference carries a `signed_dir` flag on each direction. A through hole
is unsigned — either end will do, and which one is a fixturing decision. A
blind hole, a pocket floor, a counterbore is signed: there is one side you
can reach it from. Two signed directions merge only if they agree in sign;
anything unsigned merges either way, and adopts the sign of the first signed
direction that joins it.

We compare `abs(dot)` for everything, so a feature reachable only from the
top merges with one reachable only from the bottom, and a part that has to be
flipped reads as one setup.

Measured:

| fixture | our directions | we cluster to | reference |
|---|---|---|---|
| `turbine_compressor_disk` | 15 | 1 | 2 |
| `transaxle_housing_cover` | 12 | 1 | 2 |
| `lightweight_grid_panel` | 9 | 3 | (silent) |

The first two are the fifteen 0-vs-1 parts: a disk with work on both faces is
two setups and we call it one. The grid panel is the other direction and may
be a second cause — check it after the sign fix rather than before.

The fix is in `_cluster` and `_approach_directions` in
`setup_extra_checks.py`: each direction needs to carry whether it is
reversible, and the merge test needs to respect it. `cluster_tads` in the
reference's `setup_rules.cpp` is the shape to follow.

---

## 3. Measurement differences

### 3a. `thin_wall` −7, spread over 22 parts

**Changed direction.** Now +3 rather than −7: the ported passes and the
narrowed rib mute overshot slightly. The remaining spread is
`optical_periscope_housing` (4 of 8) and `electronics_enclosure_billet` (28 of
31) under, against two fixtures two over.

Closest of the large rules. Both over and under, no single cause. The
reference has two passes we have not ported, worth 10 occurrences together:

- **Freeform-inclusive walls** (2 occurrences: `sculpted_lid_thin_web`,
  `torture_freeform_leviathan`) — pass 4 in the reference's
  `thin_feature_rules.cpp`.
- **"Thin wall between adjacent features"** (4) and **"wall between a hole
  and an adjacent feature"** (1) — message variants we do not emit.

Over-reporting: `heat_exchanger_tube_sheet` 6 vs 3, and eight parts at 1–2 vs
0. Under: `heat_sink_finned_block` 0 vs 5, `optical_periscope_housing` 4 vs 8.

### 3b. `pocket_corner_radius` −7, over 16 parts

**Changed direction.** Now +5, and eleven against nine of that is
`optical_periscope_housing` alone.

`optical_periscope_housing` 11 vs 9 and seven parts at 1 vs 0, against
several at 0 vs 1. Likely the same cavity-recognition scope question as §1.

### 3c. `hole_edge_distance` −6, `hole_flat_bottom` −6

Both mixed. `hydraulic_actuator_end_cap` 0 vs 4 and `cf_knife_edge_flange` 2
vs 6 are the biggest single gaps in the first; `robotic_gripper_finger` 7 vs
9 and `fixture_plate_dowels` 2 vs 4 in the second.

### 3d. `hole_intersects_cavity` +4

**Resolved.** `hole_intersects_cavity` and `hole_intersecting` both agree
exactly as of 2026-09-08.

Four parts at 1 vs 0: `precision_spindle_end_cap`, `mtb_handlebar_stem`,
`hydraulic_actuator_end_cap`, `aerospace_tensioner_pulley`. Plus
`torture_hole_labyrinth` 0 vs 1. This rule was not touched when
`hole_intersecting` was brought in line and may need the same bounding: an
axis measured as an infinite line rather than as the bore it belongs to.

---

## 4. Single-fixture differences

One part each. Cheap to investigate, and several may turn out to be the
reference's gap rather than ours.

| rule | fixture | ours | reference |
|---|---|---|---|
| `slot_nonstandard_width` | `pump_cover_part_marking` | 2 | 0 |
| `hole_countersink_angle` | `hydraulic_manifold_block_v2` | 2 | 0 |
| `minimum_feature_size` | `pump_cover_part_marking` | 1 | 0 |
| `small_part_holding` | `small_part` | 1 | 0 |
| `rib_height_aspect` | `aerospace_tensioner_pulley` | 1 | 0 |
| `hole_intersecting` | `worm_gearbox_housing` | 1 | 0 |
| `cutter_radius_suboptimal` | `wr90_waveguide_flange` | 1 | 0 |
| `sheet_notch_narrow` | `sm_notch_baits` | 1 | 2 |
| `flexure_slit_process` | `mtb_handlebar_stem` | 0 | 1 |
| `casting_draft_angle` | `as_cast_no_draft` | 0 | 1 |
| `no_orthogonal_datum_trio` | `torture_freeform_leviathan` | 0 | 1 |

`pump_cover_part_marking` appears three times — probably one cause.

**`wr90_waveguide_flange` is the one where I believe we are right.** The part
has an aperture recess with four R2.070 corners, which is not half the
diameter of any standard end mill and needs an interpolated pass. The
reference's rule reads `corner_radius_mm` off a recognized feature, and its
own census for that part says "0 pockets, 0 slots" — nothing carries a corner
radius, so its rule has nothing to look at. We read the fillets directly.
Worth confirming by measuring the fixture.

---

## 5. `sharp_internal_edge`

No longer parked. Ported again on 2026-11-09 against the reference at
`fa18e7fa`, line for line, and measured against its 280 goldens: **276 of 280
fixtures agree, 119 occurrences here against 107 there**. Before the port it
was 265 of 280 and 22 against 107.

The escape vote, the uniform-junction extension and the cutter-formed test
had been ported in September; what kept the count wrong was everything
around them:

- **Every finding read the same.** The message was a fixed sentence, so the
  aggregation pass, which keys on the message, folded every 90° corner on a
  part into one row. `torture_machinists_maze` printed 1 where the
  reference printed 19. The message now names the two faces, the edge's
  midpoint and its length, as the reference's details do. The sub-tool-pitch
  case keeps a fixed wording on purpose: the reference collapses that one.
- **The ownership lists were ours, not the reference's.** We had added
  CHANNEL, THROUGH_CAVITY, V_GROOVE, FLEXURE_SLIT, BROACHED_SLOT and
  EXTERNAL_THREAD to the features that speak for their own corners, and an
  exemption for open slots. The reference has none of these; the slit family
  only seeds the one-hop wall expansion. THROUGH_CAVITY alone was the whole
  of `progressive_die_punch` 0 vs 20.
- Severity is WARN, as in the reference, not INFO.
- The cutter-formed rays search out to ten part diagonals and accept hits
  from 0.1 mm, the reference's figures, not 1000 mm and 0.5 mm. The part
  diagonal is taken from the union of the face boxes, as the reference does.
- Faces off the main connected body are skipped (the reference's guard for
  import artefacts; `nist_ftc_10` 1 → 0).
- "Owned only by a step" counts only the features in the ownership map.

The four that still disagree are all recognition, not the rule. Each was
checked against the reference engine's own per-face feature output:

| fixture | ours | ref | cause |
|---|---|---|---|
| `flexure_thin_blade` | 8 | 0 | faces 7–14 recognized as two THROUGH_CAVITY; the reference calls the same faces two FLEXURE_SLIT, whose walls the one-hop expansion then silences |
| `optics_spider_mount` | 1 | 0 | the same: four FLEXURE_SLIT (faces 29–36, 41–44, 50–53) recognized as THROUGH_CAVITY; one sub-tool-pitch row survives |
| `torture_casting_ribfield` | 1 | 2 | our O_RING_GLAND claims faces 19 and 54 as well as the reference's 35, 42, 51. Face 19 is the gland's base circle onto the deck (face 3), so the rim test silences a corner the reference reports |
| `torture_freeform_leviathan` | 4 | 0 | the reference recognizes a POCKET on faces 29, 30, 32; we report face 29 as FREEFORM_SURFACE and leave 30 and 32 loose, so the corners between them fire |

The reference's slit family and the gland's face set are being reworked in
the recognizer resync; re-measure after it before treating these as settled.

---

## 6. Not parity work

### Nine rules no process runs

Registered, tested, and unreachable — correctly so.

Five `GDT_*` rules plus `NOTE_SURFACE_FINISH_DEMANDING` and
`SURFACE_FINISH_PER_FACE_DEMANDING` read tolerance and surface-finish
callouts. `annotations.py` returns nothing for every part because a FreeCAD
document has nowhere to keep a feature control frame. They never fire in the
reference's own corpus either, for the same reason.

`MAX_BRIDGE_SPAN` and `MAX_OVERHANG_ANGLE` are pre-existing 3D-printing rules
with analyzers and checks but no FDM process definition to run them.

### Recognizer constants are not settings

Settled by measuring: the reference carries 395 hardcoded constants across
its 25 recognizers and reads none of them from its config. They describe what
a boss or a bend *is*, not what a shop can do about one. Five of ours already
read a threshold where the figure genuinely is policy, which is further than
the reference goes. All 127 `RuleThresholds` fields are editable.

---

## Residuals after the fa18e7fa resync

### R2: setup count, tool access, undercuts, small-part holding

| Fixture | Cause | Outcome |
|---|---|---|
| clamp_jaw_assembly_half, kitchen_sink, lightweight_grid_panel | `setup_count_high` took a STEP's wall `normal` as an approach direction. The reference's `parse_axis` reads only `axis` (signed by `axis_signed`), then `floor_normal`, then a planar `host_face`, on every feature type. Ported that order and dropped the `normal` fallback and the type filter. | fixed |
| setup_count_high | Severity bands were exclusive (`>` limit), so 4 setups was WARN. The reference grades `>= setup_count_error` (4) as ERROR. | fixed |
| tilted_hole_block, robotic_gripper_finger | Same banding fix exposed a hole_edge_distance ERROR-vs-WARN drift that had been cancelling the wrong setup severity. R1 fixed hole_edge_distance. | fixed |
| knurl_neg_flatted_shaft (+seam73) | setup part fixed (STEP `normal`). | open: extra `thin_wall` (R3) |
| aerospace_tensioner_pulley (+seam37) | Rule now matches. THROUGH_HOLE faces 12/16/17/18 come out `axis` -Z with `axis_signed`; the reference has +Z unsigned, so the count is 2 where it should be 1. | open: hole recognizer (R1) |
| small_part | `lacks_parallel_datum_pair` returns false when no outward flat reaches the datum area (no_datum_face owns that case). Ours treated "no candidates" as a missing pair. | fixed |
| four_axis_hex_block (+seam37), optical_periscope_housing | `tool_access_special_setup` read only `axis` on a fixed type list. The reference reads `axis`, `floor_normal` or `blocked_tad` on every feature, treats `>=` cos as cardinal, and skips planar-normal features (UNDERCUT, V_GROOVE, CHAMFER, DRAFT_FACE) whose tilt matches a catalogue form cutter (half-angles 30/45/60, 2 deg). The findings also merged: the reference aggregates on details (`feature_type`, `axis`), so the axis is now in the message. | fixed |
| bracket_with_undercut_pocket, complex_undercut_part_v2, undercut_part, optical_periscope_housing | Recognition already matched. `undercut_present` findings with different `face_normal`/`blocked_tad`/`axis` details were merged by our message-keyed aggregation; those details are now in the message. | fixed |
| torture_machinists_maze | Undercuts fixed (same merge cause). The extra CHANNEL [51,61] (axis +Y) adds a third setup direction, so setup is WARN, not INFO. The extra BOSS is gone. | open: CHANNEL (R3) |
| pocket_chamfered_top | The four chamfer faces become UNDERCUTs. Under OCCT 7.8, `BRepFilletAPI_MakeChamfer` builds these planes on an indirect `gp_Ax3` (`Position().Direct() == False`), so `Plane().Axis().Direction()` is minus the surface normal and `outward_normal` points into material (e.g. face 2: (0,-0.707,-0.707) vs the true (0,0.707,0.707), checked with `BRepGProp_Face` plus a solid classifier). The reference reads the plane normal the same way, and its output has no undercuts here, so its OCCT 8.0 chamfer planes are presumably direct. | kernel (probable) |

Also ported in passing, with no change measured: the undercut recognizer's curved pass now follows the reference. Candidates are cylinders/tori with a concave neighbour (not `is_internal`). Excluded faces are the six hole types only (not PARTIAL_BORE). Tori are tested along the reference's unset `cyl_cone_axis` (+Z, a suspected reference slip). External cylinders add cardinal pairs only for axis components under 0.05. Access is strictly under 30 deg. `undercut_present` no longer stands down on 5-axis, and `tool_access_blocked` is gated to 3-axis, as in the reference.

### R3: thin walls, blends, freeform, part rules, pockets and bosses

| Fixture | Cause | Outcome |
|---|---|---|
| sculpted_lid_thin_web, freeform_transition_bracket | Pass 4 of the reference's `ThinWallRule` was missing: each freeform face (B-spline, revolved, extruded, other) is paired with every plane and every other freeform face. The partner area must be at most 25 %. Adjacent pairs are skipped. The two mid-UV normals must be opposed (dot at most -0.5) and both must point away from the other face. The thickness is the BRepExtrema distance (0.01 mm to warn). ERROR when strictly under the floor. | fixed |
| turbo_compressor_housing | The opposed planar pass accepted a 0.001 mm pair. The reference drops a centroid separation or measured gap under 0.01 mm. | fixed |
| electronics_enclosure_billet (thin_wall) | The reference aggregates on details, and an opposed-pass finding always carries `wall_length_mm`. Four 1 mm walls of four lengths merged into one of ours. The run length is now in the overview. | fixed |
| electronics_enclosure_billet (BOSS), as_cast_no_draft, nist_ftc_06 (BOSS) | Our boss wall test used orientation; the reference's `is_wall_like` is "not planar, or planar and smaller than twice the top". A 6 x 6 x 13 standoff's walls are larger than that, so it is not enclosed and not a boss. Ported as is. | fixed |
| torture_casting_ribfield (thin_wall) | The reference qualifies an opposed pair on its centroid plane separation (thin, or thin by aspect, with the rib and lathe exclusions) before it measures. A drafted rib measures 1.5 mm at its tip but 2.5 mm between its planes, and as a rib the aspect path is closed to it. Ported the two-stage test. One unit test changed: the long gentle wedge in `test_a_wedge_closing_to_a_thin_edge_is_reported` is an opposed pair the reference does not report, so the test now uses a short wedge whose flanks really converge. | fixed |
| knurl_neg_flatted_shaft (+seam73) | The converging pass treated only recognized fillets and chamfers as a blend joining two faces. The reference counts any CYLINDER or TORUS between them. Also ERROR is now strictly under the floor. | fixed |
| nist_ctc_01, nist_ctc_02_logo, wr90_waveguide_flange, nist_ftc_08 (cutter_radius_suboptimal) | `cutter_radius_infeasible`/`suboptimal` ran on THROUGH_CAVITY and skipped open slots. The reference applies both to POCKET and SLOT only, open or not. | fixed |
| knurled_grip_shaft (+seam37), surgical_handpiece_body | On a turned part `part_aspect_ratio` read the milling process's 8:1 target, which hid every shaft between 4:1 and 8:1. It now uses the turning thresholds, or the process's pair when the process states both. The OD is the largest cylinder parallel (dot over 0.99) to the axis. Bounds are inclusive. With no OD it falls through to the box rule, as the reference does. | fixed |
| optics_spider_mount | Material-removal bar stock now follows the reference: a vertex-based cylinder about the axis (TURNED takes it; MILL_TURN takes it only when it is smaller than the face-box billet), plus the profile-extrusion bar. | fixed |
| nist_ftc_07, nist_ftc_10 (freeform_internal_radius) | `freeform_internal_radius` and `freeform_finishing` scanned FREEFORM_SURFACE features. The reference scans the AAG: a face is sculpted when it has concave curvature or convex curvature tighter than 1/500. It excludes revolved faces on a lathe and MARKING_TEXT faces, and the finishing estimate also excludes blend bands. The unreachable finishing finding is a WARN. | fixed |
| torture_machinists_maze, relieved_logo_plaque | The pocket recognizer lacked three post-walk steps: missed-wall absorption (planar, across a concave edge, \|dot\| under 0.3 with the floor), the axis-coverage floor pull-in, and the off-cardinal through-slit rejection. The true floor's tie-break is now total area. The maze pocket gets its fourth island wall, which absorbs the extra CHANNEL. The plaque pocket now holds enough marking faces to lose containment to the MARKING_TEXT. | fixed |
| heat_exchanger_tube_sheet, torture_casting_ribfield (category) | The reference files `groove_square_corner` under category `slot`. | fixed |
| sm_hybrid_milled_pocket | The reference files `sheet_intent_sharp_corners` under category `process`. | fixed |
| nist_ctc_02 (CHAMFER, chamfer_nonstandard_angle), nist_ftc_08 (material_removal 84.97 vs 85.02), freeform_transition_bracket (material_removal), torture_hole_labyrinth | The reference meshes the part on import (deflection 1.0, angle 0.5) before the AAG is built, so node boxes are triangulation boxes. On ctc_02 a 3-degree taper cone's slant goes from 1.958 to 2.065 mm, which puts it under the 20-70 degree band and removes it. On the bracket the B-spline control-hull box (182621 mm3, 75 %) collapses. R1 put the pre-mesh in the analyzer. | fixed (R1's pre-mesh) |
| nist_ftc_06 | Matches without the pre-mesh. With it, ThroughCavityRecognizer emits two 6-face cavities that swallow the two slit SLOTs. | open: through-cavity under pre-mesh (R1/parent) |
| nist_ftc_10 | BOSS and freeform fixed. Still has an extra THROUGH_CAVITY of 7 faces over the two 5 mm slits, where the reference keeps SLOTs from its slit recognizer. pocket_square_corner fires on that cavity. | open: through-cavity recognizer |
| torture_freeform_leviathan | The reference has POCKET [29,30,32]: a +Y plane floor with two B-spline walls. Our floor test needs a planar wall normal. The reference reads `plane_normal` on the B-spline neighbours, which is the default (0,0,1) flipped by `is_reversed`. Orientation is the same in its STEP and ours (both walls unreversed), which gives one wall, not two, so that alone does not explain its pocket. Also moved under the pre-mesh. | open |
| pocket_chamfered_top | Our builder's MakeChamfer planes come out indirect under OCCT 7.8, so their outward normals point into material and four UNDERCUTs follow. The reference's STEP has the same faces direct (checked: face 4 normal (0.707,0,0.707), not reversed). | kernel / fixture build (see R2) |

Also noted: the boss recognizer still skips walls with freeform curvature, which the reference's `is_wall_like` does not do. This is a local addition and was left in place; it has not been measured.

### R1: holes and threads

| Fixture | Cause | Outcome |
|---|---|---|
| nist_ctc_05 | Our hole pass promoted a bore only on a helix found anywhere on the part; the reference's `try_thread` Check 1 reads the bore's own edges: at least 2 B-spline edges whose axial midpoint is outside the outer 15% bands, summing (3-point estimate) to at least 0.8·π·d, then `match_tap_drill`; tapped depth = axial reach of edges advancing at least 0.3 pitch. The STEP writes the bore seam as a B-spline, visited twice, so all ten 30.48 mm holes qualify. Ported exactly, with the reference's cross-bore guard (any neighbouring cylinder refuses, coaxial included) and its BLIND/THROUGH/BREAKTHROUGH-only gate. The helix search is gone from the hole pass. | fixed |
| pmi_torture_gauntlet | `try_counterbore`'s buried-band test also accepts a PMI thread callout on the seed cylinder as proof of a relief, and only then emits a THREAD_RELIEF_GROOVE (width = band depth, depth = radial step, adjacent designation/pitch). Ported. `thread_relief_width` was not active for milling; the reference runs it on every part and files it under `thread`. | fixed |
| (row 114) | ThreadShoulderProximityRule height gate: once the box admits a face, re-measure its top with `span_of_face` along the approach. | done |
| nist_ctc_04 | `hole_multi_pass`: the reference counts `drilling_passes` (1 + voids wider than 3 diameters of the first cylinder) in its through-merge, and the rule reads that; ours fired on any void past 2 d. `material_removal_high`: the reference's billet is the union of the AAG face boxes, ours was the whole-shape box (162 vs 131 mm tall here). | fixed |
| euro_lock_cylinder (+seam37) | `is_on_part_axis` is gated on TURNED / MILL_TURN (a profile-extrusion blank is never rotated), 0.99 dot, first cylinder/cone only. | fixed |
| hole_countersink_angle | A through countersink's depth is the sum of its cylinders' and cones' analytic V ranges (cone scaled by cos half-angle); ours kept the cylinder alone. | fixed |
| hydraulic_manifold_block, fixture_plate_dowels | The reference's `hole_intersecting` details carry `hole_a`/`hole_b`, so two pairs at the same gap never aggregate. The pair ids are in our message now. | fixed |
| aerospace_hydraulic_servo_valve_body, servo_coupling_clamp, door_closer_body, worm_gearbox_housing | `hole_intersecting` and `hole_intersects_cavity` ported as written: bore boxes from cylinders/cones, strict overlap, then bounded axis segments (`band_along`) via the reference's closed-form segment distance within r1+r2+0.1, thin-plate guard; the cavity rule's touch test is the same segment distance with no span check, blend-capped is asked of the first cylinder only, our invented lathe-bore exemption is gone, and the through-breach cavity list drops CHANNEL and V_GROOVE. The reference's "first cylinder" is its seed (smallest radius); our face lists are sorted, so it is recovered as the smallest-radius cylinder. | fixed |
| off_cardinal_viz_block (+seam37), five_axis_compound_angles, tilted_hole_block, robotic_gripper_finger | `hole_edge_distance`: always WARN (bore-out included); planar part measured at both ends of the level's axis segment with the entry/exit-side skip (`cylinderSurfaceToAabbMinDistance`); nothing at all when every side is skipped; finding faces are the winning boundary (barrel ties, else planes whose axis-to-plane wall matches within 0.05, plus coplanar faces), which is what the thin-wall dedup compares. `hole_partial_entry` collects every planar neighbour of a bore wall, not only convex rims. A blind multi-cylinder bore is left as its first pass described it (the reference writes run depth/voids only for through bores) -- off_cardinal's counterbore depth 23 -> 14. | fixed |
| aerospace_tensioner_pulley (+seam37) | Merging split fragments copies only the more specific type; the reference keeps the first fragment's parameters (here: unsigned axis, `flat_bottom` false past the relief). | fixed |
| torture_hole_labyrinth, freeform_transition_bracket, nist_ctc_02, nist_ftc_08, heat_exchanger_tube_sheet, relieved_logo_plaque, sm_hybrid_milled_pocket, torture_casting_ribfield, torture_machinists_maze | **The reference analyses a meshed part.** Its STEP import meshes every shape (`XdeProcessor::compute_model_hash`, xde_processor.cpp:878: `BRepMesh_IncrementalMesh(shape, 1.0, false, 0.5)`) before the AAG is built, so every `BRepBndLib::Add` box after that -- each AagNode.bbox, every envelope a rule unions from them -- comes from the triangulation (tight on spline faces, enlarged by the mesh deflection elsewhere). `MachiningAnalyzer` now does the same on a copy (`_as_the_reference_reads_it`: `BRepBuilderAPI_Copy` without its mesh, `BRepTools.Clean`, then the reference's mesh call), so the document shape is untouched and view-deflection meshes cannot leak in; face ids map 1:1 (checked on four fixtures). Without the deflection enlargement (nodes plus tolerance only) none of these fixtures is fixed, so the reference's boxes do carry it. Sheet stays 57/57. | fixed |
| nist_ftc_06 | Broken by the mesh. A Ø25.4 spherical face meshes at deflection 0.724 under OCCT 7.8, which enlarges its box and so the part box by 0.72 mm on every side. `is_part_outer_face` (tolerance 0.5, read off that part box in both engines) then finds no outer face at all, and two flood cavities absorb outer faces 48/101 and swallow the two slit SLOTs. Both ports read the same mesh-derived box; the reference's goldens keep the slots, and its own comments calibrate the 0.5 mm tolerance against ~0.1 mm of padding, so its OCCT 8.0 mesher must leave that sphere under 0.5 mm. | kernel (probable: mesher deflection) |
| nist_ftc_10 | Not a mesh effect: the extra 7-face, 6-corner THROUGH_CAVITY [182-188] swallowing slt_2/slt_3 is identical with and without the mesh and predates this lane (present at P5-115); no face's box overhangs the planar envelope here, so `is_part_outer` is unaffected. One of the reference's flood gates rejects it and ours does not; needs a trace of the through-cavity gates. | open (through-cavity recognizer) |

Also ported in passing: `thread_wall_thickness`'s no-plane fallback is the reference's Fallback B (first bore wall's centroid to the whole-part box, less the radius) rather than the edge-distance measurement. The external-thread helix route has no reference counterpart; no golden contains EXTERNAL_THREAD and no fixture shows drift from it, so it stays.

Unit tests changed: `test_machining_threads.py` (tapped-hole tests now use a bore whose seam is a spline -- Check 1's signature -- and a swept helix is asserted refused, as the reference's cross-bore guard refuses it); `test_machining_thread_rules.py` (tapped holes are declared, the swept helix no longer promotes or measures as one hole); `test_machining_thread_sources.py` (same); `test_machining_holes.py` (the network case is built from unfragmented bores: the reference measures each hole along its seed cylinder only).

### F: the last four (chamfer frames, slit cavity, freeform pocket, sphere mesh)

| Fixture | Cause | Outcome |
|---|---|---|
| pocket_chamfered_top | Not a fixture problem: any FreeCAD part chamfered by OCCT 7.8's `BRepFilletAPI_MakeChamfer` gets left-handed chamfer planes, and reading `Plane().Axis().Direction()` as the normal turns them inside out (four false UNDERCUTs here). `AagBuilder` now stores the surface's own normal (D1U x D1V): the axis on a direct frame, reversed on an indirect one; the orientation flip follows as before (`outward_normal`, `corner_access`, `relief_raster`). The reference reads the axis as is, and on OCCT 8.0 those planes are direct, so on its kernel the two readings agree. This is the plane counterpart of the reference's own `material_side` fix for cylinders and cones (9ac3e748b), and works like the PMI KernelBridge. No other plane-normal derivation in the engine needed it: `part_checks.py` and the builder's same-surface test only compare axes up to sign. Corpus: fixed 1, broken 0 (`.resync\F-1.json`). | fixed |
| nist_ftc_10 | Our flood pass counted corners as every concave **or convex** meeting inside the set (`_count_corners`, a local addition). The reference's `count_corners` counts CONCAVE meetings only, in both passes, and has done so since the recognizer was written. The two 5 mm slits joined across their convex ledge counted 6 here, which skipped the four-corner handover. The reference counts 4, and its own trace table beside `kThroughSpanShare` lists this exact candidate as a rejection at span share 0.7419. Both passes now use `_concave_corners`. One unit test changed: a T-slot reports `corner_count` 4, as the reference reports it. It is still claimed, because its walls reach through the part. | fixed |
| torture_freeform_leviathan (POCKET) | R3's premise was wrong: a default-constructed `gp_Dir` is +X on OCCT 7.8 (checked in OCP) and on 8.0 (header), not +Z. The reference's floor test reads `plane_normal` on every non-cylinder, non-cone neighbour, so the B-spline walls 29 and 30 read as (1,0,0). Both sit on x = 0, and their centroid offsets from floor 32 along +X are +4.5e-7 and +3.0e-9, so both pass "faces the floor" and the floor gets two walls. Ported as `_normal_as_the_reference_reads_it` in the floor test. The result hangs on centroid rounding, which looks unintended in the reference; it is ported anyway. While there, the BFS opening-face guard (majority convex to planar outside faces, unless they are anti-parallel) now applies to every neighbour. Before, it applied to planes only. The reference applies it to all types, and only the squareness test is plane-only. | fixed |
| torture_freeform_leviathan (turned_profile_radius) | The reference runs `turned_profile_radius` on every part. Ours listed it only in `cnc_turning.yaml`, and this MILL_TURN fixture is analysed under milling. It is now in `cnc_milling.yaml` (rule list, messages, priority MEDIUM, config N/A), as R1 did for `thread_relief_width`. With the pocket in place, the FREEFORM_SURFACE on face 29, the four `sharp_internal_edge` findings and the missing `setup_count_high` INFO all fell in line. | fixed |
| nist_ftc_06 | Still a kernel difference, with more evidence now. Under OCCT 7.8 the reference's mesh call (1.0, angular 0.5) stores deflection 0.7237 on sphere face 13 (R12.7). Its same-radius twin, face 12, stores 0.4308. Face 13 has an edge whose midpoint sags 0.695 mm, a 0.67 rad chord, past the 0.5 rad angular limit. Four cones (61/62/124/125, R up to 15.875) store 0.5031, also just over the 0.5 mm outer-face tolerance. `Bnd_Box::Add(Bnd_Box)` keeps one gap, the maximum, and applies it to every side (checked: a 0.7-gap box inside a 0.1-gap box gives a 0.7 gap all round). So the part box is 0.7237 proud everywhere, and even without the sphere it would be 0.5031 proud. That is enough to make `is_part_outer_face` (tolerance 0.5) find nothing. For the reference to keep its slots, its OCCT 8.0 mesh must keep every face of this part under about 0.5. The headers we have (7.9.3 and 8.0, no sources) show no change of meaning: `Bnd_Box` still has one `Gap` with `Enlarge` = max, and `BRepBndLib::Add` still documents deflection plus tolerance. The `BRepMesh` sphere, cone, mesh-algo-factory, deflection and base-algo headers differ only in C++ modernisation. The one documentation change is `BRepMesh_DefaultRangeSplitter::computeLengthU/V`, now "using grid evaluation", and its effect cannot be read from a header. The reference's GLB is meshed at 0.05/0.2 after analysis, so it says nothing about the 1.0 mesh. There is no demonstrable OCCT 8 box rule to reproduce. | kernel |

### Final state (2026-11-09)

Every "open" row in the R1-R3 tables above was closed by a later lane: the
tensioner pulley and the machinists' maze by R1/R3, `knurl_neg_flatted_shaft`'s
thin wall by R3, `nist_ftc_10` and `torture_freeform_leviathan` by F. And
`nist_ftc_06` matches after all. Its sphere still meshes at 0.72 mm deflection
under OCCT 7.8, and the part box is still padded by that much, but the two
flood cavities that padding admitted are now rejected by the reference's
concave-only corner count (F, `nist_ftc_10` row), so the slots survive. The
mesh-padding difference is real and is worth remembering when a fixture
with a coarse-meshing sphere moves; it no longer changes any golden.

Final run: 280 of 280 (`check_golden.py --jobs 4`, 681 s), full unit suite
1237 passed.

## Scratch tooling

Not committed — these live in the session scratchpad and are rebuilt as
needed. Worth knowing they exist before writing them again:

- a per-rule parity table, ours against the reference's raw counts
- a per-part breakdown for one rule
- a "why was this edge suppressed" tracer for `sharp_internal_edge`
- a feature census dump for one fixture

**One warning.** The parity harness wraps each check in a broad `except` and
counts a raised exception as zero findings. Three times this session that
turned a crash into a plausible-looking number — a missing import once read
as a clean parity result. Narrow the catch before trusting a figure that
moved further than expected.
