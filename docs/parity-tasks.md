<!--
SPDX-License-Identifier: LGPL-2.1-or-later
SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
-->

# Outstanding parity work

Where this workbench still disagrees with the reference engine over the
211-part corpus, what causes each disagreement, and which fixture to open
first.

**56 of 75 rules agree exactly.** Totals over the 211-part corpus, in
occurrences rather than published rows: 1121 here, 1218 there, L1 131.
Process classification agrees on all 211 parts, and 157 of them produce an
identical set of findings.

Ninety-three of that 131 is `sharp_internal_edge`, which is parked
deliberately (§5), and fourteen more is one settings bug (§0a). The live tail
is **24 findings across eleven rules**.

## Status

Measured 2026-09-08, against the reference at `26e76d14`.

| rule | ours | reference | delta | where |
| --- | --- | --- | --- | --- |
| `sharp_internal_edge` | 54 | 147 | −93 | parked, §5 |
| `setup_count_high` | 42 | 48 | −6 | one apiece over four fixtures |
| `pocket_square_corner` | 89 | 84 | +5 | `optical_periscope_housing` 11 of 9 |
| `sheet_formed_pitch` | 10 | 6 | +4 | §0a |
| `thin_wall` | 102 | 99 | +3 | `optical_periscope_housing` 4 of 8, `electronics_enclosure_billet` 28 of 31 |
| `sheet_bend_angle_extreme` | 9 | 7 | +2 | §0a |
| `sheet_hem_dimensions` | 2 | 4 | −2 | §0a |
| `sheet_hole_near_bend` | 1 | 3 | −2 | §0a |
| `sheet_hole_pitch` | 0 | 2 | −2 | §0a |
| `hole_deep_risk` | 79 | 81 | −2 | `torture_hole_labyrinth`, `hole_countersink_angle` |
| `hole_flat_bottom` | 74 | 76 | −2 | `countersink`, `off_cardinal_viz_block` |
| `sheet_flange_short` | 0 | 1 | −1 | §0a |
| `sheet_formed_near_bend` | 3 | 4 | −1 | §0a |
| `part_aspect_ratio` | 11 | 12 | −1 | one fixture |
| `material_removal_high` | 9 | 10 | −1 | one fixture |
| `turned_profile_radius` | 2 | 3 | −1 | one fixture |
| `hole_edge_distance` | 34 | 33 | +1 | `euro_lock_cylinder` 0 of 2 |
| `small_part_holding` | 2 | 1 | +1 | `small_part` |
| `cutter_radius_suboptimal` | 3 | 2 | +1 | `wr90_waveguide_flange` |

Four parts carry 82 of the 131, and all four of them are
`sharp_internal_edge`: `photonic_fiber_v_groove_array` (13 of 53),
`progressive_die_punch` (4 of 24), `torture_machinists_maze` (26 of 37) and
`complex_undercut_part_v2` (11 of 22).

Setting those aside, the periscope housing is the part worth opening first:
it carries the largest share of both remaining spreads, over on square
corners and under on thin walls at once.

## 0a. The shipped sheet limits override the rules

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

## 5. Parked

### `sharp_internal_edge` −93

Parked deliberately. The accessibility work is done: the escape vote and the
cutter-formed test are ported, and over-reporting went from 250 findings to
54. What is left is under-reporting, and it is recognition coverage — where
our recognizers claim a cavity the reference leaves loose, we report the
cavity once through its own rule and the reference reports each of its
corners as well.

Worst: `torture_machinists_maze` 8 vs 19, `progressive_die_punch` 0 vs 20,
`complex_undercut_part_v2` 4 vs 15, `sm_lookalike_billet` 0 vs 8.

Over-reporting that remains: `torture_freeform_leviathan` 4 vs 0,
`heat_exchanger_tube_sheet` 2 vs 0.

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
