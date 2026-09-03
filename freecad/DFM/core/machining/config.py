# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Shop capability and rule thresholds for machining analysis.

Two kinds of setting live here, and the distinction matters:

* **Thresholds** are what a rule compares against. A few are genuinely
  material-dependent and belong in the process YAML's material block; the rest
  are shop policy and live here.
* **Capability** is what the shop can actually do -- machine mode, the tool
  library, the drill catalogs. No material changes them.

Defaults are conservative general-purpose values. Everything is overridable
from the DFM preferences page.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Optional


# =============================================================================
# Tooling
# =============================================================================


@dataclass
class ToolEntry:
    """One tool, or one size range, in the shop's library.

    `unit` records the sizing system: "metric", "imperial", or empty for
    unit-agnostic tools such as boring bars and turning inserts. Rules that
    match a feature dimension against a stocked size honour the configured
    unit system; rules that ask a physical question -- will this flute reach,
    can this corner radius be cut -- consider every tool regardless.
    """

    type: str  # end_mill | ball_nose | drill | tap | reamer | boring_bar | turning_insert
    min_diameter_mm: float = 1.0
    max_diameter_mm: float = 25.0
    corner_radius_mm: float = 0.0  # 0 = sharp; D/2 on a ball nose marks the full tip
    max_flute_length_mm: float = 30.0
    max_reach_mm: float = 60.0  # holder plus flute
    unit: str = ""

    def fits_diameter(self, diameter_mm: float, tolerance_mm: float = 0.0) -> bool:
        return (
            self.min_diameter_mm - tolerance_mm
            <= diameter_mm
            <= self.max_diameter_mm + tolerance_mm
        )


_METRIC_END_MILLS = (
    1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0,
    10.0, 12.0, 14.0, 15.0, 16.0, 18.0, 20.0, 25.0,
)

_METRIC_BALL_NOSES = (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0)

_IMPERIAL_END_MILLS = (
    1.588, 2.381, 3.175, 3.969, 4.763, 6.350, 7.938,
    9.525, 11.113, 12.700, 15.875, 19.050, 25.400,
)

_TURNING_INSERT_NOSE_RADII = (0.2, 0.4, 0.8, 1.2)

_METRIC_TAPS = (
    2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0,
    14.0, 16.0, 18.0, 20.0, 24.0, 27.0, 30.0,
)

# UNC number sizes #2-#12 plus fractional 1/4"-1".
_IMPERIAL_TAPS = (
    2.184, 2.845, 3.505, 4.166, 4.826, 5.486,
    6.350, 7.938, 9.525, 11.113, 12.700, 15.875, 19.050, 25.400,
)

_METRIC_REAMERS = (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 25.0)

_IMPERIAL_REAMERS = (3.175, 4.763, 6.350, 7.938, 9.525, 12.700, 15.875, 19.050, 25.400)


def default_tool_library() -> list[ToolEntry]:
    """A general-purpose job-shop library: metric and imperial, mill and lathe."""
    tools: list[ToolEntry] = []

    def end_mill(diameter: float, corner_radius: float, unit: str) -> ToolEntry:
        return ToolEntry(
            type="end_mill",
            min_diameter_mm=diameter,
            max_diameter_mm=diameter,
            corner_radius_mm=corner_radius,
            max_flute_length_mm=diameter * 3.0,
            max_reach_mm=diameter * 6.0,
            unit=unit,
        )

    for diameter in _METRIC_END_MILLS:
        tools.append(end_mill(diameter, 0.0, "metric"))
        if diameter >= 3.0:
            tools.append(end_mill(diameter, diameter * 0.05, "metric"))

    for diameter in _METRIC_BALL_NOSES:
        tools.append(
            ToolEntry(
                type="ball_nose",
                min_diameter_mm=diameter,
                max_diameter_mm=diameter,
                corner_radius_mm=diameter * 0.5,
                max_flute_length_mm=diameter * 3.0,
                max_reach_mm=diameter * 6.0,
                unit="metric",
            )
        )

    for diameter in _IMPERIAL_END_MILLS:
        tools.append(end_mill(diameter, 0.0, "imperial"))

    for nose_radius in _TURNING_INSERT_NOSE_RADII:
        tools.append(
            ToolEntry(
                type="turning_insert",
                min_diameter_mm=0.0,
                max_diameter_mm=0.0,
                corner_radius_mm=nose_radius,
                max_flute_length_mm=0.0,
                max_reach_mm=0.0,
            )
        )

    tools.append(
        ToolEntry(
            type="drill",
            min_diameter_mm=0.5,
            max_diameter_mm=32.0,
            max_flute_length_mm=80.0,
            max_reach_mm=100.0,
        )
    )

    for diameters, unit in ((_METRIC_TAPS, "metric"), (_IMPERIAL_TAPS, "imperial")):
        for diameter in diameters:
            tools.append(
                ToolEntry(
                    type="tap",
                    min_diameter_mm=diameter,
                    max_diameter_mm=diameter,
                    max_flute_length_mm=diameter * 2.5,
                    max_reach_mm=diameter * 4.0,
                    unit=unit,
                )
            )

    for diameters, unit in ((_METRIC_REAMERS, "metric"), (_IMPERIAL_REAMERS, "imperial")):
        for diameter in diameters:
            tools.append(
                ToolEntry(
                    type="reamer",
                    min_diameter_mm=diameter,
                    max_diameter_mm=diameter,
                    max_flute_length_mm=diameter * 3.0,
                    max_reach_mm=diameter * 5.0,
                    unit=unit,
                )
            )

    tools.append(
        ToolEntry(
            type="boring_bar",
            min_diameter_mm=10.0,
            max_diameter_mm=100.0,
            corner_radius_mm=0.4,
            max_flute_length_mm=80.0,
            max_reach_mm=150.0,
        )
    )

    return tools


