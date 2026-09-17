"""Pure-Python PointCloud2 reduction for the PC2 read-only wander source."""

import math
import struct


SECTORS = ('left', 'front_left', 'front', 'front_right', 'right')
_POINT_FIELD_FORMATS = {
    1: 'b',   # INT8
    2: 'B',   # UINT8
    3: 'h',   # INT16
    4: 'H',   # UINT16
    5: 'i',   # INT32
    6: 'I',   # UINT32
    7: 'f',   # FLOAT32
    8: 'd',   # FLOAT64
}


def source_stamp_seconds(header):
    stamp = getattr(header, 'stamp', None)
    if stamp is None:
        return None
    return float(stamp.sec) + float(stamp.nanosec) / 1e9


def yaw_from_quaternion(value):
    x = float(value.x)
    y = float(value.y)
    z = float(value.z)
    w = float(value.w)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def odom_payload(sample):
    pose = sample.pose.pose
    return {
        'x': float(pose.position.x),
        'y': float(pose.position.y),
        'yaw': yaw_from_quaternion(pose.orientation),
        'source_timestamp': source_stamp_seconds(sample.header),
    }


def decode_xyz(sample):
    fields = {field.name: field for field in sample.fields}
    if not all(name in fields for name in ('x', 'y', 'z')):
        raise ValueError('PointCloud2 requires x/y/z fields')
    if sample.point_step <= 0 or sample.row_step < sample.width * sample.point_step:
        raise ValueError('PointCloud2 layout is inconsistent')
    if len(sample.data) != sample.row_step * sample.height:
        raise ValueError('PointCloud2 payload length is inconsistent')
    endian = '>' if sample.is_bigendian else '<'
    unpackers = {}
    for name in ('x', 'y', 'z'):
        field = fields[name]
        if field.count != 1 or field.datatype not in _POINT_FIELD_FORMATS:
            raise ValueError('PointCloud2 x/y/z fields must be scalar numeric values')
        unpackers[name] = struct.Struct(endian + _POINT_FIELD_FORMATS[field.datatype])
        if field.offset + unpackers[name].size > sample.point_step:
            raise ValueError('PointCloud2 field exceeds point_step')

    data = bytes(sample.data)
    for row in range(sample.height):
        row_start = row * sample.row_step
        for column in range(sample.width):
            point_start = row_start + column * sample.point_step
            yield tuple(
                unpackers[name].unpack_from(data, point_start + fields[name].offset)[0]
                for name in ('x', 'y', 'z')
            )


def _axis(point, specification):
    index = {'x': 0, 'y': 1, 'z': 2}[specification.lstrip('+-')]
    sign = -1.0 if specification.startswith('-') else 1.0
    return float(point[index]) * sign


def reduce_points(points, config, debug=False):
    axes = [config[name].lstrip('+-') for name in ('forward_axis', 'left_axis', 'up_axis')]
    if sorted(axes) != ['x', 'y', 'z']:
        raise ValueError('forward/left/up axes must use x, y, z exactly once')
    edges = tuple(float(value) for value in config['sector_edges_degrees'])
    if len(edges) != 6 or tuple(sorted(edges)) != edges:
        raise ValueError('sector_edges_degrees must contain six ascending values')

    nearest = {name: float(config['max_range_m']) for name in SECTORS}
    counts = {name: 0 for name in SECTORS}
    nearest_xyz = {name: None for name in SECTORS}
    raw_min = [float('inf')] * 3
    raw_max = [float('-inf')] * 3
    usable = 0
    ordered = ('right', 'front_right', 'front', 'front_left', 'left')
    for point in points:
        if len(point) < 3 or not all(math.isfinite(float(value)) for value in point[:3]):
            continue
        for index in range(3):
            raw_min[index] = min(raw_min[index], float(point[index]))
            raw_max[index] = max(raw_max[index], float(point[index]))
        forward = _axis(point, config['forward_axis'])
        left = _axis(point, config['left_axis'])
        up = _axis(point, config['up_axis'])
        distance = math.hypot(forward, left)
        if not (float(config['min_height_m']) <= up <= float(config['max_height_m'])):
            continue
        if not (float(config['min_range_m']) <= distance <= float(config['max_range_m'])):
            continue
        self_masked = (
            -float(config['self_mask_back_m']) <= forward <= float(config['self_mask_front_m'])
            and abs(left) <= float(config['self_mask_half_width_m'])
        )
        if self_masked or forward <= 0:
            continue
        angle = math.degrees(math.atan2(left, forward))
        for index, name in enumerate(ordered):
            upper_match = angle <= edges[index + 1] if index == 4 else angle < edges[index + 1]
            if angle >= edges[index] and upper_match:
                counts[name] += 1
                usable += 1
                if distance < nearest[name]:
                    nearest[name] = distance
                    nearest_xyz[name] = [float(value) for value in point[:3]]
                break

    valid = usable >= int(config['minimum_valid_points'])
    result = {
        'obstacle_snapshot': nearest,
        'valid_points': counts,
        'valid': valid,
        'invalid_reason': None if valid else 'only %d usable points' % usable,
    }
    if debug:
        result['diagnostics'] = {
            'raw_xyz_min': None if raw_min[0] == float('inf') else raw_min,
            'raw_xyz_max': None if raw_max[0] == float('-inf') else raw_max,
            'sector_point_count': counts,
            'nearest_point_xyz': nearest_xyz,
        }
    return result
