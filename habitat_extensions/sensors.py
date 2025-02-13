#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Any, Dict, List, Optional, Type, Union

import json
import math
import torch
import numpy as np
from gym import spaces

import habitat_sim
from habitat.config import Config
from habitat.core.registry import registry
from habitat.core.simulator import (
    AgentState,
    DepthSensor,
    Sensor,
    SensorTypes,
    Simulator,
)
from habitat.core.utils import try_cv2_import
from habitat.sims.habitat_simulator.habitat_simulator import check_sim_obs
from habitat.tasks.utils import cartesian_to_polar
from habitat_extensions.geometry_utils import (
    compute_updated_pose,
    quaternion_from_coeff,
    quaternion_xyzw_to_wxyz,
    quaternion_rotate_vector,
    compute_egocentric_delta,
    compute_heading_from_quaternion,
)
from habitat.tasks.nav.nav import PointGoalSensor
from habitat_extensions.utils import truncated_normal_noise_distr
from habitat.utils.visualizations import fog_of_war, maps
from occant_utils.common import (
    subtract_pose,
    grow_projected_map,
    spatial_transform_map,
)
import occant_utils.home_robot.depth as du


cv2 = try_cv2_import()

from einops import rearrange, asnumpy


@registry.register_sensor(name="GTPoseSensor")
class GTPoseSensor(Sensor):
    r"""The agents current ground-truth pose in the coordinate frame defined by
    the episode, i.e. the axis it faces along and the origin is defined by
    its state at t=0.

    Args:
        sim: reference to the simulator for calculating task observations.
        config: not needed
    """

    def __init__(self, sim: Simulator, config: Config, *args: Any, **kwargs: Any):
        self._sim = sim

        super().__init__(config=config)

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "pose_gt"

    def _get_sensor_type(self, *args: Any, **kwargs: Any):
        return SensorTypes.POSITION

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        sensor_shape = (3,)
        return spaces.Box(
            low=np.finfo(np.float32).min,
            high=np.finfo(np.float32).max,
            shape=sensor_shape,
            dtype=np.float32,
        )

    def _quat_to_xy_heading(self, quat):
        direction_vector = np.array([0, 0, -1])

        heading_vector = quaternion_rotate_vector(quat, direction_vector)

        phi = cartesian_to_polar(-heading_vector[2], heading_vector[0])[1]
        return np.array(phi)

    def get_observation(self, *args: Any, observations, episode, **kwargs: Any):
        agent_state = self._sim.get_agent_state()

        origin = np.array(episode.start_position, dtype=np.float32)
        rotation_world_start = quaternion_from_coeff(episode.start_rotation)

        agent_position = agent_state.position

        agent_position = quaternion_rotate_vector(
            rotation_world_start.inverse(), agent_position - origin
        )

        rotation_world_agent = agent_state.rotation
        rotation_world_start = quaternion_from_coeff(episode.start_rotation)

        agent_heading = self._quat_to_xy_heading(
            rotation_world_agent.inverse() * rotation_world_start
        )
        # This is rotation from -Z to -X. We want -Z to X for this particular sensor.
        agent_heading = -agent_heading

        return np.array(
            [-agent_position[2], agent_position[0], agent_heading], dtype=np.float32,
        )


@registry.register_sensor(name="NoisyPoseSensor")
class NoisyPoseSensor(Sensor):
    r"""The agents current estimated pose in the coordinate frame defined by the
    episode, i.e. the axis it faces along and the origin is defined by its state
    at t=0 The estimate is obtained by accumulating noisy odometer readings over time.

    Args:
        sim: reference to the simulator for calculating task observations.
        config: Contains the NOISE_SCALING field for the amount of noise added
            to the odometer.
    Attributes:
        _estimated_position: the estimated agent position in real-world [X, Y, Z].
        _estimated_rotation: the estimated agent rotation expressed as quaternion.
    """

    def __init__(self, sim: Simulator, config: Config, *args: Any, **kwargs: Any):
        self._sim = sim

        super().__init__(config=config)
        self.current_episode_id = None
        self._estimated_position = None
        self._estimated_rotation = None
        cfg = self.config
        self._fwd_tn_distr = truncated_normal_noise_distr(
            cfg.FORWARD_MEAN, cfg.FORWARD_VARIANCE, 2
        )

        self._rot_tn_distr = truncated_normal_noise_distr(
            cfg.ROTATION_MEAN, cfg.ROTATION_VARIANCE, 2
        )

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "pose"

    def _get_sensor_type(self, *args: Any, **kwargs: Any):
        return SensorTypes.POSITION

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        sensor_shape = (3,)
        return spaces.Box(
            low=np.finfo(np.float32).min,
            high=np.finfo(np.float32).max,
            shape=sensor_shape,
            dtype=np.float32,
        )

    def _quat_to_xy_heading(self, quat):
        direction_vector = np.array([0, 0, -1])

        heading_vector = quaternion_rotate_vector(quat, direction_vector)

        phi = cartesian_to_polar(-heading_vector[2], heading_vector[0])[1]
        return np.array(phi)

    def _get_noisy_delta(self, agent_state):
        current_agent_position = agent_state.position
        current_agent_rotation = agent_state.rotation

        past_agent_position = self._past_gt_position
        past_agent_rotation = self._past_gt_rotation

        # GT delta
        delta_xz_gt = compute_egocentric_delta(
            past_agent_position,
            past_agent_rotation,
            current_agent_position,
            current_agent_rotation,
        )
        delta_y_gt = current_agent_position[1] - past_agent_position[1]

        # Add noise to D_rho, D_theta
        D_rho, D_phi, D_theta = delta_xz_gt
        D_rho_noisy = (D_rho + self._fwd_tn_distr.rvs() * np.sign(D_rho)).item()
        D_phi_noisy = D_phi
        D_theta_noisy = (D_theta + self._rot_tn_distr.rvs() * np.sign(D_theta)).item()

        # Compute noisy delta
        delta_xz_noisy = (D_rho_noisy, D_phi_noisy, D_theta_noisy)
        delta_y_noisy = delta_y_gt

        return delta_xz_noisy, delta_y_noisy

    def get_observation(self, *args: Any, observations, episode, **kwargs: Any):
        episode_id = (episode.episode_id, episode.scene_id)

        agent_state = self._sim.get_agent_state()
        # At the start of a new episode, reset pose estimate
        if self.current_episode_id != episode_id:
            self.current_episode_id = episode_id
            # Initialize with the ground-truth position, rotation
            self._estimated_position = agent_state.position
            self._estimated_rotation = agent_state.rotation
            self._past_gt_position = agent_state.position
            self._past_gt_rotation = agent_state.rotation

        # Compute noisy delta
        delta_xz_noisy, delta_y_noisy = self._get_noisy_delta(agent_state)

        # Update past gt states
        self._past_gt_position = agent_state.position
        self._past_gt_rotation = agent_state.rotation

        # Update noisy pose estimates
        (self._estimated_position, self._estimated_rotation,) = compute_updated_pose(
            self._estimated_position,
            self._estimated_rotation,
            delta_xz_noisy,
            delta_y_noisy,
        )

        # Compute sensor readings with noisy estimates
        origin = np.array(episode.start_position, dtype=np.float32)
        rotation_world_start = quaternion_from_coeff(episode.start_rotation)

        agent_position = self._estimated_position

        agent_position = quaternion_rotate_vector(
            rotation_world_start.inverse(), agent_position - origin
        )

        rotation_world_agent = self._estimated_rotation
        rotation_world_start = quaternion_from_coeff(episode.start_rotation)

        agent_heading = self._quat_to_xy_heading(
            rotation_world_agent.inverse() * rotation_world_start
        )
        # This is rotation from -Z to -X. We want -Z to X for this particular sensor.
        agent_heading = -agent_heading

        return np.array(
            [-agent_position[2], agent_position[0], agent_heading], dtype=np.float32,
        )


