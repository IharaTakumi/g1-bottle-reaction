import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/g1-pc2-mapping-once.py'


def load_builder():
    spec = importlib.util.spec_from_file_location(
        'build_mapping_once', ROOT / 'scripts/build-pc2-mapping-once.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mapping_client_is_python38_and_has_one_rpc_call_only():
    tree = ast.parse(SCRIPT.read_text(), feature_version=(3, 8))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    rpc_calls = [node for node in calls if isinstance(node.func, ast.Attribute)
                 and node.func.attr == '_Call']
    assert len(rpc_calls) == 1
    assert isinstance(rpc_calls[0].args[0], ast.Constant)
    assert rpc_calls[0].args[0].value == 1801
    names = {node.func.id for node in calls if isinstance(node.func, ast.Name)}
    attributes = {node.func.attr for node in calls if isinstance(node.func, ast.Attribute)}
    assert not ({'DataWriter', 'Publisher', 'ChannelPublisher'} & (names | attributes))
    source = SCRIPT.read_text()
    for forbidden in ('1802', '1804', 'LowCmd', 'Motion', 'Navigation', 'arm_sdk', 'armsdk'):
        assert forbidden not in source


def test_stdin_mapping_client_is_standalone_and_preserves_guards():
    source = load_builder().source()
    tree = ast.parse(source, feature_version=(3, 8))
    assert 'from robot_side' not in source
    assert not any(isinstance(node, ast.Name) and node.id == '__file__'
                   for node in ast.walk(tree))
    assert source.count('client._Call(1801,') == 1
    assert "ChannelFactoryInitialize(0, 'eth0')" in source
    assert "args.execute != args.enable_real_robot" in source
