# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""A part's own statement about the stock it was cut from."""

import unittest

from freecad.DFM.core.machining.blank_declaration import (
    BLANK_CHOICES,
    BLANK_PROPERTY,
    apply_declaration,
    declare_blank,
    declared_blank,
    material_family_of,
)


class FakeObject:
    """Enough of a document object to hold a property."""

    def __init__(self):
        self._properties: list[str] = []

    def addProperty(self, kind, name, group, doc):
        self._properties.append(name)
        setattr(self, name, "")
        return self


class RefusesProperties(FakeObject):
    def addProperty(self, kind, name, group, doc):
        raise RuntimeError("read-only object")


class BlankDeclarationTests(unittest.TestCase):
    def test_undeclared_object_says_nothing(self):
        self.assertIsNone(declared_blank(FakeObject()))

    def test_missing_object_says_nothing(self):
        self.assertIsNone(declared_blank(None))

    def test_declaring_adds_the_property_once(self):
        obj = FakeObject()
        self.assertTrue(declare_blank(obj, "billet"))
        self.assertEqual(declared_blank(obj), "billet")
        self.assertTrue(declare_blank(obj, "as_cast"))
        self.assertEqual(obj._properties.count(BLANK_PROPERTY), 1)
        self.assertEqual(declared_blank(obj), "as_cast")

    def test_the_enumeration_offers_an_undeclared_state_first(self):
        self.assertEqual(BLANK_CHOICES[0], "")
        self.assertIn("billet", BLANK_CHOICES)

    def test_retracting_leaves_the_property_in_place(self):
        obj = FakeObject()
        declare_blank(obj, "billet")
        self.assertTrue(declare_blank(obj, ""))
        self.assertIsNone(declared_blank(obj))
        self.assertTrue(hasattr(obj, BLANK_PROPERTY))

    def test_an_unknown_form_is_refused_rather_than_stored(self):
        obj = FakeObject()
        self.assertFalse(declare_blank(obj, "unobtanium"))
        self.assertIsNone(declared_blank(obj))

    def test_a_form_from_a_later_version_reads_as_undeclared(self):
        """A document written by a newer release must not change the rules."""
        obj = FakeObject()
        declare_blank(obj, "billet")
        setattr(obj, BLANK_PROPERTY, "sintered_preform")
        self.assertIsNone(declared_blank(obj))

    def test_an_object_that_refuses_properties_is_not_an_error(self):
        obj = RefusesProperties()
        self.assertFalse(declare_blank(obj, "billet"))

    def test_the_part_outranks_the_shop(self):
        obj = FakeObject()
        declare_blank(obj, "billet")
        prefs = apply_declaration({"MachiningBlankForm": "as_cast"}, obj)
        self.assertEqual(prefs["MachiningBlankForm"], "billet")

    def test_an_undeclared_part_leaves_the_shop_setting_alone(self):
        prefs = {"MachiningBlankForm": "as_cast", "Other": 1}
        self.assertEqual(apply_declaration(prefs, FakeObject()), prefs)
        self.assertEqual(apply_declaration(prefs, None), prefs)

    def test_applying_does_not_mutate_the_caller_s_preferences(self):
        original = {"MachiningBlankForm": "as_cast"}
        obj = FakeObject()
        declare_blank(obj, "billet")
        apply_declaration(original, obj)
        self.assertEqual(original["MachiningBlankForm"], "as_cast")


class MaterialFamilyTests(unittest.TestCase):
    """Which gauge table the sheet rules should read.

    Derived from the material the machinist already chose rather than asked
    for again -- a shop that picked an aluminium alloy has said everything
    the rule needs, and a second dropdown offering "aluminium" would be a
    question with one possible answer.
    """

    class Material:
        def __init__(self, category):
            self.category = category

    def family(self, category):
        return material_family_of(self.Material(category))

    def test_the_aluminium_categories_are_recognized(self):
        self.assertEqual(self.family("Aluminium"), "aluminium")
        self.assertEqual(self.family("Aluminum (Soft Wrought Alloy)"), "aluminium")

    def test_the_ferrous_categories_all_read_as_steel(self):
        for category in ("Steel", "Cast Iron", "Austenitic Stainless Steel"):
            self.assertEqual(self.family(category), "steel", category)

    def test_a_material_from_another_process_says_nothing(self):
        """A thermoplastic has no sheet gauge table, and should not pick one."""
        self.assertEqual(self.family("Amorphous Thermoplastic"), "")
        self.assertEqual(self.family("Default"), "")

    def test_nothing_at_all_says_nothing(self):
        self.assertEqual(self.family(""), "")
        self.assertEqual(material_family_of(None), "")

    def test_an_undeclared_family_is_judged_by_the_stricter_figure(self):
        """Steel is both the tighter ceiling and the fallback.

        A part that arrives with nothing said about it is judged strictly
        rather than leniently, which is the safe direction to be wrong in.
        """
        from freecad.DFM.core.checks.sheet.base import material_declared

        self.assertFalse(material_declared(""))
        self.assertTrue(material_declared("steel"))
        self.assertTrue(material_declared("aluminium"))


if __name__ == "__main__":
    unittest.main()