# Jobber metric drills, including the ISO coarse tap drills a shop actually
# stocks (3.3 for M4, 4.2 for M5, 6.8 for M8, 10.2 for M12, 17.5 for M20).
METRIC_DRILL_SIZES_MM = (
    0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 3.2, 3.3, 3.5, 4.0, 4.2, 4.3, 4.5,
    5.0, 5.5, 6.0, 6.35, 6.5, 6.8, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0, 10.2,
    10.5, 11.0, 11.5, 12.0, 12.7, 13.0, 14.0, 15.0, 16.0, 17.0, 17.5, 18.0,
    19.0, 20.0, 22.0, 24.0, 25.0, 25.4, 28.0, 30.0, 32.0,
)

# Fractional inch, then number drills, then letter drills.
IMPERIAL_DRILL_SIZES_MM = (
    1.588, 2.381, 3.175, 3.572, 3.969, 4.366, 4.763, 5.159, 5.556, 5.953,
    6.350, 6.747, 7.144, 7.541, 7.938, 8.334, 8.731, 9.128, 9.525, 9.922,
    10.319, 10.716, 11.113, 11.509, 11.906, 12.303, 12.700, 13.494, 14.288,
    15.081, 15.875, 16.669, 17.463, 18.256, 19.050, 19.844, 20.638, 21.431,
    22.225, 23.019, 23.813, 24.606, 25.400,
    5.791, 5.613, 5.410, 5.309, 5.105, 4.915, 4.851, 4.623, 4.496, 4.394,
    4.216, 4.089, 3.988, 3.861, 3.797, 3.734, 3.454, 3.264, 2.946, 2.819,
    2.705, 2.642, 2.527, 2.489, 2.375, 2.261, 2.083, 2.057, 1.854, 1.511,
    1.397, 1.321, 1.181, 1.092, 1.069, 1.041, 1.016,
    5.944, 6.045, 6.147, 6.248, 6.350, 6.528, 6.629, 6.756, 6.909, 7.036,
    7.137, 7.366, 7.493, 7.671, 8.026, 8.204, 8.433, 8.611, 8.839, 9.093,
    9.347, 9.576, 9.804, 10.084, 10.262, 10.490,
)


# =============================================================================
# Thresholds
# =============================================================================


