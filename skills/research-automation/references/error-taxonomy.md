# Error Taxonomy and Recovery Routes

Typed error classification with compound error analysis and recovery routing.

## Error Categories (Level 1)

| Category | Code | Examples |
|----------|------|----------|
| Environment | ENV_ERROR | Dependency conflict, CUDA mismatch, missing package |
| Data | DATA_ERROR | File missing, format error, download failure |
| Code | CODE_ERROR | Shape mismatch, import error, type error |
| Training | TRAIN_ERROR | NaN loss, OOM, gradient explosion/vanishing |
| Result | RESULT_ERROR | Large metric deviation, wrong format |
| API | API_ERROR | Rate limit, network timeout, service unavailable |

## Compound Error Analysis (Level 2)

Some errors appear simple but are actually compound. Analyze before applying fixes:

| Observed Error | Possible Compound | Correct Strategy |
|---------------|-------------------|-----------------|
| OOM + NaN | Numerical instability → memory fragmentation | Check loss scaling + gradient clipping → THEN reduce batch size |
| CODE_ERROR + TRAIN_ERROR | Code modification introduced silent bug | git revert → micro-experiment verify → re-examine modification |
| ENV_ERROR + CODE_ERROR | Dependency version produces different behavior | Pin exact version from paper's requirements.txt |
| TRAIN_ERROR + RESULT_ERROR | Training ran but produced garbage (no crash) | Check data pipeline, verify no train/eval data leak |

## Recovery Routes (Level 3)

Each error type maps to ordered auto-fix strategies:

### ENV_ERROR

```yaml
auto_fix:
  - "Try alternative package version"
  - "Relax version constraint"
  - "Use conda instead of pip (for CUDA packages)"
escalate_after: 3
escalate_to: "Switch to backup base codebase"
context_needed:
  - "pip install error log"
  - "nvidia-smi output"
  - "python version"
```

### DATA_ERROR

```yaml
auto_fix:
  - "Search alternative download source"
  - "Try HuggingFace mirror (set HF_ENDPOINT if configured in config.yaml)"
  - "Use wget -c for resume"
escalate_after: 2
escalate_to: "Report required dataset checklist to user"
context_needed:
  - "Download error log"
  - "Disk space (df -h)"
  - "Dataset name and expected size"
```

### CODE_ERROR

```yaml
auto_fix:
  - "Read error traceback → identify exact line"
  - "Check tensor shapes at error point (add debug prints)"
  - "Verify function signatures match all call sites"
escalate_after: 2
escalate_to: "git revert → try different modification approach"
context_needed:
  - "Full stack trace"
  - "Input/output shapes at error point"
  - "Git diff of recent changes"
```

### TRAIN_ERROR: OOM

```yaml
auto_fix:
  - "batch_size //= 2"
  - "Enable gradient_checkpointing"
  - "Switch to mixed precision (fp16/bf16)"
escalate_after: 3
escalate_to: "Switch to smaller model configuration"
context_needed:
  - "nvidia-smi output (current memory usage)"
  - "Model parameter count"
  - "Current batch_size and sequence_length"
```

### TRAIN_ERROR: NaN Loss

```yaml
auto_fix:
  - "Reduce learning rate by 10x"
  - "Enable gradient clipping (max_norm=1.0)"
  - "Check data for NaN/Inf values"
escalate_after: 2
escalate_to: "Revert code changes, switch to backup idea"
context_needed:
  - "Loss curve (last 100 steps)"
  - "Gradient statistics (norm per layer)"
  - "Learning rate schedule"
```

### TRAIN_ERROR: Gradient Explosion/Vanishing

```yaml
auto_fix:
  - "Add gradient clipping"
  - "Switch to LayerNorm (if using BatchNorm)"
  - "Initialize new layers with smaller weights"
escalate_after: 2
escalate_to: "Revert modification, try different approach"
context_needed:
  - "Per-layer gradient norms"
  - "Layer initialization method"
  - "Whether new layers are properly initialized"
```

### RESULT_ERROR: Large Metric Deviation

```yaml
auto_fix:
  - "Cross-check config vs paper's reported hyperparameters"
  - "Verify data preprocessing matches paper's description"
  - "Check if evaluation metric implementation matches paper"
escalate_after: 1
escalate_to: "Mark as 'imprecise reproduction', continue with caveat"
context_needed:
  - "Paper reported values vs our values"
  - "Config diff vs paper"
  - "Data split statistics"
```

### API_ERROR

```yaml
auto_fix:
  - "Wait with exponential backoff (1s → 2s → 4s, max 30s)"
  - "Switch to alternative API source"
  - "Reduce request rate"
escalate_after: 3
escalate_to: "Skip this data source, use available data"
context_needed:
  - "HTTP status code"
  - "Rate limit headers"
  - "Which API and endpoint failed"
```

## Core Principle

**NEVER retry the same method blindly.** After each failure:
1. Analyze the error (read logs, check context)
2. Determine the category and check for compound errors
3. Apply the NEXT strategy in the ordered list (not the same one again)
4. If all strategies exhausted → escalate

## Anti-Patterns

- ❌ Retry the same pip install command 5 times hoping it works
- ❌ Just reduce batch size for every training error
- ❌ Ignore NaN loss and keep training hoping it recovers
- ❌ Generate placeholder results when experiment crashes
- ❌ Skip error analysis and jump to escalation
