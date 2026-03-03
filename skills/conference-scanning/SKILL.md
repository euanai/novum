# Conference Scanning Skill

Methodology for efficiently scanning 500+ conference papers to find low-cost research opportunities.

## Overview

Used by `/research --scout "CVPR 2025" --budget=8h` to identify papers that:
1. Fit within a given GPU budget
2. Have open-source code
3. Are relevant to the user's research domain
4. Have high extension potential

## Scanning Strategy

### Funnel Approach — QA-Based Analysis

```
Stage 1: Paper List Fetch (all papers, ~8 min for CVF venues)
  → CVF venues (CVPR/ICCV/ECCV): CVF Open Access (primary) + S2 (abstracts)
  → Non-CVF venues: Semantic Scholar Bulk API (primary)
  → raw-papers.json with title, abstract, authors, pdf_url, paper_id

Stage 2: Cost-First Screening (all papers, ~seconds)
  → Load keyword lists from ~/.claude/research-pipeline/keywords/
  → KILL only if heavy_score >= 2 AND light_score == 0
  → Rank by: cost_score + domain_bonus + code_bonus
  → Top 300 → screened.json

Stage 3: PDF Download to Global Cache (200-400 papers, Master Agent)
  → Check .research/paper-cache/ — skip already-cached papers
  → Batch download: curl -sL → pdftotext → .research/paper-cache/txt/{paper_id}.txt
  → Remove PDFs after conversion (keep only TXT)
  → Update paper-cache/index.json

Stage 4: QA-Based Analysis (dispatched to opportunity-scorer, model: sonnet)
  → Read paper text from paper-cache/txt/{paper_id}.txt
  → Answer 6 questions: method, training, hardware, feasibility, code, value
  → Output: papers-analyzed-batch{N}.json (single file per batch)

Stage 5: Merge (Master Agent, Python)
  → Merge batches → papers-analyzed.json (atomic write)
  → Sort by: feasibility_verdict + research_value
  → Delete batch files

Stage 5.5: Mechanical Feasibility (Python, zero LLM cost)
  → python3 research_utils.py verify_feasibility papers-analyzed.json
  → Adds: mechanical_verdict, mechanical_flags, verdict_disagrees
  → Catches: compute gap (32×H100→1×4090), VRAM overflow (multi-model), time overflow

Stage 5.6: Second-Pass Verification (Sonnet, high-value/flagged papers only)
  → Trigger: research_value≥8 OR verdict_disagrees OR mechanical_flags non-empty
  → Re-read paper with mechanical arithmetic results as constraints
  → Updates feasibility_verdict (preserves original_verdict for audit)

Stage 6: Report Generation (Master Agent)
  → Read papers-analyzed.json → report.md
```

## Dispatch Prompt Templates

### Stage 1: Paper List Fetch (Inline Script — No Worker Dispatch)

Venue-aware strategy — Master Agent runs this directly:
```
CVF venues (CVPR/ICCV/ECCV):
  Phase A: wget CVF Open Access listing page → parse HTML (title, authors, PDF URL)
           Expected: CVPR ~2800+, ICCV ~2000+, ECCV ~2400+
           If < 100 papers → CVF not published yet, fall back to S2
  Phase B: S2 Bulk API (paginated) → title-match to enrich with abstracts
           GET /paper/search/bulk?query=&venue={venue}&year={year}
           &fields=title,abstract,externalIds,openAccessPdf
           Match rate: ~65% (S2 indexing lags behind CVF)
  Phase C: For unmatched papers → parallel scrape CVF paper pages (5 workers)
           Each page has <div id="abstract">...</div>
           ~6 min for ~1000 pages

Non-CVF venues (ICLR/NeurIPS/ICML):
  Primary: Semantic Scholar Bulk API (covers most papers with abstracts)
  TODO: OpenReview API v2 enrichment

Output: $SCOUT_DIR/raw-papers.json
  Fields: title, authors, abstract, pdf_url, paper_id, arxiv_id, doi, s2_paper_id
```

See research.md Step 1 for the complete inline Python script.

### Stage 2: Cost-First Keyword Screening Script

