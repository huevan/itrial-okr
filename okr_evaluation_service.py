"""Legacy import alias for :mod:`okr.evaluation_service`."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("okr.evaluation_service")
