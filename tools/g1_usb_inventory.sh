#!/bin/sh
# Read-only inventory, intended for: ssh g1 'sh -s' < this-file
# No camera capture, DDS, package installs, robot commands or file writes.
set -u

section() { printf '\n%s\n' "$1"; }

section 'Host / OS / network (identify the computer; do not infer USB ownership from IP)'
hostname
uname -a
if [ -r /etc/os-release ]; then sed -n '1,8p' /etc/os-release; fi
id
command -v ip >/dev/null 2>&1 && ip -br address

section 'Existing tools'
for tool in python3 ffmpeg ffprobe gst-launch-1.0 gst-inspect-1.0 v4l2-ctl lsusb fuser timeout; do
    command -v "$tool" || true
done

section 'USB inventory'
if command -v lsusb >/dev/null 2>&1; then lsusb; fi
for usb in /sys/bus/usb/devices/*; do
    [ -r "$usb/idVendor" ] || continue
    printf '%s ' "$usb"
    for field in idVendor idProduct manufacturer product; do
        [ -r "$usb/$field" ] || continue
        printf '%s=' "$field"
        tr '\n' ' ' < "$usb/$field"
        printf ' '
    done
    printf '\n'
done

section 'Video nodes and stable paths (metadata nodes are not necessarily cameras)'
for entry in /dev/video* /dev/v4l/by-id/* /dev/v4l/by-path/*; do
    [ -e "$entry" ] || continue
    ls -l "$entry"
done
for device in /sys/class/video4linux/*; do
    [ -e "$device" ] || continue
    printf '%s ' "$device"
    [ ! -r "$device/name" ] || tr '\n' ' ' < "$device/name"
    printf '\n'
    readlink -f "$device/device"
done

section 'Existing camera processes (names only; no service changes)'
ps -eo pid,comm | awk 'NR == 1 || /videohub|ffmpeg|gst-launch|teleimager|image_server/'
if command -v fuser >/dev/null 2>&1; then
    for device in /dev/video*; do
        [ -e "$device" ] || continue
        fuser -v "$device" 2>&1 || true
    done
fi

section 'V4L2 capabilities / formats (query only, bounded, no streaming)'
if command -v v4l2-ctl >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
    for device in /dev/video*; do
        [ -e "$device" ] || continue
        printf '\nDevice: %s\n' "$device"
        timeout 5 v4l2-ctl -d "$device" --info --list-formats-ext 2>&1 || true
    done
else
    printf '%s\n' 'v4l2-ctl or timeout unavailable; skipped. Do not install system packages.'
fi

section 'Existing video tools / Python module availability (bounded, no install)'
if command -v timeout >/dev/null 2>&1; then
    if command -v ffmpeg >/dev/null 2>&1; then timeout 5 ffmpeg -version 2>&1 | head -n 4; fi
    if command -v gst-launch-1.0 >/dev/null 2>&1; then timeout 5 gst-launch-1.0 --version; fi
    if command -v gst-inspect-1.0 >/dev/null 2>&1; then
        for plugin in v4l2src jpegparse rtpjpegpay udpsink; do
            if GST_REGISTRY=/dev/null GST_REGISTRY_UPDATE=no timeout 5 gst-inspect-1.0 "$plugin" >/dev/null 2>&1; then
                printf '%s available\n' "$plugin"
            else
                printf '%s unavailable or inspection failed\n' "$plugin"
            fi
        done
    fi
    if command -v python3 >/dev/null 2>&1; then
        PYTHONDONTWRITEBYTECODE=1 timeout 5 python3 -B -c 'import sys, importlib.util; print("Python:", sys.executable, sys.version); print("OpenCV module:", importlib.util.find_spec("cv2"))'
    fi
fi

section 'END: inventory only. No camera was opened for streaming; no robot commands were sent.'
