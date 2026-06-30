import asyncio

import pytest

from pylabrobot.hamilton.liquid_handlers.prep import prep_commands as PrepCmd
from pylabrobot.hamilton.liquid_handlers.prep.chatterbox import PrepChatterboxDriver
from pylabrobot.hamilton.liquid_handlers.prep.core import PrepCoreGripper, PrepGripperArm
from pylabrobot.hamilton.liquid_handlers.prep.pip_backend import PrepPIPBackend
from pylabrobot.hamilton.liquid_handlers.prep.prep import Prep
from pylabrobot.hamilton.liquid_handlers.prep.setup_params import PrepSetupParams
from pylabrobot.hamilton.tcp.packets import Address
from pylabrobot.resources.hamilton import STARLetDeck


def test_chatterbox_sets_resolved_interfaces_and_pip():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()

    assert isinstance(p.driver.mlprep_address, Address)
    addr = await p.driver.resolve_path("MLPrepRoot.PipettorRoot.Pipettor")
    assert isinstance(addr, Address)
    assert p.info.config.num_channels == 2
    assert p.pip is not None
    pip_be = p.pip.backend
    assert isinstance(pip_be, PrepPIPBackend)
    assert pip_be.num_channels == 2
    assert pip_be.setup_finished is True
    # Default PrepSetupParams: use_v1_aspirate_dispense=False → v2 probe passes (chatterbox stubs).
    assert pip_be._supports_v2_pipetting is True

    await p.stop()
    assert p.info._config is None

  asyncio.run(_run())


def test_chatterbox_use_v1_skips_v2_probe():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup(PrepSetupParams(use_v1_aspirate_dispense=True))
    assert p.pip is not None
    pip_be = p.pip.backend
    assert isinstance(pip_be, PrepPIPBackend)
    assert pip_be.setup_finished is True
    assert pip_be._supports_v2_pipetting is False

    await p.stop()

  asyncio.run(_run())


def test_prep_device_motion_method_and_power_commands():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()
    await p.park()
    await p.spread()
    assert p.method is not None
    await p.method.begin(automatic_pause=False)
    await p.method.end()
    await p.cancel_power_down()
    await p.stop()

  asyncio.run(_run())


def test_prep_method_run_context_manager_aborts_on_exception():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()
    assert p.method is not None

    calls: list[str] = []
    orig_begin = p.method.begin
    orig_end = p.method.end
    orig_abort = p.method.abort

    async def rec_begin(automatic_pause: bool = False) -> None:
      calls.append("begin")
      await orig_begin(automatic_pause=automatic_pause)

    async def rec_end() -> None:
      calls.append("end")
      await orig_end()

    async def rec_abort() -> None:
      calls.append("abort")
      await orig_abort()

    p.method.begin = rec_begin  # type: ignore[method-assign]
    p.method.end = rec_end  # type: ignore[method-assign]
    p.method.abort = rec_abort  # type: ignore[method-assign]

    # Clean exit: begin + end, no abort.
    async with p.method.run():
      pass
    assert calls == ["begin", "end"]

    # Exception inside: begin + abort, re-raised.
    calls.clear()
    with pytest.raises(RuntimeError, match="boom"):
      async with p.method.run():
        raise RuntimeError("boom")
    assert calls == ["begin", "abort"]

    await p.stop()

  asyncio.run(_run())


def test_prep_device_wires_calibration_after_setup():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()
    assert p.info.num_channels == p.info.config.num_channels
    assert p.info.has_mph == p.info.config.has_mph
    assert p.calibration is not None
    assert p.calibration.num_channels == p.info.config.num_channels
    assert p.calibration.has_mph == p.info.config.has_mph
    async with p.core_grippers() as arm:
      assert isinstance(arm, PrepGripperArm)
      assert isinstance(arm.backend, PrepCoreGripper)
    await p.stop()

  asyncio.run(_run())


def test_send_command_surfaces_clear_error_for_unresolvable_path():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()

    missing = "MLPrepRoot.MLPrep"
    orig_resolve = p.driver.resolve_path

    async def _fake_resolve(path: str):
      if path == missing:
        raise KeyError(path)
      return await orig_resolve(path)

    p.driver.resolve_path = _fake_resolve  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="firmware path"):
      await p.driver.send_command(PrepCmd.PrepPark())
    await p.stop()

  asyncio.run(_run())


def test_chatterbox_preregisters_diagnostic_paths():
  async def _run() -> None:
    d = PrepChatterboxDriver()
    await d.setup()
    assert isinstance(await d.resolve_path("MLPrepRoot.MLPrepCpu"), Address)
    assert isinstance(await d.resolve_path("MLPrepRoot.PipettorRoot.ModuleInformation"), Address)
    await d.stop()

  asyncio.run(_run())


def test_force_initialize_skips_is_initialized_check():
  """When force_initialize=True, Prep.setup() never queries is_initialized."""
  from unittest.mock import AsyncMock

  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    p.info.is_initialized = AsyncMock(side_effect=AssertionError("should not be called"))  # type: ignore[method-assign]
    await p.setup(PrepSetupParams(force_initialize=True))
    p.info.is_initialized.assert_not_called()
    await p.stop()

  asyncio.run(_run())
