# Phase Definitions

Detailed specification for each phase of the research pipeline.

## Phase Overview

| Phase | Name | Owner | Key Output |
|-------|------|-------|------------|
| 0 | Scout (optional) | Master + Workers | report.md (in scouts/{venue}/) |
| 1 | Literature Survey | Worker: literature-searcher | literature-review.md, papers-metadata.json |
| 2 | SOTA Codebase | Worker: sota-finder | sota-catalog.json, sota-comparison-table.md |
| 3 | Idea Generation | Master (global context needed) | research-ideas.md, codebase-analysis.md |
| 4 | Experiment Design | Master + Worker: architect | experiment-plan.md, cost-estimate.json |
| 5 | Baseline Reproduction | Workers: env-setup + experiment-runner | baseline-results.json, environment-lock.json |
| 6 | Experiments | Worker: experiment-runner + Master reviews | results.json per experiment |
| 7 | Analysis | Master + Worker: data-analyst | analysis-report.md, figures/ |
| 8 | Paper Draft | Master | paper-draft.md, references.bib |

## Depth Modes

- `full` — All 8 phases
- `survey` — Phase 1-3 only (literature + ideas, no experiments)
- `reproduce` — Phase 1-5 (up to baseline reproduction)

---

## Phase 0: Scout (Optional)

**Trigger**: `/research --scout "CVPR 2025" --budget=8h`

**Steps**:
1. Fetch paper list from OpenReview API / Semantic Scholar Bulk API → raw-papers.json
2. Keyword screening → screened.json
3. Pre-download PDFs to global paper-cache/txt/
4. Dispatch opportunity-scorer: QA-based analysis → papers-analyzed-batch{N}.json
5. Merge batches → papers-analyzed.json
6. Generate report.md

**Worker**: opportunity-scorer (model: sonnet — paper reading + feasibility judgment requires reasoning)

**Data storage**: .research/scouts/{venue}/ + .research/paper-cache/

---

## Phase 1: Literature Survey

**Goal**: Comprehensive literature review with structured gap analysis.

**Steps**:
1. Search papers via Semantic Scholar Bulk API (primary) + WebSearch (supplementary)
   - Round 1: direct topic search
   - Round 2: extract new keywords from Round 1 results → search again
   - Citation chain: find key papers → search "papers citing X"
2. Deduplicate across sources (DOI → ArXiv ID → title Jaccard)
3. Import to Zotero (if available) via MCP tools, with try-catch fallback to local JSON
4. Full-text analysis of core papers (curl PDF → Read tool)
5. Generate 7-dimension literature review:
   - Methods Overview
   - Experimental Comparison
   - Key Differences
   - Taxonomy
   - Overlooked Problems (→ research gaps)
   - Innovation Sparks
   - Practical Tricks

**Outputs**:
- `phase1_literature/literature-review.md` — 7-dimension structured review
- `phase1_literature/papers-metadata.json` — structured paper data (DOI, GitHub URL, GPU cost, verified status)
- `phase1_literature/references.bib` — BibTeX
- `phase1_literature/research-proposal.md` — research proposal
- `phase1_literature/reasoning.md` — search strategy, filtering rationale, gap identification process

**Quality Gate (Phase 1 → 2)**:
- [ ] literature-review.md exists and >2000 words, covers 7 dimensions
- [ ] papers-metadata.json contains ≥10 papers (with GitHub verification status)
- [ ] At least 2 research gaps identified (Dim 5: Overlooked Problems)
- [ ] references.bib is non-empty
- [ ] At least 2 search iteration rounds completed

---

## Phase 2: SOTA Codebase Discovery

**Goal**: Find TOP3 open-source codebases as base for modification.

