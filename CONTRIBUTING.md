# Contributing to Novum

Thank you for your interest in contributing to Novum! This guide explains how to add new components to the pipeline.

## Development Setup

1. Clone the repository
2. Run `bash scripts/install.sh` to install components into `~/.claude/`
3. Restart Claude Code CLI to activate hooks

## Adding a New Worker Agent

Worker agents are Markdown files in `agents/` that define specialized roles. To add a new agent:

1. Create `agents/your-agent.md` with YAML frontmatter:
   ```yaml
   ---
   name: your-agent
   description: "What this agent does"
   model: sonnet  # or opus for complex tasks
   tools: ["Bash", "Read", "Edit", "Grep", "Glob"]
   ---
   ```
2. Define the agent's responsibilities, input/output contracts, and anti-patterns
3. Add the agent to `rules/research-agents.md` with trigger conditions
4. Update `hooks/prompt-quality-guard.js` if the agent needs dispatch keyword validation
5. Update `scripts/install.sh` if the agent needs special installation steps

### Agent Design Guidelines

- **Input/Output contracts**: Define exactly what the agent receives and produces
- **Anti-patterns section**: List at least 5 things the agent must NEVER do (based on real failure modes)
- **Error resilience**: Single failures must never stop the entire batch
- **Communication protocol**: Define how the agent reports success/failure to the Master

## Adding a New Hook

Hooks are JavaScript files in `hooks/` that run as PreToolUse or PostToolUse interceptors.

1. Create `hooks/your-hook.js`
2. Read stdin for the hook input (JSON with tool_name, tool_input, cwd)
3. Exit codes:
   - `0` = ALLOW (output JSON with `{ "continue": true }`)
   - `2` = DENY (output JSON with `{ "hookSpecificOutput": { "permissionDecision": "deny" }, "systemMessage": "..." }`)
4. Register in `scripts/install.sh` under `merge_hooks()`
5. Add to `hooks/hooks.json` for reference

### Hook Design Guidelines

- Hooks must be **fast** (< 5 second timeout)
- Hooks must be **deterministic** — same input = same output
- Use `exit 0` with `systemMessage` for warnings (soft gates)
- Use `exit 2` with `permissionDecision: deny` for hard blocks
- Never make network calls from hooks

## Adding Domain Keywords

Keyword files in `skills/research-automation/references/keywords/` define domain-specific vocabulary for conference paper scanning.

1. Create a new JSON file: `keywords/your-domain.json`
2. Structure:
   ```json
   {
     "domain": "your-domain",
     "keywords": {
       "category1": ["keyword1", "keyword2"],
       "category2": ["keyword3", "keyword4"]
     }
   }
   ```
3. The opportunity-scorer agent uses these for paper relevance filtering

## Code Style

### Python
- Use type hints for public function signatures
- Follow PEP 8
- Use `atomic_json_write()` for any JSON file operations in the pipeline

### JavaScript (Hooks)
- Use strict mode (`'use strict'`)
- Read stdin synchronously (`fs.readFileSync(0, 'utf8')`)
- Always handle parse errors gracefully (fail open, not closed)

### Markdown (Agents/Commands)
- Use YAML frontmatter for metadata
- Include examples with realistic data
- List anti-patterns with specific failure modes they prevent

## Testing

### Hook Tests
```bash
# Test research-guard: should DENY writing to results.json
echo '{"tool_name":"Write","tool_input":{"file_path":".research/phase6/exp1/results.json"},"cwd":"/tmp"}' | node hooks/research-guard.js

# Test phase-gate-guard: should DENY phase advancement without prerequisites
echo '{"tool_name":"Write","tool_input":{"file_path":".research/state.json","content":"{\"current_phase\":\"phase2_sota\"}"},"cwd":"/tmp"}' | node hooks/phase-gate-guard.js
```

### Python Syntax
```bash
python3 -m py_compile scripts/lib/research_utils.py
```

### JavaScript Syntax
```bash
node -c hooks/research-guard.js
node -c hooks/phase-gate-guard.js
node -c hooks/prompt-quality-guard.js
node -c hooks/download-guard.js
node -c hooks/structured-logger.js
```

## Submitting Changes

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run the tests above
5. Submit a pull request with a clear description

## Reporting Issues

Please include:
- Claude Code version
- Node.js version
- Python version
- Full error message / hook output
- Steps to reproduce
