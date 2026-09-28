"""Read-only SDK field extraction. No clock conversion or source-freshness claim."""
import math


def lowstate_evidence(sample):
    try:
        gyro = tuple(sample.imu_state.gyroscope)
        tick = sample.tick
        if (len(gyro) != 3 or not all(type(v) in (int, float) and math.isfinite(v) for v in gyro)
                or type(tick) is not int or not 0 <= tick < 2**32):
            raise ValueError("invalid LowState evidence")
        return {"imu_gyro": gyro, "imu_tick": tick}
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"imu_gyro": None, "imu_tick": None}


def odom_evidence(sample):
    try:
        stamp = sample.header.stamp
        if (type(stamp.sec) is not int or type(stamp.nanosec) is not int or
                not 0 <= stamp.sec < 2**31 or not 0 <= stamp.nanosec < 10**9):
            raise ValueError("invalid odometry stamp")
        value = stamp.sec * 10**9 + stamp.nanosec
        return {"odom_stamp_ns": value if value > 0 else None}
    except (AttributeError, TypeError, ValueError):
        return {"odom_stamp_ns": None}
