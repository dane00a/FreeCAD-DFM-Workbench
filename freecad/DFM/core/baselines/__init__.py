# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Approved answers for the fixture corpus, and what moved since.

Four layers, and only the first two can be imported without OpenCascade:

``spec``      the schema and the comparison -- counts in, drift lines out.
``observe``   turns this workbench's findings and features into those counts.
``store``     the approved file, read and written as a whole.
``harness``   runs the rulebook over a shape; ``corpus`` finds the recipes.

The split is what lets the comparison be tested on hand-written dicts with
no geometry anywhere in sight, which is the only way the traps in it -- an
absent block meaning "not asserted", a bidirectional map, a boolean that is
also an integer -- get covered at all.
"""

from .spec import (
    BaselineSpec,
    DriftResult,
    FEATURE_DISCRIMINANTS,
    FindingTally,
    Observation,
    check_drift,
    spec_from_observation,
    tally_features,
    tally_findings,
    tally_pmi,
)
from .store import DEFAULT_BASELINE_PATH, BaselineStore

__all__ = [
    "BaselineSpec",
    "BaselineStore",
    "DEFAULT_BASELINE_PATH",
    "DriftResult",
    "FEATURE_DISCRIMINANTS",
    "FindingTally",
    "Observation",
    "check_drift",
    "spec_from_observation",
    "tally_features",
    "tally_findings",
    "tally_pmi",
]
