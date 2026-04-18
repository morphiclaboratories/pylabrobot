"""Prep device: wires PrepDriver + PrepPIPBackend to the PIP capability frontend."""

import asyncio
import random
from typing import Optional, Tuple

from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.capabilities.liquid_handling.pip import PIP
from pylabrobot.device import Device
from pylabrobot.resources.deck import Deck

from . import prep_commands as PrepCmd
from .calibration import PrepCalibration
from .chatterbox import PrepChatterboxDriver
from .core import PrepCoreGripper
from .driver import PrepDriver, PrepSetupParams


class Prep(Device):
  """Hamilton Prep liquid handler (v1 Device / Driver / PIP layout)."""

  def __init__(
    self,
    deck: Deck,
    chatterbox: bool = False,
    host: Optional[str] = None,
    port: int = 2000,
  ):
    if chatterbox:
      driver: PrepDriver = PrepChatterboxDriver()
    else:
      if not host:
        raise ValueError("host must be provided when chatterbox is False.")
      driver = PrepDriver(host=host, port=port)
    super().__init__(driver=driver)
    self.driver: PrepDriver = driver
    self.deck = deck
    self.pip: PIP  # set in setup()
    self.calibration: PrepCalibration  # set in setup()
    self.core_gripper: PrepCoreGripper  # set in setup()

  async def setup(self, backend_params: Optional[BackendParams] = None):
    if backend_params is None:
      params = PrepSetupParams(deck=self.deck)
    elif isinstance(backend_params, PrepSetupParams):
      params = backend_params
      if params.deck is None:
        params = PrepSetupParams(
          deck=self.deck,
          smart=params.smart,
          force_initialize=params.force_initialize,
          default_traverse_height=params.default_traverse_height,
          use_v1_aspirate_dispense=params.use_v1_aspirate_dispense,
        )
    else:
      raise TypeError(
        "Prep.setup expected PrepSetupParams | None for backend_params, "
        f"got {type(backend_params).__name__}"
      )

    try:
      await self.driver.setup(backend_params=params)
      self.pip = PIP(backend=self.driver.pip)
      self._capabilities = [self.pip]
      await self.pip._on_setup(backend_params=params)
      self.calibration = PrepCalibration(driver=self.driver)
      await self.calibration._on_setup(
        num_channels=self.driver.pip.num_channels,
        has_mph=self.driver.pip.has_mph,
      )
      self.core_gripper = PrepCoreGripper(
        driver=self.driver,
        deck=self.deck,
        pip=self.driver.pip,
      )
      self._setup_finished = True
    except Exception:
      await self.driver.stop()
      raise

  async def stop(self):
    if not self._setup_finished:
      return
    await self.calibration._on_stop()
    await self.core_gripper._on_stop()
    for cap in reversed(self._capabilities):
      await cap._on_stop()
    await self.driver.stop()
    self._setup_finished = False

  # -- Instrument-wide (MLPrep) -----------------------------------------------

  async def print_firmware_tree(self) -> None:
    """Walk the full firmware object tree and print a formatted tree representation."""
    await self.driver.print_firmware_tree()

  async def park(self) -> None:
    """Park the instrument."""
    await self.driver.park()

  async def spread(self) -> None:
    """Spread channels."""
    await self.driver.spread()

  async def method_begin(self, automatic_pause: bool = False) -> None:
    """Signal the start of a liquid-handling method."""
    await self.driver.method_begin(automatic_pause=automatic_pause)

  async def method_end(self) -> None:
    """Signal the end of a liquid-handling method."""
    await self.driver.method_end()

  async def method_abort(self) -> None:
    """Abort the current method."""
    await self.driver.method_abort()

  async def power_down_request(self) -> None:
    """Request power down (instrument will prepare for shutdown; use cancel_power_down to abort)."""
    await self.driver.power_down_request()

  async def confirm_power_down(self) -> None:
    """Confirm power down (completes shutdown; only call when safe to power off)."""
    await self.driver.confirm_power_down()

  async def cancel_power_down(self) -> None:
    """Cancel a pending power-down request."""
    await self.driver.cancel_power_down()

  async def get_deck_light(self) -> Tuple[int, int, int, int]:
    """Get the current deck LED colour (white, red, green, blue)."""
    return await self.driver.get_deck_light()

  async def set_deck_light(self, white: int, red: int, green: int, blue: int) -> None:
    """Set the deck LED colour."""
    await self.driver.set_deck_light(white=white, red=red, green=green, blue=blue)

  async def disco_mode(self) -> None:
    """Easter egg: cycle deck lights then restore previous state."""
    white, red, green, blue = await self.driver.get_deck_light()
    try:
      for _ in range(69):
        await self.driver.set_deck_light(
          white=random.randint(1, 255),
          red=random.randint(1, 255),
          green=random.randint(1, 255),
          blue=random.randint(1, 255),
        )
        await asyncio.sleep(0.1)
    finally:
      await self.driver.set_deck_light(white=white, red=red, green=green, blue=blue)

  async def is_initialized(self) -> bool:
    """Whether MLPrep reports initialized (GetIsInitialized)."""
    return await self.driver.is_initialized()

  async def get_tip_and_needle_definitions(self) -> Tuple[PrepCmd.TipDefinition, ...]:
    """Tip/needle definitions from MLPrep (GetTipAndNeedleDefinitions)."""
    return await self.driver.get_tip_and_needle_definitions()

  async def is_parked(self) -> bool:
    """Whether MLPrep is parked."""
    return await self.driver.is_parked()

  async def is_spread(self) -> bool:
    """Whether channels are spread."""
    return await self.driver.is_spread()

  async def request_firmware_version(self) -> Optional[str]:
    """Controller firmware version string (MLPrepCpu)."""
    return await self.driver.request_firmware_version()

  async def request_device_serial_number(self) -> Optional[str]:
    """Instrument serial number (MLPrepCpu)."""
    return await self.driver.request_device_serial_number()

  async def request_bootloader_version(self) -> Optional[str]:
    """Bootloader version string (MLPrepCpu)."""
    return await self.driver.request_bootloader_version()

  async def request_module_version(self) -> Optional[str]:
    """Pipettor module version (ModuleInformation)."""
    return await self.driver.request_module_version()

  async def request_module_part_number(self) -> Optional[str]:
    """Module part number (ModuleInformation)."""
    return await self.driver.request_module_part_number()
