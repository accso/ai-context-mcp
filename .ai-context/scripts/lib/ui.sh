# ui.sh — shared "live dashboard" helpers for scripts that manage the
# ai-context Docker containers, so a user watching the terminal sees a
# small, fixed block of status lines update IN PLACE (no scrolling)
# instead of either a raw docker/BuildKit scroll or a wall of one-shot
# echo lines.
#
# Source, don't execute:
#   source "$SCRIPT_DIR/lib/ui.sh"
#
# Falls back to plain sequential echo lines whenever stdout is not an
# interactive TTY (CI, `mise` piping output to a log, redirected `> file`)
# — ANSI cursor movement would just corrupt a non-terminal output stream,
# so the dashboard is only ever active when `[ -t 1 ]`.

# Include guard — safe to source this file more than once (e.g. from a
# script that also sources build_helpers.sh, which sources this too).
if [ -n "${_UI_SH_INCLUDED:-}" ]; then
  return 0 2>/dev/null || exit 0
fi
_UI_SH_INCLUDED=1

UI_ACTIVE=0
UI_LABELS=()
UI_STATUS=()
UI_DETAIL=()

_UI_SPINNER_FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
_UI_TICK=0

# _ui_icon <status> — single glyph for a row's current status.
_ui_icon() {
  case "$1" in
    pending) printf '·' ;;
    running) printf '%s' "${_UI_SPINNER_FRAMES[$(( _UI_TICK % 10 ))]}" ;;
    done)    printf '\033[32m\xE2\x9C\x94\033[0m' ;;  # green ✔
    failed)  printf '\033[31m\xE2\x9C\x98\033[0m' ;;  # red   ✘
  esac
}

# ui_begin <label1> <label2> ... — declare the fixed set of rows for this
# phase and reserve their on-screen space. Call ui_end once all rows are
# finished (done/failed) to release the cursor. Only one dashboard may be
# active at a time (no nesting).
ui_begin() {
  UI_LABELS=("$@")
  UI_STATUS=()
  UI_DETAIL=()
  local n=${#UI_LABELS[@]} i
  for ((i = 0; i < n; i++)); do
    UI_STATUS+=("pending")
    UI_DETAIL+=("")
  done
  if [ -t 1 ]; then
    UI_ACTIVE=1
    printf '\033[?25l'  # hide cursor while the dashboard is live
    for ((i = 0; i < n; i++)); do echo; done
    ui_render
  else
    UI_ACTIVE=0
  fi
}

# ui_render — redraw every row of the current dashboard in place. Internal;
# called automatically by ui_set. Only does anything when ui_begin
# activated dashboard mode (interactive TTY).
ui_render() {
  [ "$UI_ACTIVE" = "1" ] || return 0
  local n=${#UI_LABELS[@]} i
  _UI_TICK=$((_UI_TICK + 1))
  printf '\033[%dA' "$n"
  for ((i = 0; i < n; i++)); do
    printf '\033[2K  %s %-28s %s\n' "$(_ui_icon "${UI_STATUS[$i]}")" "${UI_LABELS[$i]}" "${UI_DETAIL[$i]}"
  done
}

# ui_set <row_idx> <pending|running|done|failed> [detail text]
# Update one row's status/detail. In non-TTY fallback mode this instead
# prints one plain line per done/failed transition (never per "running"
# tick, to avoid spamming a redirected log).
ui_set() {
  local idx="$1" status="$2" detail="${3:-}"
  UI_STATUS[$idx]="$status"
  UI_DETAIL[$idx]="$detail"
  if [ "$UI_ACTIVE" = "1" ]; then
    ui_render
  else
    case "$status" in
      done)   echo "    OK ${UI_LABELS[$idx]}${detail:+ (${detail})}" ;;
      failed) echo "ERR ${UI_LABELS[$idx]} failed${detail:+ (${detail})}" ;;
    esac
  fi
}

