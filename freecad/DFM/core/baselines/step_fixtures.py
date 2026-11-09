# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The part of the corpus that is a file rather than a recipe.

Everything under ``tests/fixtures/builders`` is built from boxes, cylinders
and booleans, for the reason that file says: the originals are commercial
work and cannot be distributed, so the recipe is kept and the result is not.
That trick only works on parts simple enough to describe in a few dozen
lines of construction, and the corpus was missing the other kind entirely
-- parts with hundreds of faces, real fillet chains, and the awkward
interactions that only turn up at that size. A recognizer can pass two
hundred tidy blocks and still fall over on the first real part it sees.

So these twelve come in as geometry. They are the NIST MBE PMI Validation
and Conformance Testing test cases (the CTC and FTC series), which exist to
be handed around: published by the National Institute of Standards and
Technology, works of the United States government and therefore not subject
to copyright protection in the United States under 17 U.S.C. 105. NIST's own
README states they may be used without restriction, asks that NIST be
acknowledged as the source, and -- separately from the copyright question --
does not permit use of the NIST logo.

That last clause is why ``nist_ctc_02_logo`` is called out here. It is
``nist_ctc_02`` with the NIST logo embossed into the face, so it is the one
file in this set whose redistribution has a question hanging over it that
the public-domain dedication does not answer. It is kept because dropping it
would lose the only fixture in the corpus with embossed lettering on a
curved region, which is exactly the geometry that breaks face classifiers;
anyone repackaging these files should look at it before they do.

None of that reaches the repository in any case. ``.gitignore`` excludes
``*.step`` deliberately and these files are untracked by design: what is
committed is the measurement in ``dfm_baseline.json``, never the geometry.
A fresh clone therefore has the builders and none of these, which is a
supported state and not a broken one -- see :func:`missing_fixtures`.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List

from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPControl import STEPControl_Reader
from OCP.TopoDS import TopoDS_Shape

from .corpus import FIXTURES_DIR


#: Where the imported parts live, if this is a working tree that has them.
#: The directory is derived from ``corpus.FIXTURES_DIR`` rather than worked
#: out again from ``__file__`` so that moving the package moves both. The
#: dependency only runs this way: ``corpus`` reaches this module through a
#: function-local import, which keeps the cycle from closing and keeps OCP
#: off the import path of everything that merely wants the recipe names.
STEP_DIR = os.path.join(FIXTURES_DIR, "step_pmi")

#: The extension a fixture file is recognized by. Only one: NIST ships
#: ``.step`` and accepting ``.stp`` as well would make ``foo.step`` and
#: ``foo.stp`` two spellings of one fixture name, which is a collision with
#: no useful resolution.
_SUFFIX = ".step"

#: Every fixture this set is supposed to contain, listed because the files
#: themselves cannot be committed. Without it a clone that is missing all
#: twelve is indistinguishable from a clone that never expected them, and
#: ``--verify`` would report a clean corpus while quietly checking nothing.
#: The names are the file basenames; the count is the whole point.
EXPECTED_FIXTURES: tuple[str, ...] = (
    "nist_ctc_01",
    "nist_ctc_02",
    "nist_ctc_02_logo",
    "nist_ctc_03",
    "nist_ctc_04",
    "nist_ctc_05",
    "nist_ftc_06",
    "nist_ftc_07",
    "nist_ftc_08",
    "nist_ftc_09",
    "nist_ftc_10",
    "nist_ftc_11",
)


class StepFixtureError(RuntimeError):
    """A STEP file that is present and will not load.

    Distinct from absence on purpose. A file that is not there is an
    ordinary fresh clone; a file that is there and unreadable is a truncated
    download or a corrupted checkout, and reporting the second as the first
    would let a corpus run pass by skipping the parts it could not read.
    """


def available() -> bool:
    """Whether any imported part is reachable from this installation."""
    return bool(_step_files())


