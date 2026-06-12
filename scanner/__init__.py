"""Lightweight scanner package initializer.

Avoid importing heavy submodules at package import time. Expose a small
lazy-loading interface for commonly-used symbols.
"""

__all__ = ["run_full_scan", "continuous_scan", "_print_table"]

def __getattr__(name):
	if name in (__all__):
		from .scanner import run_full_scan, continuous_scan, _print_table
		return locals()[name]
	raise AttributeError(f"module {__name__} has no attribute {name}")
