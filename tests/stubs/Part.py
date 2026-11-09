# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Minimal Part stand-in, beside the FreeCAD one in this directory.

A few modules import Part at the top (the analysis runner, the shape
conversion) for type names and for paths no headless test takes. With the
FreeCAD stand-in importable, tests/dfm_test_support.ensure_freecad() sees
FreeCAD and stubs nothing, so Part has to be answered here instead -- with
the same single name it would have put in place.
"""

Shape = object
