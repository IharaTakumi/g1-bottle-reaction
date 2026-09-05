#!/usr/bin/env bash
# Imports, interface inspection and ICMP only. No DDS participant or robot RPC.
set -uo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/activate-g1-env.sh" || exit 1
status=0
python - <<'PY' || status=1
import importlib
import importlib.metadata
import os
import sys
print('Python:', sys.version)
print('Executable:', sys.executable)
print('venv:', sys.prefix, 'VIRTUAL_ENV:', os.environ.get('VIRTUAL_ENV'))
assert sys.prefix == os.environ['VIRTUAL_ENV']
for name, dist in [('unitree_sdk2py.core.channel', 'unitree_sdk2py'), ('cyclonedds', 'cyclonedds')]:
    module = importlib.import_module(name)
    print(name, importlib.metadata.version(dist), module.__file__)
PY
ip -br link show dev "$G1_NETWORK_INTERFACE" || status=1
ip -4 address show dev "$G1_NETWORK_INTERFACE" || status=1
for host in 192.168.123.161 192.168.123.164; do
  ping -n -I "$G1_NETWORK_INTERFACE" -c 2 -W 2 "$host" || status=1
done
exit "$status"
