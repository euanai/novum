#!/usr/bin/env node
/**
 * structured-logger.js — Pipeline event logger (Layer 2)
 *
 * Hooks: PostToolUse, PostToolUseFailure, SubagentStart, SubagentStop
 * Output: .research/pipeline-events.jsonl (one JSON object per line)
 *
 * Only active when .research/ exists under cwd — non-/research sessions are unaffected.
 * All hooks are async:true so this never blocks tool execution.
 */

const fs = require('fs');
const path = require('path');

const MAX_SUMMARY_LEN = 500;

function truncate(str, max) {
  if (!str) return '';
  const s = typeof str === 'string' ? str : JSON.stringify(str);
  return s.length > max ? s.slice(0, max) + '...' : s;
}

function main() {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => { input += chunk; });
  process.stdin.on('end', () => {
    let data;
    try {
      data = JSON.parse(input);
    } catch {
      process.exit(0);
    }

    const cwd = data.cwd || process.cwd();
    const researchDir = path.join(cwd, '.research');

    // Only log when .research/ exists (i.e., inside a /research session)
    if (!fs.existsSync(researchDir)) {
      process.exit(0);
    }

    const logPath = path.join(researchDir, 'pipeline-events.jsonl');
    const event = data.hook_event_name;
    const entry = { ts: new Date().toISOString() };

    if (event === 'PostToolUse') {
      entry.session_id = data.session_id || '';
      entry.tool = data.tool_name || '';
      entry.tool_use_id = data.tool_use_id || '';
      entry.input_summary = truncate(data.tool_input, MAX_SUMMARY_LEN);
      entry.response_summary = truncate(data.tool_response, MAX_SUMMARY_LEN);
      entry.cwd = cwd;

      // Detect Bash exit code errors — Claude Code fires PostToolUse (not PostToolUseFailure)
      // for Bash commands with non-zero exit codes, since the Bash tool itself "succeeded".
      const resp = data.tool_response;
      const respStr = typeof resp === 'string' ? resp : JSON.stringify(resp || '');
      if (data.tool_name === 'Bash' && /Exit code [1-9]|Traceback \(most recent|Error:|FAILED/i.test(respStr)) {
        entry.event = 'BashError';
        entry.error = truncate(respStr, MAX_SUMMARY_LEN);
      } else {
        entry.event = 'PostToolUse';
      }
    } else if (event === 'PostToolUseFailure') {
      entry.event = 'PostToolUseFailure';
      entry.session_id = data.session_id || '';
      entry.tool = data.tool_name || '';
      entry.tool_use_id = data.tool_use_id || '';
      entry.input_summary = truncate(data.tool_input, MAX_SUMMARY_LEN);
      entry.error = truncate(data.error || data.tool_response, MAX_SUMMARY_LEN);
      entry.is_interrupt = !!data.is_interrupt;
      entry.cwd = cwd;
    } else if (event === 'SubagentStart') {
      entry.event = 'SubagentStart';
      entry.session_id = data.session_id || '';
      entry.agent_id = data.agent_id || '';
      entry.agent_type = data.agent_type || '';
      entry.agent_name = data.agent_name || data.name || '';
      entry.cwd = cwd;
    } else if (event === 'SubagentStop') {
      entry.event = 'SubagentStop';
      entry.session_id = data.session_id || '';
      entry.agent_id = data.agent_id || '';
      entry.transcript_path = data.transcript_path || '';
      entry.last_message = truncate(data.last_message || data.result, MAX_SUMMARY_LEN);
      entry.cwd = cwd;
    } else {
      // Unknown event type — skip
      process.exit(0);
    }

    try {
      fs.appendFileSync(logPath, JSON.stringify(entry) + '\n');
    } catch {
      // If we can't write, fail silently — logging should never break the pipeline
    }

    process.exit(0);
  });
}

main();