@registry.register_sensor(name="NoiseFreePoseSensor")
class NoiseFreePoseSensor(Sensor):
    r"""The agents current ground-truth pose in the coordinate frame defined by
    the episode, i.e. the axis it faces along and the origin is defined by
    its state at t=0.

    Args:
        sim: reference to the simulator for calculating task observations.
        config: not needed
    """

    def __init__(self, sim: Simulator, config: Config, *args: Any, **kwargs: Any):
        self._sim = sim

        super().__init__(config=config)

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "pose"

    def _get_sensor_type(self, *args: Any, **kwargs: Any):
        return SensorTypes.POSITION

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        sensor_shape = (3,)
        return spaces.Box(
            low=np.finfo(np.float32).min,
            high=np.finfo(np.float32).max,
            shape=sensor_shape,
            dtype=np.float32,
        )

    def _quat_to_xy_heading(self, quat):
        direction_vector = np.array([0, 0, -1])

        heading_vector = quaternion_rotate_vector(quat, direction_vector)

        phi = cartesian_to_polar(-heading_vector[2], heading_vector[0])[1]
        return np.array(phi)

    def get_observation(self, *args: Any, observations, episode, **kwargs: Any):
        agent_state = self._sim.get_agent_state()

        origin = np.array(episode.start_position, dtype=np.float32)
        rotation_world_start = quaternion_from_coeff(episode.start_rotation)

        agent_position = agent_state.position

        agent_position = quaternion_rotate_vector(
            rotation_world_start.inverse(), agent_position - origin
        )

        rotation_world_agent = agent_state.rotation
        rotation_world_start = quaternion_from_coeff(episode.start_rotation)

        agent_heading = self._quat_to_xy_heading(
            rotation_world_agent.inverse() * rotation_world_start
        )
        # This is rotation from -Z to -X. We want -Z to X for this particular sensor.
        agent_heading = -agent_heading

        return np.array(
            [-agent_position[2], agent_position[0], agent_heading], dtype=np.float32,
        )


@registry.register_sensor(name="NoisyDepthSensor")
class NoisyDepthSensor(DepthSensor):
    r"""
    Args:
        sim: reference to the simulator for calculating task observations.
        config: Contains the noise type parameters.
    """
    min_depth_value: float
    max_depth_value: float

    def __init__(self, config: Config):
        self.sim_sensor_type = habitat_sim.SensorType.DEPTH
        if config.NORMALIZE_DEPTH:
            self.min_depth_value = 0
            self.max_depth_value = 1
        else:
            self.min_depth_value = config.MIN_DEPTH
            self.max_depth_value = config.MAX_DEPTH

        super().__init__(config=config)

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        return spaces.Box(
            low=self.min_depth_value,
            high=self.max_depth_value,
            shape=(self.config.HEIGHT, self.config.WIDTH, 1),
            dtype=np.float32,
        )

    def get_observation(self, sim_obs):
        obs = sim_obs.get(self.uuid, None)
        check_sim_obs(obs, self)

        obs = self.add_multiplicative_noise(
            obs,
            self.config.MULT_NOISE.SHAPE,
            self.config.MULT_NOISE.SCALE,
        )
        obs = self.dropout_random_ellipses(
            obs,
            self.config.DROP_NOISE.MEAN,
            self.config.DROP_NOISE.SHAPE,
            self.config.DROP_NOISE.SCALE,
        )
        if isinstance(obs, np.ndarray):
            obs = np.clip(obs, self.config.MIN_DEPTH, self.config.MAX_DEPTH)

            obs = np.expand_dims(
                obs, axis=2
            )  # make depth observation a 3D array
        else:
            obs = obs.clamp(self.config.MIN_DEPTH, self.config.MAX_DEPTH)

            obs = obs.unsqueeze(-1)

        if self.config.NORMALIZE_DEPTH:
            # normalize depth observation to [0, 1]
            obs = (obs - self.config.MIN_DEPTH) / (
                self.config.MAX_DEPTH - self.config.MIN_DEPTH
            )

        return obs

    def add_multiplicative_noise(self, depth_img, gamma_shape=10000, gamma_scale=0.0001):
        """Add noise to depth image.
        This is adapted from the DexNet 2.0 code.
        Their code: https://github.com/BerkeleyAutomation/gqcnn/blob/75040b552f6f7fb264c27d427b404756729b5e88/gqcnn/sgd_optimizer.py
        @param depth_img: a [H x W] set of depth z values
        """
        # Multiplicative noise: Gamma random variable
        # This will randomly shift around points locally
        multiplicative_noise = np.random.gamma(
            gamma_shape, gamma_scale, size=depth_img.shape
        )
        # Apply this noise to the depth image
        depth_img = multiplicative_noise * depth_img
        return depth_img

    def dropout_random_ellipses(
        self, depth_img, dropout_mean, gamma_shape=10000, gamma_scale=0.0001
    ):
        """Randomly drop a few ellipses in the image for robustness.
        This is adapted from the DexNet 2.0 code.
        Their code: https://github.com/BerkeleyAutomation/gqcnn/blob/75040b552f6f7fb264c27d427b404756729b5e88/gqcnn/sgd_optimizer.py
        @param depth_img: a [H x W] set of depth z values
        """
        depth_img = depth_img.copy()

        # Sample number of ellipses to dropout
        num_ellipses_to_dropout = np.random.poisson(dropout_mean)

        # Sample ellipse centers
        nonzero_pixel_indices = np.array(
            np.where(depth_img > 0)
        ).T  # Shape: [#nonzero_pixels x 2]
        if len(nonzero_pixel_indices) == 0:
            return depth_img

        dropout_centers_indices = np.random.choice(
            nonzero_pixel_indices.shape[0], size=num_ellipses_to_dropout
        )
        dropout_centers = nonzero_pixel_indices[
            dropout_centers_indices, :
        ]  # Shape: [num_ellipses_to_dropout x 2]

        # Sample ellipse radii and angles
        x_radii = np.random.gamma(gamma_shape, gamma_scale, size=num_ellipses_to_dropout)
        y_radii = np.random.gamma(gamma_shape, gamma_scale, size=num_ellipses_to_dropout)
        angles = np.random.randint(0, 360, size=num_ellipses_to_dropout)

        # Dropout ellipses
        for i in range(num_ellipses_to_dropout):
            center = dropout_centers[i, :]
            x_radius = np.round(x_radii[i]).astype(int)
            y_radius = np.round(y_radii[i]).astype(int)
            angle = angles[i]

            # dropout the ellipse
            # mask is always 2d even if input is not
            mask = np.zeros(depth_img.shape[:2])
            mask = cv2.ellipse(
                mask,
                tuple(center[::-1]),
                (x_radius, y_radius),
                angle=angle,
                startAngle=0,
                endAngle=360,
                color=1,
                thickness=-1,
            )
            depth_img[mask == 1] = 0

        return depth_img


