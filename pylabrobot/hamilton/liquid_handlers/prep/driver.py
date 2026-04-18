"""PrepDriver: Hamilton TCP driver for Hamilton Prep liquid handlers (Nimbus-style layout)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Mapping, Optional, Tuple

from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.hamilton.tcp.client import HamiltonTCPClient
from pylabrobot.hamilton.tcp.error_tables import PREP_ERROR_CODES
from pylabrobot.hamilton.tcp.interface_bundle import InterfacePathSpec, resolve_interface_path_specs
from pylabrobot.hamilton.tcp.introspection import ObjectInfo
from pylabrobot.hamilton.tcp.packets import Address

from . import prep_commands as PrepCmd

if TYPE_CHECKING:
  from pylabrobot.resources.deck import Deck

  from .pip_backend import PrepPIPBackend

logger = logging.getLogger(__name__)

_EXPECTED_ROOT = "MLPrepRoot"

# Backwards-compatible alias (same as :class:`InterfacePathSpec`).
PrepInterfaceSpec = InterfacePathSpec


# Same logical interfaces as the prep_tcp reference backend (path strings).
_PREP_INTERFACES: Dict[str, InterfacePathSpec] = {
  "mlprep": InterfacePathSpec("MLPrepRoot.MLPrep", True, True),
  "pipettor": InterfacePathSpec("MLPrepRoot.PipettorRoot.Pipettor", True, True),
  "coordinator": InterfacePathSpec("MLPrepRoot.ChannelCoordinator", True, True),
  "calibration": InterfacePathSpec("MLPrepRoot.MLPrepCalibration", False, True),
  "deck_config": InterfacePathSpec("MLPrepRoot.MLPrepCalibration.DeckConfiguration", True, True),
  "mph": InterfacePathSpec("MLPrepRoot.MphRoot.MPH", False, True),
  "mlprep_service": InterfacePathSpec("MLPrepRoot.MLPrepService", False, True),
}

# Object-tree paths resolved on first use (not during :meth:`setup`). Contrast with
# :data:`_PREP_INTERFACES`, which is bulk-resolved into :class:`PrepResolvedInterfaces`.
# See :meth:`PrepDriver._lazy_diag_address`.
_PREP_LAZY_RESOLVE_PATHS: Dict[str, str] = {
  "mlprep_cpu": "MLPrepRoot.MLPrepCpu",
  "module_information": "MLPrepRoot.PipettorRoot.ModuleInformation",
}


@dataclass(frozen=True)
class PrepResolvedInterfaces:
  """Concrete Prep firmware handles after :meth:`PrepDriver.setup`."""

  mlprep: Address
  pipettor: Address
  coordinator: Address
  calibration: Optional[Address]
  deck_config: Address
  mph: Optional[Address]
  mlprep_service: Optional[Address]

  @staticmethod
  def from_resolution_map(m: Mapping[str, Optional[Address]]) -> PrepResolvedInterfaces:
    def req(key: str) -> Address:
      a = m.get(key)
      if a is None:
        raise RuntimeError(f"internal: missing required Prep interface '{key}'")
      return a

    return PrepResolvedInterfaces(
      mlprep=req("mlprep"),
      pipettor=req("pipettor"),
      coordinator=req("coordinator"),
      calibration=m.get("calibration"),
      deck_config=req("deck_config"),
      mph=m.get("mph"),
      mlprep_service=m.get("mlprep_service"),
    )


@dataclass
class PrepSetupParams(BackendParams):
  deck: Optional["Deck"] = None
  smart: bool = True
  force_initialize: bool = False
  default_traverse_height: Optional[float] = None
  use_v1_aspirate_dispense: bool = False


class PrepDriver(HamiltonTCPClient):
  """TCP transport + Prep interface resolution (addresses cached like :class:`NimbusDriver`)."""

  def __init__(
    self,
    host: str,
    port: int = 2000,
    read_timeout: float = 300.0,
    write_timeout: float = 30.0,
    auto_reconnect: bool = True,
    max_reconnect_attempts: int = 3,
    connection_timeout: int = 600,
    error_codes: Optional[Dict[Tuple[int, int, int, int, int], str]] = None,
  ):
    merged = {**PREP_ERROR_CODES, **(error_codes or {})}
    super().__init__(
      host=host,
      port=port,
      read_timeout=read_timeout,
      write_timeout=write_timeout,
      auto_reconnect=auto_reconnect,
      max_reconnect_attempts=max_reconnect_attempts,
      connection_timeout=connection_timeout,
      error_codes=merged,
    )
    self._resolved_interfaces: Dict[str, Optional[Address]] = {}
    self._prep_resolved: Optional[PrepResolvedInterfaces] = None
    self.pip: PrepPIPBackend  # set in setup()
    self._lazy_diag_cache: Dict[str, Optional[Address]] = {}

  # ---------------------------------------------------------------------------
  # Lifecycle
  # ---------------------------------------------------------------------------

  async def setup(self, backend_params: Optional[BackendParams] = None):
    from .pip_backend import PrepPIPBackend

    if backend_params is None:
      params = PrepSetupParams()
    elif isinstance(backend_params, PrepSetupParams):
      params = backend_params
    else:
      raise TypeError(
        "PrepDriver.setup expected PrepSetupParams | None for backend_params, "
        f"got {type(backend_params).__name__}"
      )

    await super().setup()

    root = await self.discovered_root_name()
    if root != _EXPECTED_ROOT:
      raise RuntimeError(
        f"Expected root '{_EXPECTED_ROOT}' (Prep), but discovered '{root}'. Wrong instrument?"
      )

    await self._resolve_prep_interfaces()

    self.pip = PrepPIPBackend(
      driver=self,
      deck=params.deck,
      default_traverse_height=params.default_traverse_height,
      use_v1_aspirate_dispense=params.use_v1_aspirate_dispense,
    )

  async def stop(self) -> None:
    await super().stop()
    self._resolved_interfaces.clear()
    self._prep_resolved = None
    self._lazy_diag_cache.clear()

  # ---------------------------------------------------------------------------
  # Interface resolution
  # ---------------------------------------------------------------------------

  def has_interface(self, name: str) -> bool:
    return name in self._resolved_interfaces and self._resolved_interfaces[name] is not None

  async def require_interface(self, name: str) -> Address:
    if name not in _PREP_INTERFACES:
      raise KeyError(f"Unknown interface: {name}")
    spec = _PREP_INTERFACES[name]
    addr = self._resolved_interfaces.get(name)
    if addr is None:
      msg = f"Could not find interface '{name}' ({spec.path}) on Prep."
      if spec.raise_when_missing:
        logger.warning("%s", msg)
      raise RuntimeError(msg)
    return addr

  @property
  def prep_interfaces(self) -> PrepResolvedInterfaces:
    if self._prep_resolved is None:
      raise RuntimeError("Prep interfaces not resolved. Call setup() first.")
    return self._prep_resolved

  async def _resolve_prep_interfaces(self) -> None:
    """Resolve all configured dot-paths; required interfaces fail fast."""
    self._resolved_interfaces.clear()
    self._prep_resolved = None
    self._resolved_interfaces.update(
      await resolve_interface_path_specs(self, _PREP_INTERFACES, instrument_label="Prep")
    )
    self._prep_resolved = PrepResolvedInterfaces.from_resolution_map(self._resolved_interfaces)

  # ---------------------------------------------------------------------------
  # Discovery and firmware tree
  # ---------------------------------------------------------------------------

  async def discovered_root_name(self) -> str:
    roots = self.get_root_object_addresses()
    if not roots:
      raise RuntimeError("No root objects discovered. Call setup() first.")
    info = await self.introspection.get_object(roots[0])
    return info.name

  async def print_firmware_tree(self) -> None:
    """Walk the full firmware object tree and print a formatted tree representation.

    Each object shows its name, address, firmware version, method count, and child count.
    Useful for diagnostics and understanding the instrument's firmware topology.
    """
    nodes = await self.get_firmware_tree_flat()
    if not nodes:
      print("(no root objects discovered)")
      return

    lines: list[str] = []
    children: dict[str, list[str]] = {}
    by_path: dict[str, tuple[Address, ObjectInfo]] = {}
    for path, addr, obj in nodes:
      by_path[path] = (addr, obj)
      parent = ".".join(path.split(".")[:-1])
      children.setdefault(parent, []).append(path)
    for child_list in children.values():
      child_list.sort()

    def render(path: str, prefix: str, is_last: bool) -> None:
      path_addr, obj = by_path[path]
      connector = "└── " if is_last else "├── "
      version_str = f", version={obj.version}" if obj.version else ""
      lines.append(
        f"{prefix}{connector}{obj.name} @ {path_addr} "
        f"(methods={obj.method_count}, children={obj.subobject_count}{version_str})"
      )
      child_prefix = prefix + ("    " if is_last else "│   ")
      direct_children = children.get(path, [])
      for idx, child_path in enumerate(direct_children):
        render(child_path, child_prefix, is_last=(idx == len(direct_children) - 1))

    roots = sorted(path for path in by_path if "." not in path)
    for idx, root_path in enumerate(roots):
      render(root_path, "", is_last=(idx == len(roots) - 1))

    print("\n".join(lines))

  # ---------------------------------------------------------------------------
  # MLPrep / instrument-wide commands
  # ---------------------------------------------------------------------------

  async def park(self) -> None:
    """Park the instrument."""
    await self.send_command(PrepCmd.PrepPark(dest=await self.require_interface("mlprep")))

  async def spread(self) -> None:
    """Spread channels."""
    await self.send_command(PrepCmd.PrepSpread(dest=await self.require_interface("mlprep")))

  async def method_begin(self, automatic_pause: bool = False) -> None:
    """Signal the start of a liquid-handling method."""
    await self.send_command(
      PrepCmd.PrepMethodBegin(
        dest=await self.require_interface("mlprep"),
        automatic_pause=automatic_pause,
      )
    )

  async def method_end(self) -> None:
    """Signal the end of a liquid-handling method."""
    await self.send_command(PrepCmd.PrepMethodEnd(dest=await self.require_interface("mlprep")))

  async def method_abort(self) -> None:
    """Abort the current method."""
    await self.send_command(PrepCmd.PrepMethodAbort(dest=await self.require_interface("mlprep")))

  async def power_down_request(self) -> None:
    """Request power down (instrument will prepare for shutdown; use cancel_power_down to abort)."""
    await self.send_command(
      PrepCmd.PrepPowerDownRequest(dest=await self.require_interface("mlprep"))
    )

  async def confirm_power_down(self) -> None:
    """Confirm power down (completes shutdown; only call when safe to power off)."""
    await self.send_command(
      PrepCmd.PrepConfirmPowerDown(dest=await self.require_interface("mlprep"))
    )

  async def cancel_power_down(self) -> None:
    """Cancel a pending power-down request."""
    await self.send_command(
      PrepCmd.PrepCancelPowerDown(dest=await self.require_interface("mlprep"))
    )

  async def get_deck_light(self) -> Tuple[int, int, int, int]:
    """Get the current deck LED colour (white, red, green, blue)."""
    result = await self.send_command(
      PrepCmd.PrepGetDeckLight(dest=await self.require_interface("mlprep"))
    )
    if result is None:
      raise ValueError("No response from GetDeckLight.")
    return (result.white, result.red, result.green, result.blue)

  async def set_deck_light(self, white: int, red: int, green: int, blue: int) -> None:
    """Set the deck LED colour."""
    await self.send_command(
      PrepCmd.PrepSetDeckLight(
        dest=await self.require_interface("mlprep"),
        white=white,
        red=red,
        green=green,
        blue=blue,
      )
    )

  # ---------------------------------------------------------------------------
  # Instrument configuration (MLPrep / deck / service)
  # ---------------------------------------------------------------------------

  async def get_present_channels(self) -> Optional[Tuple[PrepCmd.ChannelIndex, ...]]:
    """Query which channels are present (GetPresentChannels on MLPrepService)."""
    if not self.has_interface("mlprep_service"):
      return None
    try:
      service_addr = await self.require_interface("mlprep_service")
      resp = await self.send_command(PrepCmd.PrepGetPresentChannels(dest=service_addr))
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

  async def get_instrument_config(self) -> PrepCmd.InstrumentConfig:
    """Aggregate MLPrep, DeckConfiguration, and MLPrepService into :class:`PrepCmd.InstrumentConfig`."""
    mlprep = await self.require_interface("mlprep")
    enc_resp = await self.send_command(PrepCmd.PrepGetIsEnclosurePresent(dest=mlprep))
    safe_resp = await self.send_command(PrepCmd.PrepGetSafeSpeedsEnabled(dest=mlprep))
    height_resp = await self.send_command(PrepCmd.PrepGetDefaultTraverseHeight(dest=mlprep))
    has_enclosure = bool(enc_resp.value) if enc_resp else False
    safe_speeds_enabled = bool(safe_resp.value) if safe_resp else False
    default_traverse_height = float(height_resp.value) if height_resp else None

    deck_bounds: Optional[PrepCmd.DeckBounds] = None
    deck_sites: Tuple[PrepCmd.DeckSiteInfo, ...] = ()
    waste_sites: Tuple[PrepCmd.WasteSiteInfo, ...] = ()
    deck_addr = await self.require_interface("deck_config")

    bounds_resp = await self.send_command(PrepCmd.PrepGetDeckBounds(dest=deck_addr))
    if bounds_resp:
      deck_bounds = PrepCmd.DeckBounds(
        min_x=bounds_resp.min_x,
        max_x=bounds_resp.max_x,
        min_y=bounds_resp.min_y,
        max_y=bounds_resp.max_y,
        min_z=bounds_resp.min_z,
        max_z=bounds_resp.max_z,
      )

    sites_resp = await self.send_command(PrepCmd.PrepGetDeckSiteDefinitions(dest=deck_addr))
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

    waste_resp = await self.send_command(PrepCmd.PrepGetWasteSiteDefinitions(dest=deck_addr))
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
    """Query whether MLPrep reports as initialized (GetIsInitialized, cmd=2)."""
    result = await self.send_command(
      PrepCmd.PrepGetIsInitialized(dest=await self.require_interface("mlprep"))
    )
    if result is None:
      return False
    return bool(result.value)

  async def get_tip_and_needle_definitions(self) -> Tuple[PrepCmd.TipDefinition, ...]:
    """Return tip/needle definitions (GetTipAndNeedleDefinitions, cmd=11)."""
    result = await self.send_command(
      PrepCmd.PrepGetTipAndNeedleDefinitions(dest=await self.require_interface("mlprep"))
    )
    if result is None or not getattr(result, "definitions", None):
      return ()
    return tuple(result.definitions)

  async def is_parked(self) -> bool:
    """Query whether MLPrep is parked (IsParked, cmd=34, COMMAND_REQUEST)."""
    result = await self.send_command(
      PrepCmd.PrepIsParked(dest=await self.require_interface("mlprep"))
    )
    if result is None:
      return False
    return bool(result.value)

  async def is_spread(self) -> bool:
    """Query whether channels are spread (IsSpread, cmd=35, COMMAND_REQUEST)."""
    result = await self.send_command(
      PrepCmd.PrepIsSpread(dest=await self.require_interface("mlprep"))
    )
    if result is None:
      return False
    return bool(result.value)

  # ---------------------------------------------------------------------------
  # Firmware string queries (lazy resolve_path for object addresses)
  # ---------------------------------------------------------------------------

  async def _lazy_diag_address(self, key: str) -> Optional[Address]:
    """Resolve and cache a :data:`_PREP_LAZY_RESOLVE_PATHS` target (JIT; not run at setup).

    Missing or unresolvable paths are cached as ``None`` so repeated calls stay cheap.
    """
    if key not in _PREP_LAZY_RESOLVE_PATHS:
      raise KeyError(f"unknown diagnostic object key: {key!r}")
    if key in self._lazy_diag_cache:
      return self._lazy_diag_cache[key]
    path = _PREP_LAZY_RESOLVE_PATHS[key]
    try:
      addr = await self.resolve_path(path)
    except (KeyError, RuntimeError, TypeError):
      addr = None
    self._lazy_diag_cache[key] = addr
    return addr

  @staticmethod
  def _decode_firmware_string(raw: Optional[tuple]) -> Optional[str]:
    """Decode a string from a raw HOI response (Hamilton string wire format)."""
    if raw is None:
      return None
    data: bytes = raw[0]
    i = 0
    while i < len(data) - 3:
      if data[i] == 0x0F and data[i + 1] in (0x00, 0x01):
        slen = int.from_bytes(data[i + 2 : i + 4], "little")
        if slen > 0 and i + 4 + slen <= len(data):
          return data[i + 4 : i + 4 + slen].decode("utf-8", errors="replace").rstrip("\x00")
      i += 1
    return None

  async def _query_firmware_string(
    self, addr: Address, cmd_id: int, iface_id: int = 3
  ) -> Optional[str]:
    """Send a status query and decode the string response."""
    Cmd = type(
      "_FWQuery",
      (PrepCmd._PrepStatusQuery,),
      {"command_id": cmd_id, "interface_id": iface_id, "__annotations__": {"dest": Address}},
    )
    raw: Optional[tuple] = await self.send_command(
      Cmd(dest=addr), return_raw=True, raise_on_error=False
    )
    return self._decode_firmware_string(raw)

  async def request_firmware_version(self) -> Optional[str]:
    """Instrument controller firmware version string from MLPrepCpu (lazy path resolve)."""
    addr = await self._lazy_diag_address("mlprep_cpu")
    if addr is None:
      return None
    return await self._query_firmware_string(addr, cmd_id=8)

  async def request_device_serial_number(self) -> Optional[str]:
    """Instrument serial number from MLPrepCpu."""
    addr = await self._lazy_diag_address("mlprep_cpu")
    if addr is None:
      return None
    return await self._query_firmware_string(addr, cmd_id=9)

  async def request_bootloader_version(self) -> Optional[str]:
    """Bootloader version string from MLPrepCpu."""
    addr = await self._lazy_diag_address("mlprep_cpu")
    if addr is None:
      return None
    return await self._query_firmware_string(addr, cmd_id=2, iface_id=2)

  async def request_module_version(self) -> Optional[str]:
    """Pipettor module version from ModuleInformation."""
    addr = await self._lazy_diag_address("module_information")
    if addr is None:
      return None
    return await self._query_firmware_string(addr, cmd_id=8)

  async def request_module_part_number(self) -> Optional[str]:
    """Module part number from ModuleInformation."""
    addr = await self._lazy_diag_address("module_information")
    if addr is None:
      return None
    return await self._query_firmware_string(addr, cmd_id=5)
