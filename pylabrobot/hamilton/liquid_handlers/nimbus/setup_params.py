"""NimbusSetupParams — orchestrator-level setup configuration for Hamilton Nimbus."""

from __future__ import annotations

from dataclasses import dataclass

from pylabrobot.capabilities.capability import BackendParams


@dataclass
class NimbusSetupParams(BackendParams):
  require_door_lock: bool = False
  force_initialize: bool = False
