"""Hamilton Prep CoRe gripper backend (v1 GripperArmBackend) and PrepGripperArm frontend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from pylabrobot.capabilities.arms.arm import FixedAxisGripperArm
from pylabrobot.capabilities.arms.backend import GripperArmBackend
from pylabrobot.capabilities.arms.standard import CartesianPose
from pylabrobot.capabilities.capability import BackendParams
from pylabrobot.resources import Coordinate, Resource

from . import prep_commands as PrepCmd

if TYPE_CHECKING:
  from .driver import PrepDriver
  from .pip_backend import PrepPIPBackend


class PrepCoreGripperFactory:
  """Lightweight factory: ``Prep`` constructs one at setup and calls
  :meth:`build_backend` when tools are picked up."""

  def __init__(self, driver: "PrepDriver") -> None:
    self._driver = driver

  def build_backend(self, pip: "PrepPIPBackend") -> "PrepCoreGripper":
    return PrepCoreGripper(driver=self._driver, pip=pip)


class PrepCoreGripper(GripperArmBackend):
  """CoRe gripper backend for Prep — translates v1 arm interface to PrepCmd firmware commands.

  Tool management (pick_up_tool / drop_tool) is **not** part of the GripperArmBackend
  interface — it is handled by the :meth:`Prep.core_grippers` context manager.
  """

  def __init__(self, *, driver: "PrepDriver", pip: "PrepPIPBackend") -> None:
    self._driver = driver
    self._pip = pip

  @property
  def client(self):
    return self._driver

  # -- BackendParams for firmware-specific tuning --------------------------------
  # Geometry fields (resource_length, resource_height, plate_top_z_offset) have SBS
  # defaults for direct pick_up_at_location() calls.  When called via
  # PrepGripperArm.pick_up_resource(), these are auto-filled from the actual resource.

  @dataclass
  class PickUpParams(BackendParams):
    """Firmware parameters for plate pickup.

    Geometry fields are auto-populated by :class:`PrepGripperArm` when using
    ``pick_up_resource()``.  Only tuning knobs (``clearance_y``, ``grip_speed_y``,
    ``squeeze_mm``) normally need to be set by callers.
    """

    resource_length: float = 127.0
    resource_height: float = 14.0
    plate_top_z_offset: float = 0.0
    clearance_y: float = 2.5
    grip_speed_y: float = 5.0
    squeeze_mm: float = 2.0

  @dataclass
  class DropParams(BackendParams):
    """Firmware parameters for plate drop."""

    clearance_y: float = 3.0
    acceleration_scale_x: int = 1

  @dataclass
  class MoveToLocationParams(BackendParams):
    """Firmware parameters for moving a held plate."""

    acceleration_scale_x: int = 1

  # -- GripperArmBackend interface -----------------------------------------------

  async def pick_up_at_location(
    self,
    location: Coordinate,
    resource_width: float,
    backend_params: Optional[BackendParams] = None,
  ) -> None:
    """Pick up a plate at the specified location.

    Args:
      location: Plate center at grip height (x, y, grip_z) in deck coordinates.
      resource_width: Plate width along the grip axis (Y) in mm.
      backend_params: :class:`PickUpParams` for firmware-specific settings.
    """
    if not isinstance(backend_params, PrepCoreGripper.PickUpParams):
      backend_params = PrepCoreGripper.PickUpParams()

    plate_top_center = PrepCmd.XYZCoord(
      default_values=False,
      x_position=location.x,
      y_position=location.y,
      z_position=location.z + backend_params.plate_top_z_offset,
    )
    plate_dims = PrepCmd.PlateDimensions(
      default_values=False,
      length=backend_params.resource_length,
      width=resource_width,
      height=backend_params.resource_height,
    )
    grip_distance = backend_params.clearance_y + backend_params.squeeze_mm

    await self._driver.send_command(
      PrepCmd.PrepPickUpPlate(
        plate_top_center=plate_top_center,
        plate=plate_dims,
        clearance_y=backend_params.clearance_y,
        grip_speed_y=backend_params.grip_speed_y,
        grip_distance=grip_distance,
        grip_height=location.z,
      )
    )

  async def drop_at_location(
    self,
    location: Coordinate,
    resource_width: float,
    backend_params: Optional[BackendParams] = None,
  ) -> None:
    """Drop a plate at the specified location.

    Args:
      location: Plate center at place height in deck coordinates.
      resource_width: Plate width along the grip axis (Y) in mm.
      backend_params: :class:`DropParams` for firmware-specific settings.
    """
    if not isinstance(backend_params, PrepCoreGripper.DropParams):
      backend_params = PrepCoreGripper.DropParams()

    plate_top_center = PrepCmd.XYZCoord(
      default_values=False,
      x_position=location.x,
      y_position=location.y,
      z_position=location.z,
    )
    await self._driver.send_command(
      PrepCmd.PrepDropPlate(
        plate_top_center=plate_top_center,
        clearance_y=backend_params.clearance_y,
        acceleration_scale_x=backend_params.acceleration_scale_x,
      )
    )

  async def move_to_location(
    self,
    location: Coordinate,
    backend_params: Optional[BackendParams] = None,
  ) -> None:
    """Move a held plate to a new position without releasing it.

    Args:
      location: Target plate center position in deck coordinates.
      backend_params: :class:`MoveToLocationParams` for firmware-specific settings.
    """
    if not isinstance(backend_params, PrepCoreGripper.MoveToLocationParams):
      backend_params = PrepCoreGripper.MoveToLocationParams()

    plate_top_center = PrepCmd.XYZCoord(
      default_values=False,
      x_position=location.x,
      y_position=location.y,
      z_position=location.z,
    )
    await self._driver.send_command(
      PrepCmd.PrepMovePlate(
        plate_top_center=plate_top_center,
        acceleration_scale_x=backend_params.acceleration_scale_x,
      )
    )

  min_gripper_width: float = 9.0
  max_gripper_width: Optional[float] = None

  async def move_gripper(
    self,
    width: float,
    force_sensing: bool = False,
    backend_params: Optional[BackendParams] = None,
  ) -> None:
    """Release plate / open gripper (PrepReleasePlate, cmd=21)."""
    if force_sensing:
      raise NotImplementedError("Use pick_up_at_location instead.")
    await self._driver.send_command(PrepCmd.PrepReleasePlate())

  async def is_gripper_closed(self, backend_params: Optional[BackendParams] = None) -> bool:
    raise NotImplementedError("PrepCoreGripper does not support is_gripper_closed")

  async def halt(self, backend_params: Optional[BackendParams] = None) -> None:
    raise NotImplementedError("PrepCoreGripper does not support halt")

  async def park(self, backend_params: Optional[BackendParams] = None) -> None:
    raise NotImplementedError(
      "PrepCoreGripper does not support park. Tool management is handled by Prep.core_grippers()."
    )

  async def request_gripper_pose(
    self, backend_params: Optional[BackendParams] = None
  ) -> CartesianPose:
    raise NotImplementedError("PrepCoreGripper does not support request_gripper_pose")

  # -- Tool management (used by Prep.core_grippers context manager) --------------

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
    """Pick up CoRe gripper tool (PrepPickUpTool, cmd=15). Moves channels to safe Z after."""
    if tool_seek is None:
      tool_seek = tool_position_z + 10.0
    if tip_definition is None:
      tip_definition = PrepCmd.CO_RE_GRIPPER_TIP_PICKUP_PARAMETERS
    await self._driver.send_command(
      PrepCmd.PrepPickUpTool(
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
    await self._pip.move_channels_to_safe_z()

  async def drop_tool(self, *, move_to_safe_z_first: bool = True) -> None:
    """Drop CoRe gripper tool (PrepDropTool, cmd=16)."""
    if move_to_safe_z_first:
      await self._pip.move_channels_to_safe_z()
    await self._driver.send_command(PrepCmd.PrepDropTool())


class PrepGripperArm(FixedAxisGripperArm):
  """GripperArm that auto-populates Prep firmware geometry from the target resource.

  When ``pick_up_resource()`` is called, resource dimensions (length, height) and the
  plate-top Z offset are extracted from the :class:`Resource` automatically.  Users
  only need to pass firmware tuning knobs (``clearance_y``, ``grip_speed_y``,
  ``squeeze_mm``) via :class:`PrepCoreGripper.PickUpParams`.
  """

  async def pick_up_resource(
    self,
    resource: Resource,
    offset: Coordinate = Coordinate.zero(),
    pickup_distance_from_bottom: Optional[float] = None,
    backend_params: Optional[BackendParams] = None,
  ):
    if not isinstance(backend_params, PrepCoreGripper.PickUpParams):
      backend_params = PrepCoreGripper.PickUpParams()

    # Auto-fill geometry from the actual resource
    backend_params.resource_length = resource.get_absolute_size_x()
    backend_params.resource_height = resource.get_absolute_size_z()

    # plate_top_z_offset: how far above the grip point the plate top sits.
    #   grip point = bottom + pickup_distance_from_bottom
    #   plate top  = bottom + resource_height
    #   offset     = resource_height - pickup_distance_from_bottom
    pdfb = self._resolve_pickup_distance(resource, pickup_distance_from_bottom)
    backend_params.plate_top_z_offset = resource.get_absolute_size_z() - pdfb

    await super().pick_up_resource(resource, offset, pickup_distance_from_bottom, backend_params)
