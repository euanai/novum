#!/usr/bin/env bash
set -euo pipefail

# Research Pipeline Installer v1.12
# Adapted from claude-scholar/scripts/setup.sh
# Installs research-pipeline components into ~/.claude/

CLAUDE_DIR="$HOME/.claude"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Components to install to ~/.claude/ (directories and files)
# NOTE: skills/ is NOT in this list — they go to ~/.claude/research-pipeline/
# to avoid being auto-detected as user-invocable skills
COMPONENTS=(commands agents hooks rules)

info()  { echo -e "\033[1;34m[INFO]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m $*"; }
error() { echo -e "\033[1;31m[ERROR]\033[0m $*"; exit 1; }

check_deps() {
  command -v node >/dev/null || error "Node.js is required (hooks depend on it). Install it first."
  command -v python3 >/dev/null || warn "Python3 not found. research_utils.py won't work."
}

# Copy component directories (merge, don't overwrite existing)
copy_components() {
  local src="$1"
  for comp in "${COMPONENTS[@]}"; do
    if [ -e "$src/$comp" ]; then
      if [ -d "$src/$comp" ]; then
        mkdir -p "$CLAUDE_DIR/$comp"
        # Use cp -r with merge (don't delete existing files in target)
        cp -r "$src/$comp/." "$CLAUDE_DIR/$comp/"
        info "Installed: $comp/"
      else
        cp "$src/$comp" "$CLAUDE_DIR/$comp"
        info "Installed: $comp"
      fi
    fi
  done
}

# Install Python utility scripts
install_scripts() {
  local src="$1"
  local target="$CLAUDE_DIR/scripts/lib"
  mkdir -p "$target"
  if [ -f "$src/scripts/lib/research_utils.py" ]; then
    cp "$src/scripts/lib/research_utils.py" "$target/"
    chmod +x "$target/research_utils.py"
    info "Installed: scripts/lib/research_utils.py"
  fi
}

# Install keyword JSON files for conference scanning
install_keywords() {
  local src="$1"
  local keywords_src="$src/skills/research-automation/references/keywords"
  local keywords_target="$CLAUDE_DIR/research-pipeline/keywords"

  if [ -d "$keywords_src" ]; then
    mkdir -p "$keywords_target"
    cp -r "$keywords_src/." "$keywords_target/"
    info "Installed: research-pipeline/keywords/"
  fi
}

# Merge all hooks into existing settings.json
# PreToolUse: 3 guard hooks (sync)
# PostToolUse/PostToolUseFailure/SubagentStart/SubagentStop: structured-logger (async)
merge_hooks() {
  local target="$CLAUDE_DIR/settings.json"

  # If settings.json doesn't exist, skip (user needs to configure manually)
  if [ ! -f "$target" ]; then
    warn "No settings.json found. Hooks need to be registered manually."
    warn "  Add research hooks to your hooks in $target"
    return 0
  fi

  # Backup
  cp "$target" "${target}.bak"
  info "Backed up settings.json → settings.json.bak"

  node -e "
    const fs = require('fs');
    const existing = JSON.parse(fs.readFileSync('$target', 'utf8'));

    // Ensure hooks structure exists
    if (!existing.hooks) existing.hooks = {};

    const hooksDir = '$CLAUDE_DIR/hooks';

    // --- PreToolUse hooks (sync, guard hooks) ---
    if (!existing.hooks.PreToolUse) existing.hooks.PreToolUse = [];

    const preToolHooks = [
      {
        id: 'research-guard',
        matcher: 'Bash|Write|Edit',
        command: 'node ' + hooksDir + '/research-guard.js',
        timeout: 5
      },
      {
        id: 'phase-gate-guard',
        matcher: 'Write|Edit',
        command: 'node ' + hooksDir + '/phase-gate-guard.js',
        timeout: 5
      },
      {
        id: 'prompt-quality-guard',
        matcher: 'Task',
        command: 'node ' + hooksDir + '/prompt-quality-guard.js',
        timeout: 5
      },
      {
        id: 'download-guard',
        matcher: 'Bash',
        command: 'node ' + hooksDir + '/download-guard.js',
        timeout: 5
      }
    ];

    for (const hook of preToolHooks) {
      const alreadyExists = existing.hooks.PreToolUse.some(h => {
        if (Array.isArray(h.hooks)) {
          return h.hooks.some(sub => sub.command && sub.command.includes(hook.id));
        }
        return h.command && h.command.includes(hook.id);
      });

      if (!alreadyExists) {
        existing.hooks.PreToolUse.push({
          matcher: hook.matcher,
          hooks: [{
            type: 'command',
            command: hook.command,
            timeout: hook.timeout
          }]
        });
      }
    }

    // --- Async logger hooks (PostToolUse, PostToolUseFailure, SubagentStart, SubagentStop) ---
    const loggerCommand = 'node ' + hooksDir + '/structured-logger.js';
    const loggerEvents = ['PostToolUse', 'PostToolUseFailure', 'SubagentStart', 'SubagentStop'];

    for (const eventName of loggerEvents) {
      if (!existing.hooks[eventName]) existing.hooks[eventName] = [];

      const alreadyExists = existing.hooks[eventName].some(h => {
        if (Array.isArray(h.hooks)) {
          return h.hooks.some(sub => sub.command && sub.command.includes('structured-logger'));
        }
        return h.command && h.command.includes('structured-logger');
      });

      if (!alreadyExists) {
        existing.hooks[eventName].push({
          matcher: '',
          hooks: [{
            type: 'command',
            command: loggerCommand,
            async: true,
            timeout: 5
          }]
        });
      }
    }

    fs.writeFileSync('$target', JSON.stringify(existing, null, 2) + '\n');
  " || { warn "Auto-merge of hooks failed. Please manually add research hooks."; return 0; }

  info "Merged 8 research hooks into settings.json (4 PreToolUse + 4 async logger)"
}

