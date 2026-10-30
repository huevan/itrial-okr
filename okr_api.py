"""Legacy import alias for :mod:`okr.api`; no business logic lives here."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("okr.api")
