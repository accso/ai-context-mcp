#!/usr/bin/env node
/**
 * bench.js — self-serve benchmark instrumentation CLI
 * -----------------------------------------------------
 * A single dependency-free script an AGENT can invoke itself (as a normal
 * shell command) to log real wall-clock time and real-ish token counts for
 * ANY action it takes — an MCP tool call, an IDE action, a grep, a file
 * read — regardless of whether that action is itself "wrappable" as an MCP
 * server. This is the point: idea-mcp and the baseline session don't run
 * through a proxyable MCP transport the way ai-context does, so the only
 * thing that works for all three sessions uniformly is a CLI tool the agent
 * calls on its own initiative, before and after whatever it just did.
 *
 * All calls append one JSON line to a log file (default: ./bench_log.jsonl)
 * so nothing is lost mid-session and the log can be reviewed/audited later.
 *
 * ----------------------------------------------------------------------
 * MODE 1 - WRAP (preferred whenever the action IS a single shell command):
 *   node bench.js wrap --session A --query Q7 --tool "grep" -- \
 *       grep -rn "applyPendingChanges" project-c/
 *
 *   Runs the command after "--", captures real start/stop time via
 *   process.hrtime (monotonic), and token-counts the command line itself
 *   as "input" and the command's stdout+stderr as "output".
 *
 * MODE 2 - START / STOP (for calls that are NOT a single shell command --
 *   e.g. an MCP tool call the agent made through its own tool-calling
 *   mechanism, or an IDE action triggered through idea-mcp):
 *
 *   node bench.js start --session B --query Q3 --tool "search_symbol" \
 *       --input "parseIssuer"
 *   -> prints a call_id, e.g.  call_id=b3f1a9
 *
 *   ... agent performs the actual tool call through its normal mechanism ...
 *
 *   node bench.js stop --id b3f1a9 --output "$(cat output.txt)"
 *   (or) node bench.js stop --id b3f1a9 --output-file output.txt
 *
 *   This records stop time (diffed against the start timestamp saved to a
 *   small state file) and token-counts the given output text.
 *
 * MODE 3 - REPORT: aggregate a session's log into a summary table.
 *   node bench.js report --session A --log bench_log.jsonl
 *
 * Token counting:
 *   Uses a real BPE-style tokenizer package if it is installed in
 *   node_modules (checked at require time). Falls back to a word/number/
 *   punctuation-run approximator otherwise (NOT chars/4 -- see
 *   approxTokens()). Every record is tagged with which method was used so
 *   exact and estimated numbers are never silently mixed in a report.
 *
 * Time measurement:
 *   process.hrtime.bigint() -- monotonic, immune to system clock changes,
 *   nanosecond resolution. This replaces the agent guessing its own timing.
 */

'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const STATE_DIR = path.join(os.tmpdir(), 'bench_state');
if (!fs.existsSync(STATE_DIR)) fs.mkdirSync(STATE_DIR, { recursive: true });

// ---- token counting --------------------------------------------------

let realTokenizer = null;
let tokenMethod = 'approx';
try {
  // Optional exact tokenizer package (e.g. "gpt-tokenizer"). If present in
  // node_modules it is used automatically; otherwise the approximator below
  // is used and every record is tagged "approx" so nobody mistakes it for
  // an exact count.
  realTokenizer = require('gpt-tokenizer');
  tokenMethod = 'gpt-tokenizer';
} catch (e) { /* fall back to approximation below */ }

function approxTokens(text) {
  if (!text) return 0;
  // Word / number / punctuation-run tokenizer -- closer to real BPE token
  // counts than characters/4 for source code and mixed prose, because it
  // does not average away punctuation-heavy runs (braces, dots, JSON).
  const wordRe = new RegExp('[A-Za-z]+|[0-9]+|[^ \\t\\n\\rA-Za-z0-9]', 'g');
  const matches = text.match(wordRe);
  return matches ? matches.length : (text.length > 0 ? 1 : 0);
}

function countTokens(text) {
  if (text === null || text === undefined) return { count: 0, method: tokenMethod };
  const str = typeof text === 'string' ? text : JSON.stringify(text);
  if (realTokenizer) {
    try {
      return { count: realTokenizer.encode(str).length, method: 'gpt-tokenizer' };
    } catch (e) { /* fall through to approx on any encode error */ }
  }
  return { count: approxTokens(str), method: 'approx' };
}

// ---- arg parsing -------------------------------------------------------

