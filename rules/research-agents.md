# Research Pipeline Agent Rules

## Auto-Invocation Rules

The following agents are part of the `/research` pipeline and should be invoked automatically when their trigger conditions are met:

### sota-finder
- **Trigger**: When searching for SOTA methods, evaluating open-source codebases, or building performance rankings for a research topic
- **Model**: opus
- **Dispatch**: Via Task tool from Master Agent during Phase 2

### env-setup
- **Trigger**: When setting up a Python environment for codebase reproduction, installing dependencies, or validating CUDA compatibility
- **Model**: sonnet
- **Dispatch**: Via Task tool from Master Agent during Phase 5

### experiment-runner
- **Trigger**: When executing code modifications, running micro-experiments, generating training scripts, or collecting experiment results
- **Model**: opus
- **Dispatch**: Via Task tool from Master Agent during Phase 5-6
- **IMPORTANT**: This agent does NOT have Write permission for results.json or sanity-check.json (anti-fabrication constraint)

### opportunity-scorer
- **Trigger**: When scanning conference papers for research opportunities in scout mode
- **Model**: sonnet (paper reading + feasibility judgment requires reasoning, not pattern matching)
- **Dispatch**: Via Task tool from Master Agent during Phase 0 (Scout)

### pipeline-reviewer
- **Trigger**: After pipeline completion (final-report.md generated), or when `--review` flag is passed
- **Model**: opus (deep reasoning for critical analysis + simulated reviewer judgment)
- **Dispatch**: Via Task tool from Master Agent after Final Report, or standalone via `--review`
- **Note**: Post-run agent — does NOT run during pipeline execution, only after completion

## Orchestration Rules

1. **Master Agent (the /research command itself)** orchestrates all workers
2. Workers are dispatched via Task tool, NOT Agent Teams (v1 architecture)
3. Independent workers CAN be dispatched in parallel (e.g., multiple repo evaluations)
4. Workers report back to Master; Master makes all strategic decisions
5. Workers should NOT make strategic decisions (e.g., which idea to pursue)

