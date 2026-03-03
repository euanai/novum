#!/usr/bin/env python3
"""
Research Pipeline Utilities

Core infrastructure for the /research command:
1. StateManager — state.json read/write/migrate/validate
2. PaperDeduplicator — DOI/ArXiv ID/title-based deduplication
3. ProcessManager — nohup launch/PID tracking/graceful kill
4. RateLimiter — API rate limiting with exponential backoff + jitter
5. SemanticScholarBulkClient — S2 Bulk API for venue-based paper search

Adapted from claude-scholar/skills/citation-verification/scripts/api-clients.py
"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from abc import ABC, abstractmethod
from dataclasses import dataclass

try:
    import requests
except ImportError:
    requests = None  # graceful degradation


PIPELINE_VERSION = "1.12.0"
SCHEMA_VERSION_DATA = "5.0.0"


# =============================================================================
# Atomic JSON Write (crash-safe)
# =============================================================================

def atomic_json_write(path: str, data: Any, indent: int = 2):
    """Write JSON atomically: temp file + os.rename.

    Prevents data loss if the process crashes mid-write.
    Uses same-directory temp file to ensure same-filesystem rename (atomic on POSIX).
    """
    path = str(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp", prefix=".atomic_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# =============================================================================
# Paper Cache (reusable rocket booster)
# =============================================================================

class PaperCache:
    """Global paper text cache at .research/paper-cache/.

    Stores paper full-text (.txt from pdftotext) with a cross-scout index.
    Papers downloaded for one scout run are reusable across all future scouts
    and /research runs — the "reusable rocket booster".
    """

    def __init__(self, cache_dir: str):
        self.cache_dir = Path(cache_dir)
        self.txt_dir = self.cache_dir / "txt"
        self.index_path = self.cache_dir / "index.json"
        self._index: Dict = {"version": 1, "papers": {}}
        self._load()

    def _load(self):
        """Load index.json if it exists."""
        if self.index_path.exists():
            try:
                with open(self.index_path, "r") as f:
                    self._index = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass  # Start fresh if corrupted

    def resolve(self, paper_id: str) -> Optional[str]:
        """Check if paper_id is in cache. Returns canonical_id or None."""
        if paper_id in self._index.get("papers", {}):
            txt_path = self.txt_dir / f"{paper_id}.txt"
            if txt_path.exists() and txt_path.stat().st_size > 100:
                return paper_id
        return None

    def store(self, paper_id: str, txt_path: str, metadata: Dict) -> str:
        """Register a paper text file in the cache.

        If txt_path is not already in cache_dir/txt/, copies it there.
        Returns the canonical_id (= paper_id).
        """
        self.txt_dir.mkdir(parents=True, exist_ok=True)
        target = self.txt_dir / f"{paper_id}.txt"

        # Copy if source is not already in the right place
        src = Path(txt_path)
        if src.resolve() != target.resolve():
            if src.exists():
                shutil.copy2(str(src), str(target))

        entry = {
            "openreview_id": metadata.get("openreview_id", paper_id),
            "arxiv_id": metadata.get("arxiv_id"),
            "title": metadata.get("title", ""),
            "venues": metadata.get("venues", []),
            "txt_size_bytes": target.stat().st_size if target.exists() else 0,
            "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._index.setdefault("papers", {})[paper_id] = entry
        return paper_id

    def get_txt_path(self, paper_id: str) -> str:
        """Return absolute path to the cached .txt file."""
        return str(self.txt_dir / f"{paper_id}.txt")

    def paper_count(self) -> int:
        """Return number of papers in cache."""
        return len(self._index.get("papers", {}))

    def save(self):
        """Persist index.json atomically."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(str(self.index_path), self._index)


# =============================================================================
# Rate Limiting (adapted from claude-scholar api-clients.py)
# =============================================================================

class RateLimiter:
    """Rate limiter with exponential backoff and jitter.

    Supports both simple interval-based limiting and fixed-window quota tracking.
    """

    def __init__(self, calls_per_minute: int = 20):
        self.calls_per_minute = calls_per_minute
        self.min_interval = 60.0 / calls_per_minute
        self.last_call = 0.0
        # Fixed-window tracking (for APIs with quota like S2: 5000/5min)
        self.window_start = 0.0
        self.window_calls = 0
        self.window_size = 0  # 0 = disabled
        self.window_max = 0

    def configure_window(self, max_calls: int, window_seconds: float):
        """Configure fixed-window rate limiting (e.g., 5000 calls per 300s)."""
        self.window_size = window_seconds
        self.window_max = max_calls

    def wait_if_needed(self):
        """Block until the next call is allowed."""
        now = time.time()

        # Check fixed window
        if self.window_size > 0:
            if now - self.window_start > self.window_size:
                # New window
                self.window_start = now
                self.window_calls = 0
            if self.window_calls >= self.window_max:
                sleep_time = self.window_size - (now - self.window_start) + 0.1
                if sleep_time > 0:
                    time.sleep(sleep_time)
                self.window_start = time.time()
                self.window_calls = 0
            # Re-read time after potential window sleep
            now = time.time()

        # Check interval
        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        self.last_call = time.time()
        self.window_calls += 1


def backoff_delay(attempt: int, initial_ms: float = 1000, factor: float = 2.0,
                  jitter_ms: float = 500, max_ms: float = 30000) -> float:
    """Calculate backoff delay with jitter (matches OpenClaw pattern).

    Returns delay in seconds.
    """
    delay_ms = min(max_ms, initial_ms * (factor ** attempt) + random.uniform(0, jitter_ms))
    return delay_ms / 1000.0


# =============================================================================
# API Client Base (adapted from claude-scholar)
# =============================================================================

class APIClient(ABC):
    """Base class for API clients with retry and rate limiting."""

    def __init__(self, rate_limit: int = 20):
        self.rate_limiter = RateLimiter(rate_limit)

    @abstractmethod
    def search(self, **kwargs) -> Optional[Dict]:
        pass

    def _retry_request(self, func, max_retries: int = 3):
        """Execute request with exponential backoff retry."""
        for i in range(max_retries):
            try:
                self.rate_limiter.wait_if_needed()
                return func()
            except Exception as e:
                if requests and isinstance(e, requests.exceptions.HTTPError):
                    status = e.response.status_code if e.response else 0
                    if status == 429:
                        # Rate limited — use backoff
                        delay = backoff_delay(i)
                        print(f"[RateLimit] 429 received, waiting {delay:.1f}s...")
                        time.sleep(delay)
                        continue
                if i == max_retries - 1:
                    raise
                time.sleep(backoff_delay(i))
        return None


# =============================================================================
# Semantic Scholar Bulk API Client (NEW — not in claude-scholar)
# =============================================================================

class SemanticScholarBulkClient(APIClient):
    """Semantic Scholar Bulk API for venue-based paper search.

    Endpoint: GET /paper/search/bulk?query=&venue=CVPR&year=2025
    Returns up to 1000 papers per page with token-based pagination.
    """

    BASE_URL = "https://api.semanticscholar.org/graph/v1"

    def __init__(self, api_key: Optional[str] = None, rate_limit: int = 10):
        super().__init__(rate_limit)
        self.api_key = api_key or os.environ.get("S2_API_KEY", "")
        self.headers = {}
        if self.api_key:
            self.headers["x-api-key"] = self.api_key
            # With key: 1 RPS for search endpoints
            self.rate_limiter = RateLimiter(calls_per_minute=55)
        else:
            # Without key: shared pool 5000/5min
            self.rate_limiter = RateLimiter(calls_per_minute=50)
            self.rate_limiter.configure_window(max_calls=4500, window_seconds=300)

    def search_by_venue(self, venue: str, year: int,
                        fields: str = "title,abstract,year,venue,citationCount,openAccessPdf,externalIds",
                        max_papers: int = 2000) -> List[Dict]:
        """Search papers by venue and year using Bulk API.

        Args:
            venue: Conference/journal name (e.g., "CVPR", "NeurIPS")
            year: Publication year
            fields: Comma-separated fields to return
            max_papers: Maximum papers to fetch (across pages)

        Returns:
            List of paper dicts
        """
        if not requests:
            print("[Error] requests library not installed")
            return []

        papers = []
        token = None

        while len(papers) < max_papers:
            def do_request(t=token):
                params = {
                    "query": "",
                    "venue": venue,
                    "year": str(year),
                    "fields": fields,
                }
                if t:
                    params["token"] = t
                resp = requests.get(
                    f"{self.BASE_URL}/paper/search/bulk",
                    params=params,
                    headers=self.headers,
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()

            try:
                data = self._retry_request(do_request)
            except Exception as e:
                print(f"[S2 Bulk] Error: {e}")
                break

            if not data or "data" not in data:
                break

            papers.extend(data["data"])
            token = data.get("token")
            if not token:
                break  # No more pages

            print(f"[S2 Bulk] Fetched {len(papers)} papers so far...")

        return papers[:max_papers]

    def search(self, venue: str = None, year: int = None, **kwargs) -> Optional[Dict]:
        """Unified search interface."""
        if venue and year:
            return {"papers": self.search_by_venue(venue, year, **kwargs)}
        return None


# =============================================================================
# State Manager
# =============================================================================

CURRENT_SCHEMA_VERSION = 5

STATE_TEMPLATE = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "topic": "",
    "current_phase": "phase1_literature",
    "depth": "full",
    "phases": {
        "phase1_literature": {"status": "pending", "gate_results": {}},
        "phase2_sota": {"status": "pending", "gate_results": {}},
        "phase2_5_profile": {"status": "pending", "gate_results": {}},
        "phase3_ideas": {"status": "pending", "gate_results": {}},
        "phase3_5_quickval": {"status": "pending", "gate_results": {}},
        "phase4_design": {"status": "pending", "gate_results": {}},
        "phase5_baseline": {"status": "pending", "gate_results": {}},
        "phase6_experiments": {"status": "pending", "gate_results": {}},
        "phase7_analysis": {"status": "pending", "gate_results": {}},
        "phase8_writing": {"status": "pending", "gate_results": {}},
    },
    "budget": {
        "gpu_hours_estimated": 0,
        "gpu_hours_used": 0,
    },
    "iteration": {
        "cycle": 0,
        "max_cycles": 5,
        "frozen_phases": [
            "phase1_literature",
            "phase2_sota",
            "phase2_5_profile",
        ],
        "history": [],
        "tracks": {
            "explore_ratio": 0.4,       # v1.12: repurposed as diversity protection threshold
            "exploit_tested": 0,        # kept for reporting
            "explore_tested": 0,        # kept for reporting
            "current_track": None,      # DEPRECATED in v1.12 — tournament tests all simultaneously
            "track_switches": 0,        # DEPRECATED in v1.12
        },
        "tournament": {                 # NEW in v1.12
            "status": "pending",        # pending | in_progress | completed | failed
            "current_round": 0,
            "total_rounds": 3,
            "hypotheses_entered": 0,
            "champion": None,
            "champion_score": None,
            "rounds_completed": 0,
            "budget_allocation": {
                "round1_fraction": 0.15,
                "round2_fraction": 0.30,
                "round3_fraction": 0.55,
            },
        },
    },
    "training_jobs": [],
    "workers": [],
    "failures": [],
    "resource_usage": {},
}

SCOUT_STATE_TEMPLATE = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "mode": "scout",
    "venue": "",
    "budget_hours": 0,
    "current_phase": "phase0_scout",
    "phases": {
        "phase0_scout": {
            "status": "pending",
            "stage": None,  # stage1_fetch, stage2_screen, stage3_score, stage4_report
            "papers_total": 0,
            "papers_screened": 0,
            "papers_scored": 0,
            "gate_results": {},
        },
    },
    "workers": [],
    "failures": [],
    "resource_usage": {},
}


