"""Per-channel probing and facade for the Prep pipettor.

Channel-scoped topology discovery, bounds parsing, and per-channel firmware
queries live here rather than in ``pip_backend.py``.

The firmware object tree exposes channel internals as a single template under
``MLPrepRoot.Channel Root.Channel`` (and an analogous ``MLPrepRoot.MPH Channel
Root.Channel`` for MPH). Individual physical channels share that template —
per-channel identity lives in the node-ID component of the Address. We probe
the full object tree and match children by **path prefix**
(``"<root>.Channel Root.Channel.Squeeze.SDrive"``) rather than computing node
IDs directly.
"""

from __future__ import annotations

import logging
import struct as _struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from pylabrobot.hamilton.tcp.packets import Address

from . import prep_commands as PrepCmd

if TYPE_CHECKING:
  from .driver import PrepDriver
  from .info import PrepInstrumentInfo

logger = logging.getLogger(__name__)


# Channel index → firmware ``ChannelIndex`` enum for the dual-head pipettor
# (two channels share one Channel Root subtree; ordering is rear/front).
_CHANNEL_INDEX = {
  0: PrepCmd.ChannelIndex.RearChannel,
  1: PrepCmd.ChannelIndex.FrontChannel,
}


@dataclass(frozen=True)
class ChannelDriveMap:
  """Cached channel-drive topology discovered from the firmware tree.

  One entry per discovered channel for the sleeve sensor (``Squeeze.SDrive``),
  the Z drive (``ZAxis.ZDrive``), and the per-node ``NodeInformation`` object
  (used for firmware-string queries). Lists are parallel and sorted by tree
  traversal order (same order the firmware returns Channel Root instances).
  """

  sleeve_sensor_addrs: List[Address]
  zdrive_addrs: List[Address]
  node_info_addrs: List[Address]

  @property
  def num_channels_discovered(self) -> int:
    return len(self.sleeve_sensor_addrs)

  def to_dict(self) -> dict:
    """Serialize for logs / notebooks that prefer plain dicts."""
    return {
      "num_channels_discovered": self.num_channels_discovered,
      "sleeve_sensor_addrs": list(self.sleeve_sensor_addrs),
      "zdrive_addrs": list(self.zdrive_addrs),
      "node_info_addrs": list(self.node_info_addrs),
    }


# ---------------------------------------------------------------------------
# Firmware-tree discovery — module-level so it can be called independently of
# any backend instance (used when building the pip backend in Prep.setup, plus by
# diagnostic notebooks that hold only a driver).
# ---------------------------------------------------------------------------


async def _find_children_by_name(
  intro,
  parent_addr: Address,
  *names: str,
) -> dict:
  """Enumerate ``parent_addr``'s subobjects; return ``{name: Address}`` for matches.

  Bounded by ``subobject_count`` on the parent. Returns early once every
  requested name has been found. Children that raise on ``get_object`` (e.g.
  unknown firmware types) are skipped with a debug log.
  """
  parent = await intro.get_object(parent_addr)
  wanted = set(names)
  found: dict = {}
  for i in range(parent.subobject_count):
    try:
      sub_addr = await intro.get_subobject_address(parent_addr, i)
      sub = await intro.get_object(sub_addr)
    except Exception as e:
      logger.debug("subobject[%d] of %s failed: %s", i, parent_addr, e)
      continue
    if sub.name in wanted:
      found[sub.name] = sub_addr
      if len(found) == len(wanted):
        break
  return found


