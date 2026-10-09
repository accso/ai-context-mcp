# Runtime Debugging (xdebug_*)

## Goal
Use debugger evidence to answer concrete runtime questions when static code reading is not enough or to confirm ideas or results made by static analysis, including whether execution reaches or does not reach agent-selected code locations via breakpoints or tracepoints.

## When To Use This Guidance
Use this guidance when at least one of the following is true:
- runtime state or control-flow evidence is needed (actual values, branch taken, call order, thread context, or whether execution reaches a chosen location), and it cannot be derived confidently from source/logs;
- finding the relevant path would otherwise require reading many files or tracing a long call chain manually;
- the user explicitly asks to use the debugger (breakpoints, stepping, inspect values, debugger session).

If the user manually activates or refers to debugging, treat that as an explicit request to launch and use the debugger when the required tools are available. Do not reduce a manual activation to a conceptual discussion about debugging.

If the user's codebase domain itself contains debug-related concepts, reason explicitly about whether the user is talking about their code or actually asking you to launch and use the debugger. Do not treat domain concepts, features, modes, log categories, flags, or identifiers named `debug` or `debugger` as a debugger request by default.

## Activation Gate (tool availability)
Use this guidance only when debugger MCP tools are available in the current session.

Minimum required set:
- `xdebug_set_breakpoint`
- `xdebug_start_debugger_session`
- `xdebug_control_session`
- `xdebug_get_stack`
- at least one value tool: `xdebug_get_frame_values` or `xdebug_get_value_by_path` or `xdebug_evaluate_expression`
- `get_run_configurations` (needed for entry point discovery and run config resolution)

If this minimum set is not available:
- state the blocker explicitly;
- do not force a debugger workflow;
- switch to the best fallback path (e.g. `references/explore.md`).

Tool names in this file reflect this session's live MCP tool schema — bare names with **no `idea_` prefix** (e.g. `xdebug_start_debugger_session`, not `idea_xdebug_start_debugger_session`), invoked via the `idea-mcp-execute_tool` dispatcher — confirmed live 2026-08-13. Always treat the live schema as the source of truth if it differs.

## Initial Triage Before Debugging
Start with minimal static triage (error text, stack trace, and nearby source). Start debugger work when one of these is true:
- static analysis leaves multiple plausible runtime hypotheses;
- deciding between hypotheses requires concrete runtime values or exact control flow;
- stepping through the code is cheaper than reading a large cross-file path;
- the user explicitly requested debugger actions.

Before starting a new debugger run, assess whether there is already a current relevant debugger session that can be reused.

When debugger work is chosen, set the first breakpoint early and collect concrete evidence before making runtime behavior changes.

## Mandatory Runtime Evidence
Before making code edits for runtime behavior issues when debugger evidence is available, collect this minimum evidence:
- paused location (file/line) from a suspended debug session;
- top call path via `xdebug_get_stack`;
- when control-flow reachability is part of the question, evidence that execution did or did not hit an explicitly chosen breakpoint or tracepoint;
- at least one concrete runtime value via `xdebug_get_frame_values`, `xdebug_get_value_by_path`, or `xdebug_evaluate_expression`.

Do not assert runtime conclusions before this evidence is captured unless you have already stated a clear blocker that makes debugger evidence unavailable or useless for this task.

## Reproduction Mode Selection
Choose the execution mode before starting:
1. `AUTO`: the agent can run and rerun the scenario directly (run config, test, command, endpoint). Use `xdebug_start_debugger_session` to launch with debugging, or `execute_run_configuration` for a non-debug run when you only need output or exit code.
2. `ASSISTED`: reproduction requires a user-only action (UI flow, auth, external dependency, hardware interaction).
3. `HYBRID`: try `AUTO` once or twice, then switch to `ASSISTED` if the failure does not reproduce.

Practical heuristic:
- if the candidate debug target is a test, default to `AUTO`;
- never ask the user to reproduce before breakpoints are prepared.

