# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Where approved baselines live, and how they get there.

One file for the whole corpus, keyed by fixture name, sorted and written
with a newline between every value. The reference engine keeps a file per
fixture; this follows the convention already set next door by
``geometry_oracle.json`` and ``local_baseline.json``, which are shaped the
same way and answer the neighbouring question. Two hundred and twenty-five
single-key files would also make it a great deal harder to see, in one
diff, that a recognizer change moved forty of them.

The tradeoff that buys is a large file, so it is written sorted with a
one-space indent: a change to one fixture touches only that fixture's lines,
and the diff is the record of what a rule change did.

Approving is always a merge, never a replacement. Re-recording one fixture
after editing its recipe must not silently drop the other two hundred and
twenty-four -- the same trap ``generate_fixtures.py`` avoids next door, for the
same reason.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .spec import BaselineSpec


#: The default file, found relative to this module. The addon is installed
#: as a junction to the working tree during development, so ``tests`` really
#: is four levels up; in a plain user install there is no ``tests`` directory
#: at all and callers get a path that does not exist, which is the honest
#: answer. Nothing here creates it.
DEFAULT_BASELINE_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "..",
        "..",
        "tests",
        "fixtures",
        "dfm_baseline.json",
    )
)


class BaselineStore:
    """The approved baselines, read once and written back as a whole."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_BASELINE_PATH
        self.specs: dict[str, BaselineSpec] = {}
        self.load()

    # -- reading ----------------------------------------------------------

    def load(self) -> None:
        """Read the file, or start empty if there is not one yet.

        A missing file is the normal state before anything has been
        approved, and is not an error. A malformed one *is*: a baseline
        nobody can parse must not quietly degrade into a baseline that
        approves everything.
        """
        self.specs = {}
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as handle:
            raw: dict[str, Any] = json.load(handle)
        for name, data in raw.items():
            spec = BaselineSpec.from_json(data)
            # The key is the authority on the name. A file whose entry
            # disagrees with its key was hand-edited, and the key is what
            # every lookup uses.
            spec.fixture = name
            self.specs[name] = spec

    def get(self, fixture: str) -> Optional[BaselineSpec]:
        return self.specs.get(fixture)

    def __contains__(self, fixture: str) -> bool:
        return fixture in self.specs

    def __len__(self) -> int:
        return len(self.specs)

    def names(self) -> list[str]:
        return sorted(self.specs)

    # -- writing ----------------------------------------------------------

    def approve(self, spec: BaselineSpec) -> None:
        """Record a spec in memory. Nothing reaches disk until :meth:`save`.

        Kept separate so that approving a run of two hundred fixtures writes
        the file once rather than two hundred times, and so that a run
        interrupted halfway leaves the previous baselines intact rather than
        a half-updated file.
        """
        self.specs[spec.fixture] = spec

    def forget(self, fixture: str) -> bool:
        """Drop a baseline, so the fixture reads as unapproved again."""
        return self.specs.pop(fixture, None) is not None

    def save(self) -> None:
        """Write every baseline back, sorted."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {name: self.specs[name].to_json() for name in sorted(self.specs)}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
