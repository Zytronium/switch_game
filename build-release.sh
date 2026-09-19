#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/.release-build"
WHEEL_DIR="$BUILD_DIR/wheels"
PYINSTALLER_DIST_DIR="$BUILD_DIR/dist"
RELEASE_DIR="$ROOT_DIR/release"

# PyInstaller bundles the Python interpreter, but it does not make native
# dependencies portable.  Building on a newer host therefore produces an
# executable that requires that host's newer glibc.  Use Debian 12 by default
# so releases have a stable glibc 2.36 baseline instead of inheriting the
# builder's environment.  If no container runtime is installed, continue with
# a host build rather than making the build command unusable; that artifact is
# labeled with the host glibc version below and is only suitable for compatible
# systems.
if [[ "${IN_RELEASE_CONTAINER:-0}" != "1" && "${LOCAL_BUILD:-0}" != "1" ]]; then
    container_runtime="${CONTAINER_RUNTIME:-}"
    if [[ -z "$container_runtime" ]]; then
        if command -v docker >/dev/null 2>&1; then
            container_runtime="docker"
        elif command -v podman >/dev/null 2>&1; then
            container_runtime="podman"
        else
            echo "warning: docker or podman is unavailable; building for this machine's glibc" >&2
            echo "         install Docker or Podman for a portable Debian 12 release" >&2
            LOCAL_BUILD=1
        fi
    fi

    if [[ "${LOCAL_BUILD:-0}" != "1" ]]; then
        host_uid="$(id -u)"
        host_gid="$(id -g)"
        container_runtime_args=()
        if [[ "$container_runtime" == "podman" ]]; then
            # Rootless overlay storage is unsupported when the home directory
            # is on Btrfs.  Use an isolated VFS store so this build does not
            # alter or conflict with the user's normal Podman database.
            podman_storage_root="$(mktemp -d "${TMPDIR:-/tmp}/switch-game-podman.XXXXXX")"
            container_runtime_args=(
                --root "$podman_storage_root"
                --storage-driver=vfs
            )
            trap 'rm -rf "$podman_storage_root"' EXIT
        fi
        "$container_runtime" "${container_runtime_args[@]}" run --rm \
            --network=host \
            -v "$ROOT_DIR:/workspace" \
            -w /workspace \
            rust:1.86-bookworm \
            bash -c 'set -euo pipefail
                export DEBIAN_FRONTEND=noninteractive
                apt-get update
                apt-get install -y --no-install-recommends \
                    ca-certificates build-essential libpython3.11 python3 python3-venv
                IN_RELEASE_CONTAINER=1 ./build-release.sh'
        "$container_runtime" "${container_runtime_args[@]}" run --rm \
            --network=host \
            -v "$ROOT_DIR:/workspace" \
            -w /workspace \
            rust:1.86-bookworm \
            bash -c "chown -R ${host_uid}:${host_gid} /workspace/.release-build /workspace/release 2>/dev/null || true"
        exit 0
    fi
fi

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
(cd "$RELEASE_DIR" && sha256sum "$artifact_name" > "$artifact_name.sha256")

echo "Built: $artifact_path"
echo "Checksum: $artifact_path.sha256"