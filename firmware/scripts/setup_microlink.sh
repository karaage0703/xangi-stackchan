#!/usr/bin/env bash
set -euo pipefail

readonly MICROLINK_REPOSITORY="https://github.com/CamM2325/microlink.git"
readonly MICROLINK_COMMIT="216da3300f0493b0860247d43f7af5ce29df63a5"
readonly FIRMWARE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly DEPENDENCY_ROOT="${FIRMWARE_ROOT}/.deps"
readonly MICROLINK_ROOT="${DEPENDENCY_ROOT}/microlink"
readonly MANIFEST_ROOT="${FIRMWARE_ROOT}/scripts/microlink-pio"
readonly PATCH_ROOT="${FIRMWARE_ROOT}/scripts/microlink-patches"

mkdir -p "${DEPENDENCY_ROOT}"

if [[ ! -d "${MICROLINK_ROOT}/.git" ]]; then
    if [[ -e "${MICROLINK_ROOT}" ]]; then
        echo "error: ${MICROLINK_ROOT} exists but is not a Git checkout" >&2
        exit 1
    fi
    git clone --no-checkout "${MICROLINK_REPOSITORY}" "${MICROLINK_ROOT}"
    git -C "${MICROLINK_ROOT}" checkout --detach "${MICROLINK_COMMIT}"
fi

actual_commit="$(git -C "${MICROLINK_ROOT}" rev-parse HEAD)"
if [[ "${actual_commit}" != "${MICROLINK_COMMIT}" ]]; then
    echo "error: MicroLink is at ${actual_commit}; expected ${MICROLINK_COMMIT}" >&2
    echo "Move ${MICROLINK_ROOT}, then run this script again." >&2
    exit 1
fi

# MicroLink is an ESP-IDF component. PlatformIO's Arduino library scanner needs
# explicit manifests, especially to avoid compiling ARM-only WireGuard sources.
cp "${MANIFEST_ROOT}/microlink.library.json" \
    "${MICROLINK_ROOT}/components/microlink/library.json"
cp "${MANIFEST_ROOT}/wireguard_lwip.library.json" \
    "${MICROLINK_ROOT}/components/microlink/components/wireguard_lwip/library.json"

for patch in "${PATCH_ROOT}"/*.patch; do
    if git -C "${MICROLINK_ROOT}" apply --ignore-space-change --ignore-whitespace \
        --reverse --check "${patch}" >/dev/null 2>&1; then
        continue
    fi
    git -C "${MICROLINK_ROOT}" apply --ignore-space-change --ignore-whitespace \
        --check "${patch}"
    git -C "${MICROLINK_ROOT}" apply --ignore-space-change --ignore-whitespace "${patch}"
done

echo "MicroLink ${MICROLINK_COMMIT} is ready at ${MICROLINK_ROOT}"
