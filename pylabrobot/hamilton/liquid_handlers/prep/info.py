"""Prep instrument info service.

Canonical holder of device-wide metadata. ``PrepInstrumentInfo`` owns the
cached ``InstrumentConfig`` snapshot (loaded in :meth:`_on_setup`), exposes its
fields as sync properties, and performs on-demand diagnostic / firmware queries
via the driver transport (``require_interface``, ``send_command``,
``PrepDriver._lazy_diag_address``, ``PrepDriver._query_firmware_string``).

User-facing instrument-wide pool: ``prep.info``. :class:`~pylabrobot.hamilton.liquid_handlers.prep.pip_backend.PrepPIPBackend`
is built with ``prep=self`` and uses ``self._prep.info`` for the same object (config and instrument queries).
``PrepCalibration`` still receives ``info=self.info`` from ``Prep``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Tuple

from pylabrobot.hamilton.tcp.introspection import FirmwareTree

from . import prep_commands as PrepCmd

if TYPE_CHECKING:
  from .driver import PrepDriver

logger = logging.getLogger(__name__)


class PrepInstrumentInfo:
  """Owns the cached ``InstrumentConfig`` + async instrument-metadata queries."""

  def __init__(self, driver: "PrepDriver"):
    self._driver = driver
    self._config: Optional[PrepCmd.InstrumentConfig] = None

  # -- Lifecycle --------------------------------------------------------------

  async def _on_setup(self) -> None:
    """Fetch and cache the instrument config. Called from :meth:`Prep.setup`."""
    self._config = await self._load_instrument_config()

  async def _on_stop(self) -> None:
    self._config = None

  # -- Cached config ----------------------------------------------------------

  @property
  def config(self) -> PrepCmd.InstrumentConfig:
    """Cached ``InstrumentConfig``. Raises if ``_on_setup`` has not run."""
    if self._config is None:
      raise RuntimeError("Instrument config not available. Call Prep.setup() first.")
    return self._config

  @property
  def num_channels(self) -> int:
    return self.config.num_channels

  @property
  def has_mph(self) -> bool:
    return self.config.has_mph

  @property
  def deck_bounds(self) -> Optional[PrepCmd.DeckBounds]:
    return self.config.deck_bounds

  @property
  def deck_sites(self) -> Tuple[PrepCmd.DeckSiteInfo, ...]:
    return self.config.deck_sites

  @property
  def waste_sites(self) -> Tuple[PrepCmd.WasteSiteInfo, ...]:
    return self.config.waste_sites

  @property
  def default_traverse_height(self) -> Optional[float]:
    return self.config.default_traverse_height

  @property
  def has_enclosure(self) -> bool:
    return self.config.has_enclosure

  @property
  def safe_speeds_enabled(self) -> bool:
    return self.config.safe_speeds_enabled

  async def refresh(self) -> PrepCmd.InstrumentConfig:
    """Re-query instrument config and update the cached snapshot."""
    self._config = await self._load_instrument_config()
    return self._config

  # -- Instrument config (MLPrep / deck / service) ----------------------------

  async def get_present_channels(self) -> Optional[Tuple[PrepCmd.ChannelIndex, ...]]:
    """Query which channels are present (GetPresentChannels on MLPrepService)."""
    d = self._driver
    if not d.has_interface("mlprep_service"):
      return None
    try:
      service_addr = await d.require_interface("mlprep_service")
      resp = await d.send_command(PrepCmd.PrepGetPresentChannels(dest=service_addr))
      if resp is None or not getattr(resp, "channels", None):
        return None
      return tuple(
        PrepCmd.ChannelIndex(v) if v in (0, 1, 2, 3) else PrepCmd.ChannelIndex.InvalidIndex
        for v in resp.channels
      )
    except (
      TimeoutError,
      ConnectionError,
      ConnectionResetError,
      ConnectionAbortedError,
      BrokenPipeError,
      OSError,
    ):
      raise
    except Exception as e:
      logger.warning("Failed to query present channels: %s", e)
      return None

  async def _load_instrument_config(self) -> PrepCmd.InstrumentConfig:
    """Aggregate MLPrep, DeckConfiguration, and MLPrepService into ``InstrumentConfig``."""
    d = self._driver
    mlprep = await d.require_interface("mlprep")
    enc_resp = await d.send_command(PrepCmd.PrepGetIsEnclosurePresent(dest=mlprep))
    safe_resp = await d.send_command(PrepCmd.PrepGetSafeSpeedsEnabled(dest=mlprep))
    height_resp = await d.send_command(PrepCmd.PrepGetDefaultTraverseHeight(dest=mlprep))
    has_enclosure = bool(enc_resp.value) if enc_resp else False
    safe_speeds_enabled = bool(safe_resp.value) if safe_resp else False
    default_traverse_height = float(height_resp.value) if height_resp else None

    deck_bounds: Optional[PrepCmd.DeckBounds] = None
    deck_sites: Tuple[PrepCmd.DeckSiteInfo, ...] = ()
    waste_sites: Tuple[PrepCmd.WasteSiteInfo, ...] = ()
    deck_addr = await d.require_interface("deck_config")

    bounds_resp = await d.send_command(PrepCmd.PrepGetDeckBounds(dest=deck_addr))
    if bounds_resp:
      deck_bounds = PrepCmd.DeckBounds(
        min_x=bounds_resp.min_x,
        max_x=bounds_resp.max_x,
        min_y=bounds_resp.min_y,
        max_y=bounds_resp.max_y,
        min_z=bounds_resp.min_z,
        max_z=bounds_resp.max_z,
      )

    sites_resp = await d.send_command(PrepCmd.PrepGetDeckSiteDefinitions(dest=deck_addr))
    if sites_resp and sites_resp.sites:
      deck_sites = tuple(
        PrepCmd.DeckSiteInfo(
          id=int(s.id),
          left_bottom_front_x=float(s.left_bottom_front_x),
          left_bottom_front_y=float(s.left_bottom_front_y),
          left_bottom_front_z=float(s.left_bottom_front_z),
          length=float(s.length),
          width=float(s.width),
          height=float(s.height),
        )
        for s in sites_resp.sites
      )
      logger.debug("Discovered %d deck sites", len(deck_sites))

    waste_resp = await d.send_command(PrepCmd.PrepGetWasteSiteDefinitions(dest=deck_addr))
    if waste_resp and waste_resp.sites:
      waste_sites = tuple(
        PrepCmd.WasteSiteInfo(
          index=int(s.index),
          x_position=float(s.x_position),
          y_position=float(s.y_position),
          z_position=float(s.z_position),
          z_seek=float(s.z_seek),
        )
        for s in waste_resp.sites
      )
      logger.debug("Discovered %d waste sites: %s", len(waste_sites), waste_sites)

    present = await self.get_present_channels()
    if present is not None:
      dual = [
        c
        for c in present
        if c in (PrepCmd.ChannelIndex.FrontChannel, PrepCmd.ChannelIndex.RearChannel)
      ]
      num_channels = len(dual)
      has_mph = PrepCmd.ChannelIndex.MPHChannel in present
    else:
      num_channels = 2
      has_mph = False

    return PrepCmd.InstrumentConfig(
      deck_bounds=deck_bounds,
      has_enclosure=has_enclosure,
      safe_speeds_enabled=safe_speeds_enabled,
      deck_sites=deck_sites,
      waste_sites=waste_sites,
      default_traverse_height=default_traverse_height,
      num_channels=num_channels,
      has_mph=has_mph,
    )

  async def is_initialized(self) -> bool:
    """Whether MLPrep reports as initialized (GetIsInitialized, cmd=2)."""
    result = await self._driver.send_command(
      PrepCmd.PrepGetIsInitialized(dest=await self._driver.require_interface("mlprep"))
    )
    if result is None:
      return False
    return bool(result.value)

  async def get_tip_and_needle_definitions(self) -> Tuple[PrepCmd.TipDefinition, ...]:
    """Tip/needle definitions (GetTipAndNeedleDefinitions, cmd=11)."""
    result = await self._driver.send_command(
      PrepCmd.PrepGetTipAndNeedleDefinitions(dest=await self._driver.require_interface("mlprep"))
    )
    if result is None or not getattr(result, "definitions", None):
      return ()
    return tuple(result.definitions)

  # -- Firmware string queries (orchestration; decode on PrepDriver) ----------

  async def get_firmware_version(self) -> Optional[str]:
    addr = await self._driver._lazy_diag_address("mlprep_cpu")
    if addr is None:
      return None
    return await self._driver._query_firmware_string(addr, cmd_id=8)

  async def get_device_serial_number(self) -> Optional[str]:
    addr = await self._driver._lazy_diag_address("mlprep_cpu")
    if addr is None:
      return None
    return await self._driver._query_firmware_string(addr, cmd_id=9)

  async def get_bootloader_version(self) -> Optional[str]:
    addr = await self._driver._lazy_diag_address("mlprep_cpu")
    if addr is None:
      return None
    return await self._driver._query_firmware_string(addr, cmd_id=2, iface_id=2)

  async def get_module_version(self) -> Optional[str]:
    addr = await self._driver._lazy_diag_address("module_information")
    if addr is None:
      return None
    return await self._driver._query_firmware_string(addr, cmd_id=8)

  async def get_module_part_number(self) -> Optional[str]:
    addr = await self._driver._lazy_diag_address("module_information")
    if addr is None:
      return None
    return await self._driver._query_firmware_string(addr, cmd_id=5)

  async def get_firmware_tree(self, refresh: bool = False) -> FirmwareTree:
    """Firmware object tree. ``print(await info.get_firmware_tree())`` for a diagnostic dump."""
    return await self._driver.introspection.get_firmware_tree(refresh=refresh)