async def discover_channel_drives(
  driver: "PrepDriver",
  *,
  root_name: str = "Channel Root",
) -> ChannelDriveMap:
  """Discover per-channel drive addresses via bounded subobject enumeration.

  MLPrepRoot exposes one ``<root_name>`` child per physical channel (siblings
  with identical names, distinguished by the ``node`` component of their
  :class:`Address`). For each one we walk:

  - ``<root>.Channel.Squeeze.SDrive``     → sleeve sensor
  - ``<root>.Channel.ZAxis.ZDrive``       → Z drive
  - ``<root>.NodeInformation``            → per-channel firmware strings

  Uses ``get_subobject_address`` / ``get_object`` along the known path shape —
  no full-tree traversal. Pass ``root_name="MPH Channel Root"`` for the 8MPH
  head. For a full firmware-tree dump use
  :meth:`PrepInstrumentInfo.get_firmware_tree`.
  """
  intro = driver.introspection
  try:
    mlprep_root = await driver.resolve_path("MLPrepRoot")
    root_info = await intro.get_object(mlprep_root)
  except (KeyError, RuntimeError) as e:
    logger.debug("MLPrepRoot unavailable (%s); skipping channel discovery", e)
    return ChannelDriveMap(sleeve_sensor_addrs=[], zdrive_addrs=[], node_info_addrs=[])

  channel_root_addrs: List[Address] = []
  for i in range(root_info.subobject_count):
    try:
      sub_addr = await intro.get_subobject_address(mlprep_root, i)
      sub = await intro.get_object(sub_addr)
    except Exception as e:
      logger.debug("MLPrepRoot subobject[%d] failed: %s", i, e)
      continue
    if sub.name == root_name:
      channel_root_addrs.append(sub_addr)

  sleeve: List[Address] = []
  zdrive: List[Address] = []
  node_info: List[Address] = []

  for ch_root in channel_root_addrs:
    top = await _find_children_by_name(intro, ch_root, "Channel", "NodeInformation")
    if "NodeInformation" in top:
      node_info.append(top["NodeInformation"])

    channel_addr = top.get("Channel")
    if channel_addr is None:
      logger.warning("%s @ %s has no 'Channel' child", root_name, ch_root)
      continue

    axes = await _find_children_by_name(intro, channel_addr, "Squeeze", "ZAxis")
    if (sq_parent := axes.get("Squeeze")) is not None:
      sq = await _find_children_by_name(intro, sq_parent, "SDrive")
      if "SDrive" in sq:
        sleeve.append(sq["SDrive"])
    if (zx_parent := axes.get("ZAxis")) is not None:
      zx = await _find_children_by_name(intro, zx_parent, "ZDrive")
      if "ZDrive" in zx:
        zdrive.append(zx["ZDrive"])

  logger.info("Discovered %d %s channel drive pair(s)", len(channel_root_addrs), root_name)
  return ChannelDriveMap(
    sleeve_sensor_addrs=sleeve,
    zdrive_addrs=zdrive,
    node_info_addrs=node_info,
  )


# ---------------------------------------------------------------------------
# Per-channel movement bounds — parses PipettorService.GetChannelBounds.
# ---------------------------------------------------------------------------


async def request_channel_bounds(driver: "PrepDriver") -> List[dict]:
  """Request per-channel movement bounds from the firmware (cmd=10).

  Returns one dict per channel (keys ``x_min``, ``x_max``, ``y_min``, ``y_max``,
  ``z_min``, ``z_max`` in mm), ordered by channel index. Returns ``[]`` when
  the service cannot be resolved or the response is empty.

  These are the firmware-enforced limits — positions outside these ranges will
  be rejected with 0x0F04 (X), 0x0F05 (Y), or 0x0F06 (Z). Z bounds are for
  empty channels; with a tip attached the effective Z minimum is higher.
  """
  try:
    raw = await driver.send_command(
      PrepCmd.PrepGetChannelBounds(),
      return_raw=True,
      raise_on_error=False,
    )
  except RuntimeError:
    return []
  if raw is None:
    return []

  # Parse per-channel bounds from raw response.
  # Each channel block: channel_enum (u32 at 0x20), then 6× f32 (at 0x28):
  # x_min, x_max, y_min, y_max, z_min, z_max
  data = raw[0]
  _CHANNEL_ENUM_TO_IDX = {v: k for k, v in _CHANNEL_INDEX.items()}
  indexed: list = []

  i = 0
  while i < len(data) - 20:
    if data[i] == 0x20 and data[i + 1] == 0x00 and data[i + 2] == 0x04:
      ch_val = _struct.unpack_from("<I", data, i + 4)[0]
      ch_idx = _CHANNEL_ENUM_TO_IDX.get(ch_val)

      j = i + 8
      floats: List[float] = []
      while len(floats) < 6 and j < len(data) - 7:
        if data[j] == 0x28 and data[j + 1] == 0x00:
          floats.append(_struct.unpack_from("<f", data, j + 4)[0])
          j += 8
        else:
          j += 1

      if ch_idx is not None and len(floats) == 6:
        indexed.append(
          (
            ch_idx,
            {
              "x_min": floats[0],
              "x_max": floats[1],
              "y_min": floats[2],
              "y_max": floats[3],
              "z_min": floats[4],
              "z_max": floats[5],
            },
          )
        )
      i = j
    else:
      i += 1

  indexed.sort(key=lambda pair: pair[0])
  return [bounds for _, bounds in indexed]


