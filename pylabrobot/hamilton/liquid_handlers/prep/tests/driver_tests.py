import asyncio

import pytest

from pylabrobot.hamilton.liquid_handlers.prep.chatterbox import PrepChatterboxDriver
from pylabrobot.hamilton.liquid_handlers.prep.driver import (
  _PREP_LAZY_RESOLVE_PATHS,
  PrepResolvedInterfaces,
)
from pylabrobot.hamilton.liquid_handlers.prep.prep import Prep
from pylabrobot.hamilton.tcp.packets import Address
from pylabrobot.resources.hamilton import STARLetDeck


def test_chatterbox_sets_resolved_interfaces_and_pip():
  async def _run() -> None:
    d = PrepChatterboxDriver(num_channels=2)
    await d.setup()

    assert d.has_interface("pipettor")
    assert d.prep_interfaces.pipettor == Address(1, 1, 257)
    addr = await d.require_interface("pipettor")
    assert addr == Address(1, 1, 257)
    assert d.pip.num_channels == 2
    assert d.pip.setup_finished is True

    await d.stop()

  asyncio.run(_run())


def test_prep_device_delegates_instrument_methods_to_driver():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()
    await p.park()
    await p.spread()
    await p.method_begin(automatic_pause=False)
    await p.method_end()
    await p.cancel_power_down()
    await p.stop()

  asyncio.run(_run())


def test_prep_device_wires_calibration_after_setup():
  async def _run() -> None:
    deck = STARLetDeck()
    p = Prep(deck=deck, chatterbox=True)
    await p.setup()
    assert p.calibration.num_channels == p.driver.pip.num_channels
    assert p.calibration.has_mph == p.driver.pip.has_mph
    assert p.core_gripper.deck is deck
    assert p.core_gripper.client is p.driver
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
    assert await d.resolve_path(_PREP_LAZY_RESOLVE_PATHS["mlprep_cpu"]) == Address(1, 1, 270)
    assert await d.resolve_path(_PREP_LAZY_RESOLVE_PATHS["module_information"]) == Address(
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