@registry.register_sensor(name="GTEgoMap")
class GTEgoMap(Sensor):
    r"""Estimates the top-down occupancy based on current depth-map.

    Args:
        sim: reference to the simulator for calculating task observations.
        config: contains the MAP_SCALE, MAP_SIZE, HEIGHT_THRESH fields to
                decide grid-size, extents of the projection, and the thresholds
                for determining obstacles and explored space.
    """

    def __init__(self, sim: Simulator, config: Config, *args: Any, **kwargs: Any):
        super().__init__(config=config)
        self._sim = sim
        self.screen_h = self._sim.habitat_config.DEPTH_SENSOR.HEIGHT
        self.screen_w = self._sim.habitat_config.DEPTH_SENSOR.WIDTH
        self.camera_matrix = du.get_camera_matrix(
            self.screen_w, self.screen_h, self._sim.habitat_config.DEPTH_SENSOR.HFOV
        )
        self.resolution = self.config.MAP_SCALE * 100.0 # in centimeters
        self.xy_resolution = self.z_resolution = self.resolution
        self.vision_range = self.config.MAP_SIZE
        self.exp_pred_threshold = 1.0
        self.map_pred_threshold = 1.0
        self.max_depth = 3.5 * 100.0
        self.min_depth = 0.5 * 100.0
        self.agent_height = self._sim.habitat_config.DEPTH_SENSOR.POSITION[1] * 100.0
        # Height thresholds for obstacles
        self.height_thresh = self.config.HEIGHT_THRESH
        self.max_voxel_height = int(360 / self.z_resolution)
        self.min_voxel_height = int(-40 / self.z_resolution)
        self.min_mapped_height = int(
            25 / self.z_resolution - self.min_voxel_height
        )
        self.max_mapped_height = int(
            (self.agent_height + 1) / self.z_resolution - self.min_voxel_height
        )
        self.du_scale = 4
        self.shift_loc = [self.vision_range * self.xy_resolution // 2, 0, 0.0]
        self.tilt_angle = self._sim.habitat_config.DEPTH_SENSOR.ORIENTATION[0]  # in radians

        # Depth processing
        self.src_min_depth = float(self._sim.habitat_config.DEPTH_SENSOR.MIN_DEPTH) * 100.0
        self.src_max_depth = float(self._sim.habitat_config.DEPTH_SENSOR.MAX_DEPTH) * 100.0
        self.src_normalized_depth = self._sim.habitat_config.DEPTH_SENSOR.NORMALIZE_DEPTH
        device_id = self._sim.config.sim_cfg.gpu_device_id
        self.device = torch.device("cuda:{}".format(device_id) if torch.cuda.is_available() else "cpu")

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "ego_map_gt"

    def _get_sensor_type(self, *args: Any, **kwargs: Any):
        return SensorTypes.COLOR

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        sensor_shape = (self.config.MAP_SIZE, self.config.MAP_SIZE, 2)
        return spaces.Box(low=0, high=1, shape=sensor_shape, dtype=np.uint8,)

    def _get_depth_projection(self, rgb_map: torch.Tensor, depth_map: torch.Tensor):
        """Generate a local map given a new observation using parameter-free
        differentiable projective geometry.
        Args:
            depth_map: current frame containing depth (batch_size, frame_height, frame_width)
        Returns:
            local_map: current local map updated with current observation of shape
                (batch_size, 2, map_height, map_width)
        """
        batch_size, h, w = depth_map.size()
        device, dtype = depth_map.device, depth_map.dtype
        tilt = torch.ones(batch_size).to(device) * self.tilt_angle

        depth = depth_map.float()
        if self.src_normalized_depth:
            depth = depth * (self.src_max_depth - self.src_min_depth) + self.src_min_depth
        else:
            # convert to cm
            depth = depth * 100.0
        depth[depth > self.max_depth] = 0
        depth[depth == self.min_depth] = 0
        point_cloud_t = du.get_point_cloud_from_z_t(
            depth, self.camera_matrix, device, scale=self.du_scale
        )
        point_cloud_base_coords = du.transform_camera_view_t(
            point_cloud_t, self.agent_height, asnumpy(tilt * 180.0 / math.pi), device
        )
        point_cloud_map_coords = du.transform_pose_t(
            point_cloud_base_coords, self.shift_loc, device
        )
        if False:
            # from occant_utils.home_robot.point_cloud import show_point_cloud

            rgb = rgb_map[:, :3, :: self.du_scale, :: self.du_scale].permute(0, 2, 3, 1)
            xyz = point_cloud_map_coords[0].reshape(-1, 3).numpy()
            rgb = rgb[0].reshape(-1, 3).numpy()
            print("-> Showing point cloud in camera coords")
            # show_point_cloud(
            #     (xyz / 100.0) (rgb / 255.0), orig=np.zeros(3)
            # )
            import matplotlib.pyplot as plt
            import matplotlib
            matplotlib.use("agg")
            fig = plt.figure()
            plt.scatter(xyz[:, 0], xyz[:, 1], c=rgb / 255.0)
            plt.savefig("image_1.png")
            fig = plt.figure()
            plt.scatter(xyz[:, 0], xyz[:, 2], c=rgb / 255.0)
            plt.savefig("image_2.png")
            fig = plt.figure()
            plt.imshow(rgb.reshape(h, w, 3) / 255.0)
            plt.savefig("image_3.png")

        voxel_channels = 1

        init_grid = torch.zeros(
            batch_size,
            voxel_channels,
            self.vision_range,
            self.vision_range,
            self.max_voxel_height - self.min_voxel_height,
            device=device,
            dtype=torch.float32,
        )
        feat = torch.ones(
            batch_size,
            voxel_channels,
            self.screen_h // self.du_scale * self.screen_w // self.du_scale,
            device=device,
            dtype=torch.float32,
        )

        XYZ_cm_std = point_cloud_map_coords.float()
        XYZ_cm_std[..., :2] = XYZ_cm_std[..., :2] / self.xy_resolution
        XYZ_cm_std[..., :2] = (
            (XYZ_cm_std[..., :2] - self.vision_range // 2.0) / self.vision_range * 2.0
        )
        XYZ_cm_std[..., 2] = XYZ_cm_std[..., 2] / self.z_resolution
        XYZ_cm_std[..., 2] = (
            (
                XYZ_cm_std[..., 2]
                - (self.max_voxel_height + self.min_voxel_height) // 2.0
            )
            / (self.max_voxel_height - self.min_voxel_height)
            * 2.0
        )
        XYZ_cm_std = XYZ_cm_std.permute(0, 3, 1, 2)
        XYZ_cm_std = XYZ_cm_std.view(
            XYZ_cm_std.shape[0],
            XYZ_cm_std.shape[1],
            XYZ_cm_std.shape[2] * XYZ_cm_std.shape[3],
        )

        voxels = du.splat_feat_nd(init_grid, feat, XYZ_cm_std).transpose(2, 3)

        agent_height_proj = voxels[
            ..., self.min_mapped_height : self.max_mapped_height
        ].sum(4)
        # all_height_proj = voxels.sum(4)
        all_height_proj = voxels[..., 0 : self.max_mapped_height].sum(4)

        fp_map_pred = agent_height_proj[:, 0:1, :, :]
        fp_exp_pred = all_height_proj[:, 0:1, :, :]
        fp_map_pred = ((fp_map_pred / self.map_pred_threshold) >= 1.0).float()
        fp_exp_pred = ((fp_exp_pred / self.exp_pred_threshold) >= 1.0).float()
        output = torch.cat([fp_map_pred, fp_exp_pred], dim=1)  # (B, 2, H, W)
        # Post-hoc transformations to fix coordinate system
        output = torch.flip(output, [2])

        return output

    def get_observation(self, *args: Any, observations, episode, **kwargs: Any):
        sim_depth = asnumpy(observations["depth"])  # (H, W, 1)
        sim_rgb = asnumpy(observations["rgb"])  # (H, W, 3)
        sim_depth = torch.from_numpy(sim_depth).squeeze(2).unsqueeze(0)  # (1, H, W)
        sim_rgb = torch.from_numpy(sim_rgb).permute(2, 0, 1).unsqueeze(0)  # (1, 3, H, W)
        sim_depth = sim_depth.to(self.device)
        sim_rgb = sim_rgb.to(self.device)
        ego_map_gt = self._get_depth_projection(sim_rgb, sim_depth)  # (1, 2, H, W)
        ego_map_gt = asnumpy(rearrange(ego_map_gt, "() c h w -> h w c"))

        return ego_map_gt


@registry.register_sensor(name="GTEgoMapHistory")
class GTEgoMapHistory(GTEgoMap):
    r"""Estimates the top-down occupancy based on current depth-map and past observations.

    Args:
        sim: reference to the simulator for calculating task observations.
        config: contains the MAP_SCALE, MAP_SIZE, HEIGHT_THRESH fields to
                decide grid-size, extents of the projection, and the thresholds
                for determining obstacles and explored space.
    """

    def __init__(self, sim: Simulator, config: Config, *args: Any, **kwargs: Any):
        super().__init__(sim, config, *args, **kwargs)
        # Maintain a history of point-cloud observations
        self.past_observations = None
        self._active_episode_id = None

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "ego_map_gt"

    def _get_world_coords(self):
        """
        Conventions:
            X is rightward positive
            Y is forward positive
            theta is measured counter-clockwise starting from X-axis
        """
        # Get agent position and rotation
        agent_state = self._sim.get_agent_state()
        agent_position = np.array(agent_state.position) * 100.0
        agent_rotation = -compute_heading_from_quaternion(agent_state.rotation)
        # convert to world coordinates
        world_coords = [agent_position[0], -agent_position[2], agent_rotation]
        return world_coords

    def _get_depth_projection(self, rgb_map: torch.Tensor, depth_map: torch.Tensor):
        """Generate a local map given a new observation using parameter-free
        differentiable projective geometry.
        Args:
            depth_map: current frame containing depth (batch_size, frame_height, frame_width)
        Returns:
            local_map: current local map updated with current observation of shape
                (batch_size, 2, map_height, map_width)
        """
        batch_size, h, w = depth_map.size()
        assert batch_size == 1
        device, dtype = depth_map.device, depth_map.dtype
        tilt = torch.ones(batch_size).to(device) * self.tilt_angle

        depth = depth_map.float()
        if self.src_normalized_depth:
            depth = depth * (self.src_max_depth - self.src_min_depth) + self.src_min_depth
        else:
            # convert to cm
            depth = depth * 100.0
        depth[depth > self.max_depth] = 0
        depth[depth == self.min_depth] = 0
        point_cloud_t = du.get_point_cloud_from_z_t(
            depth, self.camera_matrix, device, scale=self.du_scale
        )
        point_cloud_base_coords = du.transform_camera_view_t(
            point_cloud_t, self.agent_height, asnumpy(tilt * 180.0 / math.pi), device
        )
        # Transform to world coordinates
        world_coords = self._get_world_coords()
        point_cloud_world_coords = du.transform_pose_t(
            point_cloud_base_coords, world_coords, device
        )  # (1, H, W, 3)

        point_cloud_world_coords = rearrange(
            point_cloud_world_coords, "() h w c -> (h w) c"
        )  # assumes batch_size = 1
        # Remove invalid points
        valid_points = rearrange(depth[:, ::self.du_scale, ::self.du_scale] != 0, "() h w -> (h w)")
        point_cloud_world_coords = asnumpy(point_cloud_world_coords[valid_points])
        # Add points to past
        if self.past_observations is None:
            self.past_observations = [point_cloud_world_coords]
        else:
            self.past_observations.append(point_cloud_world_coords)
            if len(self.past_observations) > self.config.HISTORY_LIMIT:
                self.past_observations = self.past_observations[-self.config.HISTORY_LIMIT:]
        past_observations = np.concatenate(self.past_observations, axis=0)
        point_cloud_world_coords_h = torch.from_numpy(past_observations).float()
        point_cloud_world_coords_h = point_cloud_world_coords_h.unsqueeze(0)#.to(device)  # (1, N, 3)
        point_cloud_base_coords_h = du.inverse_transform_pose_t( 
            point_cloud_world_coords_h, world_coords, torch.device("cpu")#device
        )

        # Transform to map coordinates
        point_cloud_map_coords_h = du.transform_pose_t(
            point_cloud_base_coords_h, self.shift_loc, torch.device("cpu")#device
        )  # (1, N, 3)
        if False:
            # from occant_utils.home_robot.point_cloud import show_point_cloud

            rgb = rgb_map[:, :3, :: self.du_scale, :: self.du_scale].permute(0, 2, 3, 1).cpu()
            xyz = point_cloud_map_coords_h[0].cpu().reshape(-1, 3).numpy()
            print("-> Showing point cloud in camera coords")
            # show_point_cloud(
            #     (xyz / 100.0) (rgb / 255.0), orig=np.zeros(3)
            # )
            import matplotlib.pyplot as plt
            import matplotlib
            matplotlib.use("agg")
            fig = plt.figure()
            plt.scatter(xyz[:, 0], xyz[:, 1], c=xyz[:, 2])
            plt.savefig("image_1.png")
            fig = plt.figure()
            plt.scatter(xyz[:, 0], xyz[:, 2], c=xyz[:, 1])
            plt.savefig("image_2.png")
            fig = plt.figure()
            plt.imshow(rgb[0].numpy() / 255.0)
            plt.savefig("image_3.png")

        voxel_channels = 1

        init_grid = torch.zeros(
            batch_size,
            voxel_channels,
            self.vision_range,
            self.vision_range,
            self.max_voxel_height - self.min_voxel_height,
            device=device,
            dtype=torch.float32,
        )

        XYZ_cm_std = point_cloud_map_coords_h.float()
        XYZ_cm_std[..., :2] = XYZ_cm_std[..., :2] / self.xy_resolution
        XYZ_cm_std[..., :2] = (
            (XYZ_cm_std[..., :2] - self.vision_range // 2.0) / self.vision_range * 2.0
        )
        XYZ_cm_std[..., 2] = XYZ_cm_std[..., 2] / self.z_resolution
        XYZ_cm_std[..., 2] = (
            (
                XYZ_cm_std[..., 2]
                - (self.max_voxel_height + self.min_voxel_height) // 2.0
            )
            / (self.max_voxel_height - self.min_voxel_height)
            * 2.0
        )  # (1, N, 3)
        # Remove out-of-bounds points
        valid_points = (XYZ_cm_std[0, :, 0] <= 1.0) & (XYZ_cm_std[0, :, 0] >= -1.0) & \
            (XYZ_cm_std[0, :, 1] <= 1.0) & (XYZ_cm_std[0, :, 1] >= -1.0)  # (N, )
        XYZ_cm_std = XYZ_cm_std[:, valid_points, :]
        XYZ_cm_std = rearrange(XYZ_cm_std, "b n c -> b c n").to(device)

        feat = torch.ones(
            batch_size,
            voxel_channels,
            XYZ_cm_std.shape[2],
            device=device,
            dtype=torch.float32,
        )

        voxels = du.splat_feat_nd(init_grid, feat, XYZ_cm_std).transpose(2, 3)

        agent_height_proj = voxels[
            ..., self.min_mapped_height : self.max_mapped_height
        ].sum(4)
        # all_height_proj = voxels.sum(4)
        all_height_proj = voxels[..., 0 : self.max_mapped_height].sum(4)

        fp_map_pred = agent_height_proj[:, 0:1, :, :]
        fp_exp_pred = all_height_proj[:, 0:1, :, :]
        fp_map_pred = ((fp_map_pred / self.map_pred_threshold) >= 1.0).float()
        fp_exp_pred = ((fp_exp_pred / self.exp_pred_threshold) >= 1.0).float()
        output = torch.cat([fp_map_pred, fp_exp_pred], dim=1)  # (B, 2, H, W)
        # Post-hoc transformations to fix coordinate system
        output = torch.flip(output, [2])

        return output

    def get_observation(self, *args: Any, observations, episode, **kwargs: Any):
        episode_id = f"{episode.scene_id}_{episode.episode_id}"
        if episode_id != self._active_episode_id:
            self.past_observations = None
            self._active_episode_id = episode_id
        sim_depth = asnumpy(observations["depth"])  # (H, W, 1)
        sim_rgb = asnumpy(observations["rgb"])  # (H, W, 3)
        sim_depth = torch.from_numpy(sim_depth).squeeze(2).unsqueeze(0)  # (1, H, W)
        sim_rgb = torch.from_numpy(sim_rgb).permute(2, 0, 1).unsqueeze(0)  # (1, 3, H, W)
        sim_depth = sim_depth.to(self.device)
        sim_rgb = sim_rgb.to(self.device)
        ego_map_gt = self._get_depth_projection(sim_rgb, sim_depth)  # (1, 2, H, W)
        ego_map_gt = asnumpy(rearrange(ego_map_gt, "() c h w -> h w c"))

        return ego_map_gt


@registry.register_sensor(name="GTEgoWallMap")
class GTEgoWallMap(GTEgoMap):
    r"""Estimates the top-down occupancy based on current depth-map.

    Args:
        sim: reference to the simulator for calculating task observations.
        config: contains the MAP_SCALE, MAP_SIZE, HEIGHT_THRESH fields to
                decide grid-size, extents of the projection, and the thresholds
                for determining obstacles and explored space.
    """

    def __init__(self, sim: Simulator, config: Config, *args: Any, **kwargs: Any):
        super().__init__(sim, config, *args, **kwargs)
        self.max_voxel_height = int(360 / self.z_resolution)
        self.min_voxel_height = int(100 / self.z_resolution)
        self.min_mapped_height = int(
            100 / self.z_resolution - self.min_voxel_height
        )
        self.max_mapped_height = int(
            (self.agent_height + 1) / self.z_resolution - self.min_voxel_height
        )

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "ego_wall_map_gt"


@registry.register_sensor(name="GTEgoMapAnticipated")
class GTEgoMapAnticipated(GTEgoMap):
    r"""Anticipates the top-down occupancy based on current depth-map grown using the
        ground-truth occupancy map.

    Args:
        sim: reference to the simulator for calculating task observations.
        config: contains the MAP_SCALE, MAP_SIZE, HEIGHT_THRESH fields to
                decide grid-size, extents of the projection, and the thresholds
                for determining obstacles and explored space.
    """

    def __init__(self, sim: Simulator, config: Config, *args: Any, **kwargs: Any):
        super().__init__(sim, config=config, *args, **kwargs)
        self.map_scale = config.MAP_SCALE
        self.map_size = config.MAP_SIZE
        self._num_samples = config.NUM_TOPDOWN_MAP_SAMPLE_POINTS
        self._coordinate_min = maps.COORDINATE_MIN
        self._coordinate_max = maps.COORDINATE_MAX
        self._mask_close = config.MASK_CLOSE_PIXELS
        resolution = (self._coordinate_max - self._coordinate_min) / self.map_scale
        self._map_resolution = (int(resolution), int(resolution))
        self.current_episode_id = None
        if hasattr(config, "REGION_GROWING_ITERATIONS"):
            self._region_growing_iterations = config.REGION_GROWING_ITERATIONS
        else:
            self._region_growing_iterations = 2
        if self.config.GT_TYPE == "wall_occupancy":
            maps_info_path = self.config.ALL_MAPS_INFO_PATH
            self._all_maps_info = json.load(open(maps_info_path, "r"))
            self.current_episode_id = None
            self._scene_maps_info = None
            self._scene_maps = None

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "ego_map_gt_anticipated"

    def get_original_map(self):
        top_down_map = maps.get_topdown_map(
            self._sim, self._map_resolution, self._num_samples, False,
        )
        return top_down_map

    def _get_seen_occupancy(self, episode, agent_state):
        episode_id = (episode.episode_id, episode.scene_id)
        # Load the episode specific maps only if the episode has changed
        if self.current_episode_id != episode_id:
            self.current_episode_id = episode_id
            if self.config.GT_TYPE == "wall_occupancy":
                scene_id = episode.scene_id.split("/")[-1]
                self._scene_maps_info = self._all_maps_info[scene_id]
                # Load the maps per floor
                seen_maps, wall_maps = self._load_transformed_wall_maps(
                    self._scene_maps_info, episode,
                )
                self._scene_maps = {}
                self._scene_maps["seen_maps"] = seen_maps
                self._scene_maps["wall_maps"] = wall_maps

        agent_state = self._sim.get_agent_state()
        current_height = agent_state.position[1]
        best_floor_idx = None
        best_floor_dist = math.inf
        for floor_idx, floor_data in enumerate(self._scene_maps_info):
            floor_height = floor_data["floor_height"]
            if abs(current_height - floor_height) < best_floor_dist:
                best_floor_idx = floor_idx
                best_floor_dist = abs(current_height - floor_height)
        assert best_floor_idx is not None
        current_seen_map = self._scene_maps["seen_maps"][best_floor_idx]  # (H, W, 2)

        # ========= Get local egocentric crop of the current wall map =========
        # Compute relative pose of agent from start location
        start_position = episode.start_position  # (X, Y, Z)
        start_rotation = quaternion_xyzw_to_wxyz(episode.start_rotation)
        start_heading = compute_heading_from_quaternion(start_rotation)
        start_pose = torch.Tensor(
            [[-start_position[2], start_position[0], start_heading]]
        )
        agent_position = agent_state.position
        agent_heading = compute_heading_from_quaternion(agent_state.rotation)
        agent_pose = torch.Tensor(
            [[-agent_position[2], agent_position[0], agent_heading]]
        )
        rel_pose = subtract_pose(start_pose, agent_pose)[0]  # (3,)

        # Compute agent position on the map image
        map_scale = self.config.MAP_SCALE

        H, W = current_seen_map.shape[:2]
        Hby2, Wby2 = (H + 1) // 2, (W + 1) // 2
        agent_map_x = int(rel_pose[1].item() / map_scale + Wby2)
        agent_map_y = int(-rel_pose[0].item() / map_scale + Hby2)

        # Crop the region around the agent.
        mrange = int(1.5 * self.map_size)

        # Add extra padding if map range is coordinates go out of bounds
        y_start = agent_map_y - mrange
        y_end = agent_map_y + mrange
        x_start = agent_map_x - mrange
        x_end = agent_map_x + mrange

        x_l_pad, y_l_pad, x_r_pad, y_r_pad = 0, 0, 0, 0

        H, W = current_seen_map.shape[:2]
        if x_start < 0:
            x_l_pad = int(-x_start)
            x_start += x_l_pad
            x_end += x_l_pad
        if x_end >= W:
            x_r_pad = int(x_end - W + 1)
        if y_start < 0:
            y_l_pad = int(-y_start)
            y_start += y_l_pad
            y_end += y_l_pad
        if y_end >= H:
            y_r_pad = int(y_end - H + 1)

        ego_map = np.pad(
            current_seen_map,
            ((y_l_pad, y_r_pad), (x_l_pad, x_r_pad), (0, 0))
        )
        ego_map = ego_map[y_start : (y_end + 1), x_start : (x_end + 1)]

        agent_heading = rel_pose[2].item()
        agent_heading = math.degrees(agent_heading)

        half_size = ego_map.shape[0] // 2
        center = (half_size, half_size)
        M = cv2.getRotationMatrix2D(center, agent_heading, scale=1.0)

        ego_map = cv2.warpAffine(
            ego_map,
            M,
            (ego_map.shape[1], ego_map.shape[0]),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(1,),
        )

        ego_map = ego_map.astype(np.float32)
        mrange = int(self.map_size)
        ego_map = ego_map[
            (half_size - mrange) : (half_size + mrange),
            (half_size - mrange) : (half_size + mrange),
        ]

        # Get forward region infront of the agent
        half_size = ego_map.shape[0] // 2
        quarter_size = ego_map.shape[0] // 4
        center = (half_size, half_size)

        ego_map = ego_map[0:half_size, quarter_size : (quarter_size + half_size)]

        # Dilate the obstacle map
        dilation_mask = np.ones((5, 5))
        ego_map[:, :, 0] = cv2.dilate(ego_map[:, :, 0], dilation_mask, iterations=1)
        return ego_map

    def _get_wall_occupancy(self, episode, agent_state):
        episode_id = (episode.episode_id, episode.scene_id)
        # Load the episode specific maps only if the episode has changed
        if self.current_episode_id != episode_id:
            self.current_episode_id = episode_id
            if self.config.GT_TYPE == "wall_occupancy":
                scene_id = episode.scene_id.split("/")[-1]
                self._scene_maps_info = self._all_maps_info[scene_id]
                # Load the maps per floor
                seen_maps, wall_maps = self._load_transformed_wall_maps(
                    self._scene_maps_info, episode,
                )
                self._scene_maps = {}
                self._scene_maps["seen_maps"] = seen_maps
                self._scene_maps["wall_maps"] = wall_maps

        agent_state = self._sim.get_agent_state()
        current_height = agent_state.position[1]
        best_floor_idx = None
        best_floor_dist = math.inf
        for floor_idx, floor_data in enumerate(self._scene_maps_info):
            floor_height = floor_data["floor_height"]
            if abs(current_height - floor_height) < best_floor_dist:
                best_floor_idx = floor_idx
                best_floor_dist = abs(current_height - floor_height)
        assert best_floor_idx is not None
        current_wall_map = self._scene_maps["wall_maps"][best_floor_idx]
        # Take only channel 0 as both channels have save values
        current_wall_map = current_wall_map[..., 0]

        # ========= Get local egocentric crop of the current wall map =========
        # Compute relative pose of agent from start location
        start_position = episode.start_position  # (X, Y, Z)
        start_rotation = quaternion_xyzw_to_wxyz(episode.start_rotation)
        start_heading = compute_heading_from_quaternion(start_rotation)
        start_pose = torch.Tensor(
            [[-start_position[2], start_position[0], start_heading]]
        )
        agent_position = agent_state.position
        agent_heading = compute_heading_from_quaternion(agent_state.rotation)
        agent_pose = torch.Tensor(
            [[-agent_position[2], agent_position[0], agent_heading]]
        )
        rel_pose = subtract_pose(start_pose, agent_pose)[0]  # (3,)

        # Compute agent position on the map image
        map_scale = self.config.MAP_SCALE

        H, W = current_wall_map.shape[:2]
        Hby2, Wby2 = (H + 1) // 2, (W + 1) // 2
        agent_map_x = int(rel_pose[1].item() / map_scale + Wby2)
        agent_map_y = int(-rel_pose[0].item() / map_scale + Hby2)

        # Crop the region around the agent.
        mrange = int(1.5 * self.map_size)

        # Add extra padding if map range is coordinates go out of bounds
        y_start = agent_map_y - mrange
        y_end = agent_map_y + mrange
        x_start = agent_map_x - mrange
        x_end = agent_map_x + mrange

        x_l_pad, y_l_pad, x_r_pad, y_r_pad = 0, 0, 0, 0

        H, W = current_wall_map.shape
        if x_start < 0:
            x_l_pad = int(-x_start)
            x_start += x_l_pad
            x_end += x_l_pad
        if x_end >= W:
            x_r_pad = int(x_end - W + 1)
        if y_start < 0:
            y_l_pad = int(-y_start)
            y_start += y_l_pad
            y_end += y_l_pad
        if y_end >= H:
            y_r_pad = int(y_end - H + 1)

        ego_map = np.pad(current_wall_map, ((y_l_pad, y_r_pad), (x_l_pad, x_r_pad)))
        ego_map = ego_map[y_start : (y_end + 1), x_start : (x_end + 1)]

        agent_heading = rel_pose[2].item()
        agent_heading = math.degrees(agent_heading)

        half_size = ego_map.shape[0] // 2
        center = (half_size, half_size)
        M = cv2.getRotationMatrix2D(center, agent_heading, scale=1.0)

        ego_map = cv2.warpAffine(
            ego_map,
            M,
            (ego_map.shape[1], ego_map.shape[0]),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(1,),
        )

        ego_map = ego_map.astype(np.float32)
        mrange = int(self.map_size)
        ego_map = ego_map[
            (half_size - mrange) : (half_size + mrange),
            (half_size - mrange) : (half_size + mrange),
        ]

        # Get forward region infront of the agent
        half_size = ego_map.shape[0] // 2
        quarter_size = ego_map.shape[0] // 4
        center = (half_size, half_size)

        ego_map = ego_map[0:half_size, quarter_size : (quarter_size + half_size)]

        # Append explored status in the 2nd channel
        ego_map = np.stack([ego_map, ego_map], axis=2)

        return ego_map

    def _load_transformed_wall_maps(self, scene_map_info, episode):
        seen_maps = []
        wall_maps = []
        start_position = episode.start_position  # (X, Y, Z)
        start_rotation = quaternion_xyzw_to_wxyz(episode.start_rotation)
        start_heading = compute_heading_from_quaternion(start_rotation)
        for floor_data in scene_map_info:
            seen_map = np.load(floor_data["seen_map_path"])
            wall_map = np.load(floor_data["wall_map_path"])
            # ===== Transform the maps relative to the episode start pose =====
            map_view_position = floor_data["world_position"]
            map_view_heading = floor_data["world_heading"]
            # Originally, Z is downward and X is rightward.
            # Convert it to X upward and Y rightward
            x_map, y_map = -map_view_position[2], map_view_position[0]
            theta_map = map_view_heading
            x_start, y_start = -start_position[2], start_position[0]
            theta_start = start_heading
            # Compute relative coordinates
            r_rel = math.sqrt((x_start - x_map) ** 2 + (y_start - y_map) ** 2)
            phi_rel = math.atan2(y_start - y_map, x_start - x_map) - theta_map
            x_rel = r_rel * math.cos(phi_rel) / self.config.MAP_SCALE
            y_rel = r_rel * math.sin(phi_rel) / self.config.MAP_SCALE
            theta_rel = theta_start - theta_map
            # Convert these to image coordinates with X being rightward and Y
            # being downward
            x_img_rel = y_rel
            y_img_rel = -x_rel
            theta_img_rel = theta_rel
            x_trans = torch.Tensor([[x_img_rel, y_img_rel, theta_img_rel]])
            # Perform the transformations
            p_seen_map = rearrange(torch.Tensor(seen_map), "h w c -> () c h w")
            p_wall_map = rearrange(torch.Tensor(wall_map), "h w c -> () c h w")
            p_seen_map_trans = spatial_transform_map(p_seen_map, x_trans)
            p_wall_map_trans = spatial_transform_map(p_wall_map, x_trans)
            seen_map_trans = asnumpy(p_seen_map_trans)
            seen_map_trans = rearrange(seen_map_trans, "() c h w -> h w c")
            wall_map_trans = asnumpy(p_wall_map_trans)
            wall_map_trans = rearrange(wall_map_trans, "() c h w -> h w c")
            seen_maps.append(seen_map_trans)
            wall_maps.append(wall_map_trans)

        return seen_maps, wall_maps

    def _get_mesh_occupancy(self, episode, agent_state):
        episode_id = (episode.episode_id, episode.scene_id)
        if self.current_episode_id != episode_id:
            self.current_episode_id = episode_id
            # Transpose to make x rightward and y downward
            self._top_down_map = self.get_original_map().T

        agent_position = agent_state.position
        agent_rotation = agent_state.rotation
        a_x, a_y = maps.to_grid(
            agent_position[0],
            agent_position[2],
            self._coordinate_min,
            self._coordinate_max,
            self._map_resolution,
        )

        # Crop region centered around the agent
        mrange = int(self.map_size * 1.5)
        ego_map = self._top_down_map[
            (a_y - mrange) : (a_y + mrange), (a_x - mrange) : (a_x + mrange)
        ]
        if ego_map.shape[0] == 0 or ego_map.shape[1] == 0:
            ego_map = np.zeros((2 * mrange + 1, 2 * mrange + 1), dtype=np.uint8)

        # Rotate to get egocentric map
        # Negative since the value returned is clockwise rotation about Y,
        # but we need anti-clockwise rotation
        agent_heading = -compute_heading_from_quaternion(agent_rotation)
        agent_heading = math.degrees(agent_heading)

        half_size = ego_map.shape[0] // 2
        center = (half_size, half_size)
        M = cv2.getRotationMatrix2D(center, agent_heading, scale=1.0)

        ego_map = (
            cv2.warpAffine(
                ego_map * 255,
                M,
                (ego_map.shape[1], ego_map.shape[0]),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(1,),
            ).astype(np.float32)
            / 255.0
        )

        mrange = int(self.map_size)
        ego_map = ego_map[
            (half_size - mrange) : (half_size + mrange),
            (half_size - mrange) : (half_size + mrange),
        ]
        ego_map[ego_map > 0.5] = 1.0
        ego_map[ego_map <= 0.5] = 0.0

        # This map is currently 0 if occupied and 1 if unoccupied. Flip it.
        ego_map = 1.0 - ego_map

        # Flip the x axis because to_grid() flips the conventions
        ego_map = np.flip(ego_map, axis=1)

        # Get forward region infront of the agent
        half_size = ego_map.shape[0] // 2
        quarter_size = ego_map.shape[0] // 4
        center = (half_size, half_size)

        ego_map = ego_map[0:half_size, quarter_size : (quarter_size + half_size)]

        # Append explored status in the 2nd channel
        ego_map = np.stack([ego_map, np.ones_like(ego_map)], axis=2)

        return ego_map

    def _get_grown_depth_projection(self, episode, agent_state, sim_depth, sim_rgb):
        # Get projected occupancy
        sim_depth = asnumpy(sim_depth)  # (H, W, 1)
        sim_rgb = asnumpy(sim_rgb)  # (H, W, 3)
        sim_depth = torch.from_numpy(sim_depth).squeeze(2).unsqueeze(0)  # (1, H, W)
        sim_rgb = torch.from_numpy(sim_rgb).permute(2, 0, 1).unsqueeze(0)  # (1, 3, H, W)
        sim_depth = sim_depth.to(self.device)
        sim_rgb = sim_rgb.to(self.device)
        projected_occupancy = self._get_depth_projection(sim_rgb, sim_depth)
        projected_occupancy = asnumpy(rearrange(projected_occupancy, "() c h w -> h w c"))
        # Get mesh occupancy
        mesh_occupancy = self._get_mesh_occupancy(episode, agent_state)
        grown_map = grow_projected_map(
            projected_occupancy, mesh_occupancy, self._region_growing_iterations,
        )
        return grown_map

    def get_observation(self, *args: Any, observations, episode, **kwargs: Any):
        agent_state = self._sim.get_agent_state()
        if self.config.GT_TYPE == "grown_occupancy":
            sim_depth = observations["depth"]
            sim_rgb = observations["rgb"]
            ego_map_gt_anticipated = self._get_grown_depth_projection(
                episode, agent_state, sim_depth, sim_rgb
            )
        elif self.config.GT_TYPE == "full_occupancy":
            ego_map_gt_anticipated = self._get_mesh_occupancy(episode, agent_state,)
        elif self.config.GT_TYPE == "wall_occupancy":
            sim_rgb = observations["rgb"]
            sim_depth = observations["depth"]
            sim_depth = asnumpy(sim_depth)  # (H, W, 1)
            sim_rgb = asnumpy(sim_rgb)  # (H, W, 3)
            sim_depth = torch.from_numpy(sim_depth).squeeze(2).unsqueeze(0)  # (1, H, W)
            sim_rgb = torch.from_numpy(sim_rgb).permute(2, 0, 1).unsqueeze(0)  # (1, 3, H, W)
            sim_depth = sim_depth.to(self.device)
            sim_rgb = sim_rgb.to(self.device)
            full_occupancy = self._get_seen_occupancy(episode, agent_state,)
            wall_occupancy = self._get_wall_occupancy(episode, agent_state,)

            # Invalid points are zeros
            wall_top_down = ((1 - wall_occupancy[..., 0]).T).astype(np.uint8)
            current_mask = np.zeros_like(wall_top_down)
            current_point = np.array(
                [(wall_top_down.shape[0] + 1) // 2, (wall_top_down.shape[1] - 1),]
            )
            current_angle = -np.radians(90)

            current_mask = fog_of_war.reveal_fog_of_war(
                wall_top_down,
                current_mask,
                current_point,
                current_angle,
                self.config.WALL_FOV,
                max_line_len=100.0,
            ).T

            if self._mask_close:
                npixs = int(current_mask.shape[0] * self.config.MASK_CLOSE_PERC / 100.0)
                current_mask[-npixs :, :] = 0.0

            # Add the GT ego map to this
            ego_map_gt = self._get_depth_projection(sim_rgb, sim_depth)
            ego_map_gt = asnumpy(rearrange(ego_map_gt, "() c h w -> h w c"))
            current_mask = np.maximum(current_mask, ego_map_gt[..., 1])

            dilation_mask = np.ones((5, 5))

            current_mask = cv2.dilate(
                current_mask.astype(np.float32), dilation_mask, iterations=2,
            ).astype(np.float32)

            ego_map_gt_anticipated = full_occupancy * current_mask[:, :, np.newaxis]

        return ego_map_gt_anticipated


@registry.register_sensor(name="CollisionSensor")
class CollisionSensor(Sensor):
    def __init__(self, sim: Simulator, config: Config, *args: Any, **kwargs: Any):
        self._sim = sim
        super().__init__(config=config)

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "collision_sensor"

    def _get_sensor_type(self, *args: Any, **kwargs: Any):
        return SensorTypes.PATH

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        return spaces.Box(low=0.0, high=1.0, shape=(1,), dtype=np.float32,)

    def get_observation(self, *args: Any, observations, episode, **kwargs: Any):

        if self._sim.previous_step_collided:
            return np.array([1.0])
        else:
            return np.array([0.0])


@registry.register_sensor(name="NoisyPointGoalWithGPSCompassSensor")
class NoisyIntegratedPointGoalGPSAndCompassSensor(PointGoalSensor):
    r"""Sensor that integrates PointGoals observations (which are used PointGoal Navigation) and GPS+Compass. Adds noise to the observations at each time-step.

    For the agent in simulator the forward direction is along negative-z.
    In polar coordinate format the angle returned is azimuth to the goal.

    Args:
        sim: reference to the simulator for calculating task observations.
        config: config for the PointGoal sensor. Can contain field for
            GOAL_FORMAT which can be used to specify the format in which
            the pointgoal is specified. Current options for goal format are
            cartesian and polar.

            Also contains a DIMENSIONALITY field which specifes the number
            of dimensions ued to specify the goal, must be in [2, 3]

    Attributes:
        _goal_format: format for specifying the goal which can be done
            in cartesian or polar coordinates.
        _dimensionality: number of dimensions used to specify the goal
    """

    def __init__(self, *args: Any, sim: Simulator, config: Config, **kwargs: Any):
        super().__init__(*args, sim=sim, config=config, **kwargs)

        self.current_episode_id = None
        self._estimated_position = None
        self._estimated_rotation = None

        self._fwd_tn_distr = truncated_normal_noise_distr(
            self.config.FORWARD_MEAN, self.config.FORWARD_VARIANCE, 2
        )

        self._rot_tn_distr = truncated_normal_noise_distr(
            self.config.ROTATION_MEAN, self.config.ROTATION_VARIANCE, 2
        )

    def _get_uuid(self, *args: Any, **kwargs: Any):
        return "noisy_pointgoal_with_gps_compass"

    def _quat_to_xy_heading(self, quat):
        direction_vector = np.array([0, 0, -1])

        heading_vector = quaternion_rotate_vector(quat, direction_vector)

        phi = cartesian_to_polar(-heading_vector[2], heading_vector[0])[1]
        return np.array(phi)

    def _get_noisy_delta(self, agent_state):
        current_agent_position = agent_state.position
        current_agent_rotation = agent_state.rotation

        past_agent_position = self._past_gt_position
        past_agent_rotation = self._past_gt_rotation

        # GT delta
        delta_xz_gt = compute_egocentric_delta(
            past_agent_position,
            past_agent_rotation,
            current_agent_position,
            current_agent_rotation,
        )
        delta_y_gt = current_agent_position[1] - past_agent_position[1]

        # Add noise to D_rho, D_theta
        D_rho, D_phi, D_theta = delta_xz_gt
        D_rho_noisy = D_rho + self._fwd_tn_distr.rvs() * np.sign(D_rho).item()
        D_phi_noisy = D_phi
        D_theta_noisy = D_theta + self._rot_tn_distr.rvs() * np.sign(D_theta).item()

        # Compute noisy delta
        delta_xz_noisy = (D_rho_noisy, D_phi_noisy, D_theta_noisy)
        delta_y_noisy = delta_y_gt

        return delta_xz_noisy, delta_y_noisy

    def _compute_pointgoal(self, source_position, source_rotation, goal_position):
        # This is updated to invert the sign of phi (changing conventions from
        # the original pointgoal definition.)
        direction_vector = goal_position - source_position
        direction_vector_agent = quaternion_rotate_vector(
            source_rotation.inverse(), direction_vector
        )

        if self._goal_format == "POLAR":
            if self._dimensionality == 2:
                rho, phi_orig = cartesian_to_polar(
                    -direction_vector_agent[2], direction_vector_agent[0]
                )
                phi = -phi_orig
                return np.array([rho, -phi], dtype=np.float32)
            else:
                _, phi_orig = cartesian_to_polar(
                    -direction_vector_agent[2], direction_vector_agent[0]
                )
                phi = -phi_orig
                theta = np.arccos(
                    direction_vector_agent[1] / np.linalg.norm(direction_vector_agent)
                )
                rho = np.linalg.norm(direction_vector_agent)

                return np.array([rho, -phi, theta], dtype=np.float32)
        else:
            if self._dimensionality == 2:
                return np.array(
                    [-direction_vector_agent[2], direction_vector_agent[0]],
                    dtype=np.float32,
                )
            else:
                return direction_vector_agent

    def get_observation(self, *args: Any, observations, episode, **kwargs: Any):
        agent_state = self._sim.get_agent_state()
        episode_id = (episode.episode_id, episode.scene_id)
        # At the start of a new episode, reset pose estimate
        if self.current_episode_id != episode_id:
            self.current_episode_id = episode_id
            # Initialize with the ground-truth position, rotation
            self._estimated_position = agent_state.position
            self._estimated_rotation = agent_state.rotation
            self._past_gt_position = agent_state.position
            self._past_gt_rotation = agent_state.rotation

        # Compute noisy delta
        delta_xz_noisy, delta_y_noisy = self._get_noisy_delta(agent_state)

        # Update past gt states
        self._past_gt_position = agent_state.position
        self._past_gt_rotation = agent_state.rotation

        # Update noisy pose estimates
        (self._estimated_position, self._estimated_rotation) = compute_updated_pose(
            self._estimated_position,
            self._estimated_rotation,
            delta_xz_noisy,
            delta_y_noisy,
        )

        agent_position = self._estimated_position
        rotation_world_agent = self._estimated_rotation
        goal_position = np.array(episode.goals[0].position, dtype=np.float32)

        return self._compute_pointgoal(
            agent_position, rotation_world_agent, goal_position
        )
