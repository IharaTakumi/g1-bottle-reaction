"""Single source of truth for the attended Hackathon patrol profile."""

FORWARD_SPEED_M_S = 0.50
# Operator may raise the speed up to this; only 0.50 is validated on the real G1.
MAX_FORWARD_SPEED_M_S = 0.80
# Course length (end to end) the operator may choose; the default is 4.0 m.
COURSE_LENGTH_RANGE_M = (1.0, 5.0)
COURSE_LENGTH_M = 4.0
LIDAR_STOP_DISTANCE_M = 0.50
TELEMETRY_RECOVERY_S = 2.0
REACTION_COMPLETION_TIMEOUT_S = 35.0
