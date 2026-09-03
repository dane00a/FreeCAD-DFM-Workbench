# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The feature list as a table, for people who want it outside the workbench.

An estimator asked what a part costs will count its features, because that is
what the operation list is made of. The recognizers have already done that
counting, and handing it over as a table saves the count being done again by
eye off the model.

The columns are fixed rather than derived from whatever parameters happen to
be present. A census whose shape changes from part to part cannot be pasted
into a spreadsheet next to last month's, which is the only thing anybody
actually wants to do with it.
"""

from __future__ import annotations

import csv
from collections import Counter
from typing import Iterable, Sequence

from .features import FeatureInstance


#: The census columns, in order. The first four are structural; the rest are
#: the dimensions common enough across feature types to be worth a column of
#: their own, and are simply blank where a feature does not carry them.
CENSUS_COLUMNS: tuple[str, ...] = (
    "instance_id",
    "type",
    "face_count",
    "faces",
    "diameter_mm",
    "width_mm",
    "length_mm",
    "depth_mm",
    "height_mm",
    "radius_mm",
    "corner_radius_mm",
    "thread_designation",
    "is_through",
    "is_internal",
)

_STRUCTURAL_COLUMNS = 4


def census_rows(features: Iterable[FeatureInstance]) -> list[list[str]]:
    """The census as rows of strings, header first.

    Everything is rendered to a string here rather than at write time, so the
    file and anything else that shows the table agree exactly -- including
    how many decimal places a dimension gets.
    """
    rows: list[list[str]] = [list(CENSUS_COLUMNS)]
    for feature in features:
        row = [
            feature.instance_id,
            feature.type,
            str(len(feature.faces)),
            " ".join(str(face_id) for face_id in feature.faces),
        ]
        for column in CENSUS_COLUMNS[_STRUCTURAL_COLUMNS:]:
            row.append(_render(feature.param(column)))
        rows.append(row)
    return rows


def _render(value) -> str:
    """One parameter as it should read in a spreadsheet.

    A missing parameter is blank rather than zero: a pocket has no diameter,
    and writing 0.00 there would invite somebody to average it.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def write_census(path: str, features: Iterable[FeatureInstance]) -> None:
    """Write the census to a CSV file."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(census_rows(features))


def census_summary(features: Sequence[FeatureInstance]) -> str:
    """A one-line tally, commonest kind first.

    For the log and for anywhere a whole table would be too much: it answers
    "did the workbench understand this part" in a single line.
    """
    counts = Counter(feature.type for feature in features)
    if not counts:
        return "no features recognized"
    return ", ".join(
        f"{number} {name.replace('_', ' ').lower()}"
        for name, number in counts.most_common()
    )
