#!/usr/bin/env node
/**
 * PreToolUse Hook: Phase Gate Guard (Layer 1)
 *
 * Event: PreToolUse on Write|Edit targeting state.json
 * Purpose: Mechanically enforce phase transition prerequisites.
 *
 * When the Master Agent tries to advance current_phase in state.json,
 * this hook checks that all prerequisite files exist and meet quality thresholds.
 * If prerequisites are not met, the write is DENIED with a systemMessage
 * listing exactly what's missing.
 *
 * Gate rules are hardcoded from phase-definitions.md — deterministic, zero-cost,
 * unit-testable.
 */

const fs = require('fs');
const path = require('path');

// Read stdin input
let input = {};
try {
  const stdinData = fs.readFileSync(0, 'utf8');
  if (stdinData.trim()) {
    input = JSON.parse(stdinData);
  }
} catch {
  // Allow on parse failure
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

const toolName = input.tool_name || '';
const cwd = input.cwd || process.cwd();

// Only intercept Write/Edit to state.json inside .research/
if (toolName !== 'Write' && toolName !== 'Edit') {
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

const filePath = input.tool_input?.file_path || '';
const resolved = path.resolve(cwd, filePath);

if (!resolved.includes('.research') || path.basename(resolved) !== 'state.json') {
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

// --- Detect phase advancement ---
// For Write: parse full new content for current_phase
// For Edit: check if new_string contains current_phase change
let newPhase = null;

if (toolName === 'Write') {
  const content = input.tool_input?.content || '';
  try {
    const parsed = JSON.parse(content);
    newPhase = parsed.current_phase || null;
  } catch {
    // Not valid JSON write to state.json — let research-guard handle
    console.log(JSON.stringify({ continue: true }));
    process.exit(0);
  }
} else if (toolName === 'Edit') {
  const newString = input.tool_input?.new_string || '';
  // Match patterns like "current_phase": "phase2_sota"
  const match = newString.match(/"current_phase"\s*:\s*"([^"]+)"/);
  if (match) {
    newPhase = match[1];
  }
}

if (!newPhase) {
  // Not a phase advancement — allow
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

// Read current state to detect FROM which phase
let currentPhase = null;
try {
  const stateContent = fs.readFileSync(resolved, 'utf8');
  const state = JSON.parse(stateContent);
  currentPhase = state.current_phase || null;
} catch {
  // No existing state or parse error — allow (first init)
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

// If not actually changing phase, allow
if (currentPhase === newPhase) {
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

// --- Cycle suffix handling ---
// phase3_ideas_c2 → phase3_ideas (strip _cN suffix for rule matching)
function basePhase(phase) {
  return phase.replace(/_c\d+$/, '');
}

// Extract cycle number from phase name (0 if no suffix)
function cycleOf(phase) {
  const m = phase.match(/_c(\d+)$/);
  return m ? parseInt(m[1], 10) : 0;
}

// --- Gate Rules ---
// Each gate maps: "fromPhase→toPhase" => array of checks
// Check types: { file, minWords?, minEntries?, nonEmpty? }

const researchDir = path.dirname(resolved);

const GATE_RULES = {
  'phase1_literature→phase2_sota': [
    { file: 'phase1_literature/literature-review.md', minWords: 2000, label: 'literature-review.md (>2000 words)' },
    { file: 'phase1_literature/papers-metadata.json', minEntries: 10, label: 'papers-metadata.json (>=10 entries)' },
    { file: 'phase1_literature/references.bib', nonEmpty: true, label: 'references.bib (non-empty)' },
    { file: 'phase1_literature/reasoning.md', exists: true, label: 'reasoning.md (exists)' },
  ],
  'phase2_sota→phase3_ideas': [
    { file: 'phase2_sota/sota-catalog.json', minEntriesWithField: { count: 3, field: 'code.url' }, label: 'sota-catalog.json (>=3 candidates with code.url)' },
    { file: 'phase2_sota/sota-comparison-table.md', minWords: 500, label: 'sota-comparison-table.md (>500 words)' },
    { file: 'phase2_sota/reasoning.md', exists: true, label: 'reasoning.md (exists)' },
  ],
  'phase3_ideas→phase4_design': [
    { file: 'phase3_ideas/research-hypotheses.md', minIdeas: 3, altFile: 'phase3_ideas/research-ideas.md', label: 'research-hypotheses.md (>=3 hypotheses/ideas)' },
    { file: 'phase3_ideas/codebase-analysis.md', exists: true, label: 'codebase-analysis.md (exists)' },
    { file: 'phase3_ideas/reasoning.md', exists: true, label: 'reasoning.md (exists)' },
  ],
  'phase4_design→phase5_baseline': [
    { file: 'phase4_design/experiment-plan.md', exists: true, label: 'experiment-plan.md (exists)' },
    { file: 'phase4_design/cost-estimate.json', exists: true, label: 'cost-estimate.json (exists)' },
    { file: 'phase4_design/tournament-budget.json', exists: true,
      label: 'tournament-budget.json (tournament budget allocation)' },  // v1.12
    { file: 'phase4_design/reasoning.md', exists: true, label: 'reasoning.md (exists)' },
  ],
  'phase5_baseline→phase6_experiments': [
    { file: 'phase5_baseline/baseline-results.json', nonEmpty: true, label: 'baseline-results.json (non-empty)' },
    { file: 'phase5_baseline/environment-lock.json', exists: true, label: 'environment-lock.json (exists)' },
    { file: 'phase5_baseline/reasoning.md', exists: true, label: 'reasoning.md (exists)' },
  ],
  'phase6_experiments→phase7_analysis': [
    { file: 'phase6_experiments', hasResultsJson: true, label: '>=1 results.json (non-empty) in experiments' },
    { file: 'phase6_experiments', hasSanityCheck: true, label: 'sanity-check.json exists in experiments' },
    { file: 'phase6_experiments/tournament.json', tournamentCheck: true,
      label: 'tournament.json (completed, champion identified)' },  // v1.12
    // reasoning.md can be in any experiment subdir
  ],
  'phase7_analysis→phase8_writing': [
    { file: 'phase7_analysis/analysis-report.md', exists: true, label: 'analysis-report.md (exists)' },
    { file: 'phase7_analysis/results-table.md', exists: true, label: 'results-table.md (exists)' },
    { file: 'phase7_analysis/reasoning.md', exists: true, label: 'reasoning.md (exists)' },
    // v1.10: Innovation must beat baseline — publishability verdict required
    { file: 'phase7_analysis/publishability-verdict.json', publishabilityCheck: true,
      label: 'publishability-verdict.json (verdict=pass, improvement>0)' },
  ],
};

// Use base phase names (without _cN suffix) for rule matching
const baseFrom = basePhase(currentPhase);
const baseTo = basePhase(newPhase);
const gateKey = `${baseFrom}→${baseTo}`;
const rules = GATE_RULES[gateKey];

// Determine cycle context for iteration-specific checks
const fromCycle = cycleOf(currentPhase);
const toCycle = cycleOf(newPhase);

if (!rules) {
  // Unknown transition (e.g., scout mode, or skipping phases) — allow
  console.log(JSON.stringify({ continue: true }));
  process.exit(0);
}

// --- Check each rule ---
const failures = [];

// Resolve file paths: replace base phase dir names with cycle-suffixed versions
function resolveRulePath(filePath) {
  if (fromCycle === 0) return filePath;
  // Replace known phase directory prefixes with cycle-suffixed versions
  const phasePrefix = filePath.split('/')[0];
  const knownPhases = [
    'phase3_ideas', 'phase3_5_quickval', 'phase4_design',
    'phase5_baseline', 'phase6_experiments', 'phase7_analysis', 'phase8_writing'
  ];
  if (knownPhases.includes(phasePrefix)) {
    return filePath.replace(phasePrefix, `${phasePrefix}_c${fromCycle}`);
  }
  return filePath;
}

for (const rule of rules) {
  let fullPath = path.join(researchDir, resolveRulePath(rule.file));

  // Special: Phase 6 experiment directory checks
  if (rule.hasResultsJson) {
    const found = findInExperimentDirs(fullPath, 'results.json');
    if (!found) {
      failures.push(`❌ ${rule.label}`);
    }
    continue;
  }
  if (rule.hasSanityCheck) {
    const found = findInExperimentDirs(fullPath, 'sanity-check.json');
    if (!found) {
      failures.push(`❌ ${rule.label}`);
    }
    continue;
  }

  // v1.10: Publishability verdict check — innovation must beat baseline
  if (rule.publishabilityCheck) {
    if (!fs.existsSync(fullPath)) {
      failures.push(`❌ ${rule.label} — file not found. Phase 7 must produce publishability-verdict.json.`);
    } else {
      try {
        const content = fs.readFileSync(fullPath, 'utf8');
        const verdict = JSON.parse(content);
        if (verdict.verdict !== 'pass') {
          failures.push(`❌ ${rule.label} — verdict is "${verdict.verdict}", not "pass". ` +
            `Innovation must beat baseline to write a paper. Use failure exit path.`);
        } else if (typeof verdict.improvement_pct === 'number' && verdict.improvement_pct <= 0) {
          failures.push(`❌ ${rule.label} — improvement_pct=${verdict.improvement_pct}% (<=0). ` +
            `No measurable improvement over baseline.`);
        }
      } catch (e) {
        failures.push(`❌ ${rule.label} — cannot read or parse JSON: ${e.message}`);
      }
    }
    continue;
  }

  // v1.12: Tournament completion check
  if (rule.tournamentCheck) {
    if (!fs.existsSync(fullPath)) {
      failures.push(`❌ ${rule.label} — tournament.json not found. Phase 6 requires tournament protocol.`);
    } else {
      try {
        const tournament = JSON.parse(fs.readFileSync(fullPath, 'utf8'));
        if (tournament.status !== 'completed') {
          failures.push(`❌ ${rule.label} — tournament status is "${tournament.status}", not "completed".`);
        }
        if (!tournament.champion) {
          failures.push(`❌ ${rule.label} — no champion. All hypotheses failed? Use failure exit path.`);
        }
      } catch (e) {
        failures.push(`❌ ${rule.label} — cannot parse tournament.json: ${e.message}`);
      }
    }
    continue;
  }

  // File existence check — with altFile fallback (e.g., research-hypotheses.md → research-ideas.md)
  if (!fs.existsSync(fullPath) && rule.altFile) {
    const altFullPath = path.join(researchDir, resolveRulePath(rule.altFile));
    if (fs.existsSync(altFullPath)) {
      fullPath = altFullPath;
    }
  }
  if (!fs.existsSync(fullPath)) {
    failures.push(`❌ ${rule.label} — file not found`);
    continue;
  }

  // exists-only check
  if (rule.exists) {
    continue; // Already passed existence check
  }

  // Non-empty check
  if (rule.nonEmpty) {
    const stat = fs.statSync(fullPath);
    if (stat.size < 10) {
      failures.push(`❌ ${rule.label} — file is empty or too small`);
    }
    continue;
  }

  // Word count check
  if (rule.minWords) {
    try {
      const content = fs.readFileSync(fullPath, 'utf8');
      const wordCount = content.split(/\s+/).filter(w => w.length > 0).length;
      if (wordCount < rule.minWords) {
        failures.push(`❌ ${rule.label} — only ${wordCount} words (need ${rule.minWords})`);
      }
    } catch {
      failures.push(`❌ ${rule.label} — cannot read file`);
    }
    continue;
  }

  // JSON entry count check
  if (rule.minEntries) {
    try {
      const content = fs.readFileSync(fullPath, 'utf8');
      const data = JSON.parse(content);
      const entries = Array.isArray(data) ? data : (data.candidates || data.papers || data.entries || data.methods || []);
      if (entries.length < rule.minEntries) {
        failures.push(`❌ ${rule.label} — only ${entries.length} entries (need ${rule.minEntries})`);
      }
    } catch {
      failures.push(`❌ ${rule.label} — invalid JSON or cannot read`);
    }
    continue;
  }

  // JSON entries with specific field check
  if (rule.minEntriesWithField) {
    try {
      const content = fs.readFileSync(fullPath, 'utf8');
      const data = JSON.parse(content);
      const entries = Array.isArray(data) ? data : (data.candidates || data.papers || data.entries || data.methods || data.catalog || []);
      const { count, field } = rule.minEntriesWithField;
      // Support dot-notation field paths (e.g., "code.url" → e.code.url)
      const getNestedField = (obj, path) => {
        return path.split('.').reduce((o, k) => (o && o[k] !== undefined) ? o[k] : undefined, obj);
      };
      const matching = entries.filter(e => {
        const val = getNestedField(e, field);
        return val && val !== '' && val !== 'unknown';
      });
      if (matching.length < count) {
        failures.push(`❌ ${rule.label} — only ${matching.length} entries with ${field} (need ${count})`);
      }
    } catch {
      failures.push(`❌ ${rule.label} — invalid JSON or cannot read`);
    }
    continue;
  }

  // Idea/hypothesis count check (search for ## or ### headers)
  if (rule.minIdeas) {
    try {
      const content = fs.readFileSync(fullPath, 'utf8');
      // Count idea/hypothesis headers
      const ideaPatterns = [
        /^##\s+Idea\s/gm,
        /^###\s+Idea\s/gm,
        /^\d+\.\s+\*\*(?:Idea|idea)/gm,
        /^##\s+\d+\./gm,
        /^##\s+Hypothesis\s+H?\d+/gmi,  // v1.8 hypothesis card: "## Hypothesis H1: ..."
      ];
      let ideaCount = 0;
      for (const pattern of ideaPatterns) {
        const matches = content.match(pattern);
        if (matches) ideaCount = Math.max(ideaCount, matches.length);
      }
      if (ideaCount < rule.minIdeas) {
        failures.push(`❌ ${rule.label} — found ~${ideaCount} ideas (need ${rule.minIdeas})`);
      }
    } catch {
      failures.push(`❌ ${rule.label} — cannot read file`);
    }
    continue;
  }
}

// --- v1.11: Freshness warning on Phase 2 → Phase 3 transition ---
if (baseFrom === 'phase2_sota' && baseTo === 'phase3_ideas' && failures.length === 0) {
  const reasoningPath = path.join(researchDir, 'phase2_sota', 'reasoning.md');
  if (fs.existsSync(reasoningPath)) {
    const content = fs.readFileSync(reasoningPath, 'utf8');
    if (!/freshness\s+analysis/i.test(content)) {
      // Soft warning (exit 0 + systemMessage) — does NOT block advancement
      console.error(JSON.stringify({
        continue: true,
        systemMessage: '⚠️ sota-finder reasoning.md has no "Freshness Analysis" section. ' +
                      'SOTA baseline may be outdated. Ensure arXiv recency check was performed (v1.11).'
      }));
    }
  }
}

// --- v1.11: EXPLORE hypothesis warning on Phase 3 → Phase 4 transition ---
if (baseFrom === 'phase3_ideas' && baseTo === 'phase4_design' && failures.length === 0) {
  const hypothesesDir = fromCycle > 0 ? `phase3_ideas_c${fromCycle}` : 'phase3_ideas';
  const hypothesesPath = path.join(researchDir, hypothesesDir, 'research-hypotheses.md');
  if (fs.existsSync(hypothesesPath)) {
    const content = fs.readFileSync(hypothesesPath, 'utf8');
    if (!/track:\s*EXPLORE/i.test(content)) {
      console.error(JSON.stringify({
        continue: true,
        systemMessage: '⚠️ No EXPLORE hypothesis found in research-hypotheses.md. ' +
                      'Pipeline should have at least 1 cross-domain exploration hypothesis (v1.11). ' +
                      'Check Step 3.1B problem decomposition and cross-domain search.'
      }));
    }
  }
}

// --- v1.9: Knowledge base consultation soft warning ---

// Helper: find workspace .research/ (the one with paper-cache/)
function findWorkspaceResearch(projectResearchDir) {
  let current = path.resolve(projectResearchDir);
  for (let i = 0; i < 5; i++) {
    const parent = path.dirname(current);
    const candidate = path.join(parent, '.research');
    if (candidate !== current && fs.existsSync(candidate) &&
        fs.existsSync(path.join(candidate, 'paper-cache'))) {
      return candidate;
    }
    current = parent;
  }
  return null;
}

// Warn if knowledge base exists but Phase 3 reasoning.md
// does not mention "Knowledge Summary" or any constraint ID (C-NNNN)
if (baseFrom === 'phase3_ideas' && baseTo === 'phase4_design') {
  const wsResearch = findWorkspaceResearch(researchDir);
  if (wsResearch && fs.existsSync(path.join(wsResearch, 'knowledge', 'index.json'))) {
    const reasoningDir = fromCycle > 0 ? `phase3_ideas_c${fromCycle}` : 'phase3_ideas';
    const reasoningPath = path.join(researchDir, reasoningDir, 'reasoning.md');
    if (fs.existsSync(reasoningPath)) {
      const content = fs.readFileSync(reasoningPath, 'utf8');
      if (!/knowledge\s+summary/i.test(content) && !/C-\d{4}/i.test(content)) {
        // Soft warning: systemMessage does not block advancement
        console.error(JSON.stringify({
          continue: true,
          systemMessage: '\u26a0\ufe0f Knowledge base exists at ' + wsResearch + '/knowledge/ ' +
            'but reasoning.md has no "Knowledge Summary" section. ' +
            'Run Step 3.0.5 to load cross-project constraints and avoid dead ends.'
        }));
        process.exit(0);
      }
    }
  }
}

// --- Iteration-specific checks (v1.8) ---

// 2a. Phase 3 → Phase 4 (cycle >= 1): require learned-constraints.md with 5-Whys depth
if (baseFrom === 'phase3_ideas' && baseTo === 'phase4_design' && fromCycle >= 1) {
  const lcPath = path.join(researchDir, 'learned-constraints.md');
  if (!fs.existsSync(lcPath)) {
    failures.push(`❌ learned-constraints.md — required for cycle ${fromCycle} (big loop must record 5-Whys root causes)`);
  } else {
    try {
      const lcContent = fs.readFileSync(lcPath, 'utf8');
      const lcWords = lcContent.split(/\s+/).filter(w => w.length > 0).length;
      if (lcWords < 200) {
        failures.push(`❌ learned-constraints.md — only ${lcWords} words (need ≥200 for root-cause depth)`);
      }
      if (!/Why/i.test(lcContent)) {
        failures.push(`❌ learned-constraints.md — missing "Why" keywords (5-Whys analysis required)`);
      }
    } catch {
      failures.push(`❌ learned-constraints.md — cannot read file`);
    }
  }
}

// 2b. Phase 4 → Phase 5/6: require mechanism proxy in experiment-plan.md
if (baseFrom === 'phase4_design' && (baseTo === 'phase5_baseline' || baseTo === 'phase6_experiments')) {
  const planDir = fromCycle > 0 ? `phase4_design_c${fromCycle}` : 'phase4_design';
  const planPath = path.join(researchDir, planDir, 'experiment-plan.md');
  if (fs.existsSync(planPath)) {
    try {
      const planContent = fs.readFileSync(planPath, 'utf8');
      if (!/mechanism.proxy|mechanism.verification/i.test(planContent)) {
        failures.push(`❌ experiment-plan.md — missing mechanism proxy section (v1.8 requires mechanism verification design)`);
      }
    } catch {
      // File read error — other checks will catch this
    }
  }
}

// 2c. Phase 7 → Phase 8: budget + cycle interlock
// DENY if: cycle < 2 AND gpu_used/gpu_estimated < 0.5 AND cycle < max_cycles
// Meaning: budget and cycles not exhausted → go back and iterate, don't write paper yet
if (baseFrom === 'phase7_analysis' && baseTo === 'phase8_writing') {
  try {
    const stateContent = fs.readFileSync(resolved, 'utf8');
    const state = JSON.parse(stateContent);
    const iteration = state.iteration || { cycle: 0, max_cycles: 5 };
    const budget = state.budget || { gpu_hours_estimated: 0, gpu_hours_used: 0 };
    const gpuRatio = budget.gpu_hours_estimated > 0
      ? budget.gpu_hours_used / budget.gpu_hours_estimated
      : 1.0; // If no estimate, don't block
    const cycleExhausted = iteration.cycle >= iteration.max_cycles;
    const budgetExhausted = gpuRatio >= 0.5;

    if (iteration.cycle < 2 && !budgetExhausted && !cycleExhausted) {
      failures.push(
        `❌ Iteration interlock: cycle=${iteration.cycle} (<2), GPU=${(gpuRatio * 100).toFixed(0)}% (<50%), ` +
        `max_cycles=${iteration.max_cycles} not reached. ` +
        `Pipeline goal is top-venue publication — iterate more before writing. ` +
        `Use Failure Diagnosis Protocol → regress to Phase 3 or 4.`
      );
    }
  } catch {
    // State parse error — allow (fail-open for this soft check)
  }
}

// --- Output decision ---
if (failures.length > 0) {
  const failureList = failures.join('\n');
  const errorOutput = {
    hookSpecificOutput: { permissionDecision: 'deny' },
    systemMessage:
      `🚫 Phase Gate Guard: Cannot advance from ${currentPhase} → ${newPhase}\n\n` +
      `The following prerequisites are not met:\n${failureList}\n\n` +
      `Complete these items before advancing. Write results to reasoning.md as you fix each one.`
  };
  console.error(JSON.stringify(errorOutput));
  process.exit(2);
} else {
  console.log(JSON.stringify({
    continue: true,
    systemMessage: `✅ Phase Gate: All prerequisites met for ${currentPhase} → ${newPhase}. Advancing.`
  }));
  process.exit(0);
}

// --- Helper: find a specific file in experiment subdirectories ---
function findInExperimentDirs(baseDir, filename) {
  if (!fs.existsSync(baseDir) || !fs.statSync(baseDir).isDirectory()) {
    return false;
  }
  try {
    const entries = fs.readdirSync(baseDir, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.isDirectory()) {
        const candidate = path.join(baseDir, entry.name, filename);
        if (fs.existsSync(candidate)) {
          const stat = fs.statSync(candidate);
          if (stat.size > 2) return true; // Non-empty (more than just "{}")
        }
      }
    }
  } catch {
    // Directory read error
  }
  return false;
}