def _step_files() -> Dict[str, str]:
    """Fixture name -> path, for whatever is actually on disk."""
    if not os.path.isdir(STEP_DIR):
        return {}
    found = {}
    for entry in sorted(os.listdir(STEP_DIR)):
        if entry.endswith(_SUFFIX):
            found[entry[: -len(_SUFFIX)]] = os.path.join(STEP_DIR, entry)
    return found


def fixture_names() -> List[str]:
    """The imported parts this installation can actually build."""
    return sorted(_step_files())


def missing_fixtures() -> List[str]:
    """Expected parts that are not on disk, so a caller can say so out loud.

    A baseline exists for each of these, and the fixture cannot be run to
    check it. That is a skip, and a skip that is not printed reads exactly
    like a pass.
    """
    present = _step_files()
    return [name for name in EXPECTED_FIXTURES if name not in present]


#: Parsed shapes, by fixture name. ``nist_ftc_09`` is six megabytes of text
#: and the corpus asks for the same part more than once in a run -- once to
#: verify, once to record, once more when someone re-runs a single fixture
#: from the panel. Parsing it each time is seconds of nothing.
_shape_cache: Dict[str, TopoDS_Shape] = {}


def read_step(path: str) -> TopoDS_Shape:
    """One STEP file as one shape, or an exception explaining why not.

    Reading and transferring are separate steps and each fails its own way:
    ``ReadFile`` refuses a file it cannot parse, and a transfer that maps
    nothing still returns a shape -- a null one. Both are checked, because
    the alternative is handing a null shape to the analysis and reading the
    resulting zero findings as a part with nothing wrong with it.
    """
    reader = STEPControl_Reader()
    status = reader.ReadFile(path)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepFixtureError(f"{os.path.basename(path)}: STEP read failed ({status})")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape is None or shape.IsNull():
        raise StepFixtureError(
            f"{os.path.basename(path)}: STEP transferred no shape"
        )
    return shape


def build(name: str) -> TopoDS_Shape:
    """The shape for one imported fixture, parsed once per process."""
    if name not in _shape_cache:
        path = _step_files().get(name)
        if path is None:
            raise StepFixtureError(
                f"{name}: no {name}{_SUFFIX} in {STEP_DIR}"
            )
        _shape_cache[name] = read_step(path)
    return _shape_cache[name]


#: The PMI read off each imported part, by fixture name. Keyed by the same
#: name as the shape cache and read against the cached shape, so the face ids
#: inside are that shape's.
_pmi_cache: Dict[str, object] = {}


def is_step_fixture(name: str) -> bool:
    """Whether a fixture comes from a file in :data:`STEP_DIR`."""
    return name in _step_files()


def pmi(name: str):
    """The tolerances, datums and notes the file carries, or None for a recipe.

    These parts are the reason PMI is read at all: twelve of them are NIST's
    PMI test cases and four were written to exercise the GD&T rules, so
    analysing their geometry without their callouts would be checking half
    of each. Read the reference engine's way -- through the XDE reader, with
    the face ids of the shape :func:`build` returns, which this does not
    change. A file whose PMI cannot be read is a :class:`StepFixtureError`,
    for the same reason an unreadable shape is.
    """
    if name not in _step_files():
        return None
    if name not in _pmi_cache:
        from ..machining.pmi import read_step_pmi

        try:
            _pmi_cache[name] = read_step_pmi(_step_files()[name], build(name))
        except Exception as exc:
            raise StepFixtureError(f"{name}: PMI read failed: {exc}") from exc
    return _pmi_cache[name]


def clear_cache() -> None:
    """Forget the parsed shapes.

    Only of use to a test that moves :data:`STEP_DIR` underneath the cache,
    which would otherwise keep answering with parts read from the old one.
    """
    _shape_cache.clear()
    _pmi_cache.clear()


def load_registry() -> Dict[str, Callable[[], TopoDS_Shape]]:
    """Every imported part, as builders, so they look like any other fixture.

    Empty when none are installed. The corpus merges this with the recipes
    and neither source knows the other exists, which is the point: a caller
    holding the merged registry cannot tell, and should not need to, whether
    a fixture came from a hundred lines of Python or from a file.
    """
    return {name: (lambda n=name: build(n)) for name in _step_files()}
