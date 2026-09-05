from __future__ import annotations

# Verified against both official Unitree 29-DoF definitions:
# - unitree_mujoco/unitree_robots/g1/g1_joint_index_dds.md
# - unitree_ros/robots/g1_description/g1_29dof_rev_1_0.xml
# GMR's unitree_g1 MJCF uses the same motor names. The cleaner still selects
# columns by the names emitted by GMR rather than assuming their array indices.
G1_29DOF_JOINT_NAMES: tuple[str, ...] = (
    "left_hip_pitch_joint",
    "left_hip_roll_joint",
    "left_hip_yaw_joint",
    "left_knee_joint",
    "left_ankle_pitch_joint",
    "left_ankle_roll_joint",
    "right_hip_pitch_joint",
    "right_hip_roll_joint",
    "right_hip_yaw_joint",
    "right_knee_joint",
    "right_ankle_pitch_joint",
    "right_ankle_roll_joint",
    "waist_yaw_joint",
    "waist_roll_joint",
    "waist_pitch_joint",
    "left_shoulder_pitch_joint",
    "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint",
    "left_elbow_joint",
    "left_wrist_roll_joint",
    "left_wrist_pitch_joint",
    "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint",
    "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",
    "right_elbow_joint",
    "right_wrist_roll_joint",
    "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
)

G1_DDS_INDEX_BY_NAME = {
    name: index for index, name in enumerate(G1_29DOF_JOINT_NAMES)
}

UPPER_BODY_JOINT_NAMES: tuple[str, ...] = G1_29DOF_JOINT_NAMES[12:]
G1_ARM_JOINT_NAMES: tuple[str, ...] = G1_29DOF_JOINT_NAMES[15:]

# Ranges in radians from Unitree's official g1_29dof_rev_1_0.xml. These model
# limits are not a substitute for runtime validation against the actual robot.
G1_UPPER_BODY_JOINT_LIMITS: dict[str, tuple[float, float]] = {
    "waist_yaw_joint": (-2.618, 2.618),
    "waist_roll_joint": (-0.52, 0.52),
    "waist_pitch_joint": (-0.52, 0.52),
    "left_shoulder_pitch_joint": (-3.0892, 2.6704),
    "left_shoulder_roll_joint": (-1.5882, 2.2515),
    "left_shoulder_yaw_joint": (-2.618, 2.618),
    "left_elbow_joint": (-1.0472, 2.0944),
    "left_wrist_roll_joint": (-1.97222, 1.97222),
    "left_wrist_pitch_joint": (-1.61443, 1.61443),
    "left_wrist_yaw_joint": (-1.61443, 1.61443),
    "right_shoulder_pitch_joint": (-3.0892, 2.6704),
    "right_shoulder_roll_joint": (-2.2515, 1.5882),
    "right_shoulder_yaw_joint": (-2.618, 2.618),
    "right_elbow_joint": (-1.0472, 2.0944),
    "right_wrist_roll_joint": (-1.97222, 1.97222),
    "right_wrist_pitch_joint": (-1.61443, 1.61443),
    "right_wrist_yaw_joint": (-1.61443, 1.61443),
}


def motion_group(joint_name: str) -> str:
    if joint_name.startswith("waist_"):
        return "waist"
    if "shoulder" in joint_name:
        return "shoulder"
    if "elbow" in joint_name:
        return "elbow"
    if "wrist" in joint_name:
        return "wrist"
    raise KeyError(f"Unknown G1 upper-body joint: {joint_name}")