# ui_end — release the cursor and stop dashboard-mode redraws. The last
# rendered frame (all rows done/failed) stays on screen as normal scrolled
# output. Safe to call even if ui_begin ran in non-TTY fallback mode.
ui_end() {
  if [ "$UI_ACTIVE" = "1" ]; then
    printf '\033[?25h'  # show cursor again
    UI_ACTIVE=0
  fi
}

# ui_watch_pid <row_idx> <pid> [log_file]
# Polls a background pid until it exits, live-updating the row with
# elapsed time (and, if a log file is given, its most recent line) while
# running, then setting the row to done/failed with total elapsed time.
# Returns the pid's real exit code so callers can react (e.g. dump the
# full log on failure).
ui_watch_pid() {
  local idx="$1" pid="$2" log_file="${3:-}"
  local start_ts last_plain_ts
  start_ts=$(date +%s)
  last_plain_ts=$start_ts

  while kill -0 "$pid" 2>/dev/null; do
    local elapsed=$(( $(date +%s) - start_ts ))
    local detail="${elapsed}s"
    if [ -n "$log_file" ]; then
      local last_line
      last_line=$(tail -n 1 "$log_file" 2>/dev/null | cut -c1-48)
      [ -n "$last_line" ] && detail="${elapsed}s  ${last_line}"
    fi
    if [ "$UI_ACTIVE" = "1" ]; then
      ui_set "$idx" running "$detail"
      sleep 0.2
    else
      local now_ts
      now_ts=$(date +%s)
      if [ $(( now_ts - last_plain_ts )) -ge 10 ]; then
        echo "    ... ${UI_LABELS[$idx]}: ${elapsed}s elapsed"
        last_plain_ts=$now_ts
      fi
      sleep 1
    fi
  done

  local ec
  if wait "$pid"; then ec=0; else ec=$?; fi
  local total_elapsed=$(( $(date +%s) - start_ts ))
  if [ "$ec" -eq 0 ]; then
    ui_set "$idx" done "${total_elapsed}s"
  else
    ui_set "$idx" failed "${total_elapsed}s"
  fi
  return "$ec"
}

# ── ui_task: single collapsible "live tail" block ─────────────────────────────
# For a standalone long-running step OUTSIDE a ui_begin/... /ui_end multi-row
# dashboard (e.g. config validation, model checks, the initial indexing run)
# that itself produces a lot of real, useful log output while running — the
# user still wants to see that it's alive and doing real work, but once it
# finishes successfully none of that output is needed any more.
#
# ui_task <label> -- <command...>
#   - Runs <command...> in the background, log captured to a temp file.
#   - While running (TTY only): shows one spinner header line ("<label> Ns")
#     plus a live-updating tail of the last UI_TASK_PANE_LINES output lines,
#     directly beneath it — like `docker compose up`'s per-service preview.
#   - On success: the tail pane is deleted in place (ANSI Delete Line — the
#     lines are removed, not just cleared/scrolled past), leaving exactly one
#     final line: "✔ <label>  Ns". Nothing stale stays on screen.
#   - On failure: the header/tail freeze showing "✘ <label> failed", then the
#     FULL captured log (not just the tail) is dumped below — nothing is ever
#     hidden on failure — and the real exit code is returned.
#   - Non-TTY fallback: no live tail (would corrupt a redirected log/CI
#     output) — periodic plain elapsed lines while running, one OK/ERR
#     summary line at the end, full log dumped only on failure.
UI_TASK_PANE_LINES=${UI_TASK_PANE_LINES:-6}

