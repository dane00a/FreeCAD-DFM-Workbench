# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Engine version of the machining recognition.

The reference engine's version this one matches, because the approved
goldens record it and a baseline approved under one version drifts under
the next. Bump it with the reference: it is part of every cache key and
every recorded baseline, so a new engine misses both.
"""

ENGINE_VERSION = "2.0.0"  # the reference at fa18e7fa (docs/resync-ledger.md)
