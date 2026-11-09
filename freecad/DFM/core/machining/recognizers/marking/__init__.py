# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The staged MARKING_TEXT pipeline.

    Stage 0  priors                 what the file says; never gates
    Stage 1  relief_generator       relief candidates, tuned for recall
    Stage 2  process_discriminator  cut, pressed or proud, from the back side
    Stage 3  shape_signature        does it read as lettering: a score
    Stage 4  verifier               can it be read back (raster verifier)
    Stage 5  ..marking_recognizer   which claim wins, at which tier
"""