# Create project-level permissions file
create_permissions() {
  local project_dir="${1:-.}"
  local settings_dir="$project_dir/.claude"
  local settings_file="$settings_dir/settings.json"

  if [ -f "$settings_file" ]; then
    info "Project settings.json already exists, skipping."
    return 0
  fi

  mkdir -p "$settings_dir"
  cat > "$settings_file" << 'SETTINGS_EOF'
{
  "permissions": {
    "allow": [
      "Bash(git *)", "Bash(python *)", "Bash(python3 *)",
      "Bash(uv *)", "Bash(pip *)", "Bash(pip3 *)",
      "Bash(mkdir *)", "Bash(ls *)", "Bash(cp *)", "Bash(mv *)",
      "Bash(nvidia-smi*)", "Bash(wget *)", "Bash(curl *)",
      "Bash(tar *)", "Bash(unzip *)", "Bash(pdftotext *)",
      "Bash(nohup *)", "Bash(kill -0 *)",
      "Read", "Edit",
      "WebFetch", "WebSearch",
      "mcp__zotero"
    ],
    "deny": [
      "Bash(rm -rf //)",
      "Bash(sudo *)",
      "Bash(git push --force *)",
      "Read(.env)", "Read(.env.*)",
      "Edit(.env)", "Edit(.env.*)"
    ]
  }
}
SETTINGS_EOF

  info "Created project permissions at $settings_file"
  info "  → Review and customize permissions before running /research"
}

main() {
  echo ""
  echo "╔══════════════════════════════════════════╗"
  echo "║    Research Pipeline Installer v1.12       ║"
  echo "╚══════════════════════════════════════════╝"
  echo ""

  check_deps

  info "Installing from: $SRC_DIR"
  copy_components "$SRC_DIR"
  install_scripts "$SRC_DIR"

  # Install skill reference docs to a non-skill location
  # (skills/ in ~/.claude/ auto-registers as user-invocable, which we don't want)
  if [ -d "$SRC_DIR/skills" ]; then
    mkdir -p "$CLAUDE_DIR/research-pipeline"
    cp -r "$SRC_DIR/skills/." "$CLAUDE_DIR/research-pipeline/"
    info "Installed: research-pipeline/ (skill references)"
  fi

  # Install keyword files for conference scanning
  install_keywords "$SRC_DIR"

  merge_hooks

  # Optional: create project-level permissions if a project dir is specified
  if [ "${1:-}" = "--project" ] && [ -n "${2:-}" ]; then
    create_permissions "$2"
  fi

  echo ""
  info "Done! Components installed to $CLAUDE_DIR"
  info "  8 hooks registered: research-guard, phase-gate-guard, prompt-quality-guard, download-guard, structured-logger (×4 events)"
  info "Restart Claude Code CLI to activate."
  echo ""
  echo "Quick start:"
  echo "  /research \"your topic here\""
  echo "  /research --scout \"CVPR 2025\" --budget=8h"
  echo ""
}

main "$@"
