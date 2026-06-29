"""PrepSetupParams — orchestrator-level setup configuration for Hamilton Prep."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pylabrobot.capabilities.capability import BackendParams


@dataclass
class PrepSetupParams(BackendParams):
  """TCP/pip setup flags. The Prep device's deck is supplied at construction, not here."""

  smart: bool = True
  force_initialize: bool = False
  default_traverse_height: Optional[float] = None
  use_v1_aspirate_dispense: bool = False
