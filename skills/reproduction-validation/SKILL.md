# Reproduction Validation Skill

Methodology for validating codebase reproduction and ensuring experimental integrity.

## Overview

Ensures that:
1. Baseline reproduction metrics match paper-reported values
2. Code modifications are verified before full training
3. Experimental results are genuine (not fabricated)

## Reproduction Quality Tiers

| Deviation from Paper | Tier | Action |
|---------------------|------|--------|
| <5% | Excellent | Proceed to experiments |
| 5-15% | Acceptable | Note deviation, likely GPU/seed variation |
| >15% | Investigate | Check config, data, environment |

## Common Reproduction Failure Modes

Based on Raff (2019) study of 255 papers:

### Environment Issues
- **CUDA version mismatch**: Paper uses CUDA 11.x, repo requires 12.x
  - Fix: install matching PyTorch build for your CUDA version
- **Undocumented preprocessing**: Data needs preprocessing not mentioned in README
  - Fix: check `data/` directory for scripts, read Issues for hints
- **Missing dependencies**: requirements.txt is incomplete
  - Fix: run code, install missing packages iteratively
- **Hardcoded paths**: `/home/author/data/` in source code
  - Fix: Grep for hardcoded paths, replace with config values

### Training Issues
- **Unreported hyperparameters**: Paper doesn't mention weight decay, warmup steps, etc.
  - Fix: check codebase config files/defaults, reference similar papers
- **Unreported data augmentation**: Significant augmentation not mentioned in paper
  - Fix: read data loading code carefully
- **Random seed sensitivity**: Results vary ±2-3% across seeds
  - Fix: run 3× with different seeds, report mean ± std
- **Pretrained weight version**: Different checkpoint version gives different baseline
  - Fix: verify exact checkpoint URL/hash from repo

### Data Issues
- **Dataset version drift**: Dataset updated since paper was published
  - Fix: check for version tags, download specific version
- **Train/val split mismatch**: Different split ratios or random seeds
  - Fix: use exact split files from repo (often in `data/splits/`)
- **Preprocessing pipeline**: Normalization, resizing, cropping differences
  - Fix: compare preprocessing code vs paper description

## Three-Layer Defense System

### Layer 1: Micro-Experiment Verification

After every code modification, before any training:

```
✓ Forward pass: output shape correct, no NaN/Inf
✓ Gradient flow: all new parameters have non-zero gradients
✓ Micro-training: 100 steps, loss decreases
✓ Before/after: same batch produces different outputs
```

**Cost**: <5 minutes. **Catches**: ~90% of code-level errors.

### Layer 2: Reproduction Comparison

After baseline training completes:

```
✓ Compare metric values vs paper Table X
✓ Check evaluation protocol matches (same split, same metric)
✓ Record deviation and assess tier (excellent/acceptable/investigate)
```

### Layer 3: Anti-Fabrication (Mechanical Enforcement)

```
✓ results.json is ONLY written by training scripts (hook-protected)
✓ sanity-check.json is ONLY written by validation scripts (hook-protected)
✓ LLM can Read these files but NOT Write/Edit them
✓ If results.json doesn't exist → report "experiment incomplete"
```

This is CODE-LEVEL enforcement via PreToolUse hook, not a prompt-level request.

## Reproducibility Checklist

Before reporting any experimental result, verify:

- [ ] Environment is locked (environment-lock.json exists)
- [ ] Random seeds are fixed and recorded
- [ ] Config files used for this run are saved
- [ ] Training log exists and shows no errors
- [ ] Evaluation uses same protocol as baseline
- [ ] Results come from training script output (not manually entered)
- [ ] Deviation from paper baseline is documented

## Fairness Constraints

For any experiment comparison:
1. Same data augmentation for our method AND baseline
2. Same hyperparameter search budget
3. Report: Method | Metric | Params | FLOPs | Train Time
4. Baseline re-run with same augmentation/tricks if we add any

## Related

- **Agent**: `experiment-runner` — executes experiments with anti-fabrication constraints
- **Agent**: `env-setup` — builds reproducible environments
- **Reference**: `micro-experiment.md` — detailed verification protocol
- **Reference**: `error-taxonomy.md` — error classification and recovery
