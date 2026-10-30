"""Legacy import alias for :mod:`okr.db`; shares database state and patches."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("okr.db")
