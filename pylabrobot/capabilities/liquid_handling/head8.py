"""Capability for 8-head (8MPH) ganged liquid handling."""

import logging
from collections import Counter
from typing import Dict, List, Optional, Union

from pylabrobot.capabilities.capability import BackendParams, Capability, need_capability_ready
from pylabrobot.resources import (
  Container,
  Coordinate,
  Tip,
  TipSpot,
  TipTracker,
  Trash,
  Well,
  does_tip_tracking,
  does_volume_tracking,
)

from .head8_backend import Head8Backend
from .standard import (
  Head8AspirationContainer,
  Head8AspirationWells,
  Head8DispenseContainer,
  Head8DispenseWells,
  Head8TipDrop,
  Head8TipPickup,
  Mix,
)

logger = logging.getLogger(__name__)

NUM_CHANNELS = 8


class Head8(Capability):
  """8-channel MPH ganged-head liquid handling: pick up tips, aspirate, dispense, drop tips.

  ``use_channels`` is always a sorted list of 0-indexed channel numbers (0 = topmost probe,
  7 = bottommost). ``spots`` / ``wells`` lists are parallel to ``use_channels``:
  ``spots[i]`` is addressed by ``use_channels[i]``.

  Use :meth:`move_to_position` for absolute gantry moves in the same deck frame as
  tip operations (probe-0 / row-A reference). On Prep, this parallels
  ``prep.pip.move_to_position`` for the dual-channel pipettor.

  See the prep user guide for usage examples.
  """

  def __init__(
    self,
    backend: Head8Backend,
    default_offset: Coordinate = Coordinate.zero(),
    deck=None,
    default_trash=None,
  ):
    super().__init__(backend=backend)
    self.backend: Head8Backend = backend
    self._tip_trackers: Dict[int, TipTracker] = {}
    self.default_offset = default_offset
    self.deck = deck
    self.default_trash = default_trash

  async def _on_setup(self, backend_params: Optional[BackendParams] = None):
    await super()._on_setup(backend_params=backend_params)
    self._tip_trackers = {ch: TipTracker(thing=f"8Head Channel {ch}") for ch in range(NUM_CHANNELS)}

  def has_tip(self, channel: int) -> bool:
    """Return True if the given channel (0-indexed) currently holds a tip."""
    return self._tip_trackers[channel].has_tip

  def get_mounted_tips(self) -> List[Optional[Tip]]:
    """Return tips for all 8 channels; None where no tip is mounted."""
    return [
      self._tip_trackers[ch].get_tip() if self._tip_trackers[ch].has_tip else None
      for ch in range(NUM_CHANNELS)
    ]

  def _resolve_use_channels(
    self,
    use_channels: Optional[List[int]],
    n: Optional[int] = None,
  ) -> List[int]:
    """Resolve use_channels, defaulting to the first n channels (or all with tips)."""
    if use_channels is not None:
      return sorted(use_channels)
    if n is not None:
      return list(range(n))
    return [ch for ch in range(NUM_CHANNELS) if self._tip_trackers[ch].has_tip]

  @need_capability_ready
  async def pick_up_tips(
    self,
    tip_spots: List[TipSpot],
    use_channels: Optional[List[int]] = None,
    offset: Coordinate = Coordinate.zero(),
    backend_params: Optional[BackendParams] = None,
  ):
    """Pick up tips from the given tip spots.

    Args:
      tip_spots: Tip spots to pick up from. ``tip_spots[i]`` is addressed by ``use_channels[i]``.
      use_channels: 0-indexed channel indices. Defaults to ``range(len(tip_spots))``.
      offset: Additional offset applied to all positions.
      backend_params: Vendor-specific parameters.
    """
    offset = self.default_offset + offset
    use_channels = self._resolve_use_channels(use_channels, n=len(tip_spots))

    if len(tip_spots) != len(use_channels):
      raise ValueError(
        f"len(tip_spots)={len(tip_spots)} must equal len(use_channels)={len(use_channels)}"
      )
    if not tip_spots:
      return

    tips: List[Optional[Tip]] = []
    for spot, ch in zip(tip_spots, use_channels):
      if not does_tip_tracking() and self._tip_trackers[ch].has_tip:
        self._tip_trackers[ch].remove_tip()
      if spot.has_tip():
        tip = spot.get_tip()
        self._tip_trackers[ch].add_tip(tip, origin=spot, commit=False)
        if does_tip_tracking() and not spot.tracker.is_disabled:
          spot.tracker.remove_tip()
        tips.append(tip)
      else:
        tips.append(None)

    pickup_op = Head8TipPickup(
      tip_spots=tip_spots,
      use_channels=tuple(use_channels),
      offset=offset,
      tips=tips,
    )
    try:
      await self.backend.pick_up_tips8(op=pickup_op, backend_params=backend_params)
    except Exception:
      for spot, ch in zip(tip_spots, use_channels):
        if does_tip_tracking() and not spot.tracker.is_disabled:
          spot.tracker.rollback()
        self._tip_trackers[ch].rollback()
      raise
    else:
      for spot, ch in zip(tip_spots, use_channels):
        if does_tip_tracking() and not spot.tracker.is_disabled:
          spot.tracker.commit()
        self._tip_trackers[ch].commit()

  @need_capability_ready
  async def move_to_position(
    self,
    x: float,
    y: float,
    z: float,
    *,
    via_lane: bool = False,
  ) -> None:
    """Move the ganged head to absolute deck coordinates (mm).

    Delegates to the backend when it implements ``move_to_position`` (e.g.
    :class:`~pylabrobot.hamilton.liquid_handlers.prep.mph_backend.PrepMPHBackend`).

    Args:
      x: Gantry X.
      y: Gantry Y at probe 0 (row A reference).
      z: Z height (e.g. traverse).
      via_lane: If True, request the firmware move-via-lane variant.
    """
    mover = getattr(self.backend, "move_to_position", None)
    if mover is None or not callable(mover):
      raise NotImplementedError(
        "This Head8 backend does not implement move_to_position "
        "(Hamilton Prep requires PrepMPHBackend)."
      )
    await mover(x, y, z, via_lane=via_lane)

  @need_capability_ready
  async def drop_tips(
    self,
    tip_spots: List[Union[TipSpot, Trash]],
    use_channels: Optional[List[int]] = None,
    allow_nonzero_volume: bool = False,
    offset: Coordinate = Coordinate.zero(),
    backend_params: Optional[BackendParams] = None,
  ):
    """Drop tips to the given tip spots or trash.

    Args:
      tip_spots: Destinations parallel to ``use_channels``.
      use_channels: 0-indexed channel indices. Defaults to all channels with tips.
      allow_nonzero_volume: If True, drop even if tips carry liquid.
      offset: Additional offset applied to all positions.
      backend_params: Vendor-specific parameters.
    """
    offset = self.default_offset + offset
    use_channels = self._resolve_use_channels(use_channels)

    if len(tip_spots) != len(use_channels):
      raise ValueError(
        f"len(tip_spots)={len(tip_spots)} must equal len(use_channels)={len(use_channels)}"
      )
    if not use_channels:
      return

    dropped_tips: List[Optional[Tip]] = []
    for spot, ch in zip(tip_spots, use_channels):
      if not self._tip_trackers[ch].has_tip:
        dropped_tips.append(None)
        continue
      tip = self._tip_trackers[ch].get_tip()
      if tip.tracker.get_used_volume() > 0 and not allow_nonzero_volume and does_volume_tracking():
        raise RuntimeError(
          f"Cannot drop tip with volume {tip.tracker.get_used_volume()} on channel {ch}"
        )
      if isinstance(spot, TipSpot) and does_tip_tracking() and not spot.tracker.is_disabled:
        spot.tracker.add_tip(tip, commit=False)
      self._tip_trackers[ch].remove_tip()
      dropped_tips.append(tip)

    drop_op = Head8TipDrop(
      resources=tip_spots,
      use_channels=tuple(use_channels),
      offset=offset,
      tips=dropped_tips,
    )
    try:
      await self.backend.drop_tips8(op=drop_op, backend_params=backend_params)
    except Exception:
      for spot, ch in zip(tip_spots, use_channels):
        if isinstance(spot, TipSpot) and does_tip_tracking() and not spot.tracker.is_disabled:
          spot.tracker.rollback()
        self._tip_trackers[ch].rollback()
      raise
    else:
      for spot, ch in zip(tip_spots, use_channels):
        if isinstance(spot, TipSpot) and does_tip_tracking() and not spot.tracker.is_disabled:
          spot.tracker.commit()
        self._tip_trackers[ch].commit()

  @need_capability_ready
  async def return_tips(
    self,
    use_channels: Optional[List[int]] = None,
    allow_nonzero_volume: bool = False,
    offset: Coordinate = Coordinate.zero(),
    drop_backend_params: Optional[BackendParams] = None,
  ):
    """Return tips to the spots they were picked up from.

    Args:
      use_channels: Channels to return tips for. Defaults to all channels with tips.
      allow_nonzero_volume: If True, return even if tips carry liquid.
      offset: Additional offset.
      drop_backend_params: Vendor-specific parameters for the drop.
    """
    use_channels = self._resolve_use_channels(use_channels)
    tip_spots: List[Union[TipSpot, Trash]] = []
    active: List[int] = []
    for ch in use_channels:
      if not self._tip_trackers[ch].has_tip:
        continue
      origin = self._tip_trackers[ch].get_tip_origin()
      if origin is None:
        raise RuntimeError(f"Channel {ch} has no tip origin — cannot return tip")
      tip_spots.append(origin)
      active.append(ch)

    if not active:
      return
    await self.drop_tips(
      tip_spots=tip_spots,
      use_channels=active,
      allow_nonzero_volume=allow_nonzero_volume,
      offset=offset,
      backend_params=drop_backend_params,
    )

  @need_capability_ready
  async def discard_tips(
    self,
    trash: Optional[Trash] = None,
    use_channels: Optional[List[int]] = None,
    allow_nonzero_volume: bool = True,
    offset: Coordinate = Coordinate.zero(),
    drop_backend_params: Optional[BackendParams] = None,
  ):
    """Discard tips into the trash.

    Args:
      trash: Trash resource. If None, looks up 8MPH trash on the deck.
      use_channels: Channels to discard tips from. Defaults to all channels with tips.
      allow_nonzero_volume: If True, discard even if tips carry liquid.
      offset: Additional offset applied to the drop position.
      drop_backend_params: Vendor-specific parameters for the drop.
    """
    if trash is None:
      if self.default_trash is not None:
        trash = self.default_trash
      elif self.deck is not None:
        trash = self.deck.get_trash_area96()
      else:
        raise ValueError("No trash provided and no deck or default_trash set on Head8. Pass trash explicitly.")

    use_channels = self._resolve_use_channels(use_channels)
    active = [ch for ch in use_channels if self._tip_trackers[ch].has_tip]
    if not active:
      return
    await self.drop_tips(
      tip_spots=[trash] * len(active),
      use_channels=active,
      allow_nonzero_volume=allow_nonzero_volume,
      offset=offset,
      backend_params=drop_backend_params,
    )

  @need_capability_ready
  async def aspirate(
    self,
    wells: Union[List[Well], Container],
    volume: float,
    use_channels: Optional[List[int]] = None,
    offset: Coordinate = Coordinate.zero(),
    flow_rate: Optional[float] = None,
    liquid_height: Optional[float] = None,
    blow_out_air_volume: Optional[float] = None,
    mix: Optional[Mix] = None,
    backend_params: Optional[BackendParams] = None,
  ):
    """Aspirate from wells or a single container using the 8MPH head.

    Args:
      wells: A list of wells (one per active channel) or a single Container (trough).
      volume: Volume per channel in µL.
      use_channels: 0-indexed active channel indices. For a list of wells, defaults to
        ``range(len(wells))``. For a container, defaults to all channels with tips.
      offset: Additional offset.
      flow_rate: Flow rate in µL/s. None = machine default.
      liquid_height: Liquid height in mm from bottom. None = machine default.
      blow_out_air_volume: Air volume to aspirate after liquid (µL).
      mix: Mix parameters.
      backend_params: Vendor-specific parameters.
    """
    offset = self.default_offset + offset
    volume = float(volume)
    flow_rate = float(flow_rate) if flow_rate is not None else None
    blow_out_air_volume = float(blow_out_air_volume) if blow_out_air_volume is not None else None

    tips = [
      self._tip_trackers[ch].get_tip() if self._tip_trackers[ch].has_tip else None
      for ch in range(NUM_CHANNELS)
    ]

    if isinstance(wells, Container):
      use_channels = self._resolve_use_channels(use_channels)
      active_tips = [tips[ch] for ch in use_channels]
      container = wells

      for tip in active_tips:
        if tip is None:
          continue
        if not container.tracker.is_disabled and does_volume_tracking():
          container.tracker.remove_liquid(volume=volume)
        tip.tracker.add_liquid(volume=volume)

      aspiration = Head8AspirationContainer(
        container=container,
        use_channels=tuple(use_channels),
        offset=offset,
        tips=active_tips,
        volume=volume,
        flow_rate=flow_rate,
        liquid_height=liquid_height,
        blow_out_air_volume=blow_out_air_volume,
        mix=mix,
      )
      all_containers = [container]
    else:
      use_channels = self._resolve_use_channels(use_channels, n=len(wells))
      if len(wells) != len(use_channels):
        raise ValueError(
          f"len(wells)={len(wells)} must equal len(use_channels)={len(use_channels)}"
        )
      if not wells:
        return

      active_tips = [tips[ch] for ch in use_channels]

      # per-well total volume (duplicate well entries → multiple probes in same well)
      well_vol: Counter = Counter()
      for w in wells:
        well_vol[id(w)] += volume

      seen_wells: dict = {}
      for w, tip in zip(wells, active_tips):
        wid = id(w)
        if wid not in seen_wells:
          seen_wells[wid] = w
          if not w.tracker.is_disabled and does_volume_tracking():
            w.tracker.remove_liquid(volume=well_vol[wid])
        if tip is not None:
          tip.tracker.add_liquid(volume=volume)

      aspiration = Head8AspirationWells(  # type: ignore[assignment]
        wells=wells,
        use_channels=tuple(use_channels),
        offset=offset,
        tips=active_tips,
        volume=volume,
        flow_rate=flow_rate,
        liquid_height=liquid_height,
        blow_out_air_volume=blow_out_air_volume,
        mix=mix,
      )
      all_containers = list(seen_wells.values())

    try:
      await self.backend.aspirate8(op=aspiration, backend_params=backend_params)
    except Exception:
      for tip in (tips[ch] for ch in use_channels):
        if tip is not None:
          tip.tracker.rollback()
      for c in all_containers:
        if does_volume_tracking() and not c.tracker.is_disabled:
          c.tracker.rollback()
      raise
    else:
      for tip in (tips[ch] for ch in use_channels):
        if tip is not None:
          tip.tracker.commit()
      for c in all_containers:
        if does_volume_tracking() and not c.tracker.is_disabled:
          c.tracker.commit()

  @need_capability_ready
  async def dispense(
    self,
    wells: Union[List[Well], Container],
    volume: float,
    use_channels: Optional[List[int]] = None,
    offset: Coordinate = Coordinate.zero(),
    flow_rate: Optional[float] = None,
    liquid_height: Optional[float] = None,
    blow_out_air_volume: Optional[float] = None,
    mix: Optional[Mix] = None,
    backend_params: Optional[BackendParams] = None,
  ):
    """Dispense to wells or a single container using the 8MPH head.

    Args:
      wells: A list of wells (one per active channel) or a single Container (trough).
      volume: Volume per channel in µL.
      use_channels: 0-indexed active channel indices. For a list of wells, defaults to
        ``range(len(wells))``. For a container, defaults to all channels with tips.
      offset: Additional offset.
      flow_rate: Flow rate in µL/s. None = machine default.
      liquid_height: Liquid height in mm from bottom. None = machine default.
      blow_out_air_volume: Air volume to dispense after liquid (µL).
      mix: Mix parameters.
      backend_params: Vendor-specific parameters.
    """
    offset = self.default_offset + offset
    volume = float(volume)
    flow_rate = float(flow_rate) if flow_rate is not None else None
    blow_out_air_volume = float(blow_out_air_volume) if blow_out_air_volume is not None else None

    tips = [
      self._tip_trackers[ch].get_tip() if self._tip_trackers[ch].has_tip else None
      for ch in range(NUM_CHANNELS)
    ]

    if isinstance(wells, Container):
      use_channels = self._resolve_use_channels(use_channels)
      active_tips = [tips[ch] for ch in use_channels]
      container = wells

      for tip in active_tips:
        if tip is None:
          continue
        if does_volume_tracking():
          tip.tracker.remove_liquid(volume=volume)
        elif tip.tracker.get_used_volume() <= volume:
          tip.tracker.remove_liquid(volume=min(tip.tracker.get_used_volume(), volume))
      if not container.tracker.is_disabled and does_volume_tracking():
        container.tracker.add_liquid(
          volume=len([t for t in active_tips if t is not None]) * volume
        )

      dispense_op = Head8DispenseContainer(
        container=container,
        use_channels=tuple(use_channels),
        offset=offset,
        tips=active_tips,
        volume=volume,
        flow_rate=flow_rate,
        liquid_height=liquid_height,
        blow_out_air_volume=blow_out_air_volume,
        mix=mix,
      )
      all_containers = [container]
    else:
      use_channels = self._resolve_use_channels(use_channels, n=len(wells))
      if len(wells) != len(use_channels):
        raise ValueError(
          f"len(wells)={len(wells)} must equal len(use_channels)={len(use_channels)}"
        )
      if not wells:
        return

      active_tips = [tips[ch] for ch in use_channels]

      well_vol: Counter = Counter()
      for w in wells:
        well_vol[id(w)] += volume

      for tip in active_tips:
        if tip is None:
          continue
        if does_volume_tracking():
          tip.tracker.remove_liquid(volume=volume)
        elif tip.tracker.get_used_volume() <= volume:
          tip.tracker.remove_liquid(volume=min(tip.tracker.get_used_volume(), volume))

      seen_wells: dict = {}
      for w in wells:
        wid = id(w)
        if wid not in seen_wells:
          seen_wells[wid] = w
          if not w.tracker.is_disabled and does_volume_tracking():
            w.tracker.add_liquid(volume=well_vol[wid])

      dispense_op = Head8DispenseWells(  # type: ignore[assignment]
        wells=wells,
        use_channels=tuple(use_channels),
        offset=offset,
        tips=active_tips,
        volume=volume,
        flow_rate=flow_rate,
        liquid_height=liquid_height,
        blow_out_air_volume=blow_out_air_volume,
        mix=mix,
      )
      all_containers = list(seen_wells.values())

    try:
      await self.backend.dispense8(op=dispense_op, backend_params=backend_params)
    except Exception:
      for tip in (tips[ch] for ch in use_channels):
        if tip is not None:
          tip.tracker.rollback()
      for c in all_containers:
        if does_volume_tracking() and not c.tracker.is_disabled:
          c.tracker.rollback()
      raise
    else:
      for tip in (tips[ch] for ch in use_channels):
        if tip is not None:
          tip.tracker.commit()
      for c in all_containers:
        if does_volume_tracking() and not c.tracker.is_disabled:
          c.tracker.commit()