class StateManager:
    """Manages .research/state.json — read, write, migrate, validate.

    Supports cross-session resumption via schema versioning and migration.
    """

    def __init__(self, research_dir: str = ".research"):
        self.research_dir = Path(research_dir)
        self.state_path = self.research_dir / "state.json"

    def exists(self) -> bool:
        return self.state_path.exists()

    def load(self) -> Dict:
        """Load state.json, applying migrations if needed."""
        if not self.state_path.exists():
            return {}
        with open(self.state_path, "r") as f:
            state = json.load(f)
        state = self._migrate(state)
        return state

    def save(self, state: Dict):
        """Write state.json atomically."""
        self.research_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(str(self.state_path), state)

    def resolve_path(self, rel_path: str) -> str:
        """Resolve a relative path stored in state.json to absolute.

        State stores paths relative to .research/ root.
        E.g., "scouts/iclr_2026" → "/abs/path/.research/scouts/iclr_2026"
        """
        return str(self.research_dir.resolve() / rel_path)

    def make_relative(self, abs_path: str) -> str:
        """Convert absolute path to relative (from .research/ root).

        E.g., "/abs/path/.research/scouts/iclr_2026" → "scouts/iclr_2026"
        """
        try:
            return str(Path(abs_path).relative_to(self.research_dir.resolve()))
        except ValueError:
            return abs_path  # Not under .research — return as-is

    def init(self, topic: str, depth: str = "full") -> Dict:
        """Initialize a new research project state."""
        state = json.loads(json.dumps(STATE_TEMPLATE))  # deep copy
        state["topic"] = topic
        state["depth"] = depth
        state["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save(state)
        return state

    def init_scout(self, venue: str, budget_hours: float) -> Dict:
        """Initialize a scout mode state (Phase 0 only, no Phase 1-8 dirs).

        Uses v2 directory structure: .research/scouts/{venue}/
        Stores relative paths in state.json for portability.
        """
        state = json.loads(json.dumps(SCOUT_STATE_TEMPLATE))  # deep copy
        state["venue"] = venue
        state["budget_hours"] = budget_hours
        state["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        # Create v2 directory structure
        scout_dir = create_scout_dirs_v2(str(self.research_dir), venue)
        paper_cache_dir = os.path.join(str(self.research_dir.resolve()), "paper-cache")
        os.makedirs(os.path.join(paper_cache_dir, "txt"), exist_ok=True)

        # Store RELATIVE paths in state.json
        state["scout_dir"] = self.make_relative(scout_dir)
        state["paper_cache_dir"] = self.make_relative(paper_cache_dir)
        state["research_dir"] = str(self.research_dir.resolve())

        # Write metadata.json
        write_scout_metadata(scout_dir, venue, budget_hours)

        self.save(state)
        return state

    def update_scout_stage(self, state: Dict, stage: str, metrics: Dict) -> Dict:
        """Update scout phase stage and metrics."""
        phase = state.get("phases", {}).get("phase0_scout", {})
        phase["stage"] = stage
        phase.update(metrics)
        state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save(state)
        return state

    def update_phase(self, state: Dict, phase: str, updates: Dict) -> Dict:
        """Update a specific phase's data."""
        if phase in state.get("phases", {}):
            state["phases"][phase].update(updates)
        state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save(state)
        return state

    def advance_phase(self, state: Dict, from_phase: str, to_phase: str) -> Dict:
        """Mark from_phase as completed and set to_phase as current."""
        if from_phase in state.get("phases", {}):
            state["phases"][from_phase]["status"] = "completed"
            state["phases"][from_phase]["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if to_phase in state.get("phases", {}):
            state["phases"][to_phase]["status"] = "in_progress"
            state["phases"][to_phase]["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        state["current_phase"] = to_phase
        self.save(state)
        return state

    def add_gpu_hours(self, state: Dict, hours: float) -> Dict:
        """Increment GPU hours used in budget tracking."""
        if 'budget' not in state:
            state['budget'] = {'gpu_hours_estimated': 0, 'gpu_hours_used': 0}
        state['budget']['gpu_hours_used'] = state['budget'].get('gpu_hours_used', 0) + hours
        self.save(state)
        return state

    def add_failure(self, state: Dict, phase: str, error: str,
                    auto_fix: str = "", retry: int = 0) -> Dict:
        """Log a failure event."""
        state.setdefault("failures", []).append({
            "phase": phase,
            "error": error,
            "auto_fix": auto_fix,
            "retry": retry,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self.save(state)
        return state

    def add_training_job(self, state: Dict, name: str, pid_file: str,
                         log_path: str) -> Dict:
        """Register a background training job."""
        state.setdefault("training_jobs", []).append({
            "name": name,
            "pid_file": pid_file,
            "log_path": log_path,
            "done_flag": str(Path(pid_file).parent / ".done"),
            "fail_flag": str(Path(pid_file).parent / ".failed"),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": "running",
        })
        self.save(state)
        return state

    def _migrate(self, state: Dict) -> Dict:
        """Apply schema migrations (v0 → v1 → v2 → v3)."""
        version = state.get("schema_version", 0)

        if version < 1:
            state["schema_version"] = 1
            state.setdefault("training_jobs", [])
            state.setdefault("failures", [])
            state.setdefault("workers", [])

        if version < 2:
            # Add gate_results to each phase
            for phase_key in state.get("phases", {}):
                state["phases"][phase_key].setdefault("gate_results", {})
            state.setdefault("resource_usage", {})
            state["schema_version"] = 2

        if version < 3:
            # Add iteration block for hypothesis-driven loop
            state.setdefault("iteration", {
                "cycle": 0,
                "max_cycles": 5,
                "frozen_phases": [
                    "phase1_literature",
                    "phase2_sota",
                    "phase2_5_profile",
                ],
                "history": [],
            })
            state["schema_version"] = 3

        if version < 4:
            # v1.11: Add tracks field for dual-track exploration/exploitation
            iteration = state.setdefault("iteration", {
                "cycle": 0, "max_cycles": 5,
                "frozen_phases": ["phase1_literature", "phase2_sota", "phase2_5_profile"],
                "history": [],
            })
            iteration.setdefault("tracks", {
                "explore_ratio": 0.4,
                "exploit_tested": 0,
                "explore_tested": 0,
                "current_track": None,
                "track_switches": 0,
            })
            state["schema_version"] = 4

        if version < 5:
            # v1.12: Add tournament block for hypothesis successive halving
            iteration = state.setdefault("iteration", {
                "cycle": 0, "max_cycles": 5,
                "frozen_phases": ["phase1_literature", "phase2_sota", "phase2_5_profile"],
                "history": [],
            })
            iteration.setdefault("tournament", {
                "status": "pending",
                "current_round": 0,
                "total_rounds": 3,
                "hypotheses_entered": 0,
                "champion": None,
                "champion_score": None,
                "rounds_completed": 0,
                "budget_allocation": {
                    "round1_fraction": 0.15,
                    "round2_fraction": 0.30,
                    "round3_fraction": 0.55,
                },
            })
            state["schema_version"] = 5

        return state

    def get_resume_context(self, state: Dict) -> Dict:
        """Generate context needed for session resumption.

        Returns a dict with current phase, files to read, training job status,
        and iteration context (cycle, history, learned constraints).
        """
        phase = state.get("current_phase", "phase1_literature")
        iteration = state.get("iteration", {"cycle": 0, "history": []})
        cycle = iteration.get("cycle", 0)
        context = {
            "current_phase": phase,
            "topic": state.get("topic", ""),
            "depth": state.get("depth", "full"),
            "cycle": cycle,
            "iteration_history": iteration.get("history", []),
            "files_to_read": [],
            "training_status": [],
        }

        # Strip cycle suffix to determine base phase for file lookup
        base_phase = re.sub(r'_c\d+$', '', phase)

        # Determine which files to read based on current base phase
        base = str(self.research_dir)

        # For cycle > 0, use cycle-suffixed directories
        def pd(name):
            return self.phase_dir(name, cycle)

        phase_files_map = {
            "phase3_ideas": [
                f"{base}/phase1_literature/literature-review.md",
                f"{base}/phase2_sota/sota-comparison-table.md",
                f"{base}/phase2_sota/sota-catalog.json",
            ],
            "phase4_design": [
                f"{base}/{pd('phase3_ideas')}/research-hypotheses.md",
                f"{base}/{pd('phase3_ideas')}/codebase-analysis.md",
            ],
            "phase5_baseline": [
                f"{base}/{pd('phase3_ideas')}/research-hypotheses.md",
                f"{base}/{pd('phase4_design')}/experiment-plan.md",
            ],
            "phase6_experiments": [
                f"{base}/phase5_baseline/baseline-results.json",
                f"{base}/{pd('phase4_design')}/experiment-plan.md",
            ],
            "phase7_analysis": [
                f"{base}/{pd('phase6_experiments')}/",
            ],
            "phase8_writing": [
                f"{base}/{pd('phase7_analysis')}/analysis-report.md",
                f"{base}/{pd('phase3_ideas')}/research-hypotheses.md",
            ],
        }

        # Always include current phase reasoning
        context["files_to_read"].append(f"{base}/{phase}/reasoning.md")
        context["files_to_read"].extend(phase_files_map.get(base_phase, []))

        # For cycle > 0: also read learned constraints and iteration history
        if cycle > 0:
            context["files_to_read"].append(f"{base}/learned-constraints.md")

        # Check training jobs
        for job in state.get("training_jobs", []):
            if job.get("status") == "running":
                context["training_status"].append(job)

        return context

    @staticmethod
    def phase_dir(phase_name: str, cycle: int = None) -> str:
        """Return phase directory name with cycle suffix.

        Cycle 0 → 'phase3_ideas' (backward compatible with existing data).
        Cycle N (N>=1) → 'phase3_ideas_cN'.
        If cycle is None, returns base name (no suffix).
        """
        if cycle is None or cycle == 0:
            return phase_name
        return f"{phase_name}_c{cycle}"

    def regress_to_phase3(self, state: Dict, failure_summary: Dict) -> Dict:
        """Big loop: record failure → increment cycle → create new dirs → reset to Phase 3.

        failure_summary must contain:
          - hypotheses_tested: list of hypothesis strings
          - best_metric: {name, value, baseline}
          - outcome: string description
          - root_cause_whys: list of 5 Why strings (MANDATORY)
          - learned_constraints: list of root-cause constraint strings
          - gpu_hours_this_cycle: float
        """
        iteration = state.setdefault("iteration", {
            "cycle": 0, "max_cycles": 5,
            "frozen_phases": ["phase1_literature", "phase2_sota", "phase2_5_profile"],
            "history": [],
        })

        # Validate 5-Whys depth
        whys = failure_summary.get("root_cause_whys", [])
        if len(whys) < 3:
            raise ValueError(
                f"regress_to_phase3 requires >=3 root_cause_whys, got {len(whys)}. "
                "Do 5-Whys analysis before calling regress."
            )

        # Record failure in history
        record = {
            "cycle": iteration["cycle"],
            "hypotheses_tested": failure_summary.get("hypotheses_tested", []),
            "best_metric": failure_summary.get("best_metric", {}),
            "outcome": failure_summary.get("outcome", "unknown"),
            "root_cause_whys": whys,
            "learned_constraints": failure_summary.get("learned_constraints", []),
            "gpu_hours_this_cycle": failure_summary.get("gpu_hours_this_cycle", 0),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        iteration["history"].append(record)

        # Auto-extract constraints to workspace knowledge base (v1.9)
        self._extract_constraints_to_kb(record, state.get("topic", ""))

        # Increment cycle
        iteration["cycle"] += 1
        new_cycle = iteration["cycle"]

        # Create new cycle directories
        cycle_phases = [
            "phase3_ideas", "phase3_5_quickval", "phase4_design",
            "phase6_experiments", "phase7_analysis",
        ]
        for phase in cycle_phases:
            dir_name = self.phase_dir(phase, new_cycle)
            dir_path = self.research_dir / dir_name
            dir_path.mkdir(parents=True, exist_ok=True)

        # Set current phase to new cycle's Phase 3
        new_phase = self.phase_dir("phase3_ideas", new_cycle)
        state["current_phase"] = new_phase

        # Add phase entries for the new cycle
        for phase in cycle_phases:
            phase_key = self.phase_dir(phase, new_cycle)
            state["phases"][phase_key] = {"status": "pending", "gate_results": {}}
        state["phases"][new_phase]["status"] = "in_progress"
        state["phases"][new_phase]["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        # v1.12: Reset tournament state for new cycle (keep budget_allocation)
        tournament = iteration.get("tournament", {})
        tournament["status"] = "pending"
        tournament["current_round"] = 0
        tournament["champion"] = None
        tournament["champion_score"] = None
        iteration["tournament"] = tournament

        self.save(state)
        return state

    def regress_to_phase4(self, state: Dict, diagnosis_notes: str) -> Dict:
        """Medium loop: same cycle, reset to Phase 4 for redesigned experiment.

        Does NOT increment cycle. Resets current_phase to phase4_design_cN
        (or phase4_design for cycle 0).
        """
        iteration = state.get("iteration", {"cycle": 0})
        current_cycle = iteration.get("cycle", 0)

        # Target phase
        target_phase = self.phase_dir("phase4_design", current_cycle)

        # Ensure directory exists
        dir_path = self.research_dir / target_phase
        dir_path.mkdir(parents=True, exist_ok=True)

        # Reset phase status
        state["phases"].setdefault(target_phase, {"status": "pending", "gate_results": {}})
        state["phases"][target_phase]["status"] = "in_progress"
        state["phases"][target_phase]["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if diagnosis_notes:
            state["phases"][target_phase]["regression_notes"] = diagnosis_notes

        state["current_phase"] = target_phase

        # v1.12: Reset tournament state (keep budget_allocation and rounds_completed)
        tournament = iteration.get("tournament", {})
        tournament["status"] = "pending"
        tournament["current_round"] = 0
        tournament["champion"] = None
        tournament["champion_score"] = None
        iteration["tournament"] = tournament

        self.save(state)
        return state

    # --- Track management (v1.11: dual-track explore/exploit) ---

    def track_status(self, state: Dict) -> Dict:
        """Return current track stats from state."""
        tracks = state.get("iteration", {}).get("tracks", {})
        return {
            "explore_ratio": tracks.get("explore_ratio", 0.4),
            "exploit_tested": tracks.get("exploit_tested", 0),
            "explore_tested": tracks.get("explore_tested", 0),
            "current_track": tracks.get("current_track"),
            "track_switches": tracks.get("track_switches", 0),
        }

    def track_switch(self, state: Dict, new_track: str) -> Dict:
        """Record a track switch (EXPLOIT↔EXPLORE) in state.

        DEPRECATED in v1.12 — tournament tests all tracks simultaneously.
        Kept for backward compatibility with v1.11 state files.
        """
        if new_track not in ("EXPLOIT", "EXPLORE"):
            raise ValueError(f"Invalid track: {new_track}. Must be EXPLOIT or EXPLORE.")
        tracks = state.setdefault("iteration", {}).setdefault("tracks", {
            "explore_ratio": 0.4, "exploit_tested": 0,
            "explore_tested": 0, "current_track": None, "track_switches": 0,
        })
        tracks["current_track"] = new_track
        tracks["track_switches"] = tracks.get("track_switches", 0) + 1
        self.save(state)
        return state

    def track_tested(self, state: Dict, track: str) -> Dict:
        """Increment tested count for a track."""
        if track not in ("EXPLOIT", "EXPLORE"):
            raise ValueError(f"Invalid track: {track}. Must be EXPLOIT or EXPLORE.")
        tracks = state.setdefault("iteration", {}).setdefault("tracks", {
            "explore_ratio": 0.4, "exploit_tested": 0,
            "explore_tested": 0, "current_track": None, "track_switches": 0,
        })
        key = f"{track.lower()}_tested"
        tracks[key] = tracks.get(key, 0) + 1
        self.save(state)
        return state

    # --- Tournament management (v1.12: hypothesis successive halving) ---

    def tournament_init(self, state: Dict, budget_file: str) -> Dict:
        """Initialize tournament from budget file.

        Reads tournament-budget.json, populates state.iteration.tournament,
        and creates tournament.json in phase6_experiments directory.
        """
        with open(budget_file, "r") as f:
            budget = json.load(f)

        iteration = state.setdefault("iteration", {})
        tournament = iteration.setdefault("tournament", {})

        n_hypotheses = budget.get("hypotheses_entering", 0)
        tournament["status"] = "in_progress"
        tournament["current_round"] = 1
        tournament["total_rounds"] = len(budget.get("rounds", []))
        tournament["hypotheses_entered"] = n_hypotheses
        tournament["champion"] = None
        tournament["champion_score"] = None
        tournament["rounds_completed"] = 0

        # Store budget allocation from file
        rounds = budget.get("rounds", [])
        if len(rounds) >= 3:
            tournament["budget_allocation"] = {
                "round1_fraction": rounds[0].get("budget_fraction", 0.15),
                "round2_fraction": rounds[1].get("budget_fraction", 0.30),
                "round3_fraction": rounds[2].get("budget_fraction", 0.55),
            }

        # Create tournament.json in phase6_experiments
        cycle = iteration.get("cycle", 0)
        exp_dir = self.research_dir / self.phase_dir("phase6_experiments", cycle)
        exp_dir.mkdir(parents=True, exist_ok=True)
        tournament_file = exp_dir / "tournament.json"

        tournament_data = {
            "status": "in_progress",
            "primary_metric": budget.get("primary_metric", ""),
            "metric_direction": budget.get("metric_direction", "higher_is_better"),
            "venue_target": budget.get("venue_target", "poster"),
            "scoring_weights": budget.get("scoring_weights", {}),
            "total_gpu_hours": budget.get("total_gpu_hours", 0),
            "hypotheses": {f"H{i+1}": {"status": "competing", "track": None}
                          for i in range(n_hypotheses)},
            "rounds": [],
            "champion": None,
            "champion_score": None,
        }
        atomic_json_write(str(tournament_file), tournament_data)

        self.save(state)
        return state

    def tournament_record_score(self, state: Dict, round_num: int,
                                hyp_id: str, scores: Dict) -> Dict:
        """Record per-hypothesis per-round scores in tournament.json."""
        iteration = state.get("iteration", {})
        cycle = iteration.get("cycle", 0)
        exp_dir = self.research_dir / self.phase_dir("phase6_experiments", cycle)
        tournament_file = exp_dir / "tournament.json"

        if not tournament_file.exists():
            raise FileNotFoundError(f"tournament.json not found at {tournament_file}")

        with open(tournament_file, "r") as f:
            tdata = json.load(f)

        # Ensure round entry exists
        while len(tdata["rounds"]) < round_num:
            tdata["rounds"].append({"round": len(tdata["rounds"]) + 1, "scores": {}, "eliminated": [], "advanced": []})

        round_entry = tdata["rounds"][round_num - 1]
        round_entry["scores"][hyp_id] = scores

        atomic_json_write(str(tournament_file), tdata)
        return state

    def tournament_eliminate(self, state: Dict, round_num: int,
                             eliminated: List[str], advanced: List[str]) -> Dict:
        """Record elimination decisions for a round."""
        iteration = state.setdefault("iteration", {})
        tournament = iteration.setdefault("tournament", {})
        cycle = iteration.get("cycle", 0)

        # Update tournament.json
        exp_dir = self.research_dir / self.phase_dir("phase6_experiments", cycle)
        tournament_file = exp_dir / "tournament.json"

        if tournament_file.exists():
            with open(tournament_file, "r") as f:
                tdata = json.load(f)

            while len(tdata["rounds"]) < round_num:
                tdata["rounds"].append({"round": len(tdata["rounds"]) + 1, "scores": {}, "eliminated": [], "advanced": []})

            round_entry = tdata["rounds"][round_num - 1]
            round_entry["eliminated"] = eliminated
            round_entry["advanced"] = advanced

            # Update hypothesis statuses
            for h in eliminated:
                if h in tdata["hypotheses"]:
                    tdata["hypotheses"][h]["status"] = "eliminated"
                    tdata["hypotheses"][h]["eliminated_round"] = round_num
            for h in advanced:
                if h in tdata["hypotheses"]:
                    tdata["hypotheses"][h]["status"] = "competing"

            atomic_json_write(str(tournament_file), tdata)

        # Update state summary
        tournament["rounds_completed"] = round_num
        tournament["current_round"] = round_num + 1

        # Update track tested counters from eliminated hypotheses
        tracks = iteration.setdefault("tracks", {})
        for h in eliminated + advanced:
            # Track info stored in tournament.json hypotheses
            if tournament_file.exists():
                with open(tournament_file, "r") as f:
                    tdata2 = json.load(f)
                track = tdata2.get("hypotheses", {}).get(h, {}).get("track")
                if track == "EXPLOIT":
                    tracks["exploit_tested"] = tracks.get("exploit_tested", 0) + 1
                elif track == "EXPLORE":
                    tracks["explore_tested"] = tracks.get("explore_tested", 0) + 1

        self.save(state)
        return state

    def tournament_complete(self, state: Dict, champion_id: str, score: float) -> Dict:
        """Mark tournament as completed with champion."""
        iteration = state.setdefault("iteration", {})
        tournament = iteration.setdefault("tournament", {})
        cycle = iteration.get("cycle", 0)

        tournament["status"] = "completed"
        tournament["champion"] = champion_id
        tournament["champion_score"] = score

        # Update tournament.json
        exp_dir = self.research_dir / self.phase_dir("phase6_experiments", cycle)
        tournament_file = exp_dir / "tournament.json"

        if tournament_file.exists():
            with open(tournament_file, "r") as f:
                tdata = json.load(f)
            tdata["status"] = "completed"
            tdata["champion"] = champion_id
            tdata["champion_score"] = score
            atomic_json_write(str(tournament_file), tdata)

        self.save(state)
        return state

    def tournament_fail(self, state: Dict, reason: str) -> Dict:
        """Mark tournament as failed (all eliminated or no champion)."""
        iteration = state.setdefault("iteration", {})
        tournament = iteration.setdefault("tournament", {})
        cycle = iteration.get("cycle", 0)

        tournament["status"] = "failed"
        tournament["champion"] = None
        tournament["champion_score"] = None

        # Update tournament.json
        exp_dir = self.research_dir / self.phase_dir("phase6_experiments", cycle)
        tournament_file = exp_dir / "tournament.json"

        if tournament_file.exists():
            with open(tournament_file, "r") as f:
                tdata = json.load(f)
            tdata["status"] = "failed"
            tdata["champion"] = None
            tdata["failure_reason"] = reason
            atomic_json_write(str(tournament_file), tdata)

        self.save(state)
        return state

    def tournament_status(self, state: Dict) -> str:
        """Return formatted tournament status summary."""
        iteration = state.get("iteration", {})
        tournament = iteration.get("tournament", {})
        cycle = iteration.get("cycle", 0)

        lines = [
            f"Tournament Status: {tournament.get('status', 'pending')}",
            f"  Round: {tournament.get('current_round', 0)}/{tournament.get('total_rounds', 3)}",
            f"  Hypotheses entered: {tournament.get('hypotheses_entered', 0)}",
            f"  Rounds completed: {tournament.get('rounds_completed', 0)}",
            f"  Champion: {tournament.get('champion', 'None')}",
            f"  Champion score: {tournament.get('champion_score', 'N/A')}",
        ]

        # Read detailed data from tournament.json if available
        exp_dir = self.research_dir / self.phase_dir("phase6_experiments", cycle)
        tournament_file = exp_dir / "tournament.json"
        if tournament_file.exists():
            try:
                with open(tournament_file, "r") as f:
                    tdata = json.load(f)
                for r in tdata.get("rounds", []):
                    rnum = r.get("round", "?")
                    advanced = r.get("advanced", [])
                    eliminated = r.get("eliminated", [])
                    lines.append(f"  Round {rnum}: {len(advanced)} advanced, {len(eliminated)} eliminated")
                    for hid, sc in r.get("scores", {}).items():
                        total = sc.get("total", "?")
                        lines.append(f"    {hid}: {total}")
            except (json.JSONDecodeError, OSError):
                pass

        return "\n".join(lines)

    def _find_workspace_research(self) -> Optional[Path]:
        """Find workspace-level .research/ (the one with paper-cache/)."""
        current = self.research_dir.resolve()
        for _ in range(5):
            parent = current.parent
            candidate = parent / ".research"
            if candidate != current and candidate.exists() and \
               (candidate / "paper-cache").exists():
                return candidate
            current = parent
        return None

    def _extract_constraints_to_kb(self, cycle_record: Dict, topic: str):
        """Auto-extract learned constraints to workspace knowledge base."""
        ws_research = self._find_workspace_research()
        if not ws_research:
            return
        kb = KnowledgeBase(str(ws_research / "knowledge"))
        domain = normalize_topic(topic)
        project_name = self.research_dir.parent.name

        for constraint_text in cycle_record.get("learned_constraints", []):
            # Simple dedup: title word-set Jaccard > 0.6 means duplicate
            existing = kb.get_constraints_for_domain(domain)
            new_words = set(constraint_text.lower().split())
            is_dup = any(
                len(new_words & set(e.get("title", "").lower().split())) /
                max(len(new_words | set(e.get("title", "").lower().split())), 1) > 0.6
                for e in existing
            )
            if is_dup:
                continue

            title = constraint_text.split(".")[0][:120]
            whys = cycle_record.get("root_cause_whys", [])
            content = f"# {title}\n\n"
            content += "## Root Cause (5-Whys)\n\n"
            for why in whys:
                content += f"- {why}\n"
            content += f"\n## Constraint\n\n{constraint_text}\n"
            content += f"\n## Evidence\n\n"
            content += f"- Project: {project_name}, Cycle {cycle_record.get('cycle', 0)}\n"
            for h in cycle_record.get("hypotheses_tested", []):
                content += f"- Hypothesis: {h}\n"
            content += f"- Outcome: {cycle_record.get('outcome', 'unknown')}\n"

            kb.add_constraint(
                title=title, domain=domain, severity="hard",
                content_md=content, source_project=project_name,
                source_phase="phase6_experiments",
                source_cycle=cycle_record.get("cycle", 0),
                tags=_extract_tags(constraint_text),
            )


# =============================================================================
# Knowledge Base (cross-project knowledge accumulation, v1.9)
# =============================================================================

def normalize_topic(topic: str) -> str:
    """Topic string → filesystem-safe slug.

    'Adversarial Robustness of VLMs' → 'adversarial-robustness-vlms'
    """
    slug = topic.lower().strip()
    for word in ["of", "the", "for", "and", "in", "on", "a", "an", "with"]:
        slug = re.sub(rf'\b{word}\b', '', slug)
    slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
    slug = re.sub(r'-+', '-', slug)
    if len(slug) > 60:
        slug = slug[:60].rsplit('-', 1)[0]
    return slug


def _write_atomic(path_str: str, content: str):
    """Atomic text file write (temp + rename)."""
    parent = os.path.dirname(path_str) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp", prefix=".kb_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path_str)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _extract_tags(text: str) -> List[str]:
    """Extract candidate tags from constraint/technique text."""
    keywords = [
        "differentiable", "deterministic", "stochastic", "purifier",
        "adversarial", "training-free", "white-box", "gradient",
        "APGD", "autoregressive", "diffusion", "KV-cache",
        "attention", "transformer", "VRAM", "latency",
        "prefix-cache", "quantization", "pruning", "distillation",
    ]
    text_lower = text.lower()
    return [k for k in keywords if k.lower() in text_lower]


class KnowledgeBase:
    """Cross-project knowledge at .research/knowledge/.

    Three entry types:
    - constraints/ — root-cause constraints from 5-Whys (C-NNNN.md)
    - techniques/ — reusable methods proven in experiments (T-NNNN.md)
    - domains/ — method family trees + dead ends + open directions ({slug}.md)

    Follows PaperCache pattern: index.json + content files + atomic_json_write.
    """

    def __init__(self, knowledge_dir: str):
        self.knowledge_dir = Path(knowledge_dir)
        self.index_path = self.knowledge_dir / "index.json"
        self._index = {
            "schema_version": 1,
            "next_constraint_id": 1,
            "next_technique_id": 1,
            "entries": {},
        }
        self._load()
        # Ensure index.json exists on first access
        if not self.index_path.exists():
            self.save()

    def _load(self):
        if self.index_path.exists():
            try:
                with open(self.index_path) as f:
                    self._index = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(str(self.index_path), self._index)

    # --- Write methods ---

    def add_constraint(self, title: str, domain: str, severity: str,
                       content_md: str, source_project: str,
                       source_phase: str, source_cycle: int = 0,
                       tags: Optional[List[str]] = None) -> str:
        """Write constraints/C-NNNN.md + update index. Returns ID."""
        cid = f"C-{self._index['next_constraint_id']:04d}"
        self._index['next_constraint_id'] += 1
        file_rel = f"constraints/{cid}.md"
        file_abs = self.knowledge_dir / file_rel
        file_abs.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(str(file_abs), content_md)
        self._index["entries"][cid] = {
            "type": "constraint",
            "title": title,
            "domain": domain,
            "severity": severity,
            "tags": tags or [],
            "source_project": source_project,
            "source_phase": source_phase,
            "source_cycle": source_cycle,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "file": file_rel,
        }
        self.save()
        return cid

    def add_technique(self, title: str, domain: str, content_md: str,
                      source_project: str, source_phase: str,
                      measured_impact: str = "", tags: Optional[List[str]] = None) -> str:
        """Write techniques/T-NNNN.md + update index. Returns ID."""
        tid = f"T-{self._index['next_technique_id']:04d}"
        self._index['next_technique_id'] += 1
        file_rel = f"techniques/{tid}.md"
        file_abs = self.knowledge_dir / file_rel
        file_abs.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(str(file_abs), content_md)
        self._index["entries"][tid] = {
            "type": "technique",
            "title": title,
            "domain": domain,
            "tags": tags or [],
            "source_project": source_project,
            "source_phase": source_phase,
            "measured_impact": measured_impact,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "file": file_rel,
        }
        self.save()
        return tid

    def update_domain(self, topic: str, content_md: str,
                      source_project: str, key_papers: Optional[List[str]] = None,
                      tags: Optional[List[str]] = None) -> str:
        """Write/overwrite domains/{slug}.md + update index. Returns ID."""
        slug = normalize_topic(topic)
        did = f"D-{slug}"
        file_rel = f"domains/{slug}.md"
        file_abs = self.knowledge_dir / file_rel
        file_abs.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(str(file_abs), content_md)
        existing = self._index.get("entries", {}).get(did, {})
        projects = list(set(existing.get("projects", []) + [source_project]))
        self._index["entries"][did] = {
            "type": "domain",
            "title": topic,
            "tags": tags or [],
            "projects": projects,
            "key_papers": key_papers or [],
            "last_updated": time.strftime("%Y-%m-%d"),
            "file": file_rel,
        }
        self.save()
        return did

    # --- Query methods ---

    def query(self, entry_type: Optional[str] = None,
              domain: Optional[str] = None,
              keyword: Optional[str] = None) -> List[Dict]:
        """Query by type/domain/keyword. Returns index entries list."""
        results = []
        for eid, entry in self._index.get("entries", {}).items():
            if entry_type and entry.get("type") != entry_type:
                continue
            if domain and entry.get("domain") != domain:
                continue
            if keyword:
                kw = keyword.lower()
                if kw not in entry.get("title", "").lower() and \
                   not any(kw in t.lower() for t in entry.get("tags", [])):
                    continue
            results.append({"id": eid, **entry})
        return results

    def read_entry(self, entry_id: str) -> Optional[str]:
        """Read full markdown content of an entry."""
        entry = self._index.get("entries", {}).get(entry_id)
        if not entry:
            return None
        entry_path = self.knowledge_dir / entry["file"]
        return entry_path.read_text() if entry_path.exists() else None

    def get_constraints_for_domain(self, domain: str) -> List[Dict]:
        """Get all constraints for a domain."""
        return [{"id": eid, **e}
                for eid, e in self._index.get("entries", {}).items()
                if e.get("type") == "constraint" and e.get("domain") == domain]

    def get_domain_file(self, topic: str) -> Optional[str]:
        """Return absolute path to domain file, or None if not exists."""
        slug = normalize_topic(topic)
        domain_path = self.knowledge_dir / "domains" / f"{slug}.md"
        return str(domain_path) if domain_path.exists() else None

    def summary(self) -> Dict:
        """Return knowledge base summary stats."""
        entries = self._index.get("entries", {})
        return {
            "constraints": sum(1 for e in entries.values() if e["type"] == "constraint"),
            "techniques": sum(1 for e in entries.values() if e["type"] == "technique"),
            "domains": sum(1 for e in entries.values() if e["type"] == "domain"),
            "total": len(entries),
        }


def _find_workspace_research_standalone() -> str:
    """Find workspace .research/ from cwd (standalone, no StateManager)."""
    current = Path.cwd().resolve()
    for _ in range(10):
        candidate = current / ".research"
        if candidate.exists() and (candidate / "paper-cache").exists():
            return str(candidate)
        # Also check if we're inside a project subdir
        if (current / ".research" / "state.json").exists():
            parent_research = current.parent / ".research"
            if parent_research.exists() and (parent_research / "paper-cache").exists():
                return str(parent_research)
        current = current.parent
    raise FileNotFoundError("No workspace .research/ with paper-cache/ found")


def _migrate_constraints_from_file(kb: KnowledgeBase, text: str,
                                   domain: str, project_name: str):
    """Parse learned-constraints.md text and add constraints to KB."""
    # Parse constraint blocks: lines starting with "## C" or "### C" or "- C\d:" or "**C\d"
    blocks = re.split(r'\n(?=##\s+C\d|###\s+C\d|- C\d|\*\*C\d)', text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        # Extract title from first line
        first_line = block.split('\n')[0]
        title_match = re.match(r'(?:##\s+|###\s+|- |\*\*)(C\d+[:\s].*)', first_line)
        if not title_match:
            continue
        title = title_match.group(1).strip().rstrip('*')[:120]

        # Dedup: Jaccard > 0.6
        existing = kb.get_constraints_for_domain(domain)
        new_words = set(title.lower().split())
        is_dup = any(
            len(new_words & set(e.get("title", "").lower().split())) /
            max(len(new_words | set(e.get("title", "").lower().split())), 1) > 0.6
            for e in existing
        )
        if is_dup:
            continue

        kb.add_constraint(
            title=title, domain=domain, severity="hard",
            content_md=block, source_project=project_name,
            source_phase="migration", source_cycle=0,
            tags=_extract_tags(block),
        )


def migrate_knowledge(project_dir: Optional[str] = None):
    """Backfill knowledge base from existing project artifacts."""
    ws_research = _find_workspace_research_standalone()
    kb = KnowledgeBase(os.path.join(ws_research, "knowledge"))

    if project_dir:
        projects = [Path(project_dir)]
    else:
        # Find all projects under workspace that have .research/state.json
        ws_root = Path(ws_research).parent
        projects = []
        for d in ws_root.iterdir():
            if d.is_dir() and (d / ".research" / "state.json").exists():
                projects.append(d)

    for proj in projects:
        research = proj / ".research"
        try:
            with open(research / "state.json") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue

        topic = state.get("topic", proj.name)
        domain = normalize_topic(topic)
        project_name = proj.name

        # Migrate learned-constraints.md
        lc = research / "learned-constraints.md"
        if lc.exists():
            _migrate_constraints_from_file(kb, lc.read_text(), domain, project_name)

        # Migrate literature-review.md → domain
        lit = research / "phase1_literature" / "literature-review.md"
        if lit.exists():
            content = lit.read_text()
            # Truncate to 8000 chars to keep domain files manageable
            kb.update_domain(topic, content[:8000], project_name,
                             tags=[domain])

    print(json.dumps(kb.summary(), indent=2))


# =============================================================================
# Paper Deduplicator
# =============================================================================

class PaperDeduplicator:
    """Multi-source paper deduplication using DOI, ArXiv ID, and title matching.

    Implements three strategies:
    1. Exact match on DOI
    2. Exact match on ArXiv ID
    3. Fuzzy title match (Jaccard word overlap > threshold)
    """

    def __init__(self, threshold: float = 0.9):
        self.threshold = threshold
        self.papers: List[Dict] = []
        self._doi_index: Dict[str, int] = {}
        self._arxiv_index: Dict[str, int] = {}

    def load_from_file(self, path: str):
        """Load existing papers from papers-metadata.json."""
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
            self.papers = data if isinstance(data, list) else data.get("papers", [])
            self._rebuild_index()

    def save_to_file(self, path: str):
        """Save papers to papers-metadata.json."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"papers": self.papers, "count": len(self.papers)}, f,
                      indent=2, ensure_ascii=False)

    def _rebuild_index(self):
        """Rebuild DOI and ArXiv ID indexes."""
        self._doi_index.clear()
        self._arxiv_index.clear()
        for i, paper in enumerate(self.papers):
            doi = paper.get("doi", "")
            if doi:
                self._doi_index[doi.lower()] = i
            arxiv_id = paper.get("arxiv_id", "")
            if arxiv_id:
                self._arxiv_index[arxiv_id.lower()] = i

    def is_duplicate(self, paper: Dict) -> Tuple[bool, Optional[int]]:
        """Check if paper is a duplicate.

        Returns:
            (is_dup, existing_index): True and index if duplicate found
        """
        # Strategy 1: DOI exact match
        doi = paper.get("doi", "")
        if doi and doi.lower() in self._doi_index:
            return True, self._doi_index[doi.lower()]

        # Strategy 2: ArXiv ID exact match
        arxiv_id = paper.get("arxiv_id", "")
        if arxiv_id and arxiv_id.lower() in self._arxiv_index:
            return True, self._arxiv_index[arxiv_id.lower()]

        # Strategy 3: Fuzzy title match
        title = paper.get("title", "")
        if title:
            for i, existing in enumerate(self.papers):
                if self._title_similarity(title, existing.get("title", "")) > self.threshold:
                    return True, i

        return False, None

    def add_paper(self, paper: Dict) -> Tuple[bool, int]:
        """Add paper if not duplicate. Returns (was_added, index).

        If duplicate, merges missing fields from new paper into existing.
        """
        is_dup, idx = self.is_duplicate(paper)
        if is_dup and idx is not None:
            # Merge: fill in missing fields from new paper
            existing = self.papers[idx]
            for key, value in paper.items():
                if value and not existing.get(key):
                    existing[key] = value
            return False, idx

        # New paper
        idx = len(self.papers)
        self.papers.append(paper)
        # Update indexes
        doi = paper.get("doi", "")
        if doi:
            self._doi_index[doi.lower()] = idx
        arxiv_id = paper.get("arxiv_id", "")
        if arxiv_id:
            self._arxiv_index[arxiv_id.lower()] = idx
        return True, idx

    @staticmethod
    def _title_similarity(title_a: str, title_b: str) -> float:
        """Compute Jaccard word overlap between two titles."""
        def tokenize(s: str) -> set:
            # Lowercase, remove punctuation, split into words
            s = re.sub(r"[^\w\s]", "", s.lower())
            return set(s.split())

        words_a = tokenize(title_a)
        words_b = tokenize(title_b)
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)


# =============================================================================
# Process Manager
# =============================================================================

class ProcessManager:
    """Manages background training processes (nohup-based).

    Handles: launch, PID tracking, status check, graceful kill, cleanup.
    Implements Graceful Kill Hierarchy: SIGTERM → grace period → SIGKILL.
    """

    @staticmethod
    def launch_background(command: str, log_path: str, pid_file: str,
                          cwd: str = ".") -> int:
        """Launch a command via nohup in the background.

        Args:
            command: Shell command to run
            log_path: Path for stdout/stderr log
            pid_file: Path to write PID
            cwd: Working directory

        Returns:
            PID of the launched process
        """
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(pid_file) or ".", exist_ok=True)

        # Use nohup + setsid for process independence
        full_cmd = f"nohup {command} > {log_path} 2>&1 & echo $!"
        result = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, cwd=cwd
        )
        pid = int(result.stdout.strip())

        with open(pid_file, "w") as f:
            f.write(str(pid))

        return pid

    @staticmethod
    def is_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def read_pid(pid_file: str) -> Optional[int]:
        """Read PID from file."""
        try:
            with open(pid_file, "r") as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def graceful_kill(pid: int, grace_period: int = 60) -> str:
        """Kill a process with graceful hierarchy: SIGTERM → wait → SIGKILL.

        Also kills the process group to handle torchrun workers.

        Returns:
            "terminated" | "killed" | "already_dead"
        """
        if not ProcessManager.is_alive(pid):
            return "already_dead"

        # Step 1: SIGTERM (graceful)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "already_dead"

        # Step 2: Wait for grace period
        for _ in range(grace_period):
            if not ProcessManager.is_alive(pid):
                return "terminated"
            time.sleep(1)

        # Step 3: SIGKILL (force) — kill process group
        try:
            pgid = os.getpgid(pid)
            # Safety: never kill our own process group
            if pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return "terminated"

        return "killed"

    @staticmethod
    def check_training_job(job: Dict) -> Dict:
        """Check the status of a training job registered in state.json.

        Args:
            job: Training job dict from state.json["training_jobs"]

        Returns:
            Updated job dict with current status and details
        """
        result = dict(job)

        done_flag = job.get("done_flag", "")
        fail_flag = job.get("fail_flag", "")
        pid_file = job.get("pid_file", "")

        # Check completion flags first
        if done_flag and os.path.exists(done_flag):
            result["status"] = "completed"
            return result

        if fail_flag and os.path.exists(fail_flag):
            result["status"] = "failed"
            # Read failure info if available
            try:
                with open(fail_flag, "r") as f:
                    result["error"] = f.read().strip()[:500]
            except Exception:
                pass
            return result

        # Check PID
        pid = ProcessManager.read_pid(pid_file) if pid_file else None
        if pid is None:
            result["status"] = "unknown"
            return result

        if ProcessManager.is_alive(pid):
            result["status"] = "running"
            result["pid"] = pid
            # Read last few lines of log
            log_path = job.get("log_path", "")
            if log_path and os.path.exists(log_path):
                try:
                    with open(log_path, "r") as f:
                        lines = f.readlines()
                        result["log_tail"] = "".join(lines[-5:])
                except Exception:
                    pass
        else:
            # PID dead but no .done/.failed — abnormal exit
            result["status"] = "crashed"
            log_path = job.get("log_path", "")
            if log_path and os.path.exists(log_path):
                try:
                    with open(log_path, "r") as f:
                        lines = f.readlines()
                        result["log_tail"] = "".join(lines[-20:])
                except Exception:
                    pass

        return result

    @staticmethod
    def cleanup_gpu_processes():
        """Find and report orphan GPU processes (does NOT auto-kill)."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,name,used_memory",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return ""


# =============================================================================
# Pipeline Logger (Layer 2 — semantic events from Master Agent)
# =============================================================================

class PipelineLogger:
    """Pipeline-level semantic event logging.

    Writes structured events to .research/pipeline-events.jsonl.
    Events from this logger merge with hook-generated events (structured-logger.js)
    in the same file, forming a complete Layer 2 event stream.

    Event types:
      phase_start / phase_end   — phase boundaries (duration = end.ts - start.ts)
      worker_dispatch           — Worker dispatched (agent name, model, task)
      worker_complete           — Worker returned (success/failure, summary)
      gate_check                — Gate check result (pass/fail, per-criterion)
      decision                  — Key decision made (with rationale)
      error                     — Error occurred (with context)
    """

    def __init__(self, research_dir: str):
        self.log_path = os.path.join(research_dir, 'pipeline-events.jsonl')

    def log(self, event_type: str, message: str, phase: str = None,
            worker: str = None, metadata: dict = None):
        """Append one structured event line."""
        entry = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': event_type,
            'phase': phase,
            'worker': worker,
            'message': message,
            'metadata': metadata or {}
        }
        # Remove None values for cleaner output
        entry = {k: v for k, v in entry.items() if v is not None}
        try:
            os.makedirs(os.path.dirname(self.log_path) or '.', exist_ok=True)
            with open(self.log_path, 'a') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except OSError:
            pass  # Logging should never break the pipeline


def generate_execution_report(research_dir: str) -> str:
    """Generate execution-report.md from pipeline-events.jsonl + state.json.

    Returns the path to the generated report.
    """
    events_path = os.path.join(research_dir, 'pipeline-events.jsonl')
    state_path = os.path.join(research_dir, 'state.json')
    report_path = os.path.join(research_dir, 'execution-report.md')

    # Load events
    events = []
    if os.path.exists(events_path):
        with open(events_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    # Load state
    state = {}
    if os.path.exists(state_path):
        with open(state_path, 'r') as f:
            state = json.load(f)

    topic = state.get('topic', state.get('venue', 'unknown'))
    now = datetime.now(timezone.utc).isoformat()

    lines = [
        f'# Execution Report — "{topic}"',
        f'Generated: {now}',
        '',
    ]

    # --- Timeline ---
    phase_starts = {}
    phase_ends = {}
    for e in events:
        if e.get('event') == 'phase_start':
            phase_starts[e.get('phase', '?')] = e.get('ts', '')
        elif e.get('event') == 'phase_end':
            phase_ends[e.get('phase', '?')] = e.get('ts', '')

    if phase_starts:
        lines.append('## Timeline')
        lines.append('| Phase | Started | Ended | Duration | Status |')
        lines.append('|-------|---------|-------|----------|--------|')
        for phase in sorted(set(list(phase_starts.keys()) + list(phase_ends.keys()))):
            start_ts = phase_starts.get(phase, '')
            end_ts = phase_ends.get(phase, '')
            duration = ''
            if start_ts and end_ts:
                try:
                    t0 = datetime.fromisoformat(start_ts)
                    t1 = datetime.fromisoformat(end_ts)
                    delta = t1 - t0
                    mins = int(delta.total_seconds() / 60)
                    duration = f'{mins}min'
                except (ValueError, TypeError):
                    duration = '?'
            status = '✅' if end_ts else '🔄'
            # Truncate timestamps for readability
            start_short = start_ts[11:16] if len(start_ts) > 16 else start_ts
            end_short = end_ts[11:16] if len(end_ts) > 16 else end_ts
            lines.append(f'| {phase} | {start_short} | {end_short} | {duration} | {status} |')
        lines.append('')

    # --- Tool Usage ---
    tool_counts = {}
    tool_failures = {}
    for e in events:
        if e.get('event') == 'PostToolUse':
            tool = e.get('tool', '?')
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
        elif e.get('event') == 'PostToolUseFailure':
            tool = e.get('tool', '?')
            tool_failures[tool] = tool_failures.get(tool, 0) + 1
            tool_counts[tool] = tool_counts.get(tool, 0) + 1

    if tool_counts:
        lines.append('## Tool Usage')
        lines.append('| Tool | Count | Failures |')
        lines.append('|------|-------|----------|')
        for tool in sorted(tool_counts.keys()):
            count = tool_counts[tool]
            fails = tool_failures.get(tool, 0)
            lines.append(f'| {tool} | {count} | {fails} |')
        lines.append('')

    # --- Worker Dispatches ---
    workers = []
    worker_starts = {}
    for e in events:
        if e.get('event') == 'worker_dispatch':
            workers.append(e)
        elif e.get('event') == 'SubagentStart':
            worker_starts[e.get('agent_name', e.get('agent_id', ''))] = e.get('ts', '')
        elif e.get('event') == 'worker_complete':
            workers.append(e)

    dispatch_events = [e for e in events if e.get('event') == 'worker_dispatch']
    complete_events = [e for e in events if e.get('event') == 'worker_complete']

    if dispatch_events:
        lines.append('## Worker Dispatches')
        lines.append('| # | Name | Phase | Result |')
        lines.append('|---|------|-------|--------|')
        for i, d in enumerate(dispatch_events, 1):
            name = d.get('worker', '?')
            phase = d.get('phase', '?')
            # Try to find matching completion
            comp = None
            for c in complete_events:
                if c.get('worker') == name:
                    comp = c
                    break
            result = comp.get('message', '?')[:60] if comp else '(no completion event)'
            lines.append(f'| {i} | {name} | {phase} | {result} |')
        lines.append('')

    # --- Gate Results ---
    gate_events = [e for e in events if e.get('event') == 'gate_check']
    if gate_events:
        lines.append('## Gate Results')
        lines.append('| Transition | Result | Details |')
        lines.append('|-----------|--------|---------|')
        for g in gate_events:
            phase = g.get('phase', '?')
            meta = g.get('metadata', {})
            passed = meta.get('passed', '?')
            result = '✅ PASS' if passed else '❌ FAIL'
            details = g.get('message', '')[:80]
            lines.append(f'| {phase} | {result} | {details} |')
        lines.append('')

    # --- Errors & Failures ---
    errors = [e for e in events if e.get('event') in ('error', 'PostToolUseFailure')]
    if errors:
        lines.append('## Errors & Failures')
        for i, e in enumerate(errors, 1):
            ts_short = e.get('ts', '')[11:19] if len(e.get('ts', '')) > 19 else e.get('ts', '')
            tool = e.get('tool', '')
            msg = e.get('message', e.get('error', ''))[:120]
            lines.append(f'{i}. [{ts_short}] {tool}: {msg}')
        lines.append('')

    # --- Key Decisions ---
    decisions = [e for e in events if e.get('event') == 'decision']
    if decisions:
        lines.append('## Key Decisions')
        for i, d in enumerate(decisions, 1):
            ts_short = d.get('ts', '')[11:19] if len(d.get('ts', '')) > 19 else d.get('ts', '')
            msg = d.get('message', '')[:120]
            lines.append(f'{i}. [{ts_short}] {msg}')
        lines.append('')

    # --- Summary Stats ---
    lines.append('## Summary')
    lines.append(f'- Total events: {len(events)}')
    lines.append(f'- Tool calls: {sum(tool_counts.values())}')
    lines.append(f'- Tool failures: {sum(tool_failures.values())}')
    lines.append(f'- Workers dispatched: {len(dispatch_events)}')
    lines.append(f'- Phases started: {len(phase_starts)}')
    lines.append(f'- Phases completed: {len(phase_ends)}')
    lines.append('')

    report_text = '\n'.join(lines)
    with open(report_path, 'w') as f:
        f.write(report_text)

    return report_path


# =============================================================================
# Hardware Detection
# =============================================================================

def detect_hardware() -> Dict:
    """Detect local GPU hardware configuration.

    Returns dict with gpu_name, gpu_count, vram_gb, cuda_version, etc.
    """
    hw = {
        "gpu_name": "unknown",
        "gpu_count": 0,
        "vram_gb": 0,
        "cuda_version": "unknown",
        "pytorch_cuda": "unknown",
        "cudnn_version": "unknown",
        "disk_free_gb": 0,
    }

    # GPU info via nvidia-smi
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,count",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            if lines:
                parts = lines[0].split(", ")
                hw["gpu_name"] = parts[0].strip()
                hw["vram_gb"] = round(int(parts[1].strip()) / 1024, 1) if len(parts) > 1 else 0
                hw["gpu_count"] = len(lines)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # CUDA version via nvcc
    try:
        result = subprocess.run(
            ["nvcc", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            match = re.search(r"release (\d+\.\d+)", result.stdout)
            if match:
                hw["cuda_version"] = match.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # PyTorch CUDA version
    try:
        result = subprocess.run(
            ["python", "-c", "import torch; print(torch.version.cuda)"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            hw["pytorch_cuda"] = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Disk free space
    try:
        stat = os.statvfs(".")
        hw["disk_free_gb"] = round(stat.f_bavail * stat.f_frsize / (1024 ** 3), 1)
    except Exception:
        pass

    return hw


# =============================================================================
# GPU Performance Tables & Mechanical Feasibility (v1.4)
# =============================================================================
# Prevents LLM "System 1" intuition from overriding arithmetic.
# Root cause: LLM judged "small model → feasible" without computing
# 32×H100 / 1×RTX_4090 = 58.2× gap, or 32B+7B+7B = 110GB >> 24GB (multi-model sum).

# Relative TFLOPS (fp16), normalized to H100 = 1.0
# Sources: NVIDIA specs, verified against published benchmarks
GPU_TFLOPS_RELATIVE = {
    "H200": 1.1, "H100": 1.0, "A100_80GB": 0.6, "A100": 0.6,
    "A800": 0.57, "L40S": 0.4, "L40": 0.35, "A6000": 0.3,
    "V100": 0.25, "RTX_4090": 0.55,
    "RTX_3090": 0.3, "RTX_3080": 0.2, "RTX_2080": 0.12,
}

# VRAM in GB
GPU_VRAM_GB = {
    "H200": 141, "H100": 80, "A100_80GB": 80, "A100": 40,
    "A800": 80, "L40S": 48, "L40": 48, "A6000": 48,
    "V100": 32, "RTX_4090": 24,
    "RTX_3090": 24, "RTX_3080": 10, "RTX_2080": 11,
}

# Regex patterns: (regex, canonical_key) — ordered from specific to general
_GPU_NORMALIZE_PATTERNS = [
    (re.compile(r'H200', re.I), 'H200'),
    (re.compile(r'H100', re.I), 'H100'),
    (re.compile(r'A100.{0,5}80\s*GB', re.I), 'A100_80GB'),
    (re.compile(r'A100', re.I), 'A100'),
    (re.compile(r'A800', re.I), 'A800'),
    (re.compile(r'L40S', re.I), 'L40S'),
    (re.compile(r'L40(?!S)', re.I), 'L40'),
    (re.compile(r'A6000', re.I), 'A6000'),
    (re.compile(r'V100', re.I), 'V100'),
    (re.compile(r'4090', re.I), 'RTX_4090'),
    (re.compile(r'3090', re.I), 'RTX_3090'),
    (re.compile(r'3080', re.I), 'RTX_3080'),
    (re.compile(r'2080', re.I), 'RTX_2080'),
]


def normalize_gpu_name(raw: str) -> Optional[str]:
    """Map free-text GPU name to canonical key in GPU_TFLOPS_RELATIVE.

    Examples:
        "NVIDIA A100 80GB" → "A100_80GB"
        "four nodes of 8 H100" → "H100"  (count handled separately)
        "RTX 4090" → "RTX_4090"
        "unknown GPU" → None
    """
    if not raw:
        return None
    for pattern, key in _GPU_NORMALIZE_PATTERNS:
        if pattern.search(raw):
            return key
    return None


def compute_mechanical_feasibility(
    paper_gpu_type: Optional[str],
    paper_gpu_count: Optional[int],
    paper_training_hours: Optional[float],
    largest_model_params_b: Optional[float],
    num_models_simultaneous: int = 1,
    peak_vram_reported_gb: Optional[float] = None,
    requires_training: Optional[bool] = None,
    target_gpu: str = "RTX_4090",
    target_vram_gb: float = 24.0,
    budget_hours: float = 8.0,
) -> dict:
    """Compute mechanical feasibility — pure arithmetic, no LLM judgment.

    Returns dict with:
        mechanical_verdict: fits|exceeds_vram|exceeds_time|exceeds_both|insufficient_data
        mechanical_reasoning: human-readable computation trace
        mechanical_flags: list of red-flag strings
        mechanical_compute_ratio: float or None
        mechanical_vram_gb: float or None
        mechanical_estimated_hours: float or None
    """
    flags = []
    reasoning_parts = []
    compute_ratio = None
    estimated_vram = None
    estimated_hours = None
    exceeds_vram = False
    exceeds_time = False
    has_data = False

    # --- Compute ratio ---
    normalized_paper_gpu = normalize_gpu_name(paper_gpu_type) if paper_gpu_type else None
    paper_speed = GPU_TFLOPS_RELATIVE.get(normalized_paper_gpu) if normalized_paper_gpu else None
    target_speed = GPU_TFLOPS_RELATIVE.get(target_gpu)

    if paper_speed is not None and target_speed is not None and paper_gpu_count and paper_gpu_count > 0:
        has_data = True
        compute_ratio = round(paper_gpu_count * paper_speed / target_speed, 1)
        reasoning_parts.append(
            f"{paper_gpu_count}x{normalized_paper_gpu}(speed={paper_speed}) vs "
            f"1x{target_gpu}(speed={target_speed}) → ratio={compute_ratio}×"
        )
        if compute_ratio > 10:
            flags.append(f"compute_{compute_ratio}x_gap")

        # Time estimate from ratio
        if paper_training_hours is not None and paper_training_hours > 0:
            estimated_hours = round(paper_training_hours * compute_ratio, 1)
            reasoning_parts.append(
                f"time: {paper_training_hours}h × {compute_ratio}× = {estimated_hours}h "
                f"(budget={budget_hours}h)"
            )
            if estimated_hours > budget_hours:
                exceeds_time = True
                flags.append(f"time_{estimated_hours}h_vs_{budget_hours}h_budget")

    # --- VRAM estimate ---
    if largest_model_params_b is not None and largest_model_params_b > 0:
        has_data = True
        n_models = max(1, num_models_simultaneous)
        # fp16: params_B × 2 bytes × 1.2 overhead (activations/KV cache)
        # If multiple models, sum them. Approximate: largest × n_models
        # (conservative — assumes all models are similar size to largest)
        if n_models == 1:
            estimated_vram = round(largest_model_params_b * 2 * 1.2, 1)
            reasoning_parts.append(
                f"VRAM: {largest_model_params_b}B × 2bytes × 1.2 = {estimated_vram}GB "
                f"(target={target_vram_gb}GB)"
            )
        else:
            # If paper reports specific model sizes, caller should pass the total
            # Here we use largest × n_models as upper bound
            estimated_vram = round(largest_model_params_b * n_models * 2 * 1.2, 1)
            reasoning_parts.append(
                f"VRAM: {largest_model_params_b}B × {n_models}models × 2bytes × 1.2 = "
                f"{estimated_vram}GB (target={target_vram_gb}GB)"
            )

        if estimated_vram > target_vram_gb:
            exceeds_vram = True
            flags.append(f"vram_{estimated_vram}GB_vs_{target_vram_gb}GB")

    # Use reported VRAM if available and higher than estimate
    if peak_vram_reported_gb is not None and peak_vram_reported_gb > 0:
        has_data = True
        if estimated_vram is None or peak_vram_reported_gb > estimated_vram:
            estimated_vram = peak_vram_reported_gb
            reasoning_parts.append(f"VRAM (reported): {peak_vram_reported_gb}GB")
        if peak_vram_reported_gb > target_vram_gb:
            exceeds_vram = True
            if f"vram_{estimated_vram}GB_vs_{target_vram_gb}GB" not in flags:
                flags.append(f"vram_{peak_vram_reported_gb}GB_reported_vs_{target_vram_gb}GB")

    # --- Training requirement amplifier ---
    if requires_training is True and estimated_vram is not None:
        # Training requires ~2× VRAM (backward pass + optimizer states)
        train_vram = round(estimated_vram * 2, 1)
        if train_vram > target_vram_gb and not exceeds_vram:
            exceeds_vram = True
            flags.append(f"train_vram_{train_vram}GB_vs_{target_vram_gb}GB")
            reasoning_parts.append(
                f"training VRAM ≈ {train_vram}GB (2× inference)"
            )

    # --- Verdict ---
    if not has_data:
        verdict = "insufficient_data"
    elif exceeds_vram and exceeds_time:
        verdict = "exceeds_both"
    elif exceeds_vram:
        verdict = "exceeds_vram"
    elif exceeds_time:
        verdict = "exceeds_time"
    else:
        verdict = "fits"

    return {
        "mechanical_verdict": verdict,
        "mechanical_reasoning": "; ".join(reasoning_parts) if reasoning_parts else "no hardware data",
        "mechanical_flags": flags,
        "mechanical_compute_ratio": compute_ratio,
        "mechanical_vram_gb": estimated_vram,
        "mechanical_estimated_hours": estimated_hours,
    }


def verify_papers_batch(
    papers: List[dict],
    target_gpu: str = "RTX_4090",
    target_vram_gb: float = 24.0,
    budget_hours: float = 8.0,
) -> Tuple[List[dict], List[dict]]:
    """Run mechanical feasibility on a list of papers.

    Returns (all_papers_updated, flagged_papers) where flagged_papers
    are those with verdict_disagrees=True or non-empty mechanical_flags.
    """
    flagged = []
    for paper in papers:
        result = compute_mechanical_feasibility(
            paper_gpu_type=paper.get("paper_gpu_type") or paper.get("reported_gpu_setup"),
            paper_gpu_count=paper.get("paper_gpu_count"),
            paper_training_hours=paper.get("paper_training_hours") or paper.get("estimated_hours"),
            largest_model_params_b=paper.get("largest_model_params_b"),
            num_models_simultaneous=paper.get("num_models_simultaneous", 1) or 1,
            peak_vram_reported_gb=paper.get("peak_vram_reported_gb"),
            requires_training=paper.get("requires_training"),
            target_gpu=target_gpu,
            target_vram_gb=target_vram_gb,
            budget_hours=budget_hours,
        )

        # Check if mechanical verdict disagrees with LLM verdict
        llm_verdict = paper.get("feasibility_verdict", "")
        mech_verdict = result["mechanical_verdict"]
        disagrees = False
        if mech_verdict in ("exceeds_vram", "exceeds_time", "exceeds_both"):
            if llm_verdict in ("feasible", "tight"):
                disagrees = True
        elif mech_verdict == "fits":
            if llm_verdict == "not_feasible":
                disagrees = True

        result["verdict_disagrees"] = disagrees
        paper.update(result)

        if disagrees or result["mechanical_flags"]:
            flagged.append(paper)

    return papers, flagged


# =============================================================================
# Directory Structure Creator
# =============================================================================

def create_research_dirs(base_dir: str = ".research"):
    """Create the standard .research/ directory structure."""
    dirs = [
        "phase1_literature",
        "phase2_sota/repos",
        "phase3_ideas",
        "phase4_design",
        "phase5_baseline",
        "phase6_experiments",
        "phase7_analysis/figures",
        "phase8_writing",
    ]
    for d in dirs:
        os.makedirs(os.path.join(base_dir, d), exist_ok=True)


def normalize_venue(venue: str) -> str:
    """Canonical venue name normalization — SINGLE SOURCE OF TRUTH.

    'CVPR 2025' → 'cvpr_2025'
    'NeurIPS 2024' → 'neurips_2024'
    """
    return re.sub(r'[^\w\-]', '_', venue.lower().strip())


def create_scout_dirs(base_dir: str = ".research", venue: str = ""):
    """DEPRECATED: Use create_scout_dirs_v2() instead.
    Kept for backward compatibility during migration."""
    safe_venue = normalize_venue(venue)
    abs_base = os.path.abspath(base_dir)
    scout_dir = os.path.join(abs_base, f"scout_{safe_venue}")
    os.makedirs(scout_dir, exist_ok=True)
    return scout_dir


def create_scout_dirs_v2(base_dir: str = ".research", venue: str = ""):
    """Create v2 scout directory structure: .research/scouts/{venue}/.

    Also creates .research/paper-cache/txt/ for global paper cache.
    Returns ABSOLUTE path to the scout directory.
    """
    safe_venue = normalize_venue(venue)
    abs_base = os.path.abspath(base_dir)
    scout_dir = os.path.join(abs_base, "scouts", safe_venue)
    os.makedirs(scout_dir, exist_ok=True)
    # Ensure global paper cache exists
    os.makedirs(os.path.join(abs_base, "paper-cache", "txt"), exist_ok=True)
    return scout_dir


def write_scout_metadata(scout_dir: str, venue: str, budget_hours: float,
                         scoring_model: str = "haiku"):
    """Write metadata.json to a scout directory."""
    metadata = {
        "schema_version": SCHEMA_VERSION_DATA,
        "pipeline_version": PIPELINE_VERSION,
        "venue": venue,
        "budget_hours": budget_hours,
        "scoring_model": scoring_model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    atomic_json_write(os.path.join(scout_dir, "metadata.json"), metadata)


# =============================================================================
# Migration: v1 → v2
# =============================================================================

def migrate_scout_v1_to_v2(base_dir: str = ".research"):
    """Migrate v1 scout data to v2 three-layer structure.

    v1: .research/scout_{venue}/
    v2: .research/scouts/{venue}/ + .research/paper-cache/

    Steps:
    1. Move TXT files from scout_{venue}/pdfs/ → paper-cache/txt/
    2. Build paper-cache/index.json
    3. Move data files with renames (raw-paper-list → raw-papers, etc.)
    4. Create symlink from old path to new path
    5. Update config.json (remove venue/budget, keep hardware only)
    6. Update state.json (absolute → relative paths)
    """
    abs_base = os.path.abspath(base_dir)

    # Ensure v2 directories exist
    paper_cache_dir = os.path.join(abs_base, "paper-cache")
    txt_cache_dir = os.path.join(paper_cache_dir, "txt")
    scouts_dir = os.path.join(abs_base, "scouts")
    os.makedirs(txt_cache_dir, exist_ok=True)
    os.makedirs(scouts_dir, exist_ok=True)

    # Initialize paper cache
    cache = PaperCache(paper_cache_dir)

    # Find all v1 scout directories
    migrated = []
    for entry in os.listdir(abs_base):
        if not entry.startswith("scout_"):
            continue
        old_dir = os.path.join(abs_base, entry)
        if not os.path.isdir(old_dir) or os.path.islink(old_dir):
            continue

        # Extract venue from directory name (scout_iclr_2026 → iclr_2026)
        venue_slug = entry[len("scout_"):]
        new_dir = os.path.join(scouts_dir, venue_slug)

        print(f"\n=== Migrating {entry} → scouts/{venue_slug} ===")

        # Create new directory
        os.makedirs(new_dir, exist_ok=True)

        # Step 1: Move TXT files to paper cache
        old_pdfs_dir = os.path.join(old_dir, "pdfs")
        if os.path.isdir(old_pdfs_dir):
            txt_files = [f for f in os.listdir(old_pdfs_dir) if f.endswith(".txt")]
            for txt_file in txt_files:
                src = os.path.join(old_pdfs_dir, txt_file)
                # For CVPR: paper_N.txt → need to map to paper_id
                # For ICLR: already paper_id.txt
                paper_id = txt_file.replace(".txt", "")

                # Try to find real paper_id from raw-paper-list.json
                # (CVPR uses paper_N naming, need to remap)
                if paper_id.startswith("paper_"):
                    # CVPR v1 naming: paper_0.txt, paper_1.txt, ...
                    # We need the raw paper list to map index → real ID
                    raw_path = os.path.join(old_dir, "raw-paper-list.json")
                    if os.path.exists(raw_path):
                        try:
                            papers = json.load(open(raw_path))
                            if isinstance(papers, list):
                                idx = int(paper_id.replace("paper_", ""))
                                if idx < len(papers):
                                    real_id = papers[idx].get("paper_id", papers[idx].get("forum", paper_id))
                                    if real_id:
                                        paper_id = real_id
                        except (json.JSONDecodeError, ValueError, IndexError):
                            pass

                # Copy to cache (PaperCache.store handles dedup)
                target_txt = os.path.join(txt_cache_dir, f"{paper_id}.txt")
                if not os.path.exists(target_txt):
                    shutil.copy2(src, target_txt)
                cache.store(paper_id, target_txt, {
                    "title": "",  # Will be enriched later if needed
                    "venues": [venue_slug.replace("_", " ").upper()],
                })
            print(f"  Moved {len(txt_files)} TXT files to paper-cache/txt/")

        # Step 2: Move/rename data files
        file_map = {
            "raw-paper-list.json": "raw-papers.json",
            "stage2-screened.json": "screened.json",
        }
        for old_name, new_name in file_map.items():
            old_path = os.path.join(old_dir, old_name)
            new_path = os.path.join(new_dir, new_name)
            if os.path.exists(old_path) and not os.path.exists(new_path):
                shutil.copy2(old_path, new_path)
                print(f"  {old_name} → {new_name}")

        # Copy scored-papers.json as v1 backup (schema incompatible, needs re-scoring)
        scored_old = os.path.join(old_dir, "scored-papers.json")
        if os.path.exists(scored_old):
            backup = os.path.join(new_dir, "scored.json.v1backup")
            if not os.path.exists(backup):
                shutil.copy2(scored_old, backup)
                print(f"  scored-papers.json → scored.json.v1backup (needs re-scoring)")

        # Copy scout report
        for f in os.listdir(old_dir):
            if f.startswith("scout-report-") and f.endswith(".md"):
                old_path = os.path.join(old_dir, f)
                new_path = os.path.join(new_dir, "report.md")
                if not os.path.exists(new_path):
                    shutil.copy2(old_path, new_path)
                    print(f"  {f} → report.md")

        # Step 3: Create empty new-schema files
        for fname in ["extractions.json", "scored.json", "feasibility.json"]:
            fpath = os.path.join(new_dir, fname)
            if not os.path.exists(fpath):
                atomic_json_write(fpath, [])

        # Step 4: Write metadata.json
        meta_path = os.path.join(new_dir, "metadata.json")
        if not os.path.exists(meta_path):
            venue_display = venue_slug.replace("_", " ").upper()
            # Try to read budget from old state
            budget = 8.0
            state_path = os.path.join(abs_base, "state.json")
            if os.path.exists(state_path):
                try:
                    state = json.load(open(state_path))
                    budget = state.get("budget_hours", 8.0)
                except (json.JSONDecodeError, OSError):
                    pass
            write_scout_metadata(new_dir, venue_display, budget)
            print(f"  Created metadata.json")

        # Step 5: Create symlink from old to new
        if os.path.exists(old_dir) and not os.path.islink(old_dir):
            # Rename old dir to .old, create symlink
            old_backup = old_dir + ".v1old"
            if not os.path.exists(old_backup):
                os.rename(old_dir, old_backup)
                os.symlink(new_dir, old_dir)
                print(f"  Symlink: {entry} → scouts/{venue_slug}")
            else:
                print(f"  SKIP symlink: {old_backup} already exists")

        migrated.append(venue_slug)

    # Save paper cache index
    cache.save()
    print(f"\nPaper cache: {cache.paper_count()} papers indexed")

    # Step 6: Update config.json (hardware-only)
    config_path = os.path.join(abs_base, "config.json")
    if os.path.exists(config_path):
        try:
            config = json.load(open(config_path))
            # Keep only hardware section, remove venue-specific fields
            new_config = {"hardware": config.get("hardware", {})}
            if "hardware" not in config and "gpu_name" in config:
                # Old format: flat config
                new_config = {"hardware": {
                    k: v for k, v in config.items()
                    if k in ("gpu_name", "gpu_count", "vram_gb", "cuda_version",
                             "pytorch_cuda", "cudnn_version", "disk_free_gb")
                }}
            atomic_json_write(config_path, new_config)
            print(f"\nconfig.json updated: hardware-only (removed venue/budget fields)")
        except (json.JSONDecodeError, OSError) as e:
            print(f"\nWARN: Could not update config.json: {e}")

    # Step 7: Update state.json paths to relative
    state_path = os.path.join(abs_base, "state.json")
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path))
            sm = StateManager(abs_base)
            # Convert absolute paths to relative
            for key in ("scout_dir", "paper_cache_dir"):
                val = state.get(key, "")
                if val and os.path.isabs(val):
                    state[key] = sm.make_relative(val)
            # Update scout_dir to new location if it's old-style
            if "scout_dir" in state:
                old_val = state["scout_dir"]
                # scout_iclr_2026 → scouts/iclr_2026
                if old_val.startswith("scout_") or "/scout_" in old_val:
                    venue_slug = old_val.split("scout_")[-1].rstrip("/")
                    state["scout_dir"] = f"scouts/{venue_slug}"
            if "paper_cache_dir" not in state:
                state["paper_cache_dir"] = "paper-cache"
            sm.save(state)
            print(f"state.json updated: relative paths")
        except (json.JSONDecodeError, OSError) as e:
            print(f"\nWARN: Could not update state.json: {e}")

    # Create .gitignore
    gitignore_path = os.path.join(abs_base, ".gitignore")
    gitignore_content = """# Research pipeline data (large files)
paper-cache/txt/
scouts/*/raw-papers.json
"""
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, "w") as f:
            f.write(gitignore_content)
        print(f"\nCreated .gitignore")

    print(f"\n✅ Migration complete: {len(migrated)} scout(s) migrated")
    if migrated:
        print(f"   Venues: {', '.join(migrated)}")
    print(f"   Paper cache: {cache.paper_count()} papers")
    print(f"\n   Next: Re-run scoring with '/research --scout \"VENUE\" --budget=Nh'")
    return migrated


# =============================================================================
# CLI Entry Point (for testing)
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: research_utils.py <command> [args...]")
        print("Commands:")
        print("  init <topic> [depth]    — Initialize .research/")
        print("  init_scout <venue> <budget_hours> — Initialize scout mode (v2)")
        print("  scout_dir <venue>       — Print absolute path of scout data directory")
        print("  paper_cache_dir         — Print absolute path of paper cache directory")
        print("  research_dir            — Print absolute path of .research/ directory")
        print("  status                  — Show current state")
        print("  hardware                — Detect GPU hardware")
        print("  check-jobs              — Check training job status")
        print("  migrate                 — Migrate v1 scout data to v2 structure")
        print("  verify_feasibility <json> [gpu] [vram] [hours] — Mechanical feasibility check")
        print("  add_gpu_hours <research_dir> <hours> — Increment GPU hours used")
        print("  log <research_dir> <event_type> <message> [--phase X] [--worker Y] — Log pipeline event")
        print("  report [research_dir]   — Generate execution-report.md from pipeline-events.jsonl")
        print("  task_poll <exp_dir>     — Check background task status (PID/flags/log tail)")
        print("  regress <research_dir> <target> '<json>'   — Regress to phase3 (big loop) or phase4 (medium loop)")
        print("  phase_dir <phase_name> [cycle]             — Return phase dir name with cycle suffix")
        print("  track_status <research_dir>               — Print track stats (tested counts, current track)")
        print("  track_switch <research_dir> <EXPLOIT|EXPLORE> — Record track switch in state")
        print("  track_tested <research_dir> <EXPLOIT|EXPLORE> — Increment tested count for track")
        print("  tournament_init <research_dir> <budget_file>  — Init tournament from budget file")
        print("  tournament_score <research_dir> <round> <hyp_id> '<json>' — Record round scores")
        print("  tournament_eliminate <research_dir> <round> '<elim_json>' '<adv_json>' — Execute elimination")
        print("  tournament_complete <research_dir> <champion_id> <score> — Mark tournament complete")
        print("  tournament_fail <research_dir> <reason>     — Mark tournament failed")
        print("  tournament_status <research_dir>            — Print tournament status")
        print("  knowledge summary                          — Show knowledge base stats")
        print("  knowledge query [--type=X] [--domain=X] [--keyword=X] — Query knowledge")
        print("  knowledge read <entry_id>                  — Read entry content")
        print("  knowledge constraints [domain]             — List constraints")
        print("  knowledge migrate [project_dir]            — Backfill KB from existing projects")
        print("  knowledge knowledge_dir                    — Print KB directory path")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "init":
        topic = sys.argv[2] if len(sys.argv) > 2 else "test-topic"
        depth = sys.argv[3] if len(sys.argv) > 3 else "full"
        create_research_dirs()
        sm = StateManager()
        state = sm.init(topic, depth)
        print(f"Initialized .research/ for topic: {topic}")
        print(json.dumps(state, indent=2))

    elif cmd == "init_scout":
        venue = sys.argv[2] if len(sys.argv) > 2 else "CVPR 2025"
        budget = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
        sm = StateManager()
        state = sm.init_scout(venue, budget)
        # Resolve for display
        scout_abs = sm.resolve_path(state["scout_dir"])
        cache_abs = sm.resolve_path(state["paper_cache_dir"])
        print(f"Initialized scout mode for venue: {venue}, budget: {budget}h")
        print(f"SCOUT_DIR={scout_abs}")
        print(f"PAPER_CACHE={cache_abs}")
        print(f"RESEARCH_DIR={os.path.abspath('.research')}")

    elif cmd == "scout_dir":
        # Print the absolute scout directory path for a venue (v2: scouts/{venue})
        venue = sys.argv[2] if len(sys.argv) > 2 else "CVPR 2025"
        safe_venue = normalize_venue(venue)
        # Try v2 path first, fall back to v1
        v2_path = os.path.abspath(os.path.join(".research", "scouts", safe_venue))
        v1_path = os.path.abspath(os.path.join(".research", f"scout_{safe_venue}"))
        if os.path.exists(v2_path):
            print(v2_path)
        elif os.path.exists(v1_path):
            print(v1_path)
        else:
            print(v2_path)  # Default to v2 for new scouts

    elif cmd == "paper_cache_dir":
        print(os.path.abspath(os.path.join(".research", "paper-cache")))

    elif cmd == "research_dir":
        # Print the absolute .research/ directory path (no side effects)
        print(os.path.abspath(".research"))

    elif cmd == "status":
        sm = StateManager()
        if sm.exists():
            state = sm.load()
            mode = state.get("mode", "research")
            if mode == "scout":
                print(f"Mode: Scout")
                print(f"Venue: {state.get('venue')}")
                print(f"Budget: {state.get('budget_hours')}h")
                scout = state.get("phases", {}).get("phase0_scout", {})
                print(f"Stage: {scout.get('stage', 'pending')}")
                print(f"Papers: {scout.get('papers_total', 0)} total, "
                      f"{scout.get('papers_screened', 0)} screened, "
                      f"{scout.get('papers_scored', 0)} scored")
            else:
                print(f"Topic: {state.get('topic')}")
                print(f"Phase: {state.get('current_phase')}")
                print(f"Depth: {state.get('depth')}")
                for phase, info in state.get("phases", {}).items():
                    status = info.get("status", "unknown")
                    marker = "✓" if status == "completed" else "●" if status == "in_progress" else "○"
                    print(f"  {marker} {phase}: {status}")
        else:
            print("No .research/state.json found")

    elif cmd == "hardware":
        hw = detect_hardware()
        print(json.dumps(hw, indent=2))

    elif cmd == "check-jobs":
        sm = StateManager()
        if sm.exists():
            state = sm.load()
            for job in state.get("training_jobs", []):
                result = ProcessManager.check_training_job(job)
                print(f"{result['name']}: {result['status']}")
                if "log_tail" in result:
                    print(f"  Last log: {result['log_tail'][:200]}")
        else:
            print("No .research/state.json found")

    elif cmd == "verify_feasibility":
        if len(sys.argv) < 3:
            print("Usage: research_utils.py verify_feasibility <papers.json> [target_gpu] [target_vram] [budget_hours]")
            sys.exit(1)
        papers_path = sys.argv[2]
        target_gpu = sys.argv[3] if len(sys.argv) > 3 else "RTX_4090"
        target_vram = float(sys.argv[4]) if len(sys.argv) > 4 else 24.0
        budget = float(sys.argv[5]) if len(sys.argv) > 5 else 8.0

        with open(papers_path) as f:
            papers = json.load(f)
        print(f"Verifying {len(papers)} papers: target={target_gpu} ({target_vram}GB), budget={budget}h")
        print()

        updated, flagged = verify_papers_batch(papers, target_gpu, target_vram, budget)

        # Summary stats
        verdicts = {}
        for p in updated:
            v = p.get("mechanical_verdict", "unknown")
            verdicts[v] = verdicts.get(v, 0) + 1
        disagree_count = sum(1 for p in updated if p.get("verdict_disagrees"))

        print(f"Mechanical verdicts: {json.dumps(verdicts)}")
        print(f"Disagrees with LLM: {disagree_count}")
        print(f"Flagged papers: {len(flagged)}")
        print()

        if flagged:
            print("=== FLAGGED PAPERS ===")
            for p in flagged:
                print(f"  [{p.get('mechanical_verdict')}] {p.get('title', p.get('paper_id', '?'))}")
                print(f"    LLM: {p.get('feasibility_verdict')} | Disagrees: {p.get('verdict_disagrees')}")
                print(f"    Flags: {p.get('mechanical_flags')}")
                print(f"    Reasoning: {p.get('mechanical_reasoning')}")
                print()

        # Write updated papers back
        atomic_json_write(papers_path, updated)
        print(f"Updated {papers_path} with mechanical fields.")

    elif cmd == "log":
        if len(sys.argv) < 4:
            print("Usage: research_utils.py log <research_dir> <event_type> <message> [--phase X] [--worker Y]")
            sys.exit(1)
        rd = sys.argv[2]
        etype = sys.argv[3]
        msg = sys.argv[4] if len(sys.argv) > 4 else ""
        # Parse optional flags
        phase = None
        worker = None
        i = 5
        while i < len(sys.argv):
            if sys.argv[i] == "--phase" and i + 1 < len(sys.argv):
                phase = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--worker" and i + 1 < len(sys.argv):
                worker = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        logger = PipelineLogger(rd)
        logger.log(etype, msg, phase=phase, worker=worker)
        print(f"Logged: {etype} — {msg}")

    elif cmd == "report":
        rd = sys.argv[2] if len(sys.argv) > 2 else ".research"
        rd = os.path.abspath(rd)
        path = generate_execution_report(rd)
        print(f"Generated: {path}")

    elif cmd == "add_gpu_hours":
        if len(sys.argv) < 4:
            print("Usage: research_utils.py add_gpu_hours <research_dir> <hours>")
            sys.exit(1)
        research_dir = sys.argv[2]
        hours = float(sys.argv[3])
        sm = StateManager(research_dir)
        state = sm.load()
        sm.add_gpu_hours(state, hours)
        print(f'Added {hours:.2f} GPU-hours. Total: {state["budget"]["gpu_hours_used"]:.2f}h')

    elif cmd == "task_poll":
        # Quick status check for a background task (used by polling alarms)
        # Checks PID, .done/.failed flags, prints status + log tail
        if len(sys.argv) < 3:
            print("Usage: research_utils.py task_poll <exp_dir>")
            sys.exit(1)
        exp_dir = os.path.abspath(sys.argv[2])
        pid_file = os.path.join(exp_dir, "pid")
        log_file = os.path.join(exp_dir, "training.log")
        done_flag = os.path.join(exp_dir, ".done")
        fail_flag = os.path.join(exp_dir, ".failed")

        def _print_log_tail(path, n=10):
            if os.path.exists(path):
                with open(path) as f:
                    lines = f.readlines()
                print("LOG_TAIL:")
                for line in lines[-n:]:
                    print(f"  {line.rstrip()}")

        if os.path.exists(done_flag):
            print("TASK_STATUS: completed")
            _print_log_tail(log_file, 10)
            sys.exit(0)

        if os.path.exists(fail_flag):
            print("TASK_STATUS: failed")
            try:
                with open(fail_flag) as f:
                    print(f"ERROR: {f.read().strip()[:500]}")
            except Exception:
                pass
            _print_log_tail(log_file, 20)
            sys.exit(1)

        # Check PID
        pid = ProcessManager.read_pid(pid_file) if os.path.exists(pid_file) else None
        if pid and ProcessManager.is_alive(pid):
            print(f"TASK_STATUS: running")
            print(f"PID: {pid}")
            try:
                started = os.path.getmtime(pid_file)
                elapsed_h = (time.time() - started) / 3600
                print(f"ELAPSED: {elapsed_h:.1f}h")
            except Exception:
                pass
            _print_log_tail(log_file, 5)
            sys.exit(0)
        else:
            print("TASK_STATUS: crashed")
            print(f"PID: {pid or 'unknown'} (dead, no .done/.failed)")
            _print_log_tail(log_file, 20)
            sys.exit(1)

    elif cmd == "regress":
        # regress <research_dir> <target> '<json>'
        # target = phase3 | phase4
        if len(sys.argv) < 5:
            print("Usage: research_utils.py regress <research_dir> <target> '<json>'")
            print("  target: phase3 (big loop) or phase4 (medium loop)")
            sys.exit(1)
        research_dir = sys.argv[2]
        target = sys.argv[3]
        payload = json.loads(sys.argv[4])
        sm = StateManager(research_dir)
        state = sm.load()
        if target == "phase3":
            state = sm.regress_to_phase3(state, payload)
            print(f"Big loop: cycle → {state['iteration']['cycle']}")
            print(f"Current phase: {state['current_phase']}")
            print(f"History entries: {len(state['iteration']['history'])}")
        elif target == "phase4":
            state = sm.regress_to_phase4(state, payload.get("diagnosis_notes", ""))
            print(f"Medium loop: staying at cycle {state['iteration']['cycle']}")
            print(f"Current phase: {state['current_phase']}")
        else:
            print(f"Unknown target: {target}. Use 'phase3' or 'phase4'.")
            sys.exit(1)

    elif cmd == "phase_dir":
        # phase_dir <phase_name> [cycle]
        if len(sys.argv) < 3:
            print("Usage: research_utils.py phase_dir <phase_name> [cycle]")
            sys.exit(1)
        phase_name = sys.argv[2]
        cycle = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print(StateManager.phase_dir(phase_name, cycle))

    elif cmd == "migrate":
        base = sys.argv[2] if len(sys.argv) > 2 else ".research"
        migrate_scout_v1_to_v2(base)

    elif cmd == "track_status":
        # track_status <research_dir> — print track stats
        rd = sys.argv[2] if len(sys.argv) > 2 else ".research"
        sm = StateManager(rd)
        state = sm.load()
        ts = sm.track_status(state)
        print(json.dumps(ts, indent=2))

    elif cmd == "track_switch":
        # track_switch <research_dir> <new_track> — record track switch
        if len(sys.argv) < 4:
            print("Usage: research_utils.py track_switch <research_dir> <EXPLOIT|EXPLORE>")
            sys.exit(1)
        rd = sys.argv[2]
        new_track = sys.argv[3].upper()
        sm = StateManager(rd)
        state = sm.load()
        state = sm.track_switch(state, new_track)
        ts = sm.track_status(state)
        print(f"Track switched to {new_track}")
        print(json.dumps(ts, indent=2))

    elif cmd == "track_tested":
        # track_tested <research_dir> <track> — increment tested count
        if len(sys.argv) < 4:
            print("Usage: research_utils.py track_tested <research_dir> <EXPLOIT|EXPLORE>")
            sys.exit(1)
        rd = sys.argv[2]
        track = sys.argv[3].upper()
        sm = StateManager(rd)
        state = sm.load()
        state = sm.track_tested(state, track)
        ts = sm.track_status(state)
        print(f"Incremented {track} tested count")
        print(json.dumps(ts, indent=2))

    elif cmd == "tournament_init":
        # tournament_init <research_dir> <budget_file>
        if len(sys.argv) < 4:
            print("Usage: research_utils.py tournament_init <research_dir> <budget_file>")
            sys.exit(1)
        rd = sys.argv[2]
        budget_file = sys.argv[3]
        sm = StateManager(rd)
        state = sm.load()
        state = sm.tournament_init(state, budget_file)
        print(f"Tournament initialized: {state['iteration']['tournament']['hypotheses_entered']} hypotheses")
        print(sm.tournament_status(state))

    elif cmd == "tournament_score":
        # tournament_score <research_dir> <round> <hyp_id> '<json>'
        if len(sys.argv) < 6:
            print("Usage: research_utils.py tournament_score <research_dir> <round> <hyp_id> '<json>'")
            sys.exit(1)
        rd = sys.argv[2]
        round_num = int(sys.argv[3])
        hyp_id = sys.argv[4]
        scores = json.loads(sys.argv[5])
        sm = StateManager(rd)
        state = sm.load()
        state = sm.tournament_record_score(state, round_num, hyp_id, scores)
        print(f"Recorded scores for {hyp_id} in round {round_num}: total={scores.get('total', '?')}")

    elif cmd == "tournament_eliminate":
        # tournament_eliminate <research_dir> <round> '<elim_json>' '<adv_json>'
        if len(sys.argv) < 6:
            print("Usage: research_utils.py tournament_eliminate <research_dir> <round> '<elim_json>' '<adv_json>'")
            sys.exit(1)
        rd = sys.argv[2]
        round_num = int(sys.argv[3])
        eliminated = json.loads(sys.argv[4])
        advanced = json.loads(sys.argv[5])
        sm = StateManager(rd)
        state = sm.load()
        state = sm.tournament_eliminate(state, round_num, eliminated, advanced)
        print(f"Round {round_num}: eliminated {eliminated}, advanced {advanced}")

    elif cmd == "tournament_complete":
        # tournament_complete <research_dir> <champion_id> <score>
        if len(sys.argv) < 5:
            print("Usage: research_utils.py tournament_complete <research_dir> <champion_id> <score>")
            sys.exit(1)
        rd = sys.argv[2]
        champion_id = sys.argv[3]
        score = float(sys.argv[4])
        sm = StateManager(rd)
        state = sm.load()
        state = sm.tournament_complete(state, champion_id, score)
        print(f"Tournament completed. Champion: {champion_id} (score: {score})")

    elif cmd == "tournament_fail":
        # tournament_fail <research_dir> <reason>
        if len(sys.argv) < 4:
            print("Usage: research_utils.py tournament_fail <research_dir> <reason>")
            sys.exit(1)
        rd = sys.argv[2]
        reason = sys.argv[3]
        sm = StateManager(rd)
        state = sm.load()
        state = sm.tournament_fail(state, reason)
        print(f"Tournament failed: {reason}")

    elif cmd == "tournament_status":
        # tournament_status <research_dir>
        rd = sys.argv[2] if len(sys.argv) > 2 else ".research"
        sm = StateManager(rd)
        state = sm.load()
        print(sm.tournament_status(state))

    elif cmd == "knowledge":
        subcmd = sys.argv[2] if len(sys.argv) > 2 else "summary"
        ws = _find_workspace_research_standalone()
        kb = KnowledgeBase(os.path.join(ws, "knowledge"))

        if subcmd == "summary":
            print(json.dumps(kb.summary(), indent=2))
        elif subcmd == "query":
            kwargs = {}
            for arg in sys.argv[3:]:
                k, _, v = arg.lstrip('-').partition('=')
                if k == "type":
                    kwargs["entry_type"] = v
                elif k == "domain":
                    kwargs["domain"] = v
                elif k == "keyword":
                    kwargs["keyword"] = v
            for r in kb.query(**kwargs):
                print(f"  {r['id']}: {r['title']} [{r.get('domain', '')}]")
        elif subcmd == "read":
            if len(sys.argv) < 4:
                print("Usage: research_utils.py knowledge read <entry_id>")
                sys.exit(1)
            print(kb.read_entry(sys.argv[3]) or "Not found")
        elif subcmd == "constraints":
            domain = sys.argv[3] if len(sys.argv) > 3 else None
            cs = kb.get_constraints_for_domain(domain) if domain else kb.query(entry_type="constraint")
            for c in cs:
                print(f"  [{c.get('severity', '?').upper()}] {c['id']}: {c['title']}")
        elif subcmd == "migrate":
            migrate_knowledge(sys.argv[3] if len(sys.argv) > 3 else None)
        elif subcmd == "knowledge_dir":
            print(os.path.join(ws, "knowledge"))
        else:
            print(f"Unknown knowledge subcommand: {subcmd}")
            print("Subcommands: summary, query, read, constraints, migrate, knowledge_dir")
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
