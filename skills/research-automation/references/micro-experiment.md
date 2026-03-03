# Micro-Experiment Verification

Verification protocol for code modifications before full training.

## Three-Level Verification Pipeline

### Level 1: Micro-Experiment (100 steps, <5 minutes)

**Purpose**: Verify code correctness — does the modification produce valid outputs?

**4 Checks**:

1. **Forward Pass Validation**
   ```python
   # Random input → check output shape, no NaN/Inf
   dummy_input = torch.randn(batch_size, *input_shape).to(device)
   output = model(dummy_input)
   assert output.shape == expected_shape, f"Shape mismatch: {output.shape} vs {expected_shape}"
   assert not torch.isnan(output).any(), "NaN in output"
   assert not torch.isinf(output).any(), "Inf in output"
   ```

2. **Gradient Flow Validation**
   ```python
   # 1-step backward → all NEW parameters have non-zero gradient
   loss = criterion(output, dummy_target)
   loss.backward()
   for name, param in model.named_parameters():
       if param.requires_grad:
           assert param.grad is not None, f"No gradient for {name}"
           if is_new_parameter(name):  # Parameters added by our modification
               assert param.grad.abs().sum() > 0, f"Zero gradient for new param {name}"
   ```

3. **Micro-Training Validation**
   ```python
   # 100 steps on data subset → loss must decrease monotonically (trend, not every step)
   losses = []
   for step in range(100):
       loss = train_step(model, batch)
       losses.append(loss.item())
   # Check: first 10 avg > last 10 avg
   assert mean(losses[:10]) > mean(losses[-10:]), "Loss not decreasing"
   ```

4. **Before/After Comparison**
   ```python
   # Same batch, compare outputs before and after modification
   # Outputs MUST be different (modification actually took effect)
   output_before = model_before(same_batch)
   output_after = model_after(same_batch)
   diff = (output_before - output_after).abs().mean()
   assert diff > 1e-6, "Modification had no effect on output"
   ```

**Any check fails → git revert → Master analyzes cause → try different approach.**

### Level 2: Medium Experiment (10% data, full epochs, ~2-3 hours)

**Purpose**: Verify method effectiveness before committing GPU hours to full training.

**Protocol**:
1. Use 10% of training data (random subset, fixed seed)
2. Train for full number of epochs (same as final training)
3. Compare learning curves: modified model vs baseline

**Judgment Criteria** (trend-based, not absolute value):
- **Strong signal → proceed to Level 3**: Curves clearly separated OR >1% absolute improvement
- **Weak signal → conditional proceed**: Correct trend but small gap:
  - Scale-dependent methods (contrastive learning, data augmentation, self-supervised): Allow Level 3 with 30% data supplementary check
  - Architecture modifications: 10% data should show effect → switch idea
- **No signal → switch idea**: Curves completely overlap or wrong direction

**Hyperparameter Search** (done at this level):
- 3-5 values per hyperparameter, log scale (0.1, 0.3, 1.0, 3.0)
- Priority: learning rate > loss weight > architecture params
- Budget: hyperparameter search ≤30% of total GPU budget

### Level 3: Full Training (100% data, nohup background)

**Purpose**: Final results with confirmed best hyperparameters.

**Training Script Template** (must include):
```bash
#!/bin/bash
set -e
EXP_NAME="${1:-exp1}"
CKPT_DIR=".research/phase6_experiments/$EXP_NAME/checkpoints"

# Signal trap for graceful shutdown (torchrun worker cleanup)
cleanup() {
    echo "Caught signal, saving checkpoint and killing child processes..."
    kill $(jobs -p) 2>/dev/null
    wait
    touch ".research/phase6_experiments/$EXP_NAME/.failed"
}
trap cleanup SIGTERM SIGINT SIGHUP

# Checkpoint resume
RESUME_FLAG=""
if [ -f "$CKPT_DIR/latest.pt" ]; then
    echo "Found checkpoint, resuming..."
    RESUME_FLAG="--resume_from_checkpoint $CKPT_DIR/latest.pt"
fi

# Train
python train.py \
    --config ".research/phase6_experiments/$EXP_NAME/config.yaml" \
    --save_dir "$CKPT_DIR" \
    --save_every_n_epochs 1 \
    $RESUME_FLAG \
    2>&1 | tee ".research/phase6_experiments/$EXP_NAME/training.log"

# Completion flags
if [ $? -eq 0 ]; then
    touch ".research/phase6_experiments/$EXP_NAME/.done"
else
    touch ".research/phase6_experiments/$EXP_NAME/.failed"
fi
```

**Launch**: `nohup bash train.sh exp1 > /dev/null 2>&1 &`
**PID tracking**: `echo $! > .research/phase6_experiments/exp1/pid`

## 3-Layer Code Review (Before Micro-Experiment)

Code review is FREE (just reading code), while micro-experiment costs GPU time. Do review FIRST.

### Layer 1: Idea→Code Alignment

- [ ] Every planned change in the mapping table has been implemented
- [ ] No unplanned changes (accidentally modified unrelated code)
- [ ] Modification logic matches idea description precisely

### Layer 2: ML-Specific Silent Bug Check

For THIS specific modification type, derive "what bugs won't crash but produce garbage":

| Modification Type | Silent Bugs to Check |
|------------------|---------------------|
| New module | Input/output dimensions aligned with upstream/downstream? |
| Loss modification | New loss term gradient won't dominate/vanish? Weight reasonable? |
| Data augmentation | Applied only to train set? Validation set unaffected? |
| Attention mechanism | Mask correct? No information leakage (future → current)? |
| Feature fusion | detach() placed correctly? BN/LN statistics correct on new path? |
| General | train/eval mode toggle covers new modules? Dropout position correct? |

### Layer 3: General Correctness

- [ ] Imports correct, none missing
- [ ] Function signatures compatible with all call sites
- [ ] Consistent with base code style (naming, indentation, config patterns)
- [ ] No hardcoded magic numbers (should be in config)

## Fairness Constraints

For any experiment comparison to be valid:
1. Any data augmentation used for our method MUST also be applied to baseline
2. Hyperparameter search budget must be equal
3. Report: Method | Metric | Params | FLOPs | Train Time
4. Baseline must be re-run with same augmentation/tricks

## Git Branch Management

```
Phase 5 reproduction success:
  git checkout -b exp/{idea_name}  # Create experiment branch

Each modification batch:
  git add -A && git commit -m "feat: implement {component}"
  # Run micro-experiment
  # Fail → git revert HEAD → try different approach
  # Pass → continue

Switch idea:
  git checkout master  # Clean base
  git checkout -b exp/{new_idea_name}
```