@dataclass
class RuleThresholds:
    """Every configurable number a machining rule compares against.

    Flat by design: rules read one field each, and a flat structure keeps the
    preferences mapping and the YAML schema trivial.
    """

    # -- hole ---------------------------------------------------------------
    hole_deep_warn_ratio: float = 6.0  # depth / diameter
    hole_deep_error_ratio: float = 10.0
    # An on-axis bore in a turned part is made with a boring bar, which
    # deflects far sooner than a drill.
    hole_deep_bore_warn_ratio: float = 4.0
    hole_deep_bore_error_ratio: float = 8.0
    # A crossing void wider than this many diameters means the hole is drilled
    # from both sides, so the longest contiguous run is what the drill sees.
    hole_deep_single_pass_void_ratio: float = 2.0
    hole_edge_distance_mm: float = 2.0
    hole_web_thickness_mm: float = 1.5
    hole_flat_bottom_max_diameter_mm: float = 10.0
    hole_flat_bottom_min_diameter_mm: float = 2.0
    hole_nonstandard_max_diameter_mm: float = 25.0
    hole_intersecting_network_threshold: int = 5
    hole_intersecting_thin_plate_dd_max: float = 0.75
    standard_size_match_tol_mm: float = 0.05
    # Previously buried in the rule: a bore must be at least this slender
    # before a skewed entry matters, and an entry face is "square enough"
    # when the axis-normal dot exceeds the second value.
    hole_partial_entry_min_depth_ratio: float = 3.0
    hole_partial_entry_perpendicular_dot: float = 0.85
    hole_countersink_angle_tol_deg: float = 2.0

    # -- thread -------------------------------------------------------------
    thread_runout_min_pitches: float = 2.5
    thread_runout_min_diameters: float = 2.0  # fallback when the pitch is unknown
    thread_shoulder_min_mm: float = 1.5
    thread_shoulder_min_height_mm: float = 2.0
    thread_relief_min_width_mm: float = 1.5
    thread_relief_pitch_multiplier: float = 1.5
    thread_diameter_mismatch_mm: float = 0.5
    # Approximate thread root depth as a fraction of nominal diameter.
    thread_cut_depth_ratio: float = 0.1

    # -- pocket -------------------------------------------------------------
    pocket_deep_warn_ratio: float = 4.0  # depth / min width
    pocket_deep_error_ratio: float = 6.0
    pocket_corner_radius_min_mm: float = 0.0  # 0 = any sharp corner is reportable
    # A pocket must be at least this many cutter diameters wide to be milled
    # without a custom tool.
    pocket_narrow_opening_tool_multiple: float = 2.0

    # -- slot ---------------------------------------------------------------
    slot_deep_warn_ratio: float = 3.0
    slot_overhang_warn_ratio: float = 6.0  # length / width
    slot_overhang_depth_gate_ratio: float = 2.5  # chatter also needs stickout
    slot_nonstandard_width_max_mm: float = 8.0

    # -- thin features ------------------------------------------------------
    thin_wall_warn_mm: float = 1.5
    thin_wall_error_mm: float = 0.8
    thin_wall_aspect_warn: float = 12.0
    # Deflection scales with thickness cubed, so a 6 mm wall is rigid at any
    # practical length. Without this cap the aspect path fires on sections no
    # machinist would call a wall.
    thin_wall_aspect_max_thickness_mm: float = 4.0

    # -- boss / rib ---------------------------------------------------------
    rib_height_aspect_warn: float = 5.0
    rib_min_draft_angle_deg: float = 1.0
    boss_min_diameter_mm: float = 3.0
    boss_height_warn_ratio: float = 4.0  # height / base dimension
    boss_height_error_ratio: float = 8.0
    # A boss axis further off every machine cardinal than this needs an
    # indexed setup.
    boss_cardinal_alignment_min_dot: float = 0.95
    spherical_overhang_warn_mm: float = 1.0

    # -- tool access / setup ------------------------------------------------
    tad_angular_cluster_deg: float = 15.0
    setup_count_info_min: int = 2
    setup_count_warn: int = 3
    setup_count_error: int = 4
    datum_face_min_area_mm2: float = 200.0
    min_jaw_separation_mm: float = 5.0
    min_clamping_thickness_mm: float = 3.0
    # Below this in every bbox dimension the part is not vise-held at all, so
    # the vise-assumption rules stand down and one holding note is emitted.
    small_part_max_dim_mm: float = 30.0
    # Two faces count as square to each other when their normals' dot is below
    # this. Used by the orthogonal-datum-trio check.
    datum_perpendicular_max_dot: float = 0.1
    # A form cutter is ground to an exact angle, so a wall tilt only matches a
    # catalogue cutter within this.
    shaped_cutter_angle_tol_deg: float = 2.0

    # -- part ---------------------------------------------------------------
    part_aspect_warn_ratio: float = 8.0  # milled: bbox longest / shortest
    # Once the aspect ratio trips, mid/shortest separates a plate from a bar.
    # They warrant different advice: bars deflect and chatter, plates warp.
    plate_mid_min_ratio: float = 3.0
    turn_slender_warn_ratio: float = 4.0  # turned: length along axis / max OD
    turn_slender_error_ratio: float = 8.0
    minimum_feature_size_mm: float = 0.5

    # -- sheet metal ---------------------------------------------------------
    #
    # Almost all of these are multiples of the gauge rather than absolute
    # sizes, because that is how a sheet shop thinks: a bend radius is "one
    # material thickness", not "1.5 mm". The few that are absolute are
    # absolute for a physical reason -- a punch breaks below a size whatever
    # the sheet is, and a countersink has to leave a land.
    sheet_bend_radius_warn_factor: float = 1.0
    sheet_bend_radius_error_factor: float = 0.5
    sheet_min_flange_factor: float = 4.0
    sheet_hole_bend_clearance_factor: float = 2.5
    sheet_min_hole_factor: float = 1.0
    sheet_hole_pitch_factor: float = 2.0
    sheet_max_countersink_depth_factor: float = 0.6
    sheet_min_countersink_land_mm: float = 0.3
    # How far a brake can over-bend before springback and tooling get
    # awkward, which depends on how heavy the stock is.
    sheet_max_bend_deg_at_ga11: float = 125.0
    sheet_max_bend_deg_at_ga14: float = 180.0
    sheet_min_thickness_mm: float = 0.305
    sheet_max_thickness_steel_mm: float = 3.175
    sheet_max_thickness_alu_mm: float = 6.0
    sheet_min_tab_width_mm: float = 3.2
    sheet_tab_width_factor: float = 2.0
    sheet_tab_max_aspect: float = 5.0
    sheet_min_notch_factor: float = 1.0
    sheet_notch_max_depth_ratio: float = 10.0
    sheet_hem_min_return_factor: float = 4.0
    sheet_emboss_max_depth_factor: float = 3.0
    # 6.35 mm of hood on 1.897 mm stock: the tallest standard louver die.
    sheet_louver_max_height_factor: float = 3.3474
    sheet_formed_min_pitch_factor: float = 2.0
    sheet_formed_bend_clearance_factor: float = 3.0
    sheet_dimension_eps_mm: float = 0.02
    # Below this gauge a diagonal bend is a nuisance rather than a problem.
    sheet_diagonal_bend_min_thickness_mm: float = 1.897
    sheet_bend_over_length_eps_mm: float = 0.5
    # How many distinct features before the part is worth pricing as a
    # complicated one. Not a defect at any count: programming time and setup
    # count follow feature count more closely than any single dimension, so a
    # part can pass every other rule and still run long.
    feature_complexity_warn: float = 40.0
    feature_complexity_error: float = 80.0
    sharp_edge_min_deviation_deg: float = 30.0
    material_removal_warn_pct: float = 70.0
    material_removal_error_pct: float = 85.0
    chamfer_standard_angle_deg: float = 45.0
    chamfer_angle_tol_deg: float = 3.0

    # -- freeform -----------------------------------------------------------
    freeform_radius_safety: float = 2.0  # concave radius must exceed tool r by this
    freeform_radius_info_tier_mm: float = 3.0
    freeform_scallop_target_um: float = 25.0
    freeform_blend_band_max_radius_mm: float = 10.0
    freeform_finishing_min_area_mm2: float = 300.0

    # -- recognizer policy --------------------------------------------------
    # Read by recognizers rather than rules, but they decide which process
    # branch every process-aware rule takes, so they belong in the same place.
    flexure_slit_max_width_mm: float = 4.0
    flexure_slit_min_depth_ratio: float = 3.0
    turned_fraction_turned_min: float = 0.80
    turned_fraction_milled_max: float = 0.20
    turned_convex_share_min: float = 0.10
    profile_bar_min_length_ratio: float = 2.0

    # -- tolerance capability (dormant until a tolerance source exists) ------
    tol_grinding_max_mm: float = 0.02
    tol_lapping_max_mm: float = 0.005
    gdt_position_achievable_3axis_mm: float = 0.05
    gdt_position_achievable_3plus2_mm: float = 0.025
    gdt_position_achievable_5axis_mm: float = 0.01
    gdt_form_achievable_3axis_mm: float = 0.025
    gdt_form_achievable_3plus2_mm: float = 0.015
    gdt_form_achievable_5axis_mm: float = 0.005
    ra_lapping_um: float = 0.1
    ra_grinding_um: float = 0.8
    ra_standard_mill_um: float = 1.6

    def apply_overrides(self, overrides: dict) -> None:
        """Apply a sparse mapping of name -> value.

        A missing key means "keep the default", never zero. Integer fields are
        coerced as integers: rounding them through a float is a mistake the
        reference implementation made and had to correct.
        """
        known = {f.name for f in fields(self)}
        for name, value in overrides.items():
            if name not in known or value is None:
                continue
            # `fields()` reports annotations as strings here, so the field's
            # own default is the reliable way to tell an int field from a
            # float one.
            wants_int = isinstance(getattr(self, name), int) and not isinstance(
                getattr(self, name), bool
            )
            try:
                setattr(self, name, int(float(value)) if wants_int else float(value))
            except (TypeError, ValueError):
                continue


