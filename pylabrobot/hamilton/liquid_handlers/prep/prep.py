"""Prep device: orchestrates transport, instrument info, and peer construction."""

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Tuple

from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.capabilities.liquid_handling.head8 import Head8
from pylabrobot.capabilities.liquid_handling.pip import PIP
from pylabrobot.device import Device
from pylabrobot.resources.deck import Deck
from pylabrobot.resources.hamilton.hamilton_decks import HamiltonCoreGrippers

from . import prep_commands as PrepCmd
from .calibration import PrepCalibration
from .channels import build_prep_channels
from .chatterbox import PrepChatterboxDriver, PrepChatterboxInstrumentInfo
from .core import PrepCoreGripper, PrepCoreGripperFactory, PrepGripperArm
from .driver import PrepDriver
from .setup_params import PrepSetupParams
from .info import PrepInstrumentInfo
from .method import PrepMethodLifecycle
from .mph_backend import PrepMPHBackend
from .pip_backend import PrepPIPBackend

logger = logging.getLogger(__name__)


class Prep(Device):
  """Hamilton Prep liquid handler.

  Setup constructs peers (``pip``, ``method``, ``calibration``, core-gripper factory)
  directly. Firmware paths live on each :class:`PrepCommand` subclass and are
  resolved JIT by :meth:`PrepDriver.send_command`.
  """

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
    self.info = PrepChatterboxInstrumentInfo(driver) if chatterbox else PrepInstrumentInfo(driver)
    self._core_gripper_arm: Optional[PrepGripperArm] = None
    self.pip: Optional[PIP] = None
    self.head8: Optional[Head8] = None
    self.method: Optional[PrepMethodLifecycle] = None
    self.calibration: Optional[PrepCalibration] = None
    self._core_factory: Optional[PrepCoreGripperFactory] = None

  def _normalize_setup_params(self, backend_params: Optional[BackendParams]) -> PrepSetupParams:
    if backend_params is None:
      return PrepSetupParams()
    if isinstance(backend_params, PrepSetupParams):
      return backend_params
    raise TypeError(
      "Prep.setup expected PrepSetupParams | None for backend_params, "
      f"got {type(backend_params).__name__}"
    )

  async def setup(self, backend_params: Optional[BackendParams] = None):
    """Connect, bootstrap info, initialize MLPrep, construct peers."""
    params = self._normalize_setup_params(backend_params)
    try:
      await self.driver.setup(backend_params=params)
      await self.info._on_setup()
      await self._initialize_instrument(params)

      self.method = PrepMethodLifecycle(self.driver)
      self.calibration = PrepCalibration(driver=self.driver, info=self.info)
      pip_backend = PrepPIPBackend(
        driver=self.driver,
        info=self.info,
        deck=self.deck,
        default_traverse_height=params.default_traverse_height,
        use_v1_aspirate_dispense=params.use_v1_aspirate_dispense,
      )
      pip_backend.channels = await build_prep_channels(self.driver, self.info)
      pip_trash = (
        self.deck.get_trash_area()
        if self.deck is not None and self.deck.has_resource("trash")
        else None
      )
      self.pip = PIP(backend=pip_backend, deck=self.deck, default_trash=pip_trash)
      await self.pip._on_setup()

      if pip_backend.has_mph:
        mph_backend = PrepMPHBackend(
          driver=self.driver,
          info=self.info,
          default_traverse_height=params.default_traverse_height,
          use_v1_aspirate_dispense=params.use_v1_aspirate_dispense,
        )
        mph_backend.channels = await build_prep_channels(
          self.driver, self.info, root_name="MPH Channel Root", num_channels=8
        )
        mph_trash = (
          self.deck.get_resource("waste_mph")
          if self.deck is not None and self.deck.has_resource("waste_mph")
          else None
        )
        self.head8 = Head8(backend=mph_backend, deck=self.deck, default_trash=mph_trash)
        await self.head8._on_setup()

      self._core_factory = PrepCoreGripperFactory(driver=self.driver)

      self._capabilities = [self.pip] + ([self.head8] if self.head8 is not None else [])
      self._setup_finished = True
    except Exception:
      await self.info._on_stop()
      await self.driver.stop()
      raise

  async def _initialize_instrument(self, params: PrepSetupParams) -> None:
    """Send ``MLPrep.Initialize`` when needed."""
    if not params.force_initialize:
      try:
        already = await self.info.is_initialized()
      except Exception as e:
        logger.error("GetIsInitialized failed; cannot decide whether to init: %s", e)
        raise
      if already:
        logger.info("MLPrep already initialized, skipping Initialize")
        return

    await self.driver.send_command(
      PrepCmd.PrepInitialize(
        smart=params.smart,
        tip_drop_params=PrepCmd.InitTipDropParameters(
          default_values=True,
          x_position=287.0,
          rolloff_distance=3,
          channel_parameters=[],
        ),
      )
    )
    logger.info(
      "Prep initialization complete%s",
      " (force_initialize=True)" if params.force_initialize else "",
    )

  async def stop(self):
    if not self._setup_finished:
      return
    if self._core_gripper_arm is not None:
      logger.warning(
        "Prep.stop() called with CoRe grippers still mounted. "
        "stop() only manages connection teardown and will NOT move the instrument. "
        "Call `await prep.return_core_grippers()` first if you want the tools returned."
      )
      self._core_gripper_arm = None
    if self.pip is not None:
      await self.pip._on_stop()
    if self.head8 is not None:
      await self.head8._on_stop()
    await self.driver.stop()
    await self.info._on_stop()
    self._capabilities = []
    self.pip = None
    self.head8 = None
    self.method = None
    self.calibration = None
    self._core_factory = None
    self._setup_finished = False

  # -- CoRe grippers -----------------------------------------------------------

  @property
  def core_gripper_arm(self) -> PrepGripperArm:
    """The mounted CoRe gripper arm. Raises if grippers are not currently picked up."""
    if self._core_gripper_arm is None:
      raise RuntimeError(
        "CoRe grippers not mounted. Call `await prep.pick_up_core_grippers()` first, "
        "or use `async with prep.core_grippers() as arm:`."
      )
    return self._core_gripper_arm

  @property
  def core_grippers_mounted(self) -> bool:
    return self._core_gripper_arm is not None

  async def pick_up_core_grippers(self) -> PrepGripperArm:
    """Pick up the CoRe gripper tools and return the mounted arm."""
    if self._core_gripper_arm is not None:
      raise RuntimeError("CoRe grippers already mounted")
    if self._core_factory is None or self.pip is None:
      raise RuntimeError("Prep.setup() has not run.")

    mount = self.deck.get_resource("core_grippers")
    if not isinstance(mount, HamiltonCoreGrippers):
      raise TypeError(
        "deck must have a resource named 'core_grippers' of type HamiltonCoreGrippers"
      )

    loc = mount.get_location_wrt(self.deck)
    pip_backend = self.pip.backend
    assert isinstance(pip_backend, PrepPIPBackend)
    backend = self._core_factory.build_backend(pip=pip_backend)

    await backend.pick_up_tool(
      tool_position_x=loc.x,
      tool_position_z=loc.z,
      front_channel_position_y=loc.y + mount.front_channel_y_center,
      rear_channel_position_y=loc.y + mount.back_channel_y_center,
      tool_seek=loc.z + 10.0,
    )

    self._core_gripper_arm = PrepGripperArm(
      backend=backend, reference_resource=self.deck, grip_axis="y"
    )
    return self._core_gripper_arm

  async def return_core_grippers(self) -> None:
    if self._core_gripper_arm is None:
      return
    backend = self._core_gripper_arm.backend
    assert isinstance(backend, PrepCoreGripper)
    try:
      await backend.drop_tool()
    finally:
      self._core_gripper_arm = None

  @asynccontextmanager
  async def core_grippers(self) -> AsyncIterator[PrepGripperArm]:
    arm = await self.pick_up_core_grippers()
    try:
      yield arm
    finally:
      await self.return_core_grippers()

  # -- Motion, power, lights (MLPrep via driver transport) --------------------

  async def park(self) -> None:
    await self.driver.send_command(PrepCmd.PrepPark())

  async def spread(self) -> None:
    await self.driver.send_command(PrepCmd.PrepSpread())

  async def is_parked(self) -> bool:
    result = await self.driver.send_command(PrepCmd.PrepIsParked())
    if result is None:
      return False
    return bool(result.value)

  async def is_spread(self) -> bool:
    result = await self.driver.send_command(PrepCmd.PrepIsSpread())
    if result is None:
      return False
    return bool(result.value)

  async def power_down_request(self) -> None:
    await self.driver.send_command(PrepCmd.PrepPowerDownRequest())

  async def confirm_power_down(self) -> None:
    await self.driver.send_command(PrepCmd.PrepConfirmPowerDown())

  async def cancel_power_down(self) -> None:
    await self.driver.send_command(PrepCmd.PrepCancelPowerDown())

  async def get_deck_light(self) -> Tuple[int, int, int, int]:
    result = await self.driver.send_command(PrepCmd.PrepGetDeckLight())
    if result is None:
      raise ValueError("No response from GetDeckLight.")
    return (result.white, result.red, result.green, result.blue)

  async def set_deck_light(self, white: int, red: int, green: int, blue: int) -> None:
    await self.driver.send_command(
      PrepCmd.PrepSetDeckLight(white=white, red=red, green=green, blue=blue)
    )

  async def disco_mode(self) -> None:
    """Easter egg: cycle deck lights then restore previous state."""
    white, red, green, blue = await self.get_deck_light()
    try:
      for _ in range(69):
        await self.set_deck_light(
          white=random.randint(1, 255),
          red=random.randint(1, 255),
          green=random.randint(1, 255),
          blue=random.randint(1, 255),
        )
        await asyncio.sleep(0.1)
    finally:
      await self.set_deck_light(white=white, red=red, green=green, blue=blue)