**Steps**:
1. Determine standard benchmarks from Phase 1 literature review
2. Collect recent top-venue papers on the task (OpenReview API v2 + S2 Bulk API)
3. Download PDFs → Read → extract experimental results tables
4. Cross-validate key metrics (same method's metric confirmed in ≥2 papers)
5. Build SOTA ranking (aligned by experimental setup)
6. For candidates with code: clone repo → 5-minute Smoke Test:
   - Import main module (catch dependency/syntax errors)
   - Instantiate model with dummy config (catch init bugs)
   - Forward pass with random input (catch shape issues)
7. Evaluate code quality: README, Issues, dependencies, CUDA compatibility, modularity
8. Select TOP3 by priority: modifiable > runnable > high-performance

**Diagnostics Accumulation Mode**: Evaluate ALL candidates, collect ALL issues, then select from passing ones. Don't fail-fast on first error.

**Outputs**:
- `phase2_sota/sota-catalog.json` — full catalog with performance, code URL, SHA, dependencies, GPU needs, diagnostics
- `phase2_sota/sota-comparison-table.md` — TOP3 comparison + selection rationale
- `phase2_sota/repos/` — cloned TOP3 repositories (pinned commit)
- `phase2_sota/reasoning.md` — ranking rationale, elimination reasons

**Quality Gate (Phase 2 → 3)**:
- [ ] sota-catalog.json contains ≥3 methods with code
- [ ] TOP3 repos cloned successfully (git-lfs pull completed)
- [ ] Each repo's README has been read
- [ ] Dependency compatibility checked (vs user CUDA/PyTorch version)
- [ ] At least 1 repo passes 5-minute Smoke Test
- [ ] Dataset requirements extracted (name, size, download method)
- [ ] SOTA ranking key metrics cross-validated (≥2 papers per method)

---

## Phase 3: Idea Generation

**Goal**: Generate diverse, feasible research ideas with concrete code mappings.

**Steps**:
0. **Code Deep Dive** (prerequisite): Worker reads base repo core files → produces codebase-analysis.md
1. **Three-source generation**:
   - Literature dimensions: Dim5 (Overlooked) → direction, Dim6 (Sparks), Dim3 (gaps to bridge), Dim7 (trick transfer)
   - Code Deep Dive: forward() bottlenecks, loss simplifications, missing augmentations, hardcoded values
   - Optional: Gemini independent generation (different knowledge blindspots)
2. Enforce diversity: 5+ candidates covering ≥3 types (architecture/training/augmentation/efficiency)
3. For each idea, produce:
   - **Idea→Code Change Mapping Table** (component → file → function → change type → complexity → coupling)
   - **One-sentence positioning**: "We are the first to show that [X] significantly improves [Y] in [Z setting]."
4. **Self-adversarial evaluation** (Advocacy + Prosecution):
   - Advocacy: theoretical basis, literature support, expected gain
   - Prosecution (MUST use WebSearch): has this been done? difference large enough? top 3 failure modes?
   - Verdict: VIABLE / RISKY (backup) / KILLED

**All-killed Fallback**: If 0 VIABLE: re-examine RISKY for differentiation → narrow direction → report suggestions.

**Outputs**:
- `phase3_ideas/codebase-analysis.md` — architecture, data flow, extension points, limitations
- `phase3_ideas/research-ideas.md` — ranked ideas with mapping tables and positioning
- `phase3_ideas/reasoning.md` — advocacy/prosecution process, killed ideas and why

**Quality Gate (Phase 3 → 4)**:
- [ ] codebase-analysis.md completed (Code Deep Dive)
- [ ] research-ideas.md contains ≥3 candidate ideas
- [ ] Each idea has Idea→Code Change mapping (with coupling column)
- [ ] Each VIABLE idea has one-sentence positioning
- [ ] At least 1 idea passes prosecution (VIABLE verdict)
- [ ] Ideas cover ≥2 different types
- [ ] All-killed fallback handled (if 0 VIABLE)

---

## Phase 4: Experiment Design

**Goal**: Concrete experiment plan with cost estimation.

**Steps**:
1. Design standard ablation: baseline, +A only, +B only, +C only, full model
2. Verify all file/function references in plan actually exist in codebase (Grep confirmation)
3. Estimate GPU cost (single run × repeat count, include data download time)
4. Check dataset availability (already have / downloading / need to download)

**Outputs**:
- `phase4_design/experiment-plan.md` — experiment plan with file references
- `phase4_design/architecture-changes.md` — code modification plan
- `phase4_design/cost-estimate.json` — GPU hours, data download, repeat runs
- `phase4_design/reasoning.md` — design decisions, ablation variable selection

**Quality Gate (Phase 4 → 5)**:
- [ ] experiment-plan.md file/function references verified to exist in codebase
- [ ] GPU cost estimate ≤ user budget (including ≥3 repeat experiments)
- [ ] Ablation design includes standard 5 groups
- [ ] Dataset availability confirmed

---

## Phase 5: Baseline Reproduction

**Goal**: Reproduce baseline results on user's hardware.

**Steps**:
1. env-setup Worker: detect hardware → uv venv → install dependencies → check git-lfs → download data
2. Run baseline training/evaluation
3. Compare results vs paper values:
   - <5%: excellent
   - 5-15%: acceptable (normal GPU/seed variation)
   - >15%: investigate (check config, data, environment) → 2 fix attempts → mark as "imprecise" and continue

**Parallel Strategy**: Reproduce TOP2 bases simultaneously. P(≥1 success) = 1-(1-0.4)² = 64%.

**Outputs**:
- `phase5_baseline/environment-lock.json` — frozen environment
- `phase5_baseline/baseline-results.json` — metrics vs paper values
- `phase5_baseline/reproduction-log.md` — full log
- `phase5_baseline/reasoning.md` — environment issues, metric deviation analysis

**Quality Gate (Phase 5 → 6)**:
- [ ] Base code runs on user's environment (validation script succeeds)
- [ ] Baseline metrics vs paper deviation <15% (or marked "imprecise")
- [ ] environment-lock.json generated

---

## Phase 6: Experiments

**Goal**: Implement ideas, verify, and train.

**Three-level verification**:
- Level 1: Micro-experiment (100 steps, <5min) — code correctness
- Level 2: Medium experiment (10% data, full epochs, ~2-3h) — method effectiveness + hyperparameter search
- Level 3: Full training (100% data, nohup background) — final results

**Code modification flow**: See `micro-experiment.md` for details.

**Outputs per experiment**:
- `phase6_experiments/{exp_name}/config.yaml`
- `phase6_experiments/{exp_name}/results.json` (script-generated, LLM read-only)
- `phase6_experiments/{exp_name}/sanity-check.json` (script-generated, LLM read-only)
- `phase6_experiments/{exp_name}/reasoning.md`

**Quality Gate (Phase 6 → 7)**:
- [ ] At least 1 experiment has valid results.json (non-empty, no NaN)
- [ ] Main metric exceeds baseline ≥1.0% (or domain standard deviation)
- [ ] sanity-check.json all passed
- [ ] No fabricated data flags
- [ ] 3-layer code review completed (Layer 1/2/3 all passed)

---

## Phase 7: Analysis

**Goal**: Statistical analysis and narrative construction.

**Steps**:
1. Collect all experiment results
2. Statistical tests (p-value, confidence intervals) with ≥3 repeated runs
3. Generate comparison tables (LaTeX-ready) with Params, FLOPs, Train Time columns
4. Create visualization figures
5. Build narrative structure (story-outline.md based on Phase 3 positioning)

**Outputs**:
- `phase7_analysis/analysis-report.md`
- `phase7_analysis/results-table.md`
- `phase7_analysis/story-outline.md`
- `phase7_analysis/figures/`
- `phase7_analysis/reasoning.md`

**Quality Gate (Phase 7 → 8)**:
- [ ] analysis-report.md exists
- [ ] Statistical tests executed
- [ ] At least 1 comparison figure generated

---

## Phase 8: Paper Draft

**Goal**: High-quality paper draft (not submission-ready, needs user polish).

**Steps**:
1. Use ml-paper-writing skill for structure
2. Use writing-anti-ai skill for naturalness
3. Use paper-self-review skill for quality check
4. Generate verified references.bib from Zotero/metadata

**Outputs**:
- `phase8_writing/paper-draft.md`
- `phase8_writing/paper-review.md` — self-review report
- `phase8_writing/references.bib` — verified citations
- `phase8_writing/reasoning.md`

---

## State Schema (v2)

```json
{
  "schema_version": 2,
  "topic": "string",
  "current_phase": "phase1_literature",
  "depth": "full|survey|reproduce",
  "created_at": "ISO timestamp",
  "last_updated": "ISO timestamp",
  "phases": {
    "phase1_literature": {
      "status": "pending|in_progress|completed|gate_failed",
      "started_at": "ISO timestamp",
      "completed_at": "ISO timestamp",
      "gate_results": {
        "papers_count": 0,
        "gaps_found": 0,
        "all_passed": false
      }
    }
  },
  "budget": {
    "gpu_hours_estimated": 0,
    "gpu_hours_used": 0
  },
  "training_jobs": [
    {
      "name": "baseline",
      "pid_file": ".research/phase5_baseline/pid",
      "log_path": "training.log",
      "done_flag": ".done",
      "fail_flag": ".failed",
      "status": "running|completed|failed|crashed"
    }
  ],
  "workers": [],
  "failures": [],
  "resource_usage": {}
}
```

## Session Resumption Protocol

When `/research --resume` is called:
1. Read state.json → current phase + history
2. Read current phase's reasoning.md → restore decision context
3. Read key predecessor outputs (phase-specific, see get_resume_context())
4. Check training_jobs → PID alive? .done/.failed flags?
5. Output resumption summary to user
