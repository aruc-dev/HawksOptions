"""Dashboard test package.

Allows both:
  python3 -m unittest dashboard.tests -v
  python3 -m unittest discover -s dashboard/tests -v
"""
from __future__ import annotations

from pathlib import Path


def load_tests(loader, standard_tests, pattern):
    return loader.discover(str(Path(__file__).resolve().parent), pattern or "test*.py")