```python
# Cost-first screening (Master Agent runs this inline via python3 -c)
# Key design decisions:
#   - KILL only when confirmed heavy AND no lightweight signal (heavy>=2 & light==0)
#   - Domain match is a BONUS (0-3), NOT a gate — low-cost papers survive without domain match
#   - Deterministic: same input → same output, no LLM judgment
#   - Top 300 fixed cutoff for reproducibility
import json, os

kw_dir = os.path.expanduser('~/.claude/research-pipeline/keywords')
with open(f'{kw_dir}/cv-domains.json') as f:
    domains = json.load(f)['domains']
with open(f'{kw_dir}/lightweight-signals.json') as f:
    lightweight = json.load(f)
with open(f'{kw_dir}/heavy-signals.json') as f:
    heavy = json.load(f)

papers = json.load(open('$SCOUT_DIR/raw-papers.json'))
results = []

for p in papers:
    text = f\"{p.get('title','')} {p.get('abstract','')}\".lower()

    # Lightweight signals (positive cost indicators)
    light_score, light_matches = 0.0, []
    for name, sig in lightweight['positive_signals'].items():
        if any(kw.lower() in text for kw in sig['keywords']):
            light_score += sig['weight']
            light_matches.append(name)

    # Heavy signals (negative cost indicators)
    heavy_score, heavy_matches = 0, []
    for name, sig in heavy['negative_signals'].items():
        if any(kw.lower() in text for kw in sig['keywords']):
            heavy_score += 1
            heavy_matches.append(name)

    # KILL condition: confirmed heavy AND no lightweight signal
    if heavy_score >= 2 and light_score == 0:
        continue

    # Cost score: reward lightweight signals, bonus for zero heavy
    cost_score = min(light_score * 3, 9.0) + (1.0 if heavy_score == 0 else 0)

    # Domain bonus (0-3): additive, NOT a gate
    domain_matches = []
    for dname, dinfo in domains.items():
        if any(kw.lower() in text for kw in dinfo.get('include', [])):
            domain_matches.append(dname)
    domain_bonus = min(len(domain_matches), 3)

    # Code availability bonus (0-2)
    code_bonus = 2 if ('github.com' in text or 'code available' in text or 'code is available' in text) else 0

    composite = cost_score + domain_bonus + code_bonus
    results.append({
        'title': p.get('title',''), 'authors': p.get('authors',''),
        'abstract': p.get('abstract',''), 'pdf_url': p.get('pdf_url',''),
        'paper_id': p.get('paper_id', p.get('forum','')),
        'arxiv_id': p.get('arxiv_id',''), 's2_paper_id': p.get('s2_paper_id',''),
        'cost_score': round(cost_score, 1), 'domain_bonus': domain_bonus,
        'domain_matches': domain_matches, 'code_bonus': code_bonus,
        'composite_score': round(composite, 1),
        'light_matches': light_matches, 'heavy_matches': heavy_matches
    })

results.sort(key=lambda x: x['composite_score'], reverse=True)
top = results[:300]

import sys
sys.path.insert(0, os.path.expanduser('~/.claude/scripts/lib'))
from research_utils import atomic_json_write
atomic_json_write(top, '$SCOUT_DIR/screened.json')

killed = len(papers) - len(results)
print(f'Screening: {len(papers)} total → {killed} killed (heavy>=2 & light==0) → {len(results)} survived → top 300 saved')
print(f'With lightweight signals: {sum(1 for r in top if r[\"light_matches\"])}/{len(top)}')
print(f'No domain match: {sum(1 for r in top if r[\"domain_bonus\"]==0)}/{len(top)}')
```

### Stage 4: opportunity-scorer Dispatch

