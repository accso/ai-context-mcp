# build_helpers.sh — shared shell helpers for scripts that manage the
# ai-context Docker containers (start.sh, init.sh, restore.sh).
#
# Source this file, don't execute it:
#   source "$SCRIPT_DIR/lib/build_helpers.sh"
#
# Not `set -euo pipefail` here on purpose — this file only defines
# functions; the sourcing script keeps its own shell options.

# Pull in the live-dashboard helpers (ui_begin/ui_set/ui_end/ui_watch_pid)
# used by build_image_row below. ui.sh has its own include guard, so this
# is safe even if the calling script also sources it directly.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ui.sh"

# require_cmd <command> <install-hint>
# Exit with a clear message if a required CLI tool is missing. Previously
# duplicated verbatim in every script; centralised here so the message stays
# consistent and only needs updating in one place.
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERR '$1' not found. Install: $2"; exit 1; }
}

# build_image <image-tag> <build-context-dir> [extra docker build args...]
#
# Wraps `docker build` to avoid dumping a full, uninterrupted BuildKit
# step-by-step scroll into the terminal on every run (the previous `-q`
# quiet mode was just as bad in the other direction: it hid ALL progress,
# including on a slow build, making a normal multi-minute image build
# indistinguishable from a hang).
#
# Behavior:
#   - Runs the real build with `--progress=plain` into a temp log file
#     (never straight to the terminal), so nothing is lost.
#   - While the build runs, shows ONE continuously-updated status line with
#     elapsed time and the most recent build step, so there's visible
#     proof-of-life without scrolling the terminal — in an interactive TTY
#     via a carriage-return-overwritten line; in a non-interactive/redirected
#     context (CI, log file) via periodic plain status lines instead, since
#     \r overwrites would otherwise corrupt a piped/redirected log.
#   - On success: clears the status line, prints one clean summary line.
#   - On failure: prints the ENTIRE captured build log (nothing hidden),
#     followed by a clear ERR line, then returns non-zero — the caller
#     script's `set -e` will stop the run.
build_image() {
  local image_tag="$1" context_dir="$2"
  shift 2
  # bash 3.2 (macOS default) treats an empty array's `[@]` expansion as
  # "unbound" under `set -u` — pre-declare empty, then append, and always
  # expand via the `${arr[@]+...}` guard below, not bare `${arr[@]}`.
  local extra_args=()
  extra_args+=("$@")

  local log_file
  log_file="$(mktemp -t ai-context-build.XXXXXX)"
  local start_ts
  start_ts=$(date +%s)

  # Run the actual build in the background so we can poll its log file for
  # a live status line while it's still running.
  (
    docker build -t "$image_tag" "$context_dir" --progress=plain "${extra_args[@]+"${extra_args[@]}"}"
  ) >"$log_file" 2>&1 &
  local build_pid=$!

  if [ -t 1 ]; then
    # Interactive terminal: single self-overwriting status line.
    while kill -0 "$build_pid" 2>/dev/null; do
      local elapsed=$(( $(date +%s) - start_ts ))
      local last_line
      last_line=$(tail -n 1 "$log_file" 2>/dev/null | cut -c1-80)
      printf "\r[build] %-20s %3ds  %-80s" "$image_tag" "$elapsed" "$last_line"
      sleep 1
    done
    printf "\r%-110s\r" " "  # clear the status line
  else
    # Non-interactive (redirected/CI): periodic plain lines, no \r overwrite,
    # so a piped/saved log stays readable instead of full of carriage returns.
    # Poll every 1s (so a fast build/failure is detected promptly) but only
    # print a status line every ~10s, to avoid spamming a saved log.
    local last_print_ts=$start_ts
    while kill -0 "$build_pid" 2>/dev/null; do
      sleep 1
      local now_ts
      now_ts=$(date +%s)
      if [ $(( now_ts - last_print_ts )) -ge 10 ]; then
        echo "[build] $image_tag ... $(( now_ts - start_ts ))s elapsed"
        last_print_ts=$now_ts
      fi
    done
  fi

  local exit_code
  if wait "$build_pid"; then exit_code=0; else exit_code=$?; fi
  local total_elapsed=$(( $(date +%s) - start_ts ))

  if [ "$exit_code" -eq 0 ]; then
    echo "    OK $image_tag built in ${total_elapsed}s"
    rm -f "$log_file"
  else
    echo "ERR $image_tag build failed after ${total_elapsed}s — full build log:"
    cat "$log_file"
    echo "ERR $image_tag build failed (see log above)."
    rm -f "$log_file"
    return "$exit_code"
  fi
}

# build_image_row <row_idx> <image-tag> <build-context-dir> [extra args...]
#
# Same underlying build as build_image, but reports progress through the
# live dashboard (lib/ui.sh) row `row_idx` instead of printing its own
# status line — for use inside a ui_begin/.../ui_end block, so several
# builds/steps can share one fixed, non-scrolling status block. Caller
# must have already called `ui_begin` with a label for this row.
#
# On failure: ends the dashboard (so the frozen final state stays on
# screen as normal output) and THEN dumps the full build log below it,
# same "never hide the real error" guarantee as build_image.
build_image_row() {
  local row_idx="$1" image_tag="$2" context_dir="$3"
  shift 3
  local extra_args=()
  extra_args+=("$@")

  local log_file
  log_file="$(mktemp -t ai-context-build.XXXXXX)"

  (
    docker build -t "$image_tag" "$context_dir" --progress=plain "${extra_args[@]+"${extra_args[@]}"}"
  ) >"$log_file" 2>&1 &
  local build_pid=$!

  ui_watch_pid "$row_idx" "$build_pid" "$log_file"
  local exit_code=$?

  if [ "$exit_code" -ne 0 ]; then
    ui_end
    echo "ERR $image_tag build failed — full build log:"
    cat "$log_file"
    echo "ERR $image_tag build failed (see log above)."
  fi
  rm -f "$log_file"
  return "$exit_code"
}
