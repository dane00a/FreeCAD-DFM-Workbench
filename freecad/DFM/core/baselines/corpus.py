# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Reaching the fixture recipes from inside the addon.

The corpus lives under ``tests/fixtures/builders``, outside the installed
package, and that is where it belongs: it is two hundred build recipes and
a registry, of no use whatever to someone who installed the workbench to
check a bracket. The baseline panel needs them anyway, which leaves one
awkward import to make and one place to make it.

During development the addon directory is a junction to the working tree,
so the recipes really are four levels up from here and importing them works.
In a plain user install there is no ``tests`` directory at all, and
:func:`load_registry` says so by returning nothing -- which is why the panel
that calls it is behind a developer preference in the first place.

A fixture reaches the registry from one of two places. Most are recipes,
built from primitives by ``builders``; twelve are STEP files read off disk
by :mod:`.step_fixtures`, because parts of that size cannot be written as
construction code. The registry merges them and the caller cannot tell which
is which, which is deliberate -- a fixture is a name and a shape either way.
The merge is the one place that has to care, and it cares about exactly one
thing: that no name arrives from both.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Dict, Optional


#: The directory the fixture package sits in, if this is a working tree.
FIXTURES_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "..",
        "..",
        "tests",
        "fixtures",
    )
)


class CorpusCollision(ValueError):
    """One fixture name offered by both a recipe and a STEP file.

    There is no defensible way to pick a winner: whichever is chosen, half
    the corpus's baselines were recorded against the other shape, and the
    run would report a wall of drift whose cause is a filename. So it raises
    instead, naming what clashed, and someone renames one of them.
    """


def builders_available() -> bool:
    """Whether the fixture recipes are reachable from this installation."""
    return os.path.isdir(os.path.join(FIXTURES_DIR, "builders"))


def step_fixtures_available() -> bool:
    """Whether any of the imported STEP parts are on disk.

    Routinely false even on a working tree. The files are gitignored, so a
    fresh clone has every recipe and not one of these.
    """
    if not os.path.isdir(FIXTURES_DIR):
        return False
    try:
        from . import step_fixtures
    except ImportError:
        # No OCP, no way to read a STEP file, so there are effectively none.
        return False
    return step_fixtures.available()


def corpus_available() -> bool:
    """Whether there is any corpus here at all.

    Either source on its own is a usable corpus. The recipes without the
    STEP parts is the ordinary working tree; the STEP parts without the
    recipes should not happen but costs nothing to allow.
    """
    return builders_available() or step_fixtures_available()


def merge_sources(
    builders: Dict[str, Callable[[], object]],
    step: Dict[str, Callable[[], object]],
) -> Dict[str, Callable[[], object]]:
    """The two fixture sources as one registry, or an exception if they overlap.

    Kept separate from :func:`load_registry` so that the rule it enforces can
    be tested without twelve megabytes of STEP files on disk to test it with.
    """
    clashes = sorted(set(builders) & set(step))
    if clashes:
        raise CorpusCollision(
            "fixture name registered twice, as a recipe and as a STEP file: "
            + ", ".join(clashes)
        )
    merged = dict(builders)
    merged.update(step)
    return merged


def load_builder_registry() -> Dict[str, Callable[[], object]]:
    """Every registered fixture recipe, by name.

    Empty when the recipes are not installed, which the caller must handle:
    an empty corpus is a perfectly ordinary state for a user install and not
    a fault to raise on.

    The fixtures directory goes on ``sys.path`` rather than the repository
    root because ``builders/__init__.py`` imports its own submodules by
    relative name, the same way ``generate_fixtures.py`` arranges for.
    """
    if not builders_available():
        return {}
    if FIXTURES_DIR not in sys.path:
        sys.path.insert(0, FIXTURES_DIR)
    from builders import load_all  # type: ignore

    return load_all()


def load_step_registry() -> Dict[str, Callable[[], object]]:
    """Every imported STEP part, as builders. Empty when none are installed."""
    if not os.path.isdir(FIXTURES_DIR):
        return {}
    try:
        from . import step_fixtures
    except ImportError:
        return {}
    return step_fixtures.load_registry()


def load_registry() -> Dict[str, Callable[[], object]]:
    """Every fixture this installation can build, by name, from both sources."""
    return merge_sources(load_builder_registry(), load_step_registry())


def missing_step_fixtures() -> list:
    """Imported parts a baseline expects and this installation does not have.

    The answer is the whole list on a fresh clone, and the caller is expected
    to print it rather than skip it quietly.
    """
    try:
        from . import step_fixtures
    except ImportError:
        return []
    return step_fixtures.missing_fixtures()


def build(name: str, registry: Optional[Dict[str, Callable[[], object]]] = None):
    """One fixture shape, or nothing if nothing is registered under that name."""
    registry = registry if registry is not None else load_registry()
    builder = registry.get(name)
    return builder() if builder is not None else None
