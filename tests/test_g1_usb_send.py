from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("g1_usb_send", ROOT / "tools/g1_usb_send.py")
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def video_link(root, name, target):
    target.touch()
    link = root / name
    link.symlink_to(target)
    return link


def test_auto_selects_only_non_realsense_index_zero(tmp_path):
    video_link(tmp_path, "usb-Intel_RealSense-video-index0", tmp_path / "video4")
    video_link(tmp_path, "usb-SunplusIT_Full_HD-video-index0", tmp_path / "video6")
    video_link(tmp_path, "usb-SunplusIT_Full_HD-video-index1", tmp_path / "video7")
    assert MODULE.detect_device("auto", tmp_path) == tmp_path / "video6"


def test_auto_refuses_ambiguous_or_missing_external_camera(tmp_path):
    with pytest.raises(RuntimeError, match="exactly one"):
        MODULE.detect_device("auto", tmp_path)
    video_link(tmp_path, "usb-Camera_A-video-index0", tmp_path / "video6")
    video_link(tmp_path, "usb-Camera_B-video-index0", tmp_path / "video8")
    with pytest.raises(RuntimeError, match="Camera_A.*Camera_B"):
        MODULE.detect_device("auto", tmp_path)


def test_explicit_device_resolves_symlink(tmp_path):
    link = video_link(tmp_path, "selected", tmp_path / "video9")
    assert MODULE.detect_device(str(link), tmp_path) == tmp_path / "video9"
