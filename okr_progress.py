"""Legacy import alias for :mod:`okr.progress`."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("okr.progress")
