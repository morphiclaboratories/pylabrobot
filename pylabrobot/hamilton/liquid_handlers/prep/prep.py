"""Prep device: orchestrates transport, instrument info, PIP backend, PIP capability, and calibration."""

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Tuple

from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.capabilities.liquid_handling.pip import PIP
from pylabrobot.device import Device
from pylabrobot.resources.deck import Deck
from pylabrobot.resources.hamilton.hamilton_decks import HamiltonCoreGrippers

from . import prep_commands as PrepCmd
from .calibration import PrepCalibration
from .chatterbox import PrepChatterboxDriver, PrepChatterboxInstrumentInfo
from .core import PrepCoreGripper, PrepGripperArm
from .driver import PrepDriver, PrepSetupParams
from .info import PrepInstrumentInfo
from .method import PrepMethodLifecycle
from .pip_backend import PrepPIPBackend

logger = logging.getLogger(__name__)


class Prep(Device):
  """Hamilton Prep liquid handler.

  The deck is fixed at construction; ``setup`` takes optional :class:`~driver.PrepSetupParams`
  for driver/pip options only. :class:`~pylabrobot.hamilton.liquid_handlers.prep.driver.PrepDriver` is
  **transport only** (no pip backend field). Instrument-wide state lives on :attr:`info`
  (:class:`~pylabrobot.hamilton.liquid_handlers.prep.info.PrepInstrumentInfo`). In :meth:`setup`,
  ``Prep`` constructs :class:`~pylabrobot.hamilton.liquid_handlers.prep.pip_backend.PrepPIPBackend`
  with ``prep=self``; the pip backend reads transport and metadata only via ``self._prep.driver`` and
  ``self._prep.info`` (the same objects as :attr:`driver` and :attr:`info`).
  Setup order: transport → ``info._on_setup`` → ``PrepPIPBackend`` + :class:`~pylabrobot.capabilities.liquid_handling.pip.PIP` → calibration.
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
    self.info = (
      PrepChatterboxInstrumentInfo(driver) if chatterbox else PrepInstrumentInfo(driver)
    )
    self.method = PrepMethodLifecycle(driver)
    self.pip: PIP  # set in setup()
    self.calibration: PrepCalibration  # set in setup()
    self._core_gripper_arm: Optional[PrepGripperArm] = None

  def _normalize_setup_params(
    self, backend_params: Optional[BackendParams]
  ) -> PrepSetupParams:
    if backend_params is None:
      return PrepSetupParams()
    if isinstance(backend_params, PrepSetupParams):
      return backend_params
    raise TypeError(
      "Prep.setup expected PrepSetupParams | None for backend_params, "
      f"got {type(backend_params).__name__}"
    )

  async def setup(self, backend_params: Optional[BackendParams] = None):
    """Connect and resolve interfaces, then wire info, pip backend, PIP capability, and calibration."""
    params = self._normalize_setup_params(backend_params)
    try:
      await self.driver.setup(backend_params=params)
      await self.info._on_setup()
      pip_backend = PrepPIPBackend(
        prep=self,
        deck=self.deck,
        default_traverse_height=params.default_traverse_height,
        use_v1_aspirate_dispense=params.use_v1_aspirate_dispense,
      )
      self.pip = PIP(backend=pip_backend)
      self._capabilities = [self.pip]
      await self.pip._on_setup(backend_params=params)
      self.calibration = PrepCalibration(driver=self.driver, info=self.info)
      await self.calibration._on_setup()
      self._setup_finished = True
    except Exception:
      await self.info._on_stop()
      await self.driver.stop()
      raise

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
    await self.calibration._on_stop()
    for cap in reversed(self._capabilities):
      await cap._on_stop()
    await self.driver.stop()
    await self.info._on_stop()
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
    """Whether the CoRe gripper tools are currently picked up."""
    return self._core_gripper_arm is not None

  async def pick_up_core_grippers(self) -> PrepGripperArm:
    """Pick up the CoRe gripper tools and return the mounted arm.

    The arm is also accessible via :attr:`core_gripper_arm` until
    :meth:`return_core_grippers` is called.  Splits cleanly across Jupyter cells.

    Raises:
      RuntimeError: if grippers are already mounted.
    """
    if self._core_gripper_arm is not None:
      raise RuntimeError("CoRe grippers already mounted")

    mount = self.deck.get_resource("core_grippers")
    if not isinstance(mount, HamiltonCoreGrippers):
      raise TypeError(
        "deck must have a resource named 'core_grippers' of type HamiltonCoreGrippers"
      )

    loc = mount.get_location_wrt(self.deck)
    backend = PrepCoreGripper(driver=self.driver, pip=self.pip.backend)

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
    """Return the CoRe gripper tools.  Idempotent — safe to call when not mounted."""
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
    """Context manager that picks up CoRe gripper tools on enter, returns on exit.

    Convenience wrapper around :meth:`pick_up_core_grippers` /
    :meth:`return_core_grippers`.  For Jupyter-style split-cell workflows, call
    those methods directly instead.

    Usage::

      async with prep.core_grippers() as arm:
        await arm.move_resource(plate, destination)
    """
    arm = await self.pick_up_core_grippers()
    try:
      yield arm
    finally:
      await self.return_core_grippers()

  # -- Motion, power, lights (MLPrep via driver transport) --------------------

  async def park(self) -> None:
    """Park the instrument."""
    d = self.driver
    await d.send_command(PrepCmd.PrepPark(dest=await d.require_interface("mlprep")))

  async def spread(self) -> None:
    """Spread channels."""
    d = self.driver
    await d.send_command(PrepCmd.PrepSpread(dest=await d.require_interface("mlprep")))

  async def is_parked(self) -> bool:
    """Whether MLPrep is parked."""
    d = self.driver
    result = await d.send_command(PrepCmd.PrepIsParked(dest=await d.require_interface("mlprep")))
    if result is None:
      return False
    return bool(result.value)

  async def is_spread(self) -> bool:
    """Whether channels are spread."""
    d = self.driver
    result = await d.send_command(PrepCmd.PrepIsSpread(dest=await d.require_interface("mlprep")))
    if result is None:
      return False
    return bool(result.value)

  async def power_down_request(self) -> None:
    """Request power down (instrument will prepare for shutdown; use cancel_power_down to abort)."""
    d = self.driver
    await d.send_command(PrepCmd.PrepPowerDownRequest(dest=await d.require_interface("mlprep")))

  async def confirm_power_down(self) -> None:
    """Confirm power down (completes shutdown; only call when safe to power off)."""
    d = self.driver
    await d.send_command(PrepCmd.PrepConfirmPowerDown(dest=await d.require_interface("mlprep")))

  async def cancel_power_down(self) -> None:
    """Cancel a pending power-down request."""
    d = self.driver
    await d.send_command(PrepCmd.PrepCancelPowerDown(dest=await d.require_interface("mlprep")))

  async def get_deck_light(self) -> Tuple[int, int, int, int]:
    """Get the current deck LED colour (white, red, green, blue)."""
    d = self.driver
    result = await d.send_command(PrepCmd.PrepGetDeckLight(dest=await d.require_interface("mlprep")))
    if result is None:
      raise ValueError("No response from GetDeckLight.")
    return (result.white, result.red, result.green, result.blue)

  async def set_deck_light(self, white: int, red: int, green: int, blue: int) -> None:
    """Set the deck LED colour."""
    d = self.driver
    await d.send_command(
      PrepCmd.PrepSetDeckLight(
        dest=await d.require_interface("mlprep"),
        white=white,
        red=red,
        green=green,
        blue=blue,
      )
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
