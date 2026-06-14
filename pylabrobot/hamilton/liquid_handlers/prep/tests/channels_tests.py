"""PrepPIPChannel facade + enumeration against the chatterbox."""

from __future__ import annotations

import asyncio

from pylabrobot.hamilton.liquid_handlers.prep import Prep, PrepPIPChannel
from pylabrobot.hamilton.liquid_handlers.prep.pip_backend import PrepPIPBackend
from pylabrobot.resources.hamilton import STARLetDeck


def _run(coro):
  asyncio.run(coro)


def test_channels_match_info_num_channels():
  """PrepPIPBackend.channels length matches info.config.num_channels on a default chatterbox."""

  async def _t():
    p = Prep(deck=STARLetDeck(), chatterbox=True)
    await p.setup()
    assert p.pip is not None
    pip_be = p.pip.backend
    assert isinstance(pip_be, PrepPIPBackend)
    assert len(pip_be.channels) == p.info.config.num_channels
    for i, ch in enumerate(pip_be.channels):
      assert isinstance(ch, PrepPIPChannel)
      assert ch.index == i
    await p.stop()

  _run(_t())


def test_channels_attach_bounds_even_when_empty_offline():
  """Chatterbox firmware tree is empty, so bounds are None — but the attribute must exist."""

  async def _t():
    p = Prep(deck=STARLetDeck(), chatterbox=True)
    await p.setup()
    assert p.pip is not None
    pip_be = p.pip.backend
    assert isinstance(pip_be, PrepPIPBackend)
    for ch in pip_be.channels:
      assert hasattr(ch, "bounds")
      assert ch.bounds is None
    await p.stop()

  _run(_t())