# ---------------------------------------------------------------------------
# PrepPIPChannel — thin per-channel facade owned by PrepPIPBackend.channels
# ---------------------------------------------------------------------------


class PrepPIPChannel:
  """Per-channel facade: drive addresses, movement bounds, firmware-version queries.

  Instances are constructed by :func:`build_prep_channels` from :meth:`Prep.setup`
  and exposed as ``prep.pip.channels[i]``.
  """

  def __init__(
    self,
    *,
    index: int,
    driver: "PrepDriver",
    sleeve_sensor: Optional[Address] = None,
    zdrive: Optional[Address] = None,
    node_info: Optional[Address] = None,
    bounds: Optional[dict] = None,
  ) -> None:
    self.index = index
    self._driver = driver
    self.sleeve_sensor = sleeve_sensor
    self.zdrive = zdrive
    self.node_info = node_info
    self.bounds = bounds  # dict with x_min..z_max, or None if unavailable

  def __repr__(self) -> str:
    return (
      f"PrepPIPChannel(index={self.index}, node_info={self.node_info!r}, "
      f"bounds={'set' if self.bounds else 'unset'})"
    )

  async def request_firmware_version(self) -> Optional[str]:
    """Per-channel firmware version string (NodeInformation cmd=8).

    Serial number is intentionally not exposed here — NodeInformation's
    GetSerialNumber endpoint is unpopulated on shipped instruments, and the
    canonical instrument serial (pipettor module) is already surfaced via
    :meth:`PrepInstrumentInfo.get_device_serial_number`.
    """
    if self.node_info is None:
      return None
    return await self._driver._query_firmware_string(self.node_info, cmd_id=8, iface_id=1)


# ---------------------------------------------------------------------------
# Builder called from Prep.setup.
# ---------------------------------------------------------------------------


async def build_prep_channels(
  driver: "PrepDriver",
  info: "PrepInstrumentInfo",
  *,
  root_name: str = "Channel Root",
  num_channels: Optional[int] = None,
) -> List[PrepPIPChannel]:
  """Build per-channel facades, resolve drive addresses, fetch bounds.

  If ``num_channels`` is omitted, uses ``info.config.num_channels``.
  """
  drive_map = await discover_channel_drives(driver, root_name=root_name)

  if num_channels is None:
    try:
      num_channels = info.config.num_channels
    except RuntimeError:
      num_channels = None
  if num_channels is None:
    num_channels = drive_map.num_channels_discovered

  try:
    bounds_list = await request_channel_bounds(driver)
  except Exception as e:
    logger.warning("Failed to query channel bounds: %s", e)
    bounds_list = []

  def _drive_addr(attr: str, i: int) -> Optional[Address]:
    if drive_map is None:
      return None
    seq = getattr(drive_map, attr)
    return seq[i] if i < len(seq) else None

  channels: List[PrepPIPChannel] = []
  for i in range(num_channels):
    channels.append(
      PrepPIPChannel(
        index=i,
        driver=driver,
        sleeve_sensor=_drive_addr("sleeve_sensor_addrs", i),
        zdrive=_drive_addr("zdrive_addrs", i),
        node_info=_drive_addr("node_info_addrs", i),
        bounds=bounds_list[i] if i < len(bounds_list) else None,
      )
    )
  return channels


__all__ = [
  "ChannelDriveMap",
  "PrepPIPChannel",
  "build_prep_channels",
  "discover_channel_drives",
  "request_channel_bounds",
]
