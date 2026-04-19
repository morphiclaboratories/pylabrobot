"""PrepChatterboxDriver: minimal driver for tests without TCP hardware."""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Union

from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.hamilton.tcp.commands import TCPCommand
from pylabrobot.hamilton.tcp.introspection import HamiltonIntrospection, MethodInfo, ObjectInfo
from pylabrobot.hamilton.tcp.packets import Address

from . import prep_commands as PrepCmd
from .driver import PREP_LAZY_RESOLVE_PATHS, PrepDriver, PrepResolvedInterfaces, PrepSetupParams
from .info import PrepInstrumentInfo

logger = logging.getLogger(__name__)

# PrepPIPBackend._probe_v2_support expects pipettor interface 1 to expose these method IDs.
_V2_PIPETTING_METHOD_IDS = frozenset(range(38, 44))


class _PrepChatterboxIntrospection(HamiltonIntrospection):
  """Offline introspection: v2 probe succeeds when ``use_v1_aspirate_dispense`` is False."""

  async def methods_for_interface(
    self, address: Union[Address, str], interface_id: int
  ) -> List[MethodInfo]:
    client = self.backend
    if not isinstance(client, PrepChatterboxDriver):
      return await super().methods_for_interface(address, interface_id)
    addr = await self._resolve_target_address(address)
    if (
      client._pipettor_addr is not None
      and addr == client._pipettor_addr
      and interface_id == 1
      and not client._use_v1_aspirate_dispense
    ):
      return [
        MethodInfo(interface_id=1, call_type=0, method_id=mid, name=f"v2_stub_{mid}")
        for mid in sorted(_V2_PIPETTING_METHOD_IDS)
      ]
    return await super().methods_for_interface(address, interface_id)


class PrepChatterboxInstrumentInfo(PrepInstrumentInfo):
  """Offline info: uses canned :class:`~prep_commands.InstrumentConfig` from the chatterbox driver."""

  async def _on_setup(self) -> None:
    d = self._driver
    assert isinstance(d, PrepChatterboxDriver)
    self._config = d._canned_config


class PrepChatterboxDriver(PrepDriver):
  """Skips TCP; uses canned addresses so PrepPIPBackend can be exercised offline.

  Canned firmware state (num_channels, has_mph, traverse height) lives on the
  chatterbox driver — :class:`PrepChatterboxInstrumentInfo` reads it for ``info.config``.

  Default :class:`~driver.PrepSetupParams` matches hardware (``use_v1_aspirate_dispense=False``):
  introspection stubs report v2 aspirate/dispense commands on the pipettor. Pass
  ``use_v1_aspirate_dispense=True`` for a thinner v1-only offline path.
  """

  def __init__(
    self,
    num_channels: int = 2,
    has_mph: bool = False,
    default_traverse_height: float = 180.0,
  ):
    super().__init__(host="chatterbox", port=2000)
    self._canned_config = PrepCmd.InstrumentConfig(
      deck_bounds=None,
      has_enclosure=False,
      safe_speeds_enabled=True,
      deck_sites=(),
      waste_sites=(),
      default_traverse_height=default_traverse_height,
      num_channels=num_channels,
      has_mph=has_mph,
    )
    self._pipettor_addr: Optional[Address] = None
    self._use_v1_aspirate_dispense: bool = False

  @property
  def introspection(self) -> HamiltonIntrospection:
    if self._introspection_impl is None:
      self._introspection_impl = _PrepChatterboxIntrospection(self)
    return self._introspection_impl

  async def setup(self, backend_params: Optional[BackendParams] = None):
    if backend_params is not None and not isinstance(backend_params, PrepSetupParams):
      raise TypeError(
        "PrepChatterboxDriver.setup expected PrepSetupParams | None for backend_params, "
        f"got {type(backend_params).__name__}"
      )

    params = backend_params if isinstance(backend_params, PrepSetupParams) else PrepSetupParams()
    self._use_v1_aspirate_dispense = params.use_v1_aspirate_dispense

    # Canned addresses — must match path resolution order used in real setup.
    self._resolved_interfaces = {
      "mlprep": Address(1, 1, 256),
      "pipettor": Address(1, 1, 257),
      "coordinator": Address(1, 1, 258),
      "calibration": Address(1, 1, 259),
      "deck_config": Address(1, 1, 260),
      "mph": Address(1, 1, 261),
      "mlprep_service": Address(1, 1, 262),
    }
    self._pipettor_addr = self._resolved_interfaces["pipettor"]
    self._prep_resolved = PrepResolvedInterfaces.from_resolution_map(self._resolved_interfaces)

    # PREP_LAZY_RESOLVE_PATHS: seed registry so resolve_path / _lazy_diag_address work offline.
    _chatterbox_lazy_diag = {
      "mlprep_cpu": (Address(1, 1, 270), "MLPrepCpu"),
      "module_information": (Address(1, 1, 271), "ModuleInformation"),
    }
    for key, (addr, oname) in _chatterbox_lazy_diag.items():
      self.registry.register(
        PREP_LAZY_RESOLVE_PATHS[key],
        ObjectInfo(name=oname, version="", method_count=0, subobject_count=0, address=addr),
      )

  async def stop(self):
    self._resolved_interfaces.clear()
    self._prep_resolved = None
    self._lazy_diag_cache.clear()
    self._pipettor_addr = None
    self._invalidate_introspection_session()

  async def send_command(
    self,
    command: TCPCommand,
    ensure_connection: bool = True,
    return_raw: bool = False,
    raise_on_error: bool = True,
    read_timeout: Optional[float] = None,
  ) -> Any:
    del ensure_connection, raise_on_error, read_timeout
    logger.info("[Prep chatterbox] %s", command.__class__.__name__)
    if return_raw:
      return (b"",)
    return None
