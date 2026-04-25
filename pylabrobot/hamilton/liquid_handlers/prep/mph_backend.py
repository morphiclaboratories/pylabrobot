"""PrepMPHBackend — concrete Head8Backend for the Hamilton Prep 8MPH head.

The 8MPH is a ganged head: a single X/Y/Z gantry and a single dispenser piston
drive all 8 probes together. Individual sleeves are mechanically coupled — partial
sleeve engagement produces insufficient grip force and tips fall off. All
operations therefore require all 8 channels simultaneously.

------------------------------
- PickupTips / DropTips: single TipPositionParameters struct; Y = probe-0 reference.
  PickupTips has tipMask (0xFF default) for Hamilton service tooling; DropTips
  has NO tip mask — all probes drop together unconditionally.
- Aspirate / Dispense: StructArray with exactly ONE entry. The gantry moves to
  the probe-0 (row A) reference position and all 8 probes operate simultaneously.
  Channel field = ChannelIndex.MPHChannel.

Physical arrangement
--------------------
Probes are ordered by Y (highest Y = probe 0 = row A). Pitch = PROBE_PITCH_MM.
"""

from __future__ import annotations

import logging
import struct as _struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Union, cast

from pylabrobot.capabilities.liquid_handling.head8_backend import Head8Backend
from pylabrobot.capabilities.liquid_handling.standard import (
  Head8AspirationContainer,
  Head8AspirationWells,
  Head8DispenseContainer,
  Head8DispenseWells,
  Head8TipDrop,
  Head8TipPickup,
)
from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.resources import Trash

from . import prep_commands as PrepCmd
from .driver import MPH_OBJECT_PATH
from .pip_backend import (
  LLDMode,
  _LldDefaults,
  _absolute_z_from_well,
  _build_container_segments,
  _effective_radius,
  default_lld_params as _default_lld_params_fn,
  lld_for_well as _lld_for_well_fn,
  lld_seek_timeout as _lld_seek_timeout,
  patch_common_with_cone as _patch_common_with_cone_fn,
  resolve_command_version as _resolve_command_version_fn,
)

if TYPE_CHECKING:
  from .driver import PrepDriver
  from .info import PrepInstrumentInfo

logger = logging.getLogger(__name__)

PROBE_PITCH_MM: float = 9.0
NUM_PROBES: int = 8
_FULL_TIP_MASK: int = 0xFF
_V2_MPH_CMD_IDS: frozenset = frozenset({29, 30, 31, 32, 33, 34})
_PROBE_POS_TOLERANCE_MM: float = 1.0  # max deviation from expected 9mm pitch before raising


@dataclass
class PrepMPHPickUpTipsParams(BackendParams):
  final_z: Optional[float] = None
  seek_speed: float = 15.0
  z_seek_offset: Optional[float] = None
  enable_tadm: bool = False
  dispenser_volume: float = 0.0
  dispenser_speed: float = 250.0


@dataclass
class PrepMPHDropTipsParams(BackendParams):
  final_z: Optional[float] = None
  seek_speed: float = 15.0
  z_seek_offset: Optional[float] = None
  tip_roll_off_distance: float = 0.0


@dataclass
class PrepMPHAspirateParams(BackendParams):
  z_final: Optional[float] = None
  z_fluid: Optional[float] = None
  z_air: Optional[float] = None
  z_minimum: Optional[float] = None
  settling_time: Optional[float] = None
  transport_air_volume: Optional[float] = None
  z_liquid_exit_speed: Optional[float] = None
  prewet_volume: Optional[float] = None
  z_bottom_search_offset: Optional[float] = None
  lld_mode: Optional[LLDMode] = None
  lld: Optional[PrepCmd.LldParameters] = None
  p_lld: Optional[PrepCmd.PLldParameters] = None
  c_lld: Optional[PrepCmd.CLldParameters] = None
  tadm: Optional[PrepCmd.TadmParameters] = None
  container_segments: Optional[List[PrepCmd.SegmentDescriptor]] = None
  auto_container_geometry: bool = False
  read_timeout: Optional[float] = None
  command_version: Optional[Literal["v1", "v2"]] = None


@dataclass
class PrepMPHDispenseParams(BackendParams):
  z_final: Optional[float] = None
  z_fluid: Optional[float] = None
  z_air: Optional[float] = None
  z_minimum: Optional[float] = None
  settling_time: Optional[float] = None
  transport_air_volume: Optional[float] = None
  z_liquid_exit_speed: Optional[float] = None
  stop_back_volume: Optional[float] = None
  cutoff_speed: Optional[float] = None
  z_bottom_search_offset: Optional[float] = None
  lld_mode: Optional[LLDMode] = None
  lld: Optional[PrepCmd.LldParameters] = None
  c_lld: Optional[PrepCmd.CLldParameters] = None
  container_segments: Optional[List[PrepCmd.SegmentDescriptor]] = None
  auto_container_geometry: bool = False
  read_timeout: Optional[float] = None
  command_version: Optional[Literal["v1", "v2"]] = None


