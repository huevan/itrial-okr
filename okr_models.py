"""Legacy import alias for :mod:`okr.models`."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("okr.models")
