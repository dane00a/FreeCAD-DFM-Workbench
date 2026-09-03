# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Imports every machining check module so its decorator runs."""

import importlib
import pkgutil


def _import_submodules() -> None:
    for _, name, _ in pkgutil.iter_modules(__path__):
        if not name.startswith("_") and name != "base":
            importlib.import_module(f".{name}", __name__)


_import_submodules()
