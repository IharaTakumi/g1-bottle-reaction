#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "Usage: setup_wsl.sh ENVIRONMENT_ROOT GVHMR_ROOT GMR_ROOT [--skip-downloads]" >&2
    exit 2
fi

environment_root=$1
gvhmr_root=$2
gmr_root=$3
skip_downloads=${4:-}
micromamba_version="2.8.1-0"
micromamba_sha256="9689782d863c05a1bf5d2d371ba527104e7a4eb4310c1637d8653b751aed9c82"
micromamba="$environment_root/bin/micromamba"
mamba_root="$environment_root/mamba"
gvhmr_env="$environment_root/envs/gvhmr"
gmr_env="$environment_root/envs/gmr"

mkdir -p "$environment_root/bin" "$environment_root/envs" "$environment_root/locks"
if [[ ! -x "$micromamba" ]]; then
    url="https://github.com/mamba-org/micromamba-releases/releases/download/$micromamba_version/micromamba-linux-64"
    tmp="$micromamba.download"
    curl --fail --location --retry 3 "$url" --output "$tmp"
    printf '%s  %s\n' "$micromamba_sha256" "$tmp" | sha256sum --check --status
    chmod 0755 "$tmp"
    mv "$tmp" "$micromamba"
fi

export MAMBA_ROOT_PREFIX="$mamba_root"

if [[ ! -x "$gvhmr_env/bin/python" ]]; then
    "$micromamba" create -y -p "$gvhmr_env" -c conda-forge \
        python=3.10 pip=24.2 ffmpeg git make gcc_linux-64 gxx_linux-64 libstdcxx-ng
fi
"$micromamba" run -p "$gvhmr_env" python -m pip install --disable-pip-version-check setuptools==80.9.0
"$micromamba" run -p "$gvhmr_env" python -m pip install --disable-pip-version-check \
    --requirement "$gvhmr_root/requirements.txt"
"$micromamba" run -p "$gvhmr_env" python -m pip install --disable-pip-version-check --editable "$gvhmr_root"

if [[ ! -x "$gmr_env/bin/python" ]]; then
    "$micromamba" create -y -p "$gmr_env" -c conda-forge \
        python=3.10 pip=24.2 ffmpeg git make gcc_linux-64 gxx_linux-64 libstdcxx-ng
fi
"$micromamba" run -p "$gmr_env" python -m pip install --disable-pip-version-check \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.3.0+cpu torchvision==0.18.0+cpu
"$micromamba" run -p "$gmr_env" python -m pip install --disable-pip-version-check numpy==1.26.4
"$micromamba" run -p "$gmr_env" python -m pip install --disable-pip-version-check --editable "$gmr_root"
"$micromamba" run -p "$gmr_env" python -m pip install --disable-pip-version-check \
    numpy==1.26.4 opencv-python==4.11.0.86

make_python_wrapper() {
    local environment=$1
    local wrapper="$environment/bin/reaction-python"
    printf '%s\n' \
        '#!/usr/bin/env sh' \
        'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)' \
        'export PATH="$script_dir:$PATH"' \
        'exec "$script_dir/python" "$@"' > "$wrapper"
    chmod 0755 "$wrapper"
}

make_python_wrapper "$gvhmr_env"
make_python_wrapper "$gmr_env"

"$micromamba" run -p "$gvhmr_env" python -m pip freeze --all > "$environment_root/locks/gvhmr.txt"
"$micromamba" run -p "$gmr_env" python -m pip freeze --all > "$environment_root/locks/gmr.txt"

download_checkpoint() {
    local relative=$1
    local url=$2
    local destination="$gvhmr_root/inputs/checkpoints/$relative"
    if [[ -s "$destination" ]]; then
        echo "[OK] $destination"
        return
    fi
    mkdir -p "$(dirname "$destination")"
    echo "[DOWNLOAD] $relative"
    curl --fail --location --retry 3 --continue-at - "$url" --output "$destination"
}

if [[ "$skip_downloads" != "--skip-downloads" ]]; then
    # These mirrors are the exact URLs used by upstream's official Colab notebook.
    download_checkpoint "gvhmr/gvhmr_siga24_release.ckpt" \
        "https://huggingface.co/camenduru/GVHMR/resolve/main/gvhmr/gvhmr_siga24_release.ckpt"
    download_checkpoint "hmr2/epoch=10-step=25000.ckpt" \
        "https://huggingface.co/camenduru/GVHMR/resolve/main/hmr2/epoch%3D10-step%3D25000.ckpt"
    download_checkpoint "vitpose/vitpose-h-multi-coco.pth" \
        "https://huggingface.co/camenduru/GVHMR/resolve/main/vitpose/vitpose-h-multi-coco.pth"
    download_checkpoint "yolo/yolov8x.pt" \
        "https://huggingface.co/camenduru/GVHMR/resolve/main/yolo/yolov8x.pt"
fi

echo
echo "Licensed body models are never downloaded automatically. Required files:"
echo "  $gvhmr_root/inputs/checkpoints/body_models/smplx/SMPLX_NEUTRAL.npz"
echo "  $gvhmr_root/inputs/checkpoints/body_models/smpl/SMPL_NEUTRAL.pkl"
echo "  $gmr_root/assets/body_models/smplx/SMPLX_NEUTRAL.npz"
echo "Download them under the SMPL/SMPL-X license and rerun setup.ps1."
