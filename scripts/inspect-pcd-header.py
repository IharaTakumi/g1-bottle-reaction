#!/usr/bin/env python3
"""Inspect one explicit absolute PCD path, without reading point data or networking.

Run only where the confirmed file is already accessible. This script does not
connect to a robot, search directories, copy maps or change file contents.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat


def inspect(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('An evidence-backed absolute path is required')
    path = path.resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Only regular files are supported')
        header = bytearray()
        line = bytearray()
        for _ in range(65536):
            byte = stream.read(1)
            if not byte:
                break
            header.extend(byte)
            line.extend(byte)
            if byte == b'\n':
                parts = bytes(line).strip().split()
                if parts and parts[0] == b'DATA':
                    if len(parts) != 2 or parts[1] not in {b'ascii', b'binary', b'binary_compressed'}:
                        raise ValueError('Invalid PCD DATA declaration')
                    return {'filename': path.name, 'absolute_path': str(path),
                            'size_bytes': metadata.st_size,
                            'modified_time_utc': datetime.fromtimestamp(metadata.st_mtime, timezone.utc).isoformat(),
                            'pcd_header': header.decode('ascii')}
                line.clear()
        raise ValueError('PCD DATA header not found within 64 KiB; point data not read')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('absolute_path')
    args = parser.parse_args()
    print(json.dumps(inspect(args.absolute_path), indent=2))


if __name__ == '__main__':
    main()
