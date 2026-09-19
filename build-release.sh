#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/.release-build"
WHEEL_DIR="$BUILD_DIR/wheels"
PYINSTALLER_DIST_DIR="$BUILD_DIR/dist"
RELEASE_DIR="$ROOT_DIR/release"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required to build the release" >&2
    exit 1
fi

if ! command -v cargo >/dev/null 2>&1; then
    echo "error: cargo is required to build the release" >&2
    exit 1
fi

rm -rf "$BUILD_DIR"
mkdir -p "$WHEEL_DIR" "$PYINSTALLER_DIST_DIR" "$RELEASE_DIR"

python3 -m venv "$BUILD_DIR/venv"
# shellcheck disable=SC1091
source "$BUILD_DIR/venv/bin/activate"

python -m pip install --upgrade pip
python -m pip install 'maturin>=1.0,<2.0' 'pyinstaller>=6.0,<7.0'

python -m maturin build \
    --release \
    --manifest-path "$ROOT_DIR/net/Cargo.toml" \
    --out "$WHEEL_DIR"

wheel_paths=("$WHEEL_DIR"/*.whl)
if [[ ! -f "${wheel_paths[0]}" || "${#wheel_paths[@]}" -ne 1 ]]; then
    echo "error: Maturin did not produce a wheel" >&2
    exit 1
fi
wheel_path="${wheel_paths[0]}"
python -m pip install --force-reinstall --no-deps "$wheel_path"

python -m PyInstaller \
    --clean \
    --noconfirm \
    --distpath "$PYINSTALLER_DIST_DIR" \
    --workpath "$BUILD_DIR/work" \
    "$ROOT_DIR/switch-n-hack.spec"

architecture="$(uname -m)"
glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
if [[ -z "$glibc_version" ]]; then
    glibc_version="unknown"
fi
artifact_name="switch-n-hack-linux-${architecture}-glibc-${glibc_version}"
artifact_path="$RELEASE_DIR/$artifact_name"

cp "$PYINSTALLER_DIST_DIR/switch-n-hack" "$artifact_path"
sha256sum "$artifact_path" > "$artifact_path.sha256"

echo "Built: $artifact_path"
echo "Checksum: $artifact_path.sha256"