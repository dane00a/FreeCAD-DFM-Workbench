# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Make `pytest` from the repo root work the same way `python -m unittest
discover` does today, without anyone having to set PYTHONPATH by hand.

The suite imports `freecad.DFM.core.*` (needs the repo root on the path) and
`FreeCAD` (needs `tests/stubs` on the path, ahead of anything else that
could answer to that name). Nothing under `tests/test_*.py` touches
`sys.path` itself -- see the stub's own docstring -- so whoever runs the
tests has always been responsible for the path, and pytest is no different.
conftest.py is pytest's hook for doing that before collection starts.

The two entries go in reverse order on purpose: inserting at index 0 twice
puts the *second* insert first. The stub directory has to win a name clash
against a real FreeCAD if one is ever importable from this interpreter
(a system site-packages entry, say) -- these tests are written against the
stub's narrower `Console`/`Vector`, not the real module -- so it goes in
last, after the repo root, to end up at the very front of sys.path.
"""

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_STUBS_DIR = os.path.join(_TESTS_DIR, "stubs")

sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _STUBS_DIR)