## Test Task Execution
- If the task is about tests, failing tests, flaky tests, or reproducing a bug through a test, use the narrowest test-running mechanism available in the current session.
- To debug a test: use `xdebug_start_debugger_session(filePath, line)` pointing at the test method or `xdebug_start_debugger_session(configurationName)` for an existing test run configuration. To run a test without debugging (e.g., to confirm it fails before setting breakpoints): use `execute_run_configuration` with the same targeting modes.
- After triggering the test scenario, inspect the resulting debugger state with `xdebug_get_debugger_status()` and continue the investigation in the relevant session.
- When the task is test-driven, do not ask the user to rerun the test manually unless no available test-running path can reproduce it or the reproduction requires user-only setup.

## Argument Hygiene
- Optional parameters are either omitted/null or intentionally set to a real value; omitted means omitted.
- Do not encode omitted optional parameters with placeholder strings such as `""`, `"/"`, `"__omit__"`, `"."`, fake paths, or other sentinels.
- Treat debugger IDs and paths as opaque runtime data: copy them from tool outputs instead of synthesizing them.

## Session Binding
- Before preparing new breakpoints or launching a new session, call `xdebug_get_debugger_status()`.
- If there is an active relevant session, continue inside it instead of starting another one.
- After `xdebug_get_debugger_status()` or `xdebug_start_debugger_session()`, capture the exact `sessionId` and reuse it in all session-scoped calls.
- If a session stops, times out, or disappears, do not reuse the old `sessionId`; refresh it with `xdebug_get_debugger_status()` before the next session-scoped call.

## Run Configuration Resolution
- Resolve `configurationName` only from `get_run_configurations`; do not pass a test method name or other derived identifier.
- Use `supportsDynamicLaunchOverrides` from `get_run_configurations` as the source of truth for `programArguments`, `workingDirectory`, and `envs`.
- Do not try to target a specific test by pushing it through `programArguments` when the chosen run configuration does not support dynamic launch overrides.
- Only use launch overrides when they materially improve reproducibility or observability for the current investigation, and only when the selected run configuration supports them.

## Breakpoint Ownership And Hygiene
- Always start with `xdebug_list_breakpoints(...)` and treat returned `owner` as source of truth (`user` or `agent`).
- Build a baseline snapshot (`breakpointId -> enabled`) before modifying anything.
- Temporarily disable `owner=user` breakpoints that are not required for the current investigation path.
- Perform temporary disable and restore via `xdebug_set_breakpoint(breakpointId=..., enabled=...)`.
- Restore disabled user breakpoints only once, at the end of the debugger cycle (success, timeout-abort, or stopped-abort).
- In `breakpointId` mode, `xdebug_set_breakpoint` applies the provided attributes as resulting state. Restore fields intentionally by passing the full desired state.
- `xdebug_remove_breakpoint` defaults to `owner=agent`; for global cleanup of all breakpoints, run two calls: `owner=agent` and `owner=user`.

## Breakpoint Targeting Modes
- `xdebug_set_breakpoint` has two modes: location mode and `breakpointId` mode.
- In location mode, pass `filePath + line` and omit `breakpointId` entirely.
- In `breakpointId` mode, pass an existing opaque canonical `breakpointId` copied from `xdebug_set_breakpoint` or `xdebug_list_breakpoints` output.
- Do not mix these modes accidentally, and do not use placeholder strings to simulate omission.
- After each successful `xdebug_set_breakpoint(...)`, inspect returned `lineText` and confirm that the truncated source excerpt matches the line you intended to stop on.
- A successful `xdebug_set_breakpoint` response does not prove that `condition`, tracepoint logging, or other deferred expression checks are valid.

## Library And Decompiled Source Debugging
Breakpoints can be set in library and SDK code regardless of language. `read_file` can read sources bundled in JARs/JRT archives, plain source files shipped with SDKs and libraries, and automatically decompile JVM `.class` files (both inside JARs and on disk). Never extract JARs manually (`jar`, `unzip`, shell commands) — manual extraction cannot decompile `.class` files and does not respect IDE source resolution.

To obtain a file path for library or SDK code, use one of:
- **stack frames**: `xdebug_get_stack` returns `file` paths including paths into libraries and archives; use the path exactly as returned;
- **symbol search**: `search_symbol(query="ClassName", includeExternal=true)` finds SDK and library symbols and returns file paths.