ui_task() {
  local label="$1"
  shift
  [ "${1:-}" = "--" ] && shift

  local log_file
  log_file="$(mktemp -t ai-context-task.XXXXXX)"
  local start_ts
  start_ts=$(date +%s)

  ( "$@" ) >"$log_file" 2>&1 &
  local pid=$!

  local ec
  if [ -t 1 ]; then
    local n=$UI_TASK_PANE_LINES
    local i
    printf '\033[?25l'
    echo  # header line
    for ((i = 0; i < n; i++)); do echo; done  # pane lines

    while kill -0 "$pid" 2>/dev/null; do
      local elapsed=$(( $(date +%s) - start_ts ))
      _UI_TICK=$((_UI_TICK + 1))
      printf '\033[%dA' $((n + 1))
      printf '\033[2K  %s %-40s %ds\n' "$(_ui_icon running)" "$label" "$elapsed"
      local shown=0 line
      while IFS= read -r line; do
        printf '\033[2K    \xE2\x94\x82 %s\n' "${line:0:100}"
        shown=$((shown + 1))
      done < <(tail -n "$n" "$log_file" 2>/dev/null)
      for ((; shown < n; shown++)); do printf '\033[2K\n'; done
      sleep 0.2
    done

    if wait "$pid"; then ec=0; else ec=$?; fi
    local elapsed=$(( $(date +%s) - start_ts ))
    printf '\033[%dA' $((n + 1))
    if [ "$ec" -eq 0 ]; then
      printf '\033[2K  %s %-40s %ds\n' "$(_ui_icon done)" "$label" "$elapsed"
      printf '\033[%dM' "$n"  # Delete Line: collapse the pane, close the gap
    else
      printf '\033[2K  %s %-40s failed (%ds)\n' "$(_ui_icon failed)" "$label" "$elapsed"
      for ((i = 0; i < n; i++)); do
        printf '\033[2K    \xE2\x94\x82 %s\n' "$(tail -n "$n" "$log_file" 2>/dev/null | sed -n "$((i + 1))p" | cut -c1-100)"
      done
    fi
    printf '\033[?25h'
  else
    local last_print_ts=$start_ts
    while kill -0 "$pid" 2>/dev/null; do
      sleep 1
      local now_ts
      now_ts=$(date +%s)
      if [ $(( now_ts - last_print_ts )) -ge 10 ]; then
        echo "    ... $label: $(( now_ts - start_ts ))s elapsed"
        last_print_ts=$now_ts
      fi
    done
    if wait "$pid"; then ec=0; else ec=$?; fi
    local elapsed=$(( $(date +%s) - start_ts ))
    if [ "$ec" -eq 0 ]; then
      echo "    OK $label (${elapsed}s)"
    else
      echo "ERR $label failed (${elapsed}s)"
    fi
  fi

  if [ "$ec" -ne 0 ]; then
    echo "ERR $label failed — full log:"
    cat "$log_file"
  fi
  rm -f "$log_file"
  return "$ec"
}

