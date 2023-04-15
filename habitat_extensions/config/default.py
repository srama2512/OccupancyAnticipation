#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import List, Optional, Union

from habitat.config.default import Config as CN
from habitat.config.default import get_config
from habitat.config.default import (
    CONFIG_FILE_SEPARATOR,
    DEFAULT_CONFIG_DIR,
)

_C = get_config()
_C.defrost()

# -----------------------------------------------------------------------------
# # SIMULATOR
# -----------------------------------------------------------------------------
_C.SIMULATOR.ACTION_SPACE_CONFIG = "pyrobotnoisy"
_C.SIMULATOR.NOISE_MODEL = CN()
_C.SIMULATOR.NOISE_MODEL.ROBOT = "LoCoBot"
_C.SIMULATOR.NOISE_MODEL.CONTROLLER = "ILQR"
_C.SIMULATOR.NOISE_MODEL.NOISE_MULTIPLIER = 1.0
# -----------------------------------------------------------------------------
# # NOISY DEPTH SENSOR
# -----------------------------------------------------------------------------
_C.SIMULATOR.NOISY_DEPTH_SENSOR = CN()
_C.SIMULATOR.NOISY_DEPTH_SENSOR.TYPE = "NoisyDepthSensor"
_C.SIMULATOR.NOISY_DEPTH_SENSOR.HEIGHT = 480
_C.SIMULATOR.NOISY_DEPTH_SENSOR.WIDTH = 640
_C.SIMULATOR.NOISY_DEPTH_SENSOR.HFOV = 90  # horizontal field of view in degrees
_C.SIMULATOR.NOISY_DEPTH_SENSOR.POSITION = [0, 1.25, 0]
_C.SIMULATOR.NOISY_DEPTH_SENSOR.ORIENTATION = [0.0, 0.0, 0.0]  # Euler's angles
_C.SIMULATOR.NOISY_DEPTH_SENSOR.MIN_DEPTH = 0.0
_C.SIMULATOR.NOISY_DEPTH_SENSOR.MAX_DEPTH = 10.0
_C.SIMULATOR.NOISY_DEPTH_SENSOR.NORMALIZE_DEPTH = True
_C.SIMULATOR.NOISY_DEPTH_SENSOR.MULT_NOISE = CN()
_C.SIMULATOR.NOISY_DEPTH_SENSOR.MULT_NOISE.SHAPE = 1000.0
_C.SIMULATOR.NOISY_DEPTH_SENSOR.MULT_NOISE.SCALE = 0.001
_C.SIMULATOR.NOISY_DEPTH_SENSOR.DROP_NOISE = CN()
_C.SIMULATOR.NOISY_DEPTH_SENSOR.DROP_NOISE.MEAN = 10
_C.SIMULATOR.NOISY_DEPTH_SENSOR.DROP_NOISE.SHAPE = 5.0
_C.SIMULATOR.NOISY_DEPTH_SENSOR.DROP_NOISE.SCALE = 1.0
# -----------------------------------------------------------------------------
# # TASK SENSORS
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# GT POSE SENSOR
# -----------------------------------------------------------------------------
_C.TASK.GT_POSE_SENSOR = CN()
_C.TASK.GT_POSE_SENSOR.TYPE = "GTPoseSensor"
# -----------------------------------------------------------------------------
# NOISY POSE SENSOR
# -----------------------------------------------------------------------------
_C.TASK.NOISY_POSE_SENSOR = CN()
_C.TASK.NOISY_POSE_SENSOR.TYPE = "NoisyPoseSensor"
_C.TASK.NOISY_POSE_SENSOR.FORWARD_MEAN = 0.025
_C.TASK.NOISY_POSE_SENSOR.FORWARD_VARIANCE = 1e-6
_C.TASK.NOISY_POSE_SENSOR.ROTATION_MEAN = 0.0157
_C.TASK.NOISY_POSE_SENSOR.ROTATION_VARIANCE = 1e-6
# -----------------------------------------------------------------------------
# NOISE FREE POSE SENSOR
# -----------------------------------------------------------------------------
_C.TASK.NOISE_FREE_POSE_SENSOR = CN()
_C.TASK.NOISE_FREE_POSE_SENSOR.TYPE = "NoiseFreePoseSensor"
# -----------------------------------------------------------------------------
# GT EGO MAP SENSOR
# -----------------------------------------------------------------------------
_C.TASK.GT_EGO_MAP = CN()
_C.TASK.GT_EGO_MAP.TYPE = "GTEgoMap"
_C.TASK.GT_EGO_MAP.MAP_SIZE = 101
_C.TASK.GT_EGO_MAP.MAP_SCALE = 0.05
_C.TASK.GT_EGO_MAP.MAX_SENSOR_RANGE = 3.25
_C.TASK.GT_EGO_MAP.HEIGHT_THRESH = [0.2, 1.5]
# -----------------------------------------------------------------------------
# GT EGO MAP SENSOR WITH HISTORY
# -----------------------------------------------------------------------------
_C.TASK.GT_EGO_MAP_HISTORY = CN()
_C.TASK.GT_EGO_MAP_HISTORY.TYPE = "GTEgoMapHistory"
_C.TASK.GT_EGO_MAP_HISTORY.MAP_SIZE = 101
_C.TASK.GT_EGO_MAP_HISTORY.MAP_SCALE = 0.05
_C.TASK.GT_EGO_MAP_HISTORY.MAX_SENSOR_RANGE = 3.25
_C.TASK.GT_EGO_MAP_HISTORY.HEIGHT_THRESH = [0.2, 1.5]
_C.TASK.GT_EGO_MAP_HISTORY.HISTORY_LIMIT = 50
# -----------------------------------------------------------------------------
# GT EGO WALL MAP SENSOR
# -----------------------------------------------------------------------------
_C.TASK.GT_EGO_WALL_MAP = CN()
_C.TASK.GT_EGO_WALL_MAP.TYPE = "GTEgoWallMap"
_C.TASK.GT_EGO_WALL_MAP.MAP_SIZE = 101
_C.TASK.GT_EGO_WALL_MAP.MAP_SCALE = 0.05
_C.TASK.GT_EGO_WALL_MAP.MAX_SENSOR_RANGE = 5.05
_C.TASK.GT_EGO_WALL_MAP.HEIGHT_THRESH = [0.2, 1.5]
# -----------------------------------------------------------------------------
# GT EGO MAP ANTICIPATED SENSOR
# -----------------------------------------------------------------------------
_C.TASK.GT_EGO_MAP_ANTICIPATED = CN()
_C.TASK.GT_EGO_MAP_ANTICIPATED.TYPE = "GTEgoMapAnticipated"
_C.TASK.GT_EGO_MAP_ANTICIPATED.MAP_SIZE = 101
_C.TASK.GT_EGO_MAP_ANTICIPATED.MAP_SCALE = 0.05
_C.TASK.GT_EGO_MAP_ANTICIPATED.MAX_SENSOR_RANGE = 5.05
_C.TASK.GT_EGO_MAP_ANTICIPATED.HEIGHT_THRESH = [0.2, 1.5]
# Use grown occupancy or full occupancy
# Can be grown_occupancy / full_occupancy / wall_occupancy
_C.TASK.GT_EGO_MAP_ANTICIPATED.REGION_GROWING_ITERATIONS = 2
_C.TASK.GT_EGO_MAP_ANTICIPATED.GT_TYPE = "grown_occupancy"
_C.TASK.GT_EGO_MAP_ANTICIPATED.ALL_MAPS_INFO_PATH = (
    "data/datasets/exploration/gibson/v1/val_mini/occant_gt_maps/all_maps_info.json"
)
# field-of-view of the GT generated
_C.TASK.GT_EGO_MAP_ANTICIPATED.WALL_FOV = 180.0
# for anticipated_occupancy option
_C.TASK.GT_EGO_MAP_ANTICIPATED.NUM_TOPDOWN_MAP_SAMPLE_POINTS = 20000
# mask nearby pixels in the map
_C.TASK.GT_EGO_MAP_ANTICIPATED.MASK_CLOSE_PIXELS = False
_C.TASK.GT_EGO_MAP_ANTICIPATED.MASK_CLOSE_PERC = 25
# -----------------------------------------------------------------------------
# COLLISION SENSOR
# -----------------------------------------------------------------------------
_C.TASK.COLLISION_SENSOR = CN()
_C.TASK.COLLISION_SENSOR.TYPE = "CollisionSensor"
# -----------------------------------------------------------------------------
# GT GLOBAL MAP MEASUREMENT
# -----------------------------------------------------------------------------
_C.TASK.GT_GLOBAL_MAP = CN()
_C.TASK.GT_GLOBAL_MAP.TYPE = "GTGlobalMap"
_C.TASK.GT_GLOBAL_MAP.MAP_SIZE = 961
_C.TASK.GT_GLOBAL_MAP.MAP_SCALE = 0.05
_C.TASK.GT_GLOBAL_MAP.NUM_TOPDOWN_MAP_SAMPLE_POINTS = 20000
_C.TASK.GT_GLOBAL_MAP.ENVIRONMENT_LAYOUTS_PATH = ""
# -----------------------------------------------------------------------------
# TopDownMapExp MEASUREMENT
# -----------------------------------------------------------------------------
_C.TASK.TOP_DOWN_MAP_EXP = CN()
_C.TASK.TOP_DOWN_MAP_EXP.TYPE = "TopDownMapExp"
_C.TASK.TOP_DOWN_MAP_EXP.MAX_EPISODE_STEPS = _C.ENVIRONMENT.MAX_EPISODE_STEPS
_C.TASK.TOP_DOWN_MAP_EXP.MAP_PADDING = 3
_C.TASK.TOP_DOWN_MAP_EXP.NUM_TOPDOWN_MAP_SAMPLE_POINTS = 20000
_C.TASK.TOP_DOWN_MAP_EXP.MAP_RESOLUTION = 1250
_C.TASK.TOP_DOWN_MAP_EXP.DRAW_SOURCE = True
_C.TASK.TOP_DOWN_MAP_EXP.DRAW_BORDER = True
_C.TASK.TOP_DOWN_MAP_EXP.DRAW_SHORTEST_PATH = False
_C.TASK.TOP_DOWN_MAP_EXP.FOG_OF_WAR = CN()
_C.TASK.TOP_DOWN_MAP_EXP.FOG_OF_WAR.DRAW = True
_C.TASK.TOP_DOWN_MAP_EXP.FOG_OF_WAR.VISIBILITY_DIST = 5.0
_C.TASK.TOP_DOWN_MAP_EXP.FOG_OF_WAR.FOV = 90
_C.TASK.TOP_DOWN_MAP_EXP.DRAW_VIEW_POINTS = True
_C.TASK.TOP_DOWN_MAP_EXP.DRAW_GOAL_POSITIONS = False
# Axes aligned bounding boxes
_C.TASK.TOP_DOWN_MAP_EXP.DRAW_GOAL_AABBS = False
# -----------------------------------------------------------------------------
# COLLISIONS MEASUREMENT
# -----------------------------------------------------------------------------
_C.TASK.COLLISIONS = CN()
_C.TASK.COLLISIONS.TYPE = "Collisions"


def get_extended_config(
    config_paths: Optional[Union[List[str], str]] = None, opts: Optional[list] = None
) -> CN:
    r"""Create a unified config with default values overwritten by values from
    :p:`config_paths` and overwritten by options from :p:`opts`.

    :param config_paths: List of config paths or string that contains comma
        separated list of config paths.
    :param opts: Config options (keys, values) in a list (e.g., passed from
        command line into the config. For example,
        :py:`opts = ['FOO.BAR', 0.5]`. Argument can be used for parameter
        sweeping or quick tests.
    """
    config = _C.clone()
    if config_paths:
        if isinstance(config_paths, str):
            if CONFIG_FILE_SEPARATOR in config_paths:
                config_paths = config_paths.split(CONFIG_FILE_SEPARATOR)
            else:
                config_paths = [config_paths]

        for config_path in config_paths:
            config.merge_from_file(config_path)

    if opts:
        config.merge_from_list(opts)

    config.freeze()
    return config
