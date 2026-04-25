"""PrepDriver: Hamilton TCP driver for Hamilton Prep liquid handlers (Nimbus-style layout).

Transport-only: opens TCP, discovers the firmware root, and resolves one bootstrap
handle — :attr:`PrepDriver.mlprep_address` (``MLPrepRoot.MLPrep``). Everything
else uses :meth:`HamiltonTCPClient.resolve_path`, which consults the introspection
registry (cache-hot after the first hit).

**JIT command targets.** Concrete :class:`~pylabrobot.hamilton.liquid_handlers.prep.prep_commands.PrepCommand`
subclasses declare ``firmware_path``; :meth:`PrepDriver.send_command` resolves
that path when ``dest`` is the unresolved sentinel. No parallel path tables on
backends.

**Bootstrap info.** :class:`~pylabrobot.hamilton.liquid_handlers.prep.info.PrepInstrumentInfo`
resolves a small set of diagnostic paths (see ``PrepInstrumentInfo._paths``)
during setup via the same ``resolve_path`` cache.

**Channel topology** (per-channel drive addresses) is discovered in
:mod:`~pylabrobot.hamilton.liquid_handlers.prep.channels` by walking the tree
from ``MLPrepRoot``, not via a separate registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, cast

from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.hamilton.tcp.client import HamiltonTCPClient
from pylabrobot.hamilton.tcp.commands import TCPCommand
from pylabrobot.hamilton.tcp.error_tables import PREP_ERROR_CODES
from pylabrobot.hamilton.tcp.packets import Address

from . import prep_commands as PrepCmd
from .prep_commands import _UNRESOLVED, PrepCommand

logger = logging.getLogger(__name__)

_EXPECTED_ROOT = "MLPrepRoot"

# Canonical firmware path strings (single source for driver, chatterbox, probes).
MLPREP_OBJECT_PATH = "MLPrepRoot.MLPrep"
PIPETTOR_OBJECT_PATH = "MLPrepRoot.PipettorRoot.Pipettor"
MPH_OBJECT_PATH = "MLPrepRoot.MphRoot.MPH"


@dataclass
class PrepSetupParams(BackendParams):
  """TCP/pip setup flags. The Prep device's deck is supplied at construction, not here."""

  smart: bool = True
  force_initialize: bool = False
  default_traverse_height: Optional[float] = None
  use_v1_aspirate_dispense: bool = False


class PrepDriver(HamiltonTCPClient):
  """Hamilton TCP client for Prep: connection, MLPrep bootstrap, firmware string decode.

  Instrument-wide motion, power, and deck-light entry points live on
  :class:`~pylabrobot.hamilton.liquid_handlers.prep.prep.Prep` and
  :class:`~pylabrobot.hamilton.liquid_handlers.prep.method.PrepMethodLifecycle`.
  Pipettor, calibration, and MPH traffic goes through :class:`PrepCommand` plus
  :meth:`send_command` / :meth:`resolve_path`, or through peers that build those
  commands.
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
    self._mlprep_address: Optional[Address] = None

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
    del params  # consumed by Prep / peers, not the transport

    await super().setup()

    root = await self.discovered_root_name()
    if root != _EXPECTED_ROOT:
      raise RuntimeError(
        f"Expected root '{_EXPECTED_ROOT}' (Prep), but discovered '{root}'. Wrong instrument?"
      )

    self._mlprep_address = await self.resolve_path(MLPREP_OBJECT_PATH)

  async def stop(self) -> None:
    await super().stop()
    self._mlprep_address = None

  # ---------------------------------------------------------------------------
  # MLPrep root handle (resolved in :meth:`setup`)
  # ---------------------------------------------------------------------------

  @property
  def mlprep_address(self) -> Address:
    """Address of ``MLPrepRoot.MLPrep``. Raises if :meth:`setup` has not run."""
    if self._mlprep_address is None:
      raise RuntimeError("MLPrep address not resolved. Call setup() first.")
    return self._mlprep_address

  # ---------------------------------------------------------------------------
  # JIT firmware-path resolution for PrepCommand.dest
  # ---------------------------------------------------------------------------

  async def send_command(
    self,
    command: TCPCommand,
    ensure_connection: bool = True,
    return_raw: bool = False,
    raise_on_error: bool = True,
    read_timeout: Optional[float] = None,
  ) -> Any:
    if isinstance(command, PrepCommand) and command.dest == _UNRESOLVED:
      path = type(command).firmware_path
      if path is None:
        raise RuntimeError(
          f"{type(command).__name__} has no firmware_path declared and no "
          "explicit dest= supplied at construction. Polymorphic-dest commands "
          "must pass dest= to send_command."
        )
      try:
        addr = await self.resolve_path(path)
      except KeyError as exc:
        raise RuntimeError(
          f"Cannot send {type(command).__name__}: firmware path "
          f"{path!r} did not resolve on this instrument ({exc})."
        ) from exc
      command.dest = addr
      command.dest_address = addr
    return await super().send_command(
      command,
      ensure_connection=ensure_connection,
      return_raw=return_raw,
      raise_on_error=raise_on_error,
      read_timeout=read_timeout,
    )

  # ---------------------------------------------------------------------------
  # Discovery
  # ---------------------------------------------------------------------------

  async def discovered_root_name(self) -> str:
    roots = self.get_root_object_addresses()
    if not roots:
      raise RuntimeError("No root objects discovered. Call setup() first.")
    info = await self.introspection.get_object(roots[0])
    return info.name

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
      (PrepCmd.PrepStatusRequest,),
      cast(
        dict[str, Any],
        {"command_id": cmd_id, "interface_id": iface_id, "__annotations__": {"dest": Address}},
      ),
    )
    raw: Optional[tuple] = await self.send_command(
      Cmd(dest=addr), return_raw=True, raise_on_error=False
    )
    return self._decode_firmware_string(raw)
