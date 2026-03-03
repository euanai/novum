#!/usr/bin/env node
/**
 * PreToolUse Hook: Prompt Quality Guard (Layer 2)
 *
 * Event: PreToolUse on Task tool
 * Purpose: Ensure Worker dispatch prompts contain required keywords/instructions
 *          so the Master Agent doesn't send under-specified prompts to Workers.
 *
 * Only checks Task calls whose `name` matches a known worker type.
 * Generic Task calls (no name match) are NOT intercepted.
 *
 * Required keywords per worker type are derived from real failure modes:
 * - Scout v1.0 skipped PDF reading because prompt didn't mention it
 * - experiment-runner fabricated results because prompt didn't say "NEVER fabricate"
 * - env-setup used slow proxy because prompt didn't mention "use package mirrors"
 */

const fs = require('fs');

// Read stdin input
let input = {};
try {
  const stdinData = fs.readFileSync(0, 'utf8');
  if (stdinData.trim()) {
    input = JSON.parse(stdinData);
  }
} catch {
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

const toolName = input.tool_name || '';

// Only intercept Task tool calls
if (toolName !== 'Task') {
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

const taskName = (input.tool_input?.name || '').toLowerCase();
const taskPrompt = (input.tool_input?.prompt || '').toLowerCase();

// Required keywords per worker type
// Format: { pattern: RegExp to match task name, keywords: [required strings in prompt] }
const WORKER_REQUIREMENTS = [
  {
    pattern: /sota[-_]?finder/,
    label: 'sota-finder',
    keywords: [
      { term: 'benchmark', reason: 'Must specify which benchmarks to evaluate' },
      { term: 'gpu', reason: 'Must pass GPU hardware constraints' },
      { term: 'smoke test', alt: 'smoke_test', reason: 'Must require 5-min smoke test on candidates' },
      { term: 'anti-pattern', alt: 'anti_pattern', reason: 'Must include anti-patterns section' },
    ]
  },
  {
    pattern: /experiment[-_]?runner/,
    label: 'experiment-runner',
    keywords: [
      { term: 'never fabricate', alt: 'anti-fabrication', reason: 'Must include anti-fabrication constraint' },
      { term: 'anti-pattern', alt: 'anti_pattern', reason: 'Must include anti-patterns section' },
      { term: 'activate', alt: 'venv', reason: 'Must instruct to activate the environment' },
    ]
  },
  {
    pattern: /env[-_]?setup/,
    label: 'env-setup',
    keywords: [
      { term: 'nvidia-smi', alt: 'nvidia_smi', reason: 'Must detect GPU hardware' },
      { term: 'uv venv', alt: 'uv pip', reason: 'Must use uv for environment management' },
      { term: 'mirror', alt: 'proxy', reason: 'Must specify mirror or proxy handling for package installation' },
    ]
  },
  {
    pattern: /opportunity[-_]?scorer/,
    label: 'opportunity-scorer',
    keywords: [
      { term: 'paper_cache', alt: 'paper-cache', reason: 'Must point to global paper cache for pre-downloaded text files' },
      { term: 'feasibility', alt: 'feasible', reason: 'Must ask about hardware feasibility' },
      { term: 'requires_training', alt: 'training required', reason: 'Must ask about training requirement' },
      { term: 'research_value', alt: 'research value', reason: 'Must ask about research value' },
      { term: 'never fabricate', alt: 'anti-fabrication', reason: 'Must include anti-fabrication constraint' },
      { term: 'budget', reason: 'Must pass budget constraint' },
      { term: 'paper_gpu_count', alt: 'gpu_count', reason: 'Must request GPU count extraction (v1.4)' },
      { term: 'num_models_simultaneous', alt: 'simultaneous', reason: 'Must request multi-model count (v1.4)' },
    ]
  },
  {
    pattern: /literature[-_]?search/,
    label: 'literature-searcher',
    keywords: [
      { term: 'semantic scholar', alt: 'semanticscholar', reason: 'Must use Semantic Scholar API' },
      { term: 'deduplic', reason: 'Must deduplicate papers across sources' },
      { term: 'papers-metadata.json', alt: 'papers_metadata', reason: 'Must save to papers-metadata.json' },
    ]
  },
];

// Find matching worker type
let matchedWorker = null;
for (const worker of WORKER_REQUIREMENTS) {
  if (worker.pattern.test(taskName)) {
    matchedWorker = worker;
    break;
  }
}

// No match — not a known worker dispatch, allow
if (!matchedWorker) {
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

// Check required keywords
const missing = [];
for (const kw of matchedWorker.keywords) {
  const hasPrimary = taskPrompt.includes(kw.term);
  const hasAlt = kw.alt ? taskPrompt.includes(kw.alt) : false;
  if (!hasPrimary && !hasAlt) {
    missing.push(`  - "${kw.term}" — ${kw.reason}`);
  }
}

if (missing.length > 0) {
  const missingList = missing.join('\n');
  const errorOutput = {
    hookSpecificOutput: { permissionDecision: 'deny' },
    systemMessage:
      `🚫 Prompt Quality Guard: Dispatch to "${matchedWorker.label}" is missing required keywords.\n\n` +
      `Missing:\n${missingList}\n\n` +
      `Add these to your dispatch prompt. They prevent known failure modes.\n` +
      `Also consider: "First, Read ~/.claude/agents/${matchedWorker.label}.md for your complete instructions."`
  };
  console.error(JSON.stringify(errorOutput));
  process.exit(2);
} else {
  console.log(JSON.stringify({
    continue: true,
    systemMessage: `✅ Prompt Quality: "${matchedWorker.label}" dispatch prompt meets requirements.`
  }));
  process.exit(0);
}
