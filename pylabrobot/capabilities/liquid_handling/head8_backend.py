"""Abstract backend for 8-head (8MPH) liquid handling."""

from abc import ABCMeta, abstractmethod
from typing import Optional, Union

from pylabrobot.capabilities.capability import BackendParams, CapabilityBackend

from .standard import (
  Head8AspirationContainer,
  Head8AspirationWells,
  Head8DispenseContainer,
  Head8DispenseWells,
  Head8TipDrop,
  Head8TipPickup,
)


class Head8Backend(CapabilityBackend, metaclass=ABCMeta):
  """Backend for 8MPH ganged-head liquid handling operations."""

  @abstractmethod
  async def pick_up_tips8(
    self, op: Head8TipPickup, backend_params: Optional[BackendParams] = None
  ):
    """Pick up tips using the 8MPH head."""

  @abstractmethod
  async def drop_tips8(
    self, op: Head8TipDrop, backend_params: Optional[BackendParams] = None
  ):
    """Drop tips using the 8MPH head."""

  @abstractmethod
  async def aspirate8(
    self,
    op: Union[Head8AspirationWells, Head8AspirationContainer],
    backend_params: Optional[BackendParams] = None,
  ):
    """Aspirate using the 8MPH head."""

  @abstractmethod
  async def dispense8(
    self,
    op: Union[Head8DispenseWells, Head8DispenseContainer],
    backend_params: Optional[BackendParams] = None,
  ):
    """Dispense using the 8MPH head."""
