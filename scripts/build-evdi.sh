#!/usr/bin/env bash
# Build the pinned kernel module and userspace library, without root/install.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
evdi_dir="$repo_dir/.deps/evdi"
evdi_commit=33b7b3c9f7c77157c2b1b0c5a40cebe19b8ba110

mkdir -p "$repo_dir/.deps"
if [[ ! -d "$evdi_dir" ]]; then
    git clone --depth 1 --branch v1.15.1 https://github.com/DisplayLink/evdi.git "$evdi_dir"
fi
if [[ $(git -C "$evdi_dir" rev-parse HEAD) != "$evdi_commit" ]]; then
    echo "Unexpected EVDI checkout in $evdi_dir; expected $evdi_commit" >&2
    exit 1
fi
if [[ -n $(git -C "$evdi_dir" status --porcelain --untracked-files=no) ]]; then
    echo "EVDI source has local modifications; inspect before rebuilding." >&2
    exit 1
fi
# Build module directly: upstream's top-level -Wextra/-Werror flags also apply
# to kernel headers and fail on Linux 7.0 header sign-comparison diagnostics.
make -C "$evdi_dir/library" -j2
make -C "$evdi_dir/module" -j2
cat <<'EOF'

Build complete. To install with DKMS and load the virtual display driver:
  sudo make -C .deps/evdi/module install_dkms
  sudo make -C .deps/evdi/library install PREFIX=/usr/local
  sudo ldconfig
  sudo modprobe evdi initial_device_count=1

Then run the Python daemon as your desktop user.
EOF