function parseFlags(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const key = argv[i].slice(2);
      const val = (i + 1 < argv.length && !argv[i + 1].startsWith('--')) ? argv[++i] : true;
      out[key] = val;
    }
  }
  return out;
}

function logPathFor(flags) {
  return flags.log || 'bench_log.jsonl';
}

function appendLog(logPath, record) {
  fs.appendFileSync(logPath, JSON.stringify(record) + '\n');
}

function readFileIfGiven(flags, key) {
  const fileKey = key + '-file';
  if (flags[fileKey]) return fs.readFileSync(flags[fileKey], 'utf8');
  if (flags[key]) return flags[key];
  return '';
}

// ---- MODE: wrap ----------------------------------------------------------

function cmdWrap(argv) {
  const dashIdx = argv.indexOf('--');
  if (dashIdx === -1) {
    console.error('Usage: bench.js wrap --session S --query Q --tool T -- <command...>');
    process.exit(1);
  }
  const flags = parseFlags(argv.slice(0, dashIdx));
  const command = argv.slice(dashIdx + 1);
  if (command.length === 0) {
    console.error('No command given after --');
    process.exit(1);
  }

  const session = flags.session || 'unspecified';
  const query = flags.query || null;
  const tool = flags.tool || command[0];
  const inputText = command.join(' ');

  const start = process.hrtime.bigint();
  const result = spawnSync(command[0], command.slice(1), { encoding: 'utf8', shell: false });
  const end = process.hrtime.bigint();

  const durationS = Number(end - start) / 1e9;
  const stdout = result.stdout || '';
  const stderr = result.stderr || '';
  const inTok = countTokens(inputText);
  const outTok = countTokens(stdout + (stderr ? '\n[stderr]\n' + stderr : ''));

  const record = {
    session, query_id: query, tool_name: tool, mode: 'wrap',
    start_iso: new Date(Date.now() - durationS * 1000).toISOString(),
    end_iso: new Date().toISOString(),
    duration_s: Number(durationS.toFixed(4)),
    input_tokens: inTok.count,
    output_tokens: outTok.count,
    token_method: (inTok.method === outTok.method) ? inTok.method : 'mixed',
    exit_code: result.status,
    input_preview: inputText.slice(0, 160),
    output_preview: stdout.slice(0, 160),
  };
  appendLog(logPathFor(flags), record);

  // Pass the wrapped command's own output straight through, so the agent
  // still sees exactly what it would have seen without the wrapper.
  process.stdout.write(stdout);
  if (stderr) process.stderr.write(stderr);
  process.exit(result.status ?? 0);
}

// ---- MODE: start / stop -------------------------------------------------

function cmdStart(argv) {
  const flags = parseFlags(argv);
  const id = crypto.randomBytes(4).toString('hex');
  const state = {
    session: flags.session || 'unspecified',
    query_id: flags.query || null,
    tool_name: flags.tool || 'unspecified',
    input_text: readFileIfGiven(flags, 'input'),
    start_ns: process.hrtime.bigint().toString(),
    start_iso: new Date().toISOString(),
    log: logPathFor(flags),
  };
  fs.writeFileSync(path.join(STATE_DIR, id + '.json'), JSON.stringify(state));
  console.log(`call_id=${id}`);
}

function cmdStop(argv) {
  const flags = parseFlags(argv);
  const id = flags.id;
  if (!id) {
    console.error('Usage: bench.js stop --id <call_id> [--output "..."] [--output-file path]');
    process.exit(1);
  }
  const statePath = path.join(STATE_DIR, id + '.json');
  if (!fs.existsSync(statePath)) {
    console.error(`No open call with id ${id}. Did you call "bench.js start" first?`);
    process.exit(1);
  }
  const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
  const endNs = process.hrtime.bigint();
  const durationS = Number(endNs - BigInt(state.start_ns)) / 1e9;
  const outputText = readFileIfGiven(flags, 'output');

  const inTok = countTokens(state.input_text);
  const outTok = countTokens(outputText);

  const record = {
    session: state.session, query_id: state.query_id, tool_name: state.tool_name, mode: 'start-stop',
    start_iso: state.start_iso, end_iso: new Date().toISOString(),
    duration_s: Number(durationS.toFixed(4)),
    input_tokens: inTok.count, output_tokens: outTok.count,
    token_method: (inTok.method === outTok.method) ? inTok.method : 'mixed',
    input_preview: (state.input_text || '').slice(0, 160),
    output_preview: outputText.slice(0, 160),
  };
  appendLog(state.log, record);
  fs.unlinkSync(statePath);
  console.log(`Logged ${state.tool_name} (${record.duration_s}s, ${record.input_tokens + record.output_tokens} tokens, method=${record.token_method})`);
}

