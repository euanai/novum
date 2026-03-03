# Research Automation Skill

Fully automated research pipeline from literature survey to paper draft.

## Overview

The `/research` command orchestrates an 8-phase pipeline driven by a Master Agent (you) that acts as a "Research PI" — making decisions, reviewing outputs, debugging failures, and iterating until completion.

## Architecture

```
Master Agent (you, Opus) — full-context decision maker
├── Phase 1: Literature Survey        ← Worker: literature-searcher
├── Phase 2: SOTA Codebase Discovery  ← Worker: sota-finder
├── Phase 3: Idea Generation          ← Master does this (needs global context)
├── Phase 4: Experiment Design        ← Master does this
├── Phase 5: Baseline Reproduction    ← Workers: env-setup + experiment-runner
├── Phase 6: Experiments              ← Worker: experiment-runner + Master reviews
├── Phase 7: Analysis                 ← Master + Worker: data-analyst
└── Phase 8: Paper Draft              ← Master does this
```

## Key Principles

1. **Failure is information, not disaster** — every phase outputs either success results or valuable failure analysis
2. **Iterate like a human researcher** — no hard retry limits; Master Agent judges when to debug, pivot, or stop
3. **Three-layer correctness** — Information correctness (Phase 1-2) → Code correctness (Phase 5-6) → Conclusion correctness (Phase 7-8)
4. **Never fabricate** — results.json is write-protected by PreToolUse hook; report failures honestly
5. **Cross-session resumable** — state.json + reasoning.md enable any new session to continue

## State Management

All state lives in `.research/state.json`. See `references/phase-definitions.md` for the full schema.

Key operations:
```python
from scripts.lib.research_utils import StateManager, create_research_dirs

sm = StateManager(".research")
state = sm.init("your topic", depth="full")
state = sm.advance_phase(state, "phase1_literature", "phase2_sota")
context = sm.get_resume_context(state)  # For --resume
```

## Phase Transition Gates

Each phase must pass a quality gate before advancing. Gates are defined in `references/phase-definitions.md`.

**If a gate fails**: attempt auto-fix (max 2 tries) → still fails → mark phase as `gate_failed` → report in final output what was achieved.

## Three Defense Layers

1. **Micro-experiment verification** (after code changes, before full training) — see `references/micro-experiment.md`
2. **Reproduction comparison** (baseline vs paper values, <5% = excellent, 5-15% = acceptable, >15% = investigate)
3. **Anti-fabrication** (PreToolUse hook blocks Write/Edit to results.json — mechanical enforcement)

## Error Handling

Errors are classified into 6 categories with typed recovery routes. See `references/error-taxonomy.md`.

## API Reference

Verified API endpoints for paper search and SOTA discovery. See `references/api-reference.md`.

## Related Components

- **Command**: `/research` — main entry point
- **Agents**: `sota-finder`, `env-setup`, `experiment-runner`, `opportunity-scorer`
- **Reused agents**: `literature-reviewer`, `data-analyst`, `architect`, `code-reviewer`
- **Reused skills**: `ml-paper-writing`, `paper-self-review`, `writing-anti-ai`, `citation-verification`
