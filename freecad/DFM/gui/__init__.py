# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2025 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

# The preference pages, by name. Resolved on first use rather than on import:
# the pages need Qt, and importing one of the plain-data modules alongside
# them -- the threshold tables, say -- has to work on a machine without it.
_PAGES = {
    "MachiningPreferences": "machining_preferences",
    "MachiningThresholds": "machining_thresholds",
    "MachiningTooling": "tool_library",
    "DFMPreferencesGeneral": "preferences",
    "DFMPreferencesAnalyzers": "preferences",
}

__all__ = tuple(_PAGES)


def __getattr__(name):
    module = _PAGES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{module}", __name__), name)