# =============================================================================
# Configuration
# =============================================================================

MACHINE_MODES = ("3axis", "3plus2", "5axis")
UNIT_SYSTEMS = ("metric", "imperial", "both")
BLANK_FORMS = ("", "billet", "as_cast", "profile_extrusion")


@dataclass
class MachiningConfig:
    """Everything an analysis needs to know about the shop and its policy."""

    machine_mode: str = "3axis"
    unit_system: str = "both"
    # Declared, never inferred from geometry: a shop knows what stock it
    # ordered and no analysis can recover that reliably.
    blank_form: str = ""
    material_family: str = ""
    precision_mode: bool = False

    tool_library: list[ToolEntry] = field(default_factory=default_tool_library)
    drill_sizes_mm: list[float] = field(default_factory=lambda: list(METRIC_DRILL_SIZES_MM))
    thresholds: RuleThresholds = field(default_factory=RuleThresholds)

    disabled_rules: list[str] = field(default_factory=list)
    disabled_categories: list[str] = field(default_factory=list)

    # -- queries ------------------------------------------------------------

    def is_rule_disabled(self, rule_id: str) -> bool:
        return rule_id in self.disabled_rules

    def is_category_disabled(self, category: str) -> bool:
        return category in self.disabled_categories

    def tool_unit_enabled(self, tool: ToolEntry) -> bool:
        """Whether a size-matching rule should consider this tool."""
        return self.unit_system == "both" or not tool.unit or tool.unit == self.unit_system

    def tools_of_type(self, tool_type: str, unit_filtered: bool = False) -> list[ToolEntry]:
        tools = [t for t in self.tool_library if t.type == tool_type]
        if unit_filtered:
            tools = [t for t in tools if self.tool_unit_enabled(t)]
        return tools

    def smallest_end_mill_diameter(self, unit_filtered: bool = True) -> Optional[float]:
        diameters = [t.min_diameter_mm for t in self.tools_of_type("end_mill", unit_filtered)]
        return min(diameters) if diameters else None

    def smallest_internal_corner_radius(self) -> Optional[float]:
        """The tightest inside corner the shop can cut.

        This is half the smallest end-mill *diameter*, not the smallest
        corner-radius field: a sharp end mill has a zero corner radius but
        still leaves its own radius in an inside corner.
        """
        diameter = self.smallest_end_mill_diameter(unit_filtered=False)
        return diameter / 2.0 if diameter else None

    def standard_corner_radii(self) -> list[float]:
        """Inside radii a sharp end mill leaves, ascending and deduplicated."""
        radii = {
            round(t.min_diameter_mm / 2.0, 6)
            for t in self.tools_of_type("end_mill")
            if t.corner_radius_mm <= 0.001
        }
        return sorted(radii)

    def smallest_turning_nose_radius(self) -> Optional[float]:
        radii = [t.corner_radius_mm for t in self.tools_of_type("turning_insert")]
        return min(radii) if radii else None

    def all_drill_sizes_mm(self) -> list[float]:
        """Every drill the shop stocks under the configured unit system."""
        sizes: list[float] = []
        if self.unit_system in ("metric", "both"):
            sizes.extend(self.drill_sizes_mm or METRIC_DRILL_SIZES_MM)
        if self.unit_system in ("imperial", "both"):
            sizes.extend(IMPERIAL_DRILL_SIZES_MM)

        sizes.sort()
        deduplicated: list[float] = []
        for size in sizes:
            if not deduplicated or abs(size - deduplicated[-1]) >= 0.001:
                deduplicated.append(size)
        return deduplicated

    # -- preferences --------------------------------------------------------

    @classmethod
    def from_preferences(cls, prefs: Optional[dict] = None) -> "MachiningConfig":
        """Build a config from the flat DFM preference dictionary.

        Threshold keys are prefixed ``Machining`` in the parameter store so
        they cannot collide with the existing analyzer preferences.
        """
        config = cls()
        if not prefs:
            return config

        mode = str(prefs.get("MachiningMachineMode", config.machine_mode))
        if mode in MACHINE_MODES:
            config.machine_mode = mode

        units = str(prefs.get("MachiningUnitSystem", config.unit_system))
        if units in UNIT_SYSTEMS:
            config.unit_system = units

        blank = str(prefs.get("MachiningBlankForm", config.blank_form))
        if blank in BLANK_FORMS:
            config.blank_form = blank

        config.material_family = str(prefs.get("MachiningMaterialFamily", ""))
        config.precision_mode = bool(prefs.get("MachiningPrecisionMode", False))

        prefix = "MachiningThreshold"
        overrides = {
            key[len(prefix) :]: value
            for key, value in prefs.items()
            if key.startswith(prefix)
        }
        config.thresholds.apply_overrides(overrides)
        return config
