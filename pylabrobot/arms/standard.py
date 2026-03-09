from dataclasses import dataclass
from typing import Dict

from pylabrobot.resources import Coordinate, Rotation


# Type alias for joint-space position (axis index -> position value)
JointCoords = Dict[int, float]


@dataclass
class GripperPose:
  location: Coordinate
  rotation: Rotation
