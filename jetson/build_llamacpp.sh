#!/usr/bin/env bash
# Build llama.cpp's llama-server natively with CUDA for the Orin (sm_87).
#
#   ./build_llamacpp.sh [--ref <tag|branch|commit>] [--dir <path>] [--jobs N] [--clean] [--dry-run]
#
# Defaults: --ref master, --dir ~/llama.cpp, --jobs 2 (CUDA template compiles can peak above
# 4 GB RSS each; more jobs than that swap on 8 GB). The built commit is written next to the
# binary (build/bin/agent-house-build.txt) and ends up in every benchmark report.
# Works the same on JetPack 6 (CUDA 12.6) and JetPack 7 (CUDA 13); anything built against one
# CUDA major must be rebuilt after a JetPack upgrade. [UNVERIFIED-ON-HW]
set -euo pipefail

# shellcheck source=jetson/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

REF=master
DIR="$HOME/llama.cpp"
JOBS=2
CLEAN=0
DRY=0
REPO_URL="https://github.com/ggml-org/llama.cpp"

while (($#)); do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift ;;
    --dir) DIR="${2:?--dir needs a value}"; shift ;;
    --jobs) JOBS="${2:?--jobs needs a value}"; shift ;;
    --clean) CLEAN=1 ;;
    --dry-run) DRY=1 ;;
    -h | --help)
      sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

run() {
  if ((DRY)); then
    echo "would run: $*"
  else
    echo "+ $*" >&2
    "$@"
  fi
}

export PATH="/usr/local/cuda/bin:$PATH"
if ((!DRY)); then
  for tool in git cmake nvcc; do
    if ! have "$tool"; then
      echo "$tool not found. Run jetson/setup.sh first (nvcc comes with the CUDA toolkit in /usr/local/cuda)." >&2
      exit 1
    fi
  done
fi

if [[ -d "$DIR/.git" ]]; then
  run git -C "$DIR" fetch --tags origin
else
  run git clone "$REPO_URL" "$DIR"
fi
run git -C "$DIR" checkout "$REF"
if ((!DRY)) && git -C "$DIR" symbolic-ref -q HEAD >/dev/null; then
  run git -C "$DIR" pull --ff-only
fi

# A build directory configured for another architecture or CUDA version is silently reused by
# cmake: start clean unless this exact configuration is already there.
cache="$DIR/build/CMakeCache.txt"
if ((CLEAN)) || { [[ -f "$cache" ]] && ! grep -q '^CMAKE_CUDA_ARCHITECTURES:[A-Z]*=87$' "$cache"; }; then
  run rm -rf "$DIR/build"
fi

run cmake -S "$DIR" -B "$DIR/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=87 \
  -DLLAMA_CURL=ON
run cmake --build "$DIR/build" --config Release -j "$JOBS" --target llama-server

if ((DRY)); then
  echo "dry run: nothing was built"
  exit 0
fi

binary="$DIR/build/bin/llama-server"
if [[ ! -x "$binary" ]]; then
  echo "the build finished but $binary is missing" >&2
  exit 1
fi
commit="$(git -C "$DIR" describe --tags --always 2>/dev/null || git -C "$DIR" rev-parse --short HEAD)"
echo "$commit" >"$DIR/build/bin/agent-house-build.txt"
echo "built $binary ($commit)"
"$binary" --version 2>&1 | head -n 3 || true
if [[ "$binary" != "$LLAMA_SERVER_BIN" ]]; then
  echo "note: $AH_ETC/config.env points at $LLAMA_SERVER_BIN; set LLAMA_SERVER_BIN=$binary there" >&2
fi