// ---- MODE: report --------------------------------------------------------

function cmdReport(argv) {
  const flags = parseFlags(argv);
  const logPath = logPathFor(flags);
  if (!fs.existsSync(logPath)) {
    console.error(`Log file not found: ${logPath}`);
    process.exit(1);
  }
  const lines = fs.readFileSync(logPath, 'utf8').split('\n').filter(Boolean);
  let records = lines.map(l => JSON.parse(l));
  if (flags.session) records = records.filter(r => r.session === flags.session);
  if (records.length === 0) {
    console.log('No matching records.');
    return;
  }

  const byQuery = {};
  for (const r of records) {
    const qid = r.query_id || 'unassigned';
    byQuery[qid] = byQuery[qid] || [];
    byQuery[qid].push(r);
  }

  console.log(`\n=== Report: session=${flags.session || 'ALL'} (${records.length} tool calls, ${Object.keys(byQuery).length} queries) ===\n`);
  console.log('query_id\tcalls\tduration_s\tinput_tok\toutput_tok\ttotal_tok\tmethod');
  let grandDuration = 0, grandTokens = 0;
  for (const [qid, recs] of Object.entries(byQuery)) {
    const dur = recs.reduce((a, r) => a + r.duration_s, 0);
    const inTok = recs.reduce((a, r) => a + r.input_tokens, 0);
    const outTok = recs.reduce((a, r) => a + r.output_tokens, 0);
    const methods = new Set(recs.map(r => r.token_method));
    grandDuration += dur; grandTokens += inTok + outTok;
    console.log(`${qid}\t${recs.length}\t${dur.toFixed(3)}\t${inTok}\t${outTok}\t${inTok + outTok}\t${[...methods].join(',')}`);
  }
  console.log('\n--- Session totals ---');
  console.log(`Total tool calls: ${records.length}`);
  console.log(`Total duration: ${grandDuration.toFixed(3)} s`);
  console.log(`Total tokens: ${grandTokens}`);
  console.log(`Avg tool calls/query: ${(records.length / Object.keys(byQuery).length).toFixed(2)}`);
  console.log(`Avg duration/query: ${(grandDuration / Object.keys(byQuery).length).toFixed(3)} s`);
  console.log(`Avg tokens/query: ${(grandTokens / Object.keys(byQuery).length).toFixed(1)}`);
  const anyApprox = records.some(r => r.token_method === 'approx' || r.token_method === 'mixed');
  if (anyApprox) console.log('NOTE: some token counts are approximated (add an exact tokenizer package to node_modules for precise counts).');

  if (flags.csv) {
    const csvLines = ['query_id,calls,duration_s,input_tokens,output_tokens,total_tokens,token_method'];
    for (const [qid, recs] of Object.entries(byQuery)) {
      const dur = recs.reduce((a, r) => a + r.duration_s, 0);
      const inTok = recs.reduce((a, r) => a + r.input_tokens, 0);
      const outTok = recs.reduce((a, r) => a + r.output_tokens, 0);
      const methods = [...new Set(recs.map(r => r.token_method))].join('|');
      csvLines.push(`${qid},${recs.length},${dur.toFixed(3)},${inTok},${outTok},${inTok + outTok},${methods}`);
    }
    fs.writeFileSync(flags.csv, csvLines.join('\n'));
    console.log(`\nWrote CSV: ${flags.csv}`);
  }
}

// ---- dispatch -------------------------------------------------------------

const [, , mode, ...rest] = process.argv;
switch (mode) {
  case 'wrap': cmdWrap(rest); break;
  case 'start': cmdStart(rest); break;
  case 'stop': cmdStop(rest); break;
  case 'report': cmdReport(rest); break;
  default:
    console.log(`bench.js -- usage:
  node bench.js wrap   --session S --query Q --tool T -- <command...>
  node bench.js start  --session S --query Q --tool T --input "..." [--input-file path]
  node bench.js stop   --id <call_id> --output "..." [--output-file path]
  node bench.js report --session S [--log bench_log.jsonl] [--csv out.csv]`);
    process.exit(mode ? 1 : 0);
}
