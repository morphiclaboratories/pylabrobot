"""PrepChatterboxDriver: minimal driver for tests without TCP hardware."""

from __future__ import annotations

import logging
from typing import Any, Optional

from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.hamilton.tcp.commands import TCPCommand
from pylabrobot.hamilton.tcp.introspection import ObjectInfo
from pylabrobot.hamilton.tcp.packets import Address

from . import prep_commands as PrepCmd
from .driver import _PREP_LAZY_RESOLVE_PATHS, PrepDriver, PrepResolvedInterfaces, PrepSetupParams
from .pip_backend import PrepPIPBackend

logger = logging.getLogger(__name__)


class PrepChatterboxPIPBackend(PrepPIPBackend):
  """Offline pip backend: :meth:`PrepChatterboxDriver.setup` seeds canned state; skip real init."""

  async def _on_setup(self, backend_params: Optional[BackendParams] = None) -> None:
    del backend_params


class PrepChatterboxDriver(PrepDriver):
  """Skips TCP; uses canned addresses so PrepPIPBackend can be exercised offline."""

  def __init__(self, num_channels: int = 2):
    super().__init__(host="chatterbox", port=2000)
    self._num_channels = num_channels

  async def setup(self, backend_params: Optional[BackendParams] = None):
    if backend_params is None:
      params = PrepSetupParams()
    elif isinstance(backend_params, PrepSetupParams):
      params = backend_params
    else:
      raise TypeError(
        "PrepChatterboxDriver.setup expected PrepSetupParams | None for backend_params, "
        f"got {type(backend_params).__name__}"
      )

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
    self._prep_resolved = PrepResolvedInterfaces.from_resolution_map(self._resolved_interfaces)

    # _PREP_LAZY_RESOLVE_PATHS: seed registry so resolve_path / _lazy_diag_address work offline.
    _chatterbox_lazy_diag = {
      "mlprep_cpu": (Address(1, 1, 270), "MLPrepCpu"),
      "module_information": (Address(1, 1, 271), "ModuleInformation"),
    }
    for key, (addr, oname) in _chatterbox_lazy_diag.items():
      self.registry.register(
        _PREP_LAZY_RESOLVE_PATHS[key],
        ObjectInfo(name=oname, version="", method_count=0, subobject_count=0, address=addr),
      )

    self.pip = PrepChatterboxPIPBackend(
      driver=self,
      deck=params.deck,
      default_traverse_height=params.default_traverse_height or 180.0,
      use_v1_aspirate_dispense=True,
    )
    th = params.default_traverse_height or 180.0
    self.pip._config = PrepCmd.InstrumentConfig(
      deck_bounds=None,
      has_enclosure=False,
      safe_speeds_enabled=True,
      deck_sites=(),
      waste_sites=(),
      default_traverse_height=th,
      num_channels=self._num_channels,
      has_mph=False,
    )
    self.pip._num_channels = self._num_channels
    self.pip._has_mph = False
    self.pip._supports_v2_pipetting = False
    self.pip.setup_finished = True

  async def stop(self):
    self._resolved_interfaces.clear()
    self._prep_resolved = None

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
