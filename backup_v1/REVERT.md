# v1 Backup — Revert Guide

Backed up: 2026-06-14  
Reason: Starting v2 rewrite — replacing the screenshot-index form-fill core in
`apply_engine.py` with the HTML-to-LLM approach already proven in
`linkedin_easy_apply.py`.

---

## What is in this folder

| File | Why backed up |
|------|---------------|
| `apply_engine.py` | Primary target of v2 rewrite. The old form-fill core: `_fill_pass`, `_fill_one`, `classify_field`, `_resolve_choice_core`, `_handle_select`. 2105 lines. |
| `external_apply.py` | Secondary engine (skill-based). 747 lines. Partially used by orchestrator. |
| `auto_agent.py` | Shared helpers. Some (`annotate_screenshot`, `collect_elements`) may become obsolete in v2. 892 lines. |
| `apply_vision.py` | Screenshot-based audit/recovery layer. May simplify in v2. 262 lines. |
| `apply_orchestrator.py` | Routing layer. Backed up in case routing changes during v2. 560 lines. |
| `workday.py` | Deterministic Workday prefill. NOT changed in v2 — backed up for completeness. |
| `apply_skills/` | Low-level Playwright dispatcher. NOT changed in v2 — backed up for completeness. |

**Not backed up (unchanged in v2):**
- `linkedin_easy_apply.py` — reference implementation, the model for v2
- `app.py`, `apply_llm.py`, `profile_manager.py`, `apply_handler.py` — unchanged

---

## How to fully revert to v1

Run these commands from the repo root (`/Users/sakshi/Documents/GitHub/Jarvis`):

```bash
cp backup_v1/apply_engine.py .
cp backup_v1/external_apply.py .
cp backup_v1/auto_agent.py .
cp backup_v1/apply_vision.py .
cp backup_v1/apply_orchestrator.py .
cp backup_v1/workday.py .
cp -r backup_v1/apply_skills ./apply_skills
```

Then restart the server:
```bash
# local
uv run uvicorn app:app --host 0.0.0.0 --port 8000

# on Azure VM (ssh in first)
sudo systemctl restart resume-apply
```

---

## What v2 changes (for context when reverting)

**v1 external form-fill flow (replaced):**
```
screenshot → red-box annotation → LLM returns index N →
  [data-agent-idx="N"] → classify_field() 15-branch heuristic →
  4-tier value resolution → multi-strategy dropdown → error scan → retry
```

**v2 external form-fill flow (new):**
```
scope to <form>/<main> → compact HTML → LLM returns CSS-selector actions →
  _dispatch_action() executor (same as linkedin_easy_apply.py) → advance → retry
```

**What stays the same in v2:**
- `converge_page()` outer loop (fill → advance → error scan → retry) — structure unchanged
- `gateway_advance()` — screenshot+index for landing pages only, not forms
- `workday_prefill()` / `workday_fill_dropdowns()` — deterministic by `data-automation-id`
- `apply_orchestrator.py` routing logic
- `apply_skills/base.py` low-level dispatcher

---

## v1 known bugs (fixed before backup)

- Gateway clicks blocked by off-site link guard (`apply_skills/base.py` lines 185-190).
  Fix: `gateway=True` flag passed through `execute_action` → `dispatch_action`.
  This fix IS present in this backup.