**MANDATORY: always read before setting breakpoints in library code.** Never guess line numbers. The correct workflow is:
1. obtain the file path (from stack frames or symbol search);
2. **always** read the source with `read_file(filePath=<path>)` to see the actual decompiled/library source content and find the exact line for the target statement or method;
3. only then set the breakpoint with `xdebug_set_breakpoint(filePath=<path>, line=...)` using the line number from step 2.

## Choosing The Entry Point And First Breakpoint
Before the first run, explicitly choose:
- the narrowest reproducible entry point (single test, focused run config, endpoint handler, UI action handler);
- the first breakpoint at the earliest executable line where uncertainty becomes observable (branch decision, mapping boundary, state transition, dynamic dispatch, error construction, queue handoff).

If unsure, place at most 2 coarse breakpoints first:
- one at the entry boundary;
- one at the suspected failure boundary.

Run once, then narrow.

## Core Workflow
1. Scope the failure:
   - capture exact error text and expected vs actual behavior;
   - identify 2-5 likely anchors in the runtime path.
2. Prepare the debug session:
   - inspect current sessions via `xdebug_get_debugger_status()`;
   - decide whether to reuse an active relevant session or launch a new one;
   - choose `AUTO`, `ASSISTED`, or `HYBRID`;
   - set initial breakpoints first and verify each returned `lineText` before proceeding.
3. Start and suspend:
   - if reusing an active relevant session, continue from its current state and only pause or resume intentionally;
   - otherwise call `xdebug_start_debugger_session(...)`;
   - after a fresh start or explicit resume, call `xdebug_control_session(action=WAIT_FOR_PAUSE, ...)`.
4. Capture evidence at each stop:
   - `xdebug_get_stack(...)`;
   - `xdebug_get_frame_values(...)`, `xdebug_get_value_by_path(...)`, or `xdebug_evaluate_expression(...)`.
5. Move deliberately:
   - prefer `STEP_OVER` or `STEP_INTO` when the needed evidence is nearby;
   - before `RESUME`, explicitly decide which breakpoint should be hit next and why.
6. After every `RESUME`, always call `xdebug_control_session(action=WAIT_FOR_PAUSE)`.
7. If waiting times out, do not resume blindly:
   - use `PAUSE`;
   - re-check enabled breakpoints and the expected path;
   - after 2 consecutive `WAIT_FOR_PAUSE` timeouts, stop retrying the same wait loop and expand breakpoint coverage.
8. For tracepoints or logging breakpoints, drain outputs with `xdebug_control_session(action=DRAIN_EVENTS)`.
9. Continue until the first incorrect state transition is proven or until you have a clearly stated blocker.
10. Summarize root cause with concrete runtime evidence.

## Default First Probe
For a reproducible runtime issue, the default first sequence is:
1. `xdebug_get_debugger_status()`
2. Reuse an active relevant session if it exists; otherwise `xdebug_set_breakpoint(...)`
3. If no relevant session exists, `xdebug_start_debugger_session(...)`
4. `xdebug_control_session(action=WAIT_FOR_PAUSE, ...)` when a fresh start or explicit resume requires suspension
5. `xdebug_get_stack(...)`
6. `xdebug_get_frame_values(...)` or `xdebug_evaluate_expression(...)`

## Debugger Tools
Tool names include the `xdebug_` prefix. Treat the summary below as a quick operating guide. The MCP information / tool metadata exposed in the current session is the source of truth for each tool's full contract, parameter semantics, and current constraints, unless this guidance explicitly provides a more specific higher-priority clarification.

- `xdebug_start_debugger_session(configurationName?, filePath?, line?, timeout?, graceWaitMs?, programArguments?, workingDirectory?, envs?)`:
  start debugging either an existing run configuration or a code location. Use exactly one target mode: `configurationName`, or `filePath + line`. For `filePath + line`, a line with `main`, a test, or another runnable entry point usually works; if unsure, use `get_run_configurations(filePath = ...)` to discover runnable locations. `supportsDynamicLaunchOverrides` is the source of truth for whether launch overrides may be passed.
