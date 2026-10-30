"""Legacy import alias for :mod:`okr.auth`; preserves existing consumers."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("okr.auth")
