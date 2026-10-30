"""Legacy import alias for :mod:`okr.wecom`."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("okr.wecom")
