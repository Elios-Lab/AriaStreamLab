"""Compatibility import path for the ``doctor`` checks.

They live in :mod:`ariastreamlab.acquisition.doctor` so that the application's Setup page can
use them too.
"""

from ariastreamlab.acquisition.doctor import Check, run_checks

__all__ = ["Check", "run_checks"]
