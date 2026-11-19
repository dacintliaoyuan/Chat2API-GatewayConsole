#!/usr/bin/env bash
# Build the Linux Rust binaries in a disposable container WITHOUT poisoning the
# host `rust/target`.
#
# Cargo bakes `CARGO_MANIFEST_DIR` into the artifacts. A container build that
# writes into the host `rust/target` leaves `/src/...` paths in the cache; the next
# Windows `cargo test` reuses those artifacts and fails on files that only exist
# inside the container (for example `ContractFixture::load_default()`).
#
# This script mounts the source read-only and points the container at its own target
# directory, then copies only the requested binaries back to the host.
set -euo pipefail

source_dir="."
output_dir=""
image="rust:1.85-bookworm"
packages=()
while [ $# -gt 0 ]; do
  case "$1" in
    --source) source_dir="$2"; shift 2 ;;
    --output) output_dir="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --package) packages+=("$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

source_dir=$(cd "$source_dir" && pwd)
output_dir=${output_dir:-"$source_dir/tmp/linux-rust"}
target_dir="$source_dir/tmp/linux-target"
mkdir -p "$output_dir" "$target_dir"

if [ ${#packages[@]} -eq 0 ]; then
  packages=(um-gateway um-console)
fi
package_args=()
for package in "${packages[@]}"; do
  package_args+=(--package "$package")
done

MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$source_dir:/src:ro" \
  -v "$target_dir:/target" \
  -e CARGO_TARGET_DIR=/target \
  -w /src/rust \
  "$image" \
  bash -c "export PATH=/usr/local/cargo/bin:\$PATH; cargo build --release --locked ${package_args[*]}"

# The candidate binary names: um-gateway -> um-gateway-candidate,
# um-console -> um-console-candidate. Any other package keeps its own name.
for package in "${packages[@]}"; do
  binary="$package-candidate"
  if [ ! -f "$target_dir/release/$binary" ]; then
    binary="$package"
  fi
  if [ ! -f "$target_dir/release/$binary" ]; then
    echo "missing built binary for $package under $target_dir/release" >&2
    exit 1
  fi
  cp "$target_dir/release/$binary" "$output_dir/$binary"
  chmod 0755 "$output_dir/$binary"
done

echo "output $output_dir"
sha256sum "$output_dir"/*
