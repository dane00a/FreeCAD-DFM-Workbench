# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""A recognized knurl, reported as the one operation it is."""

from __future__ import annotations

from ...machining.features import FeatureType
from ...models import CheckResult, Severity
from ...registries import register_check
from ...rules import Rulebook
from .base import MachiningCheck


@register_check(Rulebook.KNURL_PRESENT)
class KnurlPresentCheck(MachiningCheck):
    """A knurled patch: one knurling operation, and a specification the
    model cannot carry.

    The measured pattern is reported as a pattern. Turning a measured pitch
    into a designation would be the same error as reading a thread size off a
    tap drill, so the finding says out loud that the callout has to come from
    the customer.
    """

    @property
    def name(self) -> str:
        return "Knurl Check"

    def evaluate(self, context, rule_config, rule, feedback) -> list[CheckResult]:
        results: list[CheckResult] = []
        for knurl in context.recognition.of_type(FeatureType.KNURL):
            subtype = knurl.param("subtype") or ""
            host = knurl.param("host") or "planar"
            direction = knurl.param("direction") or ""
            pitch = knurl.number("pitch_mm") or 0.0
            depth = knurl.number("groove_depth_mm") or 0.0
            width = knurl.number("groove_width_mm") or 0.0
            incl = knurl.number("included_angle_deg") or 0.0
            area = knurl.number("patch_area_mm2") or 0.0
            grooves = int(knurl.number("groove_count") or 0)
            cells = int(knurl.number("cell_count") or 0)
            callout = knurl.param("callout") or ""

            msg = "Diamond (crossed) knurl" if subtype == "diamond" else "Straight knurl"
            msg += " on a cylindrical surface" if host == "cylindrical" else " on a flat face"
            msg += (
                ". This is a KNURLING operation (rolled with a knurling tool, or "
                "cut with a knurl cutter) -- it is not a field of milled slots, "
                "and the individual grooves should not be priced or toleranced "
                "as features."
            )
            msg += f" Measured pattern: pitch {pitch:.2f} mm"
            if depth > 0.0:
                msg += f", groove depth {depth:.2f} mm"
            if width > 0.0:
                msg += f", groove width {width:.2f} mm"
            if subtype == "diamond" and incl > 0.0:
                msg += f", groove families crossing at {incl:.1f} deg"
            if direction:
                msg += f", direction {direction}"
            if grooves > 0:
                msg += f", {grooves} groove lines"
            # "Pattern cells", not "crest cells": the seed can land on either
            # level of the texture.
            if cells > 0:
                msg += f" over {cells} pattern cells"
            if area > 0.0:
                msg += f", patch area approx {area:.2f} mm2"
            msg += "."
            msg += (
                " THE KNURL SPECIFICATION IS NOT DETERMINABLE FROM THE GEOMETRY: "
                "the pattern above is what the model carries, but the tooth form, "
                "the standard designation (e.g. DIN 82 / ANSI) and the pitch "
                "SPECIFIED (as against modelled) come from a callout, not from the "
                "model. Confirm the knurl callout with the customer before quoting "
                "tooling."
            )
            if callout:
                msg += (
                    f' A note in the file reads: "{callout}" -- treat that text as '
                    "authoritative over anything measured here."
                )
            else:
                msg += " No knurl callout was found in the file."
            msg += (
                " Points to check by hand: the part must take the radial forming "
                "force (a thin wall or thin-wall tube under the patch can "
                "collapse); a knurled surface will not hold a tight tolerance or a "
                "fine finish applied after knurling"
            )
            if host == "cylindrical":
                msg += (
                    "; and a diamond knurl has a minimum practical diameter for the "
                    "available tooling"
                )
            msg += "."

            results.append(
                self.finding(
                    rule,
                    Severity.INFO,
                    f"{subtype or 'knurl'} knurl",
                    self.render(feedback, Severity.INFO, pitch, 0.0, 0.0, "mm", msg),
                    faces=knurl.faces,
                    value=pitch,
                    limit=0.0,
                    comparison="",
                    unit="mm",
                )
            )
        return results
