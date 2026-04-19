"""PrepDriver: Hamilton TCP driver for Hamilton Prep liquid handlers (Nimbus-style layout)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Mapping, Optional, Tuple

from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.hamilton.tcp.client import HamiltonTCPClient
from pylabrobot.hamilton.tcp.error_tables import PREP_ERROR_CODES
from pylabrobot.hamilton.tcp.interface_bundle import InterfacePathSpec, resolve_interface_path_specs
from pylabrobot.hamilton.tcp.packets import Address

from . import prep_commands as PrepCmd

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
# :data:`_PREP_INTERFACES`, which is bulk-resolved in :meth:`_resolve_prep_interfaces`.
# See :meth:`PrepDriver._lazy_diag_address`.
PREP_LAZY_RESOLVE_PATHS: Dict[str, str] = {
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
  """TCP/pip setup flags. The Prep device's deck is supplied at construction, not here."""

  smart: bool = True
  force_initialize: bool = False
  default_traverse_height: Optional[float] = None
  use_v1_aspirate_dispense: bool = False


class PrepDriver(HamiltonTCPClient):
  """Hamilton TCP client for Prep: connection, interface resolution, lazy diagnostic paths, firmware string decode.

  MLPrep motion, method lifecycle, power, and deck-light commands are implemented on ``Prep`` and
  ``PrepMethodLifecycle``, not on this driver.

  Transport only — no pip backend reference. The :class:`~pylabrobot.hamilton.liquid_handlers.prep.prep.Prep`
  device constructs :class:`~pylabrobot.hamilton.liquid_handlers.prep.pip_backend.PrepPIPBackend` after
  :meth:`setup` completes transport and interface resolution.
  """

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
    self._lazy_diag_cache: Dict[str, Optional[Address]] = {}

  # ---------------------------------------------------------------------------
  # Lifecycle
  # ---------------------------------------------------------------------------

  async def setup(self, backend_params: Optional[BackendParams] = None):
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

  # ---------------------------------------------------------------------------
  # Lazy diagnostic paths (JIT :meth:`resolve_path`, same family as interface resolution)
  # ---------------------------------------------------------------------------

  async def _lazy_diag_address(self, key: str) -> Optional[Address]:
    """Resolve and cache a :data:`PREP_LAZY_RESOLVE_PATHS` target.

    Missing or unresolvable paths are cached as ``None`` so repeated calls stay cheap.
    """
    if key not in PREP_LAZY_RESOLVE_PATHS:
      raise KeyError(f"unknown diagnostic object key: {key!r}")
    if key in self._lazy_diag_cache:
      return self._lazy_diag_cache[key]
    path = PREP_LAZY_RESOLVE_PATHS[key]
    try:
      addr = await self.resolve_path(path)
    except (KeyError, RuntimeError, TypeError):
      addr = None
    self._lazy_diag_cache[key] = addr
    return addr

  # ---------------------------------------------------------------------------
  # Firmware string queries (transport: raw HOI decode + status query)
  # ---------------------------------------------------------------------------

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
