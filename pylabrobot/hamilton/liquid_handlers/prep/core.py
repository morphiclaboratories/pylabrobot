"""Hamilton Prep CoRe gripper and plate manipulation (PrepCmd via :class:`PrepDriver`)."""

from __future__ import annotations

from typing import Optional

from pylabrobot.hamilton.tcp.packets import Address
from pylabrobot.legacy.liquid_handling.standard import (
  GripDirection,
  ResourceDrop,
  ResourceMove,
  ResourcePickup,
)
from pylabrobot.resources import Coordinate
from pylabrobot.resources.deck import Deck
from pylabrobot.resources.hamilton.hamilton_decks import HamiltonCoreGrippers

from . import prep_commands as PrepCmd
from .driver import PrepDriver
from .pip_backend import PrepPIPBackend


class PrepCoreGripper:
  """CoRe tool pickup, plate grip, and moves — uses pipettor interface + optional safe-Z via PIP backend."""

  def __init__(self, driver: PrepDriver, deck: Deck, pip: PrepPIPBackend) -> None:
    self._driver = driver
    self.deck = deck
    self._pip = pip
    self._gripper_tool_on: bool = False

  @property
  def client(self) -> PrepDriver:
    return self._driver

  async def _require(self, name: str) -> Address:
    return await self._driver.require_interface(name)

  async def _on_stop(self) -> None:
    self._gripper_tool_on = False

  async def pick_up_tool(
    self,
    tool_position_x: float,
    tool_position_z: float,
    front_channel_position_y: float,
    rear_channel_position_y: float,
    *,
    tool_seek: Optional[float] = None,
    tool_x_radius: float = 2.0,
    tool_y_radius: float = 2.0,
    tip_definition: Optional[PrepCmd.TipPickupParameters] = None,
  ) -> None:
    """Pick up tool (PrepCmd.PrepPickUpTool, cmd=15). Moves channels to safe Z after."""
    if tool_seek is None:
      tool_seek = tool_position_z + 10.0
    if tip_definition is None:
      tip_definition = PrepCmd.CO_RE_GRIPPER_TIP_PICKUP_PARAMETERS
    await self._driver.send_command(
      PrepCmd.PrepPickUpTool(
        dest=await self._require("pipettor"),
        tip_definition=tip_definition,
        tool_position_x=tool_position_x,
        tool_position_z=tool_position_z,
        front_channel_position_y=front_channel_position_y,
        rear_channel_position_y=rear_channel_position_y,
        tool_seek=tool_seek,
        tool_x_radius=tool_x_radius,
        tool_y_radius=tool_y_radius,
      )
    )
    self._gripper_tool_on = True
    await self._pip.move_channels_to_safe_z()

  async def drop_tool(self, *, move_to_safe_z_first: bool = True) -> None:
    """Drop tool (PrepCmd.PrepDropTool, cmd=16)."""
    if move_to_safe_z_first:
      await self._pip.move_channels_to_safe_z()
    await self._driver.send_command(PrepCmd.PrepDropTool(dest=await self._require("pipettor")))
    self._gripper_tool_on = False

  async def release_plate(self) -> None:
    """Release plate / open gripper (PrepCmd.PrepReleasePlate, cmd=21)."""
    await self._driver.send_command(PrepCmd.PrepReleasePlate(dest=await self._require("pipettor")))

  async def pick_up_resource(
    self,
    pickup: ResourcePickup,
    *,
    clearance_y: float = 2.5,
    grip_speed_y: float = 5.0,
    squeeze_mm: float = 2.0,
  ) -> None:
    if pickup.direction != GripDirection.FRONT:
      raise NotImplementedError("PREP CORE gripper only supports GripDirection.FRONT")
    resource = pickup.resource
    center = resource.get_location_wrt(self.deck, "c", "c", "t") + pickup.offset
    grip_height = center.z - pickup.pickup_distance_from_top
    plate_top_center = PrepCmd.XYZCoord(
      default_values=False,
      x_position=center.x,
      y_position=center.y,
      z_position=center.z,
    )
    grip_distance = clearance_y + squeeze_mm
    plate_dims = PrepCmd.PlateDimensions(
      default_values=False,
      length=resource.get_absolute_size_x(),
      width=resource.get_absolute_size_y(),
      height=resource.get_absolute_size_z(),
    )
    if not self._gripper_tool_on:
      mount = self.deck.get_resource("core_grippers")
      if not isinstance(mount, HamiltonCoreGrippers):
        raise TypeError(
          "deck must have a resource named 'core_grippers' of type HamiltonCoreGrippers"
        )
      loc = mount.get_location_wrt(self.deck)
      await self.pick_up_tool(
        tool_position_x=loc.x,
        tool_position_z=loc.z,
        front_channel_position_y=loc.y + mount.front_channel_y_center,
        rear_channel_position_y=loc.y + mount.back_channel_y_center,
        tool_seek=loc.z + 10.0,
      )
    await self._driver.send_command(
      PrepCmd.PrepPickUpPlate(
        dest=await self._require("pipettor"),
        plate_top_center=plate_top_center,
        plate=plate_dims,
        clearance_y=clearance_y,
        grip_speed_y=grip_speed_y,
        grip_distance=grip_distance,
        grip_height=grip_height,
      )
    )

  async def move_picked_up_resource(self, move: ResourceMove) -> None:
    center = (
      move.location
      + move.resource.get_anchor("c", "c", "t")
      - Coordinate(z=move.pickup_distance_from_top)
      + move.offset
    )
    plate_top_center = PrepCmd.XYZCoord(
      default_values=False,
      x_position=center.x,
      y_position=center.y,
      z_position=center.z,
    )
    await self._driver.send_command(
      PrepCmd.PrepMovePlate(
        dest=await self._require("pipettor"),
        plate_top_center=plate_top_center,
        acceleration_scale_x=1,
      )
    )

  async def drop_resource(
    self,
    drop: ResourceDrop,
    *,
    return_gripper: bool = True,
    clearance_y: float = 3.0,
  ) -> None:
    resource = drop.resource
    dest_center = drop.destination + resource.get_anchor("c", "c", "t") + drop.offset
    place_z = drop.destination.z + resource.get_absolute_size_z() - drop.pickup_distance_from_top
    plate_top_center = PrepCmd.XYZCoord(
      default_values=False,
      x_position=dest_center.x,
      y_position=dest_center.y,
      z_position=place_z,
    )
    await self._driver.send_command(
      PrepCmd.PrepDropPlate(
        dest=await self._require("pipettor"),
        plate_top_center=plate_top_center,
        clearance_y=clearance_y,
        acceleration_scale_x=1,
      )
    )
    if return_gripper:
      await self.drop_tool()
