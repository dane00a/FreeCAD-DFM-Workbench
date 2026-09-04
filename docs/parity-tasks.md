<!--
SPDX-License-Identifier: LGPL-2.1-or-later
SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
-->

# Outstanding parity work

Where this workbench still disagrees with the reference engine over the
211-part corpus, what causes each disagreement, and which fixture to open
first.

**46 rules agree exactly. 29 differ.** Raw totals: 1091 here, 1218 there.

## Status, and a warning about the numbers below

The rule-level figures in this document were measured before the recognition
work of 2026-09-04 and have not been re-measured since. That work changed
what the rules are looking at, so treat every count below as an indication of
where to look rather than as a current reading. Several entries name causes
that no longer exist.

Recognition itself is now close. Against the reference's own `features.json`
over the 211-part corpus, total L1 error is 21 and 198 fixtures match exactly,
from 479 and 99 at the start of that day's work. Fifteen feature types agree
exactly, including every hole type. What remains, per type and net of sign:

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

A note on re-measuring the rules: the scratch parity harness compares rule
ids literally, and several of ours are named differently from the reference's
for the same concern -- `pocket_corner_radius`/`pocket_square_corner`,
`hole_depth_ratio`/`hole_deep_risk`, `slot_depth_ratio`/`slot_deep_risk`,
`pocket_depth_ratio`/`pocket_deep_risk`, `boss_height_ratio`/`boss_height_risk`,
`material_removal`/`material_removal_high`. Its raw output shows those as
total disagreements when they are the same finding under two names. Anything
read from it needs that translation applied first, on top of the
`occurrence_count` summing described below.

## Reading the numbers

Compare raw against raw. The reference collapses identical findings into one
row and records only the merged one, so its published counts are what a
reader sees rather than what its rules found — 448 occurrences across the
corpus sit behind 135 rows. Every count in this document is raw, recovered by
summing `details.occurrence_count`.

Getting this wrong once cost real time: `thin_wall` looked like 46 against 40
and was 46 against 99.

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

We recognize 4 RIB and 4 BOSS; the reference recognizes 19 "other". Our rib
recognizer is finding ribs, and the draft rule then says nothing about them.
Check whether `rib_draft_angle` is reading a parameter our rib recognizer
does not set.

Same fixture: `slot_depth_ratio` 1 vs 3, `slot_overhang` 1 vs 0.

---

## 2. Rules that stop short

### 2a. `feature_complexity` −7 — silent on a part with nothing on it

Every one of the seven is a part where we report 0 and the reference reports
1: `simple_box`, `small_part`, `thin_sheet`, `sealed_void`,
`single_datum_dome`, `thin_threshold_3_1mm`, `drafted_fin_channel`.

Our rule returns early when the feature count is zero. The reference still
emits its census — "0 features, ~0 operations" is a true and useful statement
about a part somebody is quoting.

**Smallest fix in this document.** One early return in
`part_detail_checks.py`.

### 2b. `setup_count_high` −16 — every approach direction is treated as reversible

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

`optical_periscope_housing` 11 vs 9 and seven parts at 1 vs 0, against
several at 0 vs 1. Likely the same cavity-recognition scope question as §1.

### 3c. `hole_edge_distance` −6, `hole_flat_bottom` −6

Both mixed. `hydraulic_actuator_end_cap` 0 vs 4 and `cf_knife_edge_flange` 2
vs 6 are the biggest single gaps in the first; `robotic_gripper_finger` 7 vs
9 and `fixture_plate_dowels` 2 vs 4 in the second.

### 3d. `hole_intersects_cavity` +4

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