Copy-paste ready template:
```
Task tool → subagent_type: "general-purpose", model: "sonnet"
name: "opportunity-scorer"
Prompt: "First, Read ~/.claude/agents/opportunity-scorer.md and follow those instructions exactly.
  You are analyzing papers from {venue} {year} for research opportunities.

  HARDWARE: {hardware_description} (e.g., 1x RTX 4090 24GB)
  BUDGET: {budget_hours}h total time

  INPUT:
  - Read $SCOUT_DIR/screened.json for paper metadata
  - Process ONLY these paper_ids: [{comma-separated list}]
  - Paper text pre-cached at: $PAPER_CACHE/txt/{paper_id}.txt

  For EACH paper_id:
  1. Read $PAPER_CACHE/txt/{paper_id}.txt (pre-downloaded — do NOT download PDFs)
  2. Answer 6 questions:
     Q1: Method summary (2-3 sentences)
     Q2: Training requirement? GPU setup? (Quote paper)
     Q3: Hardware & Compute Profile — RAW extraction:
         paper_gpu_type, paper_gpu_count, paper_training_hours,
         largest_model_params_b, num_models_simultaneous,
         peak_vram_reported_gb, reported_gpu_setup
     Q4: Feasibility on {hardware} within {budget_hours}h?
         MANDATORY ARITHMETIC: ratio = gpu_count × SPEED[gpu] / SPEED[4090=0.55]
         Multi-model VRAM: Σ(params×2×1.2)
         (LLM gen ≈ 100-300× forward, diffusion ≈ 20-50× forward)
     Q5: Code URL?
     Q6: Research value 0-10?

  OUTPUT — write ONE file: $SCOUT_DIR/papers-analyzed-batch{N}.json
  Format: [{paper_id, title, method_summary, requires_training,
    paper_gpu_type, paper_gpu_count, paper_training_hours, reported_gpu_setup,
    largest_model_params_b, num_models_simultaneous, peak_vram_reported_gb,
    feasibility_verdict, feasibility_reasoning, estimated_hours,
    code_url, research_value, research_value_reasoning, pdf_status, schema_version: '3.1.0'}]
  Return ONLY a brief summary (NOT full JSON).

  CRITICAL: paper_gpu_count num_models_simultaneous paper_cache
  NEVER fabricate. NEVER download PDFs. Write ONE file only."
```

## GPU Cost Estimation from Papers

Papers typically report GPU info in "Implementation Details" or "Experiments" section:

**Common patterns to look for**:
- "We train on N× [GPU model] for T hours"
- "Training takes T hours on [GPU]"
- "All experiments are conducted on [GPU] with batch size B"
- "Total training cost: N GPU-hours"

**If GPU info is not found**: The opportunity-scorer marks the field as `null` and sets `feasibility_verdict: "insufficient_info"`.

## QA-Based Scoring

Instead of a weighted formula, the opportunity-scorer (Sonnet) reads each paper and directly answers:

| Question | Output Fields | Why QA > Formula |
|----------|--------------|------------------|
| Q1: What method? | `method_summary` | LLM summarizes better than keyword extraction |
| Q2: Training needed? | `requires_training`, `reported_gpu_setup` | Reasoning needed, not pattern matching |
| Q3: Hardware profile? | `paper_gpu_type`, `paper_gpu_count`, `paper_training_hours`, `largest_model_params_b`, `num_models_simultaneous`, `peak_vram_reported_gb` | RAW extraction — LLM reads varied formats, Python does arithmetic |
| Q4: Feasible? | `feasibility_verdict`, `estimated_hours` | Mandatory arithmetic first (v1.4), then LLM judgment with numbers |
| Q5: Code? | `code_url` | WebSearch + verification |
| Q6: Worth it? | `research_value` | Holistic judgment across dimensions |

## Output Format

### Data Files (in .research/scouts/{venue}/)
```
raw-papers.json         ← Stage 1: raw API data
screened.json           ← Stage 2: keyword-filtered candidates
papers-analyzed.json    ← Stage 4/5/5.5/5.6: QA results + mechanical verification
report.md               ← Stage 6: human-readable report
metadata.json           ← Pipeline metadata (version, model, timestamps)
```

### Report Format (report.md)
```markdown
# Scout Report: {venue} {year}
## Summary
- Total papers: N
- After keyword screening: M
- After QA analysis: K
- Feasible: J
- Tight: L
- Not feasible: P
- Insufficient info: Q

## Top 10 Opportunities
| Rank | Title | Verdict | Est. Hours | Training? | GPU Setup | Code | Value |
|------|-------|---------|-----------|-----------|-----------|------|-------|

## Topic Clusters
### Cluster 1: {topic_name}
- Paper A: verdict, reasoning, code status, value
- Paper B: ...

## Budget Analysis
- Feasible papers: N (with reasoning)
- Tight papers: M (what makes them tight)
- Not feasible: K (why not)
```

## Data Sources

| Source | Use For | Rate Limit |
|--------|---------|-----------|
| Semantic Scholar Bulk API | Paper lists + abstracts | 4500/5min (no key) or 1 RPS (with key) |
| OpenReview API v2 | Paper lists (all top venues) | ~500ms interval |
| CVF Open Access | CVPR/ECCV PDFs | 2s interval (polite crawl) |
| arXiv | Preprint PDFs | 20/min |

## Related

- **Agent**: `opportunity-scorer` — executes the QA-based evaluation pipeline
- **Keywords**: `~/.claude/research-pipeline/keywords/` — domain + cost signal keyword lists
- **Command**: `/research --scout` — entry point
