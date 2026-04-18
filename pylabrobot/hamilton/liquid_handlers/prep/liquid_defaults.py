"""Prep-local defaults helpers (decoupled from legacy liquid_handling)."""

from __future__ import annotations

from typing import List, Optional, TypeVar

T = TypeVar("T")


def fill_in_defaults(val: Optional[List[T]], default: List[T]) -> List[T]:
  """Convert optional per-channel overrides into a full list matching ``default`` length."""
  if val is None:
    return default
  if len(val) != len(default):
    raise ValueError(f"Value length must equal num operations ({len(default)}), but is {len(val)}")
  return [v if v is not None else d for v, d in zip(val, default)]