class PrepMPHBackend(Head8Backend):
  """Concrete Head8Backend for the Hamilton Prep 8-channel Multi-Pipetting Head.

  All 8 probes must participate in every operation. Partial channel selection
  is rejected at this layer because the head is physically ganged (single drive
  per axis, single piston) and partial sleeve engagement produces insufficient
  grip force.
  """

  # Re-export LLDMode so callers can use PrepMPHBackend.LLDMode.
  LLDMode = LLDMode

  # Command dispatch tables: (effective_lld, is_tadm, use_v2) → command class
  _ASPIRATE_CMD = {
    (True,  True,  True):  PrepCmd.MphAspirateWithLldTadm2,
    (True,  True,  False): PrepCmd.MphAspirateWithLldTadm,
    (True,  False, True):  PrepCmd.MphAspirateWithLld2,
    (True,  False, False): PrepCmd.MphAspirateWithLld,
    (False, True,  True):  PrepCmd.MphAspirateTadm2,
    (False, True,  False): PrepCmd.MphAspirateTadm,
    (False, False, True):  PrepCmd.MphAspirateNoLldMonitoring2,
    (False, False, False): PrepCmd.MphAspirateNoLldMonitoring,
  }

  # Command dispatch tables: (effective_lld, use_v2) → command class
  _DISPENSE_CMD = {
    (True,  True):  PrepCmd.MphDispenseWithLld2,
    (True,  False): PrepCmd.MphDispenseWithLld,
    (False, True):  PrepCmd.MphDispenseNoLld2,
    (False, False): PrepCmd.MphDispenseNoLld,
  }

  def __init__(
    self,
    driver: "PrepDriver",
    info: "PrepInstrumentInfo",
    default_traverse_height: Optional[float] = None,
    use_v1_aspirate_dispense: bool = False,
  ) -> None:
    self._driver = driver
    self._info = info
    self._default_traverse_height = default_traverse_height
    self._use_v1_aspirate_dispense: bool = use_v1_aspirate_dispense
    self.channels: list = []  # populated by build_prep_channels after construction
    self._supports_v2_pipetting: Optional[bool] = None

  # ---------------------------------------------------------------------------
  # Setup / capability probing
  # ---------------------------------------------------------------------------

  async def _probe_v2_support(self) -> bool:
    """Return True if the MPH firmware exposes V2 aspirate/dispense (cmds 29-34)."""
    dest = await self._driver.resolve_path(MPH_OBJECT_PATH)
    methods = await self._driver.introspection.methods_for_interface(dest, interface_id=1)
    iface1_ids = {m.method_id for m in methods}
    return _V2_MPH_CMD_IDS.issubset(iface1_ids)

  async def _on_setup(self, backend_params: Optional[BackendParams] = None) -> None:
    del backend_params
    if self._use_v1_aspirate_dispense:
      self._supports_v2_pipetting = False
      logger.info("MPH V2 aspirate/dispense probe skipped (use_v1_aspirate_dispense=True)")
    else:
      try:
        supported = await self._probe_v2_support()
      except Exception:
        supported = False
      if not supported:
        raise RuntimeError(
          "V2 aspirate/dispense commands (cmd 29-34) are not supported by this MPH firmware. "
          "Pass use_v1_aspirate_dispense=True to PrepMPHBackend to use v1 commands instead."
        )
      self._supports_v2_pipetting = True
      logger.info("MPH V2 aspirate/dispense support: True")

  async def _on_stop(self) -> None:
    self._supports_v2_pipetting = None

  # ---------------------------------------------------------------------------
  # Internal helpers
  # ---------------------------------------------------------------------------

  def _resolve_command_version(self, override: Optional[Literal["v1", "v2"]] = None) -> bool:
    return _resolve_command_version_fn(
      self._supports_v2_pipetting,
      self._use_v1_aspirate_dispense,
      override,
      v2_error_hint=(
        "v2 aspirate/dispense commands (cmd 29-34) are not supported by this firmware. "
        "Use command_version='v1' or pass use_v1_aspirate_dispense=True to PrepMPHBackend."
      ),
    )

  def _resolve_traverse_height(self, final_z: Optional[float] = None) -> float:
    if final_z is not None:
      return final_z
    if self._default_traverse_height is not None:
      return self._default_traverse_height
    try:
      return float(self._info.config.default_traverse_height)
    except Exception as e:
      raise RuntimeError("No traverse height available; set default_traverse_height") from e

  def _resolve_probe_positions(self, wells) -> List[float]:
    """Compute expected probe Y positions and validate actual well Ys match.

    Probe 0 = row A = highest Y. Expected position for probe i:
      wells[0].y - i * PROBE_PITCH_MM

    Works for any labware at 9mm pitch: standard 96-well columns, or
    interleaved 384-well selections (every other row = 2 × 4.5mm = 9mm).

    Returns the expected Y values (one per probe) for logging/accounting.
    Raises ValueError if any well deviates beyond _PROBE_POS_TOLERANCE_MM.
    """
    ref_y = wells[0].get_absolute_location("c", "c", "cavity_bottom").y
    expected_ys = [ref_y - i * PROBE_PITCH_MM for i in range(len(wells))]

    mismatches = []
    for i, (well, exp_y) in enumerate(zip(wells, expected_ys)):
      actual_y = well.get_absolute_location("c", "c", "cavity_bottom").y
      if abs(actual_y - exp_y) > _PROBE_POS_TOLERANCE_MM:
        mismatches.append(
          f"  probe {i} ({well.name}): expected y={exp_y:.2f}, actual y={actual_y:.2f}"
        )

    if mismatches:
      actual_ys = [round(w.get_absolute_location("c", "c", "cavity_bottom").y, 2) for w in wells]
      raise ValueError(
        f"Wells are not at {PROBE_PITCH_MM} mm probe pitch from wells[0]. "
        f"Pass wells in row-A-first order at {PROBE_PITCH_MM} mm spacing "
        f"(for 384-well plates: every other row).\n"
        + "\n".join(mismatches)
        + f"\nActual Y values: {actual_ys}"
      )

    return expected_ys

  def _validate_container_span(self, container) -> None:
    """Raise ValueError if the container is too narrow for all 8 probes.

    Minimum Y span = (NUM_PROBES - 1) * PROBE_PITCH_MM = 63 mm.
    """
    min_span = (NUM_PROBES - 1) * PROBE_PITCH_MM
    span = container.get_size_y()
    if span < min_span:
      raise ValueError(
        f"Container '{container.name}' Y span ({span:.1f} mm) is too narrow for "
        f"{NUM_PROBES} probes at {PROBE_PITCH_MM} mm pitch "
        f"(minimum {min_span:.1f} mm required)."
      )

  def _require_all_channels(self, use_channels: List[int], op: str) -> None:
    """Raise ValueError unless use_channels is exactly [0..7].

    The 8MPH is a ganged head — all 8 probes must participate in every operation.
    Partial channel selection produces insufficient tip grip force (physical
    constraint confirmed via firmware/hardware inspection).
    """
    if list(use_channels) != list(range(NUM_PROBES)):
      raise ValueError(
        f"PrepMPHBackend.{op}: the 8MPH is a fully-ganged head — all {NUM_PROBES} "
        f"channels must participate. Received use_channels={use_channels}. "
        "Partial tip pickup/drop/aspirate/dispense is not physically supported."
      )

  def _resolve_effective_lld(
    self,
    lld_mode: Optional[LLDMode],
    lld: Optional[PrepCmd.LldParameters],
    *,
    allowed_modes: Optional[frozenset] = None,
  ) -> bool:
    """Determine whether LLD is active for this MPH pipetting call.

    Unlike the PIP backend (which takes a per-channel list), the MPH accepts a
    single LLDMode because the ganged head operates as one unit.
    """
    if lld_mode is not None:
      if lld_mode != LLDMode.OFF:
        if allowed_modes is not None and lld_mode not in allowed_modes:
          raise ValueError(
            f"Dispense does not support {lld_mode.name} LLD — only CAPACITIVE or OFF. "
            "Pressure-based LLD requires aspiration (plunger movement)."
          )
        return True
      return False
    return lld is not None

  # ---------------------------------------------------------------------------
  # Aspirate assembly helpers
  # ---------------------------------------------------------------------------

  def _assemble_aspirate_v2(
    self,
    ref_x: float,
    ref_y: float,
    volume: float,
    tube_radius: float,
    final_z: float,
    z_minimum: float,
    z_fluid: float,
    z_air: float,
    z_bottom_search_offset: float,
    settling_time: float,
    transport_air_volume: float,
    z_liquid_exit_speed: float,
    prewet_volume: float,
    blowout_volume: float,
    flow_rate: Optional[float],
    segments: List[PrepCmd.SegmentDescriptor],
    effective_lld: bool,
    is_tadm: bool,
    lld_params: PrepCmd.LldParameters,
    lld_defaults: _LldDefaults,
    tadm: PrepCmd.TadmParameters,
  ) -> Union[
    PrepCmd.AspirateParametersLldAndTadm2,
    PrepCmd.AspirateParametersLldAndMonitoring2,
    PrepCmd.AspirateParametersNoLldAndTadm2,
    PrepCmd.AspirateParametersNoLldAndMonitoring2,
  ]:
    aspirate = PrepCmd.AspirateParameters(
      default_values=False,
      x_position=ref_x,
      y_position=ref_y,
      prewet_volume=prewet_volume,
      blowout_volume=blowout_volume,
    )
    common = PrepCmd.CommonParameters.for_op(
      volume,
      tube_radius,
      flow_rate=flow_rate,
      z_final=final_z,
      z_minimum=z_minimum,
      z_liquid_exit_speed=z_liquid_exit_speed,
      transport_air_volume=transport_air_volume,
      settling_time=settling_time,
    )
    no_lld = PrepCmd.NoLldParameters.for_fixed_z(
      z_fluid=z_fluid, z_air=z_air, z_bottom_search_offset=z_bottom_search_offset
    )
    mix = PrepCmd.MixParameters.default()
    adc = PrepCmd.AdcParameters.default()

    if effective_lld and is_tadm:
      return PrepCmd.AspirateParametersLldAndTadm2(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        aspirate=aspirate,
        container_description=segments,
        common=common,
        lld=lld_params,
        p_lld=lld_defaults.p_lld,
        c_lld=lld_defaults.c_lld,
        mix=mix,
        tadm=tadm,
        adc=adc,
      )
    elif effective_lld:
      return PrepCmd.AspirateParametersLldAndMonitoring2(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        aspirate=aspirate,
        container_description=segments,
        common=common,
        lld=lld_params,
        p_lld=lld_defaults.p_lld,
        c_lld=lld_defaults.c_lld,
        mix=mix,
        aspirate_monitoring=PrepCmd.AspirateMonitoringParameters.default(),
        adc=adc,
      )
    elif is_tadm:
      return PrepCmd.AspirateParametersNoLldAndTadm2(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        aspirate=aspirate,
        container_description=segments,
        common=common,
        no_lld=no_lld,
        mix=mix,
        adc=adc,
        tadm=tadm,
      )
    else:
      return PrepCmd.AspirateParametersNoLldAndMonitoring2(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        aspirate=aspirate,
        container_description=segments,
        common=common,
        no_lld=no_lld,
        mix=mix,
        adc=adc,
        aspirate_monitoring=PrepCmd.AspirateMonitoringParameters.default(),
      )

  def _assemble_aspirate_v1(
    self,
    ref_x: float,
    ref_y: float,
    volume: float,
    tube_radius: float,
    final_z: float,
    z_minimum: float,
    z_fluid: float,
    z_air: float,
    z_bottom_search_offset: float,
    settling_time: float,
    transport_air_volume: float,
    z_liquid_exit_speed: float,
    prewet_volume: float,
    blowout_volume: float,
    flow_rate: Optional[float],
    segments: List[PrepCmd.SegmentDescriptor],
    effective_lld: bool,
    is_tadm: bool,
    lld_params: PrepCmd.LldParameters,
    lld_defaults: _LldDefaults,
    tadm: PrepCmd.TadmParameters,
  ) -> Union[
    PrepCmd.AspirateParametersLldAndTadm,
    PrepCmd.AspirateParametersLldAndMonitoring,
    PrepCmd.AspirateParametersNoLldAndTadm,
    PrepCmd.AspirateParametersNoLldAndMonitoring,
  ]:
    aspirate = PrepCmd.AspirateParameters(
      default_values=False,
      x_position=ref_x,
      y_position=ref_y,
      prewet_volume=prewet_volume,
      blowout_volume=blowout_volume,
    )
    common_v2 = PrepCmd.CommonParameters.for_op(
      volume,
      tube_radius,
      flow_rate=flow_rate,
      z_final=final_z,
      z_minimum=z_minimum,
      z_liquid_exit_speed=z_liquid_exit_speed,
      transport_air_volume=transport_air_volume,
      settling_time=settling_time,
    )
    common = _patch_common_with_cone_fn(common_v2, segments)
    no_lld = PrepCmd.NoLldParameters.for_fixed_z(
      z_fluid=z_fluid, z_air=z_air, z_bottom_search_offset=z_bottom_search_offset
    )
    mix = PrepCmd.MixParameters.default()
    adc = PrepCmd.AdcParameters.default()

    if effective_lld and is_tadm:
      return PrepCmd.AspirateParametersLldAndTadm(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        aspirate=aspirate,
        common=common,
        lld=lld_params,
        p_lld=lld_defaults.p_lld,
        c_lld=lld_defaults.c_lld,
        mix=mix,
        tadm=tadm,
        adc=adc,
      )
    elif effective_lld:
      return PrepCmd.AspirateParametersLldAndMonitoring(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        aspirate=aspirate,
        common=common,
        lld=lld_params,
        p_lld=lld_defaults.p_lld,
        c_lld=lld_defaults.c_lld,
        mix=mix,
        aspirate_monitoring=PrepCmd.AspirateMonitoringParameters.default(),
        adc=adc,
      )
    elif is_tadm:
      return PrepCmd.AspirateParametersNoLldAndTadm(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        aspirate=aspirate,
        common=common,
        no_lld=no_lld,
        mix=mix,
        adc=adc,
        tadm=tadm,
      )
    else:
      return PrepCmd.AspirateParametersNoLldAndMonitoring(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        aspirate=aspirate,
        common=common,
        no_lld=no_lld,
        mix=mix,
        adc=adc,
        aspirate_monitoring=PrepCmd.AspirateMonitoringParameters.default(),
      )

  # ---------------------------------------------------------------------------
  # Dispense assembly helpers
  # ---------------------------------------------------------------------------

  def _assemble_dispense_v2(
    self,
    ref_x: float,
    ref_y: float,
    volume: float,
    tube_radius: float,
    final_z: float,
    z_minimum: float,
    z_fluid: float,
    z_air: float,
    z_bottom_search_offset: float,
    settling_time: float,
    transport_air_volume: float,
    z_liquid_exit_speed: float,
    stop_back_volume: float,
    cutoff_speed: float,
    flow_rate: Optional[float],
    segments: List[PrepCmd.SegmentDescriptor],
    effective_lld: bool,
    lld_params: PrepCmd.LldParameters,
    lld_defaults: _LldDefaults,
  ) -> Union[PrepCmd.DispenseParametersLld2, PrepCmd.DispenseParametersNoLld2]:
    dispense = PrepCmd.DispenseParameters(
      default_values=False,
      x_position=ref_x,
      y_position=ref_y,
      stop_back_volume=stop_back_volume,
      cutoff_speed=cutoff_speed,
    )
    common = PrepCmd.CommonParameters.for_op(
      volume,
      tube_radius,
      flow_rate=flow_rate,
      z_final=final_z,
      z_minimum=z_minimum,
      z_liquid_exit_speed=z_liquid_exit_speed,
      transport_air_volume=transport_air_volume,
      settling_time=settling_time,
    )
    mix = PrepCmd.MixParameters.default()
    adc = PrepCmd.AdcParameters.default()
    tadm = PrepCmd.TadmParameters.default()

    if effective_lld:
      return PrepCmd.DispenseParametersLld2(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        dispense=dispense,
        container_description=segments,
        common=common,
        lld=lld_params,
        c_lld=lld_defaults.c_lld,
        mix=mix,
        adc=adc,
        tadm=tadm,
      )
    else:
      return PrepCmd.DispenseParametersNoLld2(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        dispense=dispense,
        container_description=segments,
        common=common,
        no_lld=PrepCmd.NoLldParameters.for_fixed_z(
          z_fluid=z_fluid, z_air=z_air, z_bottom_search_offset=z_bottom_search_offset
        ),
        mix=mix,
        adc=adc,
        tadm=tadm,
      )

  def _assemble_dispense_v1(
    self,
    ref_x: float,
    ref_y: float,
    volume: float,
    tube_radius: float,
    final_z: float,
    z_minimum: float,
    z_fluid: float,
    z_air: float,
    z_bottom_search_offset: float,
    settling_time: float,
    transport_air_volume: float,
    z_liquid_exit_speed: float,
    stop_back_volume: float,
    cutoff_speed: float,
    flow_rate: Optional[float],
    segments: List[PrepCmd.SegmentDescriptor],
    effective_lld: bool,
    lld_params: PrepCmd.LldParameters,
    lld_defaults: _LldDefaults,
  ) -> Union[PrepCmd.DispenseParametersLld, PrepCmd.DispenseParametersNoLld]:
    dispense = PrepCmd.DispenseParameters(
      default_values=False,
      x_position=ref_x,
      y_position=ref_y,
      stop_back_volume=stop_back_volume,
      cutoff_speed=cutoff_speed,
    )
    common_v2 = PrepCmd.CommonParameters.for_op(
      volume,
      tube_radius,
      flow_rate=flow_rate,
      z_final=final_z,
      z_minimum=z_minimum,
      z_liquid_exit_speed=z_liquid_exit_speed,
      transport_air_volume=transport_air_volume,
      settling_time=settling_time,
    )
    common = _patch_common_with_cone_fn(common_v2, segments)
    mix = PrepCmd.MixParameters.default()
    adc = PrepCmd.AdcParameters.default()
    tadm = PrepCmd.TadmParameters.default()

    if effective_lld:
      return PrepCmd.DispenseParametersLld(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        dispense=dispense,
        common=common,
        lld=lld_params,
        c_lld=lld_defaults.c_lld,
        mix=mix,
        adc=adc,
        tadm=tadm,
      )
    else:
      return PrepCmd.DispenseParametersNoLld(
        default_values=False,
        channel=PrepCmd.ChannelIndex.MPHChannel,
        dispense=dispense,
        common=common,
        no_lld=PrepCmd.NoLldParameters.for_fixed_z(
          z_fluid=z_fluid, z_air=z_air, z_bottom_search_offset=z_bottom_search_offset
        ),
        mix=mix,
        adc=adc,
        tadm=tadm,
      )

  # ---------------------------------------------------------------------------
  # Head8Backend interface
  # ---------------------------------------------------------------------------

  async def pick_up_tips8(
    self,
    pickup: Head8TipPickup,
    backend_params: Optional[BackendParams] = None,
  ) -> None:
    p = (
      backend_params
      if isinstance(backend_params, PrepMPHPickUpTipsParams)
      else PrepMPHPickUpTipsParams()
    )
    use_channels = list(pickup.use_channels)
    self._require_all_channels(use_channels, "pick_up_tips8")
    final_z = self._resolve_traverse_height(p.final_z)

    ref_spot = pickup.spots[0]
    rack = ref_spot.parent
    logger.info(
      "[Prep MPH] pick_up_tips: rack=%s, spots=%s",
      rack.name if rack is not None else ref_spot.name,
      [s.name.rsplit("_", 1)[-1] for s in pickup.spots],
    )
    # Use the tip from the struct — the spot tracker is already cleared by Head8 before
    # this backend method is invoked, so ref_spot.get_tip() would fail.
    tip = pickup.tips[0]
    if tip is None:
      raise RuntimeError("pick_up_tips8: first spot has no tip")
    loc = ref_spot.get_absolute_location("c", "c", "t")

    # spots[0] is always row-A (probe 0, highest Y) since all 8 channels are required.
    tip_parameters = PrepCmd.TipPositionParameters.for_op(
      PrepCmd.ChannelIndex.MPHChannel, loc, tip, z_seek_offset=p.z_seek_offset
    )
    tip_definition = PrepCmd.TipPickupParameters(
      default_values=False,
      volume=tip.maximal_volume,
      length=tip.total_tip_length - tip.fitting_depth,
      tip_type=PrepCmd.TipTypes.StandardVolume,
      has_filter=tip.has_filter,
      is_needle=False,
      is_tool=False,
    )
    await self._driver.send_command(
      PrepCmd.MphPickupTips(
        tip_parameters=tip_parameters,
        final_z=final_z,
        seek_speed=p.seek_speed,
        tip_definition=tip_definition,
        enable_tadm=p.enable_tadm,
        dispenser_volume=p.dispenser_volume,
        dispenser_speed=p.dispenser_speed,
        tip_mask=_FULL_TIP_MASK,
      )
    )

  async def drop_tips8(
    self,
    drop: Head8TipDrop,
    backend_params: Optional[BackendParams] = None,
  ) -> None:
    p = (
      backend_params
      if isinstance(backend_params, PrepMPHDropTipsParams)
      else PrepMPHDropTipsParams()
    )
    use_channels = list(drop.use_channels)
    self._require_all_channels(use_channels, "drop_tips8")
    final_z = self._resolve_traverse_height(p.final_z)

    ref_spot = drop.spots[0]
    is_trash = isinstance(ref_spot, Trash)
    dest = ref_spot if is_trash else ref_spot.parent
    logger.info(
      "[Prep MPH] drop_tips: dest=%s, spots=%s",
      dest.name if dest is not None else ref_spot.name,
      [s.name.rsplit("_", 1)[-1] for s in drop.spots],
    )
    tip = drop.tips[0]
    if tip is None:
      raise RuntimeError("drop_tips8: no tip on first channel")

    # spots[0] = probe 0 (row A, highest Y). Use "c","c","t" consistently for
    # both tip spots and trash — matches pip_backend.drop_tips and TipDropParameters.for_op.
    loc = ref_spot.get_absolute_location("c", "c", "t")
    if not is_trash:
      loc = loc + drop.offset
    drop_type = PrepCmd.TipDropType.Stall if is_trash else PrepCmd.TipDropType.FixedHeight

    drop_parameters = PrepCmd.TipDropParameters.for_op(
      PrepCmd.ChannelIndex.MPHChannel,
      loc,
      tip,
      z_seek_offset=p.z_seek_offset,
      drop_type=drop_type,
    )
    roll_off = 3.0 if (is_trash and p.tip_roll_off_distance == 0.0) else p.tip_roll_off_distance
    await self._driver.send_command(
      PrepCmd.MphDropTips(
        drop_parameters=drop_parameters,
        final_z=final_z,
        seek_speed=p.seek_speed,
        tip_roll_off_distance=roll_off,
      )
    )

  async def aspirate8(
    self,
    aspiration: Union[Head8AspirationWells, Head8AspirationContainer],
    backend_params: Optional[BackendParams] = None,
  ) -> None:
    p = (
      backend_params
      if isinstance(backend_params, PrepMPHAspirateParams)
      else PrepMPHAspirateParams()
    )
    use_channels = list(aspiration.use_channels)
    self._require_all_channels(use_channels, "aspirate8")
    tip = next((t for t in aspiration.tips if t is not None), None)
    traverse_z = self._resolve_traverse_height()
    final_z = p.z_final if p.z_final is not None else (
      traverse_z - (tip.total_tip_length - tip.fitting_depth) if tip is not None else traverse_z
    )

    if isinstance(aspiration, Head8AspirationContainer):
      container = aspiration.container
      self._validate_container_span(container)
      resource_name = container.parent.name if container.parent is not None else container.name
      op_targets = container.name
      loc = container.get_absolute_location("c", "c", "cavity_bottom")
      ref_x, ref_y = loc.x, loc.y
      wg = _absolute_z_from_well(container, aspiration.liquid_height)
      ref_segments = p.container_segments or (
        _build_container_segments(container) if p.auto_container_geometry else []
      )
      ref_resource = container
    else:
      wells = aspiration.wells
      self._resolve_probe_positions(wells)  # validates 9mm pitch; raises on mismatch
      resource_name = wells[0].parent.name if wells[0].parent is not None else wells[0].name
      op_targets = [w.name.rsplit("_", 1)[-1] for w in wells]
      ref_loc = wells[0].get_absolute_location("c", "c", "cavity_bottom")
      ref_x, ref_y = ref_loc.x, ref_loc.y
      wg = _absolute_z_from_well(wells[0], aspiration.liquid_height)
      ref_segments = p.container_segments or (
        _build_container_segments(wells[0]) if p.auto_container_geometry else []
      )
      ref_resource = wells[0]

    z_fluid = p.z_fluid if p.z_fluid is not None else wg.liquid_surface
    z_air = p.z_air if p.z_air is not None else wg.z_air
    z_minimum = p.z_minimum if p.z_minimum is not None else wg.well_bottom
    z_bottom_search_offset = p.z_bottom_search_offset if p.z_bottom_search_offset is not None else 2.0
    settling_time = p.settling_time if p.settling_time is not None else 1.0
    transport_air_volume = p.transport_air_volume if p.transport_air_volume is not None else 0.0
    z_liquid_exit_speed = p.z_liquid_exit_speed if p.z_liquid_exit_speed is not None else 10.0
    prewet_volume = p.prewet_volume if p.prewet_volume is not None else 0.0
    blowout_volume = aspiration.blow_out_air_volume or 0.0

    logger.info(
      "[Prep MPH] aspirate: resource=%s, wells=%s, volume=%.3f, flow_rate=%s",
      resource_name,
      op_targets,
      aspiration.volume,
      round(aspiration.flow_rate, 3) if aspiration.flow_rate is not None else None,
    )

    tube_radius = _effective_radius(ref_resource)
    effective_lld = self._resolve_effective_lld(p.lld_mode, p.lld)
    is_tadm = p.tadm is not None
    use_v2 = self._resolve_command_version(p.command_version)

    lld_defaults = _default_lld_params_fn(effective_lld, p.p_lld, p.c_lld)
    lld_params = _lld_for_well_fn(effective_lld, p.lld, wg.top_of_well)
    tadm = p.tadm or PrepCmd.TadmParameters.default()

    assemble = self._assemble_aspirate_v2 if use_v2 else self._assemble_aspirate_v1
    param_struct = assemble(
      ref_x=ref_x,
      ref_y=ref_y,
      volume=aspiration.volume,
      tube_radius=tube_radius,
      final_z=final_z,
      z_minimum=z_minimum,
      z_fluid=z_fluid,
      z_air=z_air,
      z_bottom_search_offset=z_bottom_search_offset,
      settling_time=settling_time,
      transport_air_volume=transport_air_volume,
      z_liquid_exit_speed=z_liquid_exit_speed,
      prewet_volume=prewet_volume,
      blowout_volume=blowout_volume,
      flow_rate=aspiration.flow_rate,
      segments=ref_segments,
      effective_lld=effective_lld,
      is_tadm=is_tadm,
      lld_params=lld_params,
      lld_defaults=lld_defaults,
      tadm=tadm,
    )

    cmd_cls = self._ASPIRATE_CMD[(effective_lld, is_tadm, use_v2)]

    read_timeout = p.read_timeout
    if read_timeout is None and effective_lld:
      read_timeout = _lld_seek_timeout(lld_params, z_minimum)

    await self._driver.send_command(
      cmd_cls(aspirate_parameters=[param_struct]),  # type: ignore[arg-type]
      read_timeout=read_timeout if effective_lld else None,
    )

  async def dispense8(
    self,
    dispense: Union[Head8DispenseWells, Head8DispenseContainer],
    backend_params: Optional[BackendParams] = None,
  ) -> None:
    p = (
      backend_params
      if isinstance(backend_params, PrepMPHDispenseParams)
      else PrepMPHDispenseParams()
    )
    use_channels = list(dispense.use_channels)
    self._require_all_channels(use_channels, "dispense8")
    tip = next((t for t in dispense.tips if t is not None), None)
    traverse_z = self._resolve_traverse_height()
    final_z = p.z_final if p.z_final is not None else (
      traverse_z - (tip.total_tip_length - tip.fitting_depth) if tip is not None else traverse_z
    )

    if isinstance(dispense, Head8DispenseContainer):
      container = dispense.container
      self._validate_container_span(container)
      resource_name = container.parent.name if container.parent is not None else container.name
      op_targets = container.name
      loc = container.get_absolute_location("c", "c", "cavity_bottom")
      ref_x, ref_y = loc.x, loc.y
      wg = _absolute_z_from_well(container, dispense.liquid_height)
      ref_segments = p.container_segments or (
        _build_container_segments(container) if p.auto_container_geometry else []
      )
      ref_resource = container
    else:
      wells = dispense.wells
      self._resolve_probe_positions(wells)  # validates 9mm pitch; raises on mismatch
      resource_name = wells[0].parent.name if wells[0].parent is not None else wells[0].name
      op_targets = [w.name.rsplit("_", 1)[-1] for w in wells]
      ref_loc = wells[0].get_absolute_location("c", "c", "cavity_bottom")
      ref_x, ref_y = ref_loc.x, ref_loc.y
      wg = _absolute_z_from_well(wells[0], dispense.liquid_height)
      ref_segments = p.container_segments or (
        _build_container_segments(wells[0]) if p.auto_container_geometry else []
      )
      ref_resource = wells[0]

    z_fluid = p.z_fluid if p.z_fluid is not None else wg.liquid_surface
    z_air = p.z_air if p.z_air is not None else wg.z_air
    z_minimum = p.z_minimum if p.z_minimum is not None else wg.well_bottom
    z_bottom_search_offset = p.z_bottom_search_offset if p.z_bottom_search_offset is not None else 2.0
    settling_time = p.settling_time if p.settling_time is not None else 0.0
    transport_air_volume = p.transport_air_volume if p.transport_air_volume is not None else 0.0
    z_liquid_exit_speed = p.z_liquid_exit_speed if p.z_liquid_exit_speed is not None else 10.0
    stop_back_volume = p.stop_back_volume if p.stop_back_volume is not None else 0.0
    cutoff_speed = p.cutoff_speed if p.cutoff_speed is not None else 100.0

    logger.info(
      "[Prep MPH] dispense: resource=%s, wells=%s, volume=%.3f, flow_rate=%s",
      resource_name,
      op_targets,
      dispense.volume,
      round(dispense.flow_rate, 3) if dispense.flow_rate is not None else None,
    )

    tube_radius = _effective_radius(ref_resource)
    _DISPENSE_ALLOWED_LLD = frozenset({LLDMode.CAPACITIVE})
    effective_lld = self._resolve_effective_lld(
      p.lld_mode, p.lld, allowed_modes=_DISPENSE_ALLOWED_LLD
    )
    use_v2 = self._resolve_command_version(p.command_version)

    lld_defaults = _default_lld_params_fn(effective_lld, c_lld=p.c_lld)
    lld_params = _lld_for_well_fn(effective_lld, p.lld, wg.top_of_well)

    assemble = self._assemble_dispense_v2 if use_v2 else self._assemble_dispense_v1
    param_struct = assemble(
      ref_x=ref_x,
      ref_y=ref_y,
      volume=dispense.volume,
      tube_radius=tube_radius,
      final_z=final_z,
      z_minimum=z_minimum,
      z_fluid=z_fluid,
      z_air=z_air,
      z_bottom_search_offset=z_bottom_search_offset,
      settling_time=settling_time,
      transport_air_volume=transport_air_volume,
      z_liquid_exit_speed=z_liquid_exit_speed,
      stop_back_volume=stop_back_volume,
      cutoff_speed=cutoff_speed,
      flow_rate=dispense.flow_rate,
      segments=ref_segments,
      effective_lld=effective_lld,
      lld_params=lld_params,
      lld_defaults=lld_defaults,
    )

    cmd_cls = self._DISPENSE_CMD[(effective_lld, use_v2)]

    read_timeout = p.read_timeout
    if read_timeout is None and effective_lld:
      read_timeout = _lld_seek_timeout(lld_params, z_minimum)

    await self._driver.send_command(
      cmd_cls(dispense_parameters=[param_struct]),  # type: ignore[arg-type]
      read_timeout=read_timeout if effective_lld else None,
    )

  # ---------------------------------------------------------------------------
  # Tip presence sensing
  # ---------------------------------------------------------------------------

  async def request_tip_presence(self) -> List[Optional[bool]]:
    """Sense whether tips are present on the 8MPH head via the sleeve sensor (cmd=15).

    The 8MPH is a single ganged controller — the firmware tree exposes one sleeve
    sensor node (on the probe-0 / channel-0 entry). The result is broadcast across
    all 8 positions since the head picks up and drops all probes together.

    Returns:
      8-element list. True=tips detected, False=no tips, None=sensor unavailable.
    """
    if not self.channels:
      raise RuntimeError("MPH channels not populated; call build_prep_channels first.")

    addr = getattr(self.channels[0], "sleeve_sensor", None)
    if addr is None:
      return [None] * NUM_PROBES

    Cmd = type(
      "_GetTipPresent",
      (PrepCmd.PrepStatusRequest,),
      cast(
        dict[str, Any],
        {"command_id": 15, "__annotations__": {"dest": type(addr)}},
      ),
    )
    raw = await self._driver.send_command(Cmd(dest=addr), return_raw=True, raise_on_error=False)
    if raw is None or len(raw[0]) < 8:
      result = False
    else:
      val = _struct.unpack_from("<I", raw[0], 4)[0]
      result = bool(val)
    return [result] * NUM_PROBES