- `xdebug_get_debugger_status()`:
  list sessions and states; use it to bind the current `sessionId` and to refresh it after `stopped` or disappearance.
- `xdebug_control_session(sessionId?, action, timeout?, eventsLimit?)`:
  `STEP_INTO | STEP_OVER | STEP_OUT | RESUME | PAUSE | STOP | WAIT_FOR_PAUSE | DRAIN_EVENTS`. When the result is `paused`, it also includes `frameValues`, a current-frame snapshot in `xdebug_get_frame_values(depth=0)` format when available.
- `xdebug_get_threads(sessionId?, limit?, offset?)`:
  list threads (requires suspended session).
- `xdebug_get_stack(sessionId?, threadId?, limit?, offset?)`:
  call stack. `threadId` should come from `xdebug_get_threads`, and `frameIndex` consumers must use the current paused result only.
- `xdebug_get_frame_values(sessionId?, frameIndex?, depth)`:
  locals and values in frame. `frameIndex` should come from the current paused `xdebug_get_stack` result.
- `xdebug_get_value_by_path(sessionId?, frameIndex?, path, depth)`:
  drill into nested values. Build `path` from the current paused `xdebug_get_frame_values` / `xdebug_get_value_by_path` output.
- `xdebug_evaluate_expression(sessionId?, frameIndex?, expression, depth)`:
  evaluate a raw expression in the language of the current frame.
- `xdebug_set_breakpoint(breakpointId?, filePath?, line?, condition?, isLogMessage?, isLogStack?, temporary?, suspendPolicy?, enabled?)`:
  create, update, relocate breakpoint, or configure a tracepoint. Keep location mode and `breakpointId` mode separate. Successful line-breakpoint responses include `lineText`, a truncated excerpt of the resolved source line; inspect it before resuming.
- `xdebug_remove_breakpoint(breakpointId?, filePath?, line?, owner=agent)`:
  remove breakpoints filtered by owner; with only `owner`, removes all breakpoints of that owner.
- `xdebug_list_breakpoints(filePath?)`:
  list breakpoints with ownership (`owner=user|agent`).
- `xdebug_run_to_line(sessionId?, filePath, line, timeout?)`:
  continue execution to a line.
- `xdebug_set_variable(sessionId?, frameIndex?, path, newValue)`:
  mutate a value by path, using an exact current `path` and a raw expression that is assignable to the target value.

## Execution Tools
- `get_run_configurations(filePath?)`:
  without `filePath`, lists the project's existing run configurations including `supportsDynamicLaunchOverrides`. With `filePath`, discovers executable entry points (test methods, main methods, run gutter icons) and their line numbers. Use to resolve `configurationName` or to find `filePath + line` targets for `xdebug_start_debugger_session` and `execute_run_configuration`.
- `execute_run_configuration(configurationName?, filePath?, line?, timeout?, waitForExit?, programArguments?, workingDirectory?, envs?)`:
  run a configuration or code location without debugging. Use exactly one target mode: `configurationName`, or `filePath + line`. Returns exit code, output snapshot, and optional `fullOutputPath`. Use this for non-debug runs (e.g., confirming a test fails before attaching the debugger).

## Events And Tracepoints
- `xdebug_control_session(...).breakpointErrorsTail` can return breakpoint validation or runtime errors for any action.
- `xdebug_control_session(action=DRAIN_EVENTS).tracepointOutputsTail` returns tracepoint logs from `isLogMessage` / `isLogStack` breakpoints.
- For tracepoint-style logging without suspension, use `xdebug_set_breakpoint(..., isLogMessage/isLogStack, suspendPolicy=NONE)`.
- Event tails above are currently populated by JVM-based debuggers (Java, Kotlin, etc.).
- After any breakpoint with `condition`, tracepoint logging, or `suspendPolicy=NONE`, do not trust it yet; check `breakpointErrorsTail` / `tracepointOutputsTail` before relying on it.

