#!/usr/bin/env node
/**
 * download-guard.js — PreToolUse hook (sync)
 *
 * DENY Bash commands that download files without explicit proxy/mirror handling.
 * Prevents accidental downloads through slow or misconfigured proxies.
 *
 * If the user has configured proxy settings in their environment, this hook
 * ensures download commands explicitly handle proxy (unset it, use mirrors,
 * or set a known-good proxy) rather than inheriting potentially slow defaults.
 *
 * Two-level detection:
 *   Level 1: Explicit download commands (pip install, git clone, wget, curl)
 *   Level 2: Python scripts/code containing download-related operations
 *
 * Exit codes: 0 = ALLOW, 2 = DENY
 */
'use strict';

const fs = require('fs');
const path = require('path');

let input;
try {
  input = JSON.parse(fs.readFileSync('/dev/stdin', 'utf8'));
} catch { process.exit(0); }

if (input.tool_name !== 'Bash') process.exit(0);

const cmd = (input.tool_input?.command || '').trim();
if (!cmd) process.exit(0);

// ── No proxy configured? Nothing to guard against ──
if (!process.env.http_proxy && !process.env.https_proxy) process.exit(0);

// ── Proxy already handled? If so, always ALLOW ──
// These patterns indicate the command explicitly manages proxy/mirror settings.
// Customize these patterns for your network environment.
const PROXY_OK = [
  /unset\s+(http_proxy|https_proxy)/,          // Explicitly unsetting proxy
  /http_proxy\s*=\s*http:\/\//,                // Setting a specific proxy
  /https_proxy\s*=\s*http:\/\//,               // Setting a specific proxy
  /--index-url\s+https?:\/\//,                 // pip with explicit index
  /-i\s+https?:\/\//,                          // pip with explicit mirror
  /HF_ENDPOINT\s*=/,                           // HuggingFace endpoint override
  /--proxy\s/,                                 // Explicit proxy flag
];
if (PROXY_OK.some(p => p.test(cmd))) process.exit(0);

// ── Level 1: Explicit download commands ──
const EXPLICIT_DOWNLOAD = [
  /\bpip3?\s+install\b/,
  /\bconda\s+(install|create|update|env\s+create)\b/,
  /\bmamba\s+(install|create|update|env\s+create)\b/,
  /\bgit\s+clone\b/,
  /\bwget\s+https?:\/\//,
  /\bcurl\s+[^|]*https?:\/\/(?!127\.|localhost)/,
  /\bhuggingface-cli\s+download\b/,
  /\bgdown\s/,
];

if (EXPLICIT_DOWNLOAD.some(p => p.test(cmd))) {
  deny('Download command detected');
  process.exit(2);
}

// ── Level 2: Python code with download operations ──
const DOWNLOAD_CODE = [
  /torchvision\.datasets/,
  /torchvision\.\w+.*download\s*=\s*True/,
  /download\s*=\s*True/,
  /datasets\.load_dataset/,
  /huggingface_hub\b/,
  /torch\.hub\.(load|download_url_to_file)/,
  /urllib\.request\.urlretrieve/,
  /gdown\.download/,
  /keras\.datasets/,
  /tf\.keras\.datasets/,
];

// Only check if command involves Python
if (/\bpython3?\b/.test(cmd)) {
  if (DOWNLOAD_CODE.some(p => p.test(cmd))) {
    deny('Python code contains download operations');
    process.exit(2);
  }

  const pyFileMatch = cmd.match(/python3?\s+(?:[-\w]+\s+)*?((?:[\w.\/~-]+\/)*[\w.-]+\.py)\b/);
  if (pyFileMatch) {
    const pyPath = pyFileMatch[1];
    const candidates = resolvePaths(pyPath, cmd);

    for (const fp of candidates) {
      try {
        const content = fs.readFileSync(fp, 'utf8');
        if (DOWNLOAD_CODE.some(p => p.test(content))) {
          const selfHandled = [
            /os\.environ\.pop\s*\(\s*['"]http_proxy/,
            /os\.environ\[['"]http_proxy['"]\]\s*=/,
            /del\s+os\.environ\[['"]http_proxy/,
          ].some(p => p.test(content));

          if (!selfHandled) {
            deny(`Script "${path.basename(fp)}" contains download operations`);
            process.exit(2);
          }
        }
        break;
      } catch { continue; }
    }
  }
}

// All checks passed
process.exit(0);

// ── Helpers ──

function deny(reason) {
  console.log(`⚠️ ${reason} without explicit proxy/mirror handling.

Fix — choose one:
  A) Unset proxy (if your default proxy is slow for downloads):
     unset http_proxy https_proxy

  B) Use a package mirror:
     pip: -i https://your-preferred-mirror/simple
     HF:  export HF_ENDPOINT=https://your-hf-mirror

  C) Set a fast proxy explicitly:
     export http_proxy=http://your-fast-proxy:port
     export https_proxy=http://your-fast-proxy:port

Add proxy/mirror handling to your command and retry.
See config.example.yaml for configuring default mirrors.`);
}

function resolvePaths(pyPath, cmd) {
  const home = process.env.HOME || '';
  const expanded = pyPath.replace(/^~/, home);
  const paths = [];

  if (path.isAbsolute(expanded)) {
    paths.push(expanded);
    return paths;
  }

  const cdMatch = cmd.match(/cd\s+([^\s;&|]+)/);
  if (cdMatch) {
    const cdDir = cdMatch[1].replace(/^~/, home);
    paths.push(path.resolve(cdDir, expanded));
  }

  paths.push(path.resolve(process.cwd(), expanded));

  return paths;
}