# ── ui_watch_parallel: watch N background pids concurrently, one dashboard
#    row each ─────────────────────────────────────────────────────────────────
# For scripts that launch several independent background jobs at once (e.g.
# per-project backups, which intentionally run in parallel) and want each to
# get its own live-updating dashboard row instead of interleaved raw output
# from all of them printing directly to the terminal at once.
#
# ui_watch_parallel <row_idx1> <pid1> <log_file1> [<row_idx2> <pid2> <log_file2> ...]
#   - log_file may be "" (empty string) if a job's output isn't captured.
#   - Polls all pids in one shared loop (not one-at-a-time), so all rows
#     update live and in parallel, same spirit as ui_watch_pid but for many
#     jobs at once instead of just one.
#   - Sets UI_PARALLEL_EXIT_CODES (global array, same order as the input
#     triples) once every job has finished, so the caller can react per-job
#     (e.g. build a failure summary) without ui_watch_parallel itself needing
#     to know what "success" means for the caller's use case.
#   - Non-TTY fallback: no per-row output while running (would interleave
#     unreadably); one periodic "N/M finished" line every ~10s, then the
#     usual ui_set OK/ERR line per job as each one completes.
ui_watch_parallel() {
  local -a idxs=() pids=() logs=()
  while [ "$#" -gt 0 ]; do
    idxs+=("$1")
    pids+=("$2")
    logs+=("${3:-}")
    shift 3
  done

  local n=${#pids[@]}
  local -a start_ts=() job_done=() job_ec=()
  local now
  now=$(date +%s)
  local i
  for ((i = 0; i < n; i++)); do
    start_ts[i]=$now
    job_done[i]=0
    job_ec[i]=0
  done

  local remaining=$n
  local last_plain_ts=$now
  while [ "$remaining" -gt 0 ]; do
    for ((i = 0; i < n; i++)); do
      [ "${job_done[i]}" = "1" ] && continue
      if kill -0 "${pids[i]}" 2>/dev/null; then
        if [ "$UI_ACTIVE" = "1" ]; then
          local elapsed=$(( $(date +%s) - start_ts[i] ))
          local detail="${elapsed}s"
          if [ -n "${logs[i]}" ]; then
            local last_line
            last_line=$(tail -n 1 "${logs[i]}" 2>/dev/null | cut -c1-40)
            [ -n "$last_line" ] && detail="${elapsed}s  ${last_line}"
          fi
          ui_set "${idxs[i]}" running "$detail"
        fi
      else
        local pec
        if wait "${pids[i]}"; then pec=0; else pec=$?; fi
        job_ec[i]=$pec
        job_done[i]=1
        remaining=$((remaining - 1))
        local total_elapsed=$(( $(date +%s) - start_ts[i] ))
        if [ "$pec" -eq 0 ]; then
          ui_set "${idxs[i]}" done "${total_elapsed}s"
        else
          ui_set "${idxs[i]}" failed "${total_elapsed}s"
        fi
      fi
    done
    if [ "$remaining" -gt 0 ]; then
      if [ "$UI_ACTIVE" != "1" ]; then
        local now_ts
        now_ts=$(date +%s)
        if [ $(( now_ts - last_plain_ts )) -ge 10 ]; then
          echo "    ... $(( n - remaining ))/${n} finished"
          last_plain_ts=$now_ts
        fi
      fi
      sleep 0.2
    fi
  done

  UI_PARALLEL_EXIT_CODES=("${job_ec[@]}")
}

# ── Single collapsing status line, one line per [tag] ────────────────────────
#
# ui_line "[tag]" "message..." — print a one-off status line, but when the
# *same* tag is used again on the very next call, overwrite that previous
# line in place instead of adding a new one. This keeps a fast sequence of
# related "[start] step A... / [start] step B... / [start] step C..." lines
# down to a single line on screen instead of a scroll, while still letting a
# different tag (or a non-adjacent repeat) start a fresh line.
#
# Every call still ends with a real newline, so the cursor is always parked
# at the start of a fresh line afterwards — safe even across a handoff to a
# separate subprocess (e.g. ai-context-up.sh calling `bash start.sh`), which
# starts with no memory of the previous process's last tag and so naturally
# treats its first line as "different" and prints it fresh.
#
# Falls back to plain one-line-per-call echo on a non-TTY (log/CI) so full
# depth is preserved there.
_UI_LAST_LINE_TAG=""
ui_line() {
  local tag="$1"; shift
  local msg="$*"
  if [ -t 1 ] && [ -n "$_UI_LAST_LINE_TAG" ] && [ "$tag" = "$_UI_LAST_LINE_TAG" ]; then
    printf '\033[1A\033[2K%s %s\n' "$tag" "$msg"
  else
    printf '%s %s\n' "$tag" "$msg"
  fi
  _UI_LAST_LINE_TAG="$tag"
}

# ui_line_reset — forget the last tag so the next ui_line call always starts
# a fresh line instead of overwriting. Call this after ANY output that did
# not go through ui_line itself (a subprocess call, an unrelated echo, a
# dashboard block, ...) so ui_line never overwrites the wrong on-screen line.
ui_line_reset() { _UI_LAST_LINE_TAG=""; }