## Expression Discipline
- `xdebug_evaluate_expression` is for evaluation in the current paused frame; pass raw expression text in the language of that frame.
- `xdebug_set_variable` is for mutation only after you have the exact target `path` from the current paused state.
- `newValue` must be a raw expression in the language of the current frame and must be assignable to the target value by the debugger/evaluator.
- Do not pass JSON-escaped payloads or literal backslash-escaped quoted text into `expression` or `newValue`.
- For global/static symbols in `condition` and `xdebug_evaluate_expression`, prefer fully-qualified names whenever possible. FQNs reduce ambiguity and avoid `Unresolved reference` issues caused by missing imports in debugger expression context.
- If paused, preflight a risky condition expression with `xdebug_evaluate_expression` before relying on it.

## Explicit Debug Loop
1. Set breakpoint at the most probable fault boundary.
2. Trigger reproduction:
   - `AUTO`: run/rerun debug target;
   - `ASSISTED`: request one precise user action sequence.
3. Wait for suspension (`WAIT_FOR_PAUSE`).
4. Capture runtime state (`xdebug_get_stack` + values/eval).
5. Decide next movement:
   - use `STEP_*` for nearby evidence;
   - set next breakpoint before `RESUME` for later evidence.
6. Resume only with explicit predicted next stop.
7. Repeat until first incorrect state transition is proven.

Never resume blindly.

## Frame Climbing And Rewind Strategy
When stopped in a frame, inspect upper frames to reconstruct data provenance:
- identify who passed the current values;
- identify earliest caller frame where state became incorrect.

If required location is already passed:
- `AUTO`: restart debug run with earlier breakpoint;
- `ASSISTED`: set earlier breakpoint, then ask user to reproduce again.

Do not continue from a point that is already too late for needed evidence.

When a stack frame points to library or decompiled code, do not skip it — read the decompiled source with `read_file` using the path from the frame, and set breakpoints there if needed to trace data provenance deeper.

## Async Frames Guidance (JVM)
- Java/Kotlin stacks can include async provenance frames that show where execution previously flowed.
- Treat such async frames as navigation signals, not as guaranteed value-evaluable runtime frames.
- Use async frame chain to place earlier breakpoints in candidate methods/entry boundaries.
- Do not conclude value-based evidence from async provenance frames alone.

## Evidence Checklist
Before concluding, capture:
- exact failing message/body;
- exception type and/or status code;
- first location where state diverges from expected;
- concrete offending keys/values (and counts when relevant);
- origin/provenance of wrong data;
- why the pipeline allowed it (missing validation/dedup/merge policy).

## Anti-Patterns
- claiming root cause without runtime values;
- encoding omitted optional parameters with placeholder strings;
- reusing stale `sessionId`, `frameIndex`, or `path` after the paused location changes;
- mixing location mode and `breakpointId` mode for `xdebug_set_breakpoint`;
- stopping at downstream symptom without tracing producing state;
- asking user to reproduce before debugger is prepared;
- broad breakpoint churn between iterations;
- code edits before root cause is proven;
- `RESUME` without justified expected next stop;
- prolonged static-only analysis when one debugger probe can disambiguate;
- ignoring library frames in the stack instead of reading their decompiled source to trace data provenance;
- extracting JARs manually (`jar`, `unzip`, shell) instead of using `read_file` which handles decompilation automatically.

## Wrap-up
After debugging:
1. Clean up: remove agent-owned breakpoints (`xdebug_remove_breakpoint(owner=agent)`), restore any disabled user breakpoints to their baseline state, and stop the session (`xdebug_control_session(action=STOP)`) if it is no longer needed.
2. Report:
   - root cause in one sentence;
   - causal chain in 3-6 bullets;
   - runtime evidence with exact observed values;
   - code references (absolute path plus line);
   - if you fell back instead of debugging, the exact reason and what evidence is still missing.
3. Next action depends on the user's goal:
   - if the user asked for information or diagnosis only: conclude with the report and a recommended fix or next step;
   - if the user's task implies a code change (bug fix, feature, refactoring): proceed to implement the fix based on the proven root cause and collected evidence.
