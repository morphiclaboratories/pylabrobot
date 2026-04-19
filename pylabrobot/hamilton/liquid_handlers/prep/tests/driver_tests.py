import asyncio

import pytest

from pylabrobot.hamilton.liquid_handlers.prep.chatterbox import PrepChatterboxDriver
from pylabrobot.hamilton.liquid_handlers.prep.driver import (
  PREP_LAZY_RESOLVE_PATHS,
  PrepResolvedInterfaces,
  PrepSetupParams,
)
from pylabrobot.hamilton.liquid_handlers.prep.core import PrepCoreGripper, PrepGripperArm
from pylabrobot.hamilton.liquid_handlers.prep.prep import Prep
from pylabrobot.hamilton.tcp.packets import Address
from pylabrobot.resources.hamilton import STARLetDeck


def test_chatterbox_sets_resolved_interfaces_and_pip():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()

    assert p.driver.has_interface("pipettor")
    assert p.driver.prep_interfaces.pipettor == Address(1, 1, 257)
    addr = await p.driver.require_interface("pipettor")
    assert addr == Address(1, 1, 257)
    assert p.info.config.num_channels == 2
    assert p.pip.backend.num_channels == 2
    assert p.pip.backend.setup_finished is True
    # Default PrepSetupParams: use_v1_aspirate_dispense=False → v2 probe passes (chatterbox stubs).
    assert p.pip.backend._supports_v2_pipetting is True

    await p.stop()
    assert p.info._config is None

  asyncio.run(_run())


def test_chatterbox_use_v1_skips_v2_probe():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup(PrepSetupParams(use_v1_aspirate_dispense=True))
    assert p.pip.backend.setup_finished is True
    assert p.pip.backend._supports_v2_pipetting is False

    await p.stop()

  asyncio.run(_run())


def test_prep_device_motion_method_and_power_commands():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()
    await p.park()
    await p.spread()
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
    assert p.calibration.num_channels == p.info.config.num_channels
    assert p.calibration.has_mph == p.info.config.has_mph
    async with p.core_grippers() as arm:
      assert isinstance(arm, PrepGripperArm)
      assert isinstance(arm.backend, PrepCoreGripper)
    await p.stop()

  asyncio.run(_run())


def test_prep_resolved_interfaces_from_map_requires_core_keys():
  m = {
    "mlprep": Address(1, 1, 1),
    "pipettor": Address(1, 1, 2),
    "coordinator": Address(1, 1, 3),
    "calibration": None,
    "deck_config": Address(1, 1, 5),
    "mph": None,
    "mlprep_service": None,
  }
  r = PrepResolvedInterfaces.from_resolution_map(m)
  assert r.calibration is None
  assert r.deck_config == Address(1, 1, 5)


def test_chatterbox_preregisters_lazy_resolve_paths():
  async def _run() -> None:
    d = PrepChatterboxDriver()
    await d.setup()
    assert await d.resolve_path(PREP_LAZY_RESOLVE_PATHS["mlprep_cpu"]) == Address(1, 1, 270)
    assert await d.resolve_path(PREP_LAZY_RESOLVE_PATHS["module_information"]) == Address(
      1, 1, 271
    )
    await d.stop()

  asyncio.run(_run())


def test_require_interface_unknown_key():
  async def _run() -> None:
    d = PrepChatterboxDriver()
    await d.setup()
    with pytest.raises(KeyError):
      await d.require_interface("no_such_iface")
    await d.stop()

  asyncio.run(_run())
