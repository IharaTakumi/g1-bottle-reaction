"""Lazy SDK boundary for the Python 3.8 PC2 commissioning runtime.

No application imports, ROS dependency, installation or network on import.
"""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading

DDS_XML = '''<CycloneDDS><Domain Id="0"><General><Interfaces>
<NetworkInterface name="eth0"/></Interfaces></General>
<Tracing><Verbosity>none</Verbosity></Tracing></Domain></CycloneDDS>'''
_lock = threading.Lock()


def require_pc2():
    if platform.system() != 'Linux' or platform.machine() not in ('aarch64', 'arm64'):
        raise RuntimeError('DDS/RPC requires Linux aarch64 PC2; Desktop execution is disabled')
    result = subprocess.run(['ip', '-j', '-4', 'address', 'show', 'dev', 'eth0'],
                            check=True, capture_output=True, text=True, timeout=5)
    interfaces = json.loads(result.stdout)
    if not any(a.get('local') == '192.168.123.164' and a.get('prefixlen') == 24
               for dev in interfaces for a in dev.get('addr_info', [])):
        raise RuntimeError('PC2 eth0=192.168.123.164/24 is required; no network configuration changed')


def configure_sdk_path():
    path = Path(os.environ.get('G1_SDK_PATH', '/home/unitree/unitree_sdk2_python')).resolve()
    if not (path / 'unitree_sdk2py/__init__.py').is_file():
        raise RuntimeError('Existing SDK checkout not found: ' + str(path))
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return path


def require_runtime():
    require_pc2()
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Use the dedicated .venv-robot interpreter for record/RPC')


class UnitreeSdkRuntime:
    def load_readonly_slam_types(self):
        configure_sdk_path()
        from unitree_sdk2py.idl.nav_msgs.msg.dds_ import Odometry_
        from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
        return Odometry_, String_

    def load_readonly_low_state_type(self):
        configure_sdk_path()
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        return LowState_

    def create_slam_single_call_client(self, interface, timeout, api_id):
        if interface != 'eth0' or api_id not in (1801, 1802, 1804):
            raise ValueError('Only PC2 eth0 and single SLAM operations are supported')
        require_runtime()
        configure_sdk_path()
        from unitree_sdk2py.core import channel
        from unitree_sdk2py.rpc.client import Client
        # SDK defaults trace to /tmp. Override in this process only; no SDK file edit.
        with _lock:
            previous = channel.ChannelConfigHasInterface
            try:
                channel.ChannelConfigHasInterface = DDS_XML
                channel.ChannelFactoryInitialize(0, 'eth0')
            finally:
                channel.ChannelConfigHasInterface = previous
        client = Client('slam_operate', False)
        client.SetTimeout(timeout)
        client._SetApiVerson('1.0.0.1')
        client._RegistApi(api_id, 0)
        return client
