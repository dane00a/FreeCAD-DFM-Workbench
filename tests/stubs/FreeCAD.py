# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the DFM addon.

"""Minimal FreeCAD stand-in so the pure-geometry core can be tested headlessly.

The analyzers, checks and machining modules depend on FreeCAD only for console
output, preferences and the Vector type. Putting this directory on sys.path
lets the whole core import under a plain Python + OCP environment, with no
FreeCAD installation. Anything that genuinely needs FreeCAD must not be
imported by tests that rely on this stub.
"""

import math
import tempfile


GuiUp = False
ActiveDocument = None


class Console:
    @staticmethod
    def PrintMessage(msg):
        pass

    @staticmethod
    def PrintWarning(msg):
        pass

    @staticmethod
    def PrintError(msg):
        pass

    @staticmethod
    def PrintDeveloperError(msg):
        pass

    @staticmethod
    def PrintLog(msg):
        pass


class Vector:
    """The subset of FreeCAD.Vector the core relies on."""

    def __init__(self, x=0.0, y=0.0, z=0.0):
        if hasattr(x, "x") and hasattr(x, "y"):
            x, y, z = x.x, x.y, x.z
        elif isinstance(x, (tuple, list)):
            x, y, z = (list(x) + [0.0, 0.0, 0.0])[:3]
        self.x, self.y, self.z = float(x), float(y), float(z)

    @property
    def Length(self):
        return math.sqrt(self.x**2 + self.y**2 + self.z**2)

    def normalize(self):
        length = self.Length
        if length < 1e-12:
            return Vector(0, 0, 1)
        return Vector(self.x / length, self.y / length, self.z / length)

    def add(self, other):
        return Vector(self.x + other.x, self.y + other.y, self.z + other.z)

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def __iter__(self):
        yield from (self.x, self.y, self.z)

    def __repr__(self):
        return f"Vector({self.x}, {self.y}, {self.z})"


class _Params:
    """Stands in for a FreeCAD parameter group; always reports defaults."""

    def GetBool(self, _key, default=False):
        return default

    def GetInt(self, _key, default=0):
        return default

    def GetFloat(self, _key, default=0.0):
        return default

    def GetString(self, _key, default=""):
        return default

    def GetContents(self):
        return []

    def SetBool(self, *_a):
        pass

    def SetInt(self, *_a):
        pass

    def SetFloat(self, *_a):
        pass

    def SetString(self, *_a):
        pass


def ParamGet(_path):
    return _Params()


def getUserAppDataDir():
    return tempfile.gettempdir()


def getHomePath():
    return tempfile.gettempdir()
