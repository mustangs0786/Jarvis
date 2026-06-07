# Engine Audit — apply_engine.py / auto_agent.py

**Date:** 2026-06-04
**Scope:** Deep end-to-end review after the country-code toggle/Afghanistan fixes. Looked for bugs that could have slipped in, plus pre-existing latent issues touching the same paths.

> TL;DR: One **HIGH** regression (cache split — silently disables the degree→Master's mapping you built), one **MEDIUM** design fragility (dropdown cache keys are unstable), and a handful of **LOW** robustness notes. Nothing else structurally wrong — the recent toggle/select fixes hold up under tracing.

---

## 🔁 ROUND 2 — deeper 360 (2026-06-04, later)

Re-audited everything incl. my own fresh changes. Two real findings, both fixed:

### 🔴 Bug D — `_handle_select` was reinventing selection on a FALSE premise (FIXED)
My previous rewrite removed `Enter` and **polled for a visually-filtered option list**. But the proven `base.py click_option` (used by `execute_action`, developed against the live tenant) explicitly documents:
> *"Workday's country/state dropdowns do NOT filter visually as you type — Enter snaps to the matching option."* ([apply_skills/base.py:341-345](d:/Projects/Resume_Builder/apply_skills/base.py#L341))

So my poll would never see a filtered list → ~3.6s wasted every time → fall through to the slow fallback. And the **Afghanistan mis-pick was simply Enter pressed with no debounce wait** (base.py waits 450ms, *then* Enter snaps correctly) — not "Enter is wrong."
**Fix:** rewrote `_handle_select` to keep only *resolution* (cache → direct value → scrape+strong/LLM/decline/hold) and delegate the **actual selection to the proven `execute_action`**. Order: (1) cache hit → apply exact option; (2) deterministic value → try directly (Enter-snaps country/state, guarded against a semantic value mis-snapping); (3) else scrape + `_resolve_choice_core` → apply. Commit is verified chip-aware (`_selected_chip` OR base.py's own verify) to cover the sibling-chip case. `_type_into_search`/poll/`_click_popup_option`-as-primary removed from this path.

### 🟠 Bug E — `_migrate_cache` corrupted stems for SHORT options (FIXED)
Stripping the stored option out of the label is right for long options ("Master's…"), but for `"No"`/`"Yes"` it mangled real words (`"notice"` → `"tice"`). It only produced harmless junk keys (no wrong hits), but fixed anyway: **only strip an option when `len(deaccent(opt)) >= 5` and it actually appears in the label.** Verified: `degree|mtech`, `phone device type|mobile` correct; the Amgen "No" question stem stays intact.

### ⚠️ Residual risk to RE-TEST live (not a code bug)
`base.py click_option` verifies a commit by reading `el.inner_text()` / inner `<input>.value`. For an **input-based** typeahead whose selected chip lives in a *sibling*, that read can be empty even on success, so base.py may return `False` and retry across its search terms (wasteful, possibly flaky). My `_apply` mitigates the **return value** by OR-ing in `_selected_chip`, but base.py's internal multi-term retry still runs. **Action:** on the next live run, watch the country-code line — expect `[cached]`/`[direct]` and a single clean `India (+91)`. If it thrashes through terms, we should teach base.py's verify to read the sibling chip too.

---

## ✅ STATUS: ALL FIXED (applied 2026-06-04)

| # | Issue | Fix applied |
|---|-------|-------------|
| **A** | cache read only one dict | `_cache_get` now reads BOTH `_resolved` + `_dropdown_resolved`, and tries raw **and** normalized keys. Verified: degree entry now visible. |
| **C** | dropdown cache keys unstable | Added `_key_stem`/`_ck_norm` (strip "Select One"/"Required"/`*`/accents) + dual-write. Plus a one-time idempotent `_migrate_cache` that re-keys legacy entries under the stem (it strips the stored option out of the label). **Verified: `degree|mtech` → Master's option hits on run 1**, country works with/without `*`, state/phone-device get stable keys. Runs at `converge_page` start, persists. |
| **L1** | `execute_action` `creds["email"]` KeyError risk | now `creds.get("email","")` / `.get("password","")`. |
| **L2** | guard let ALL selects re-resolve | restricted to `widget == "typeahead"`; native `<select>` still skips when filled. |
| **L4** | loose substring match | `t in want` now requires `len(t) >= 4`. |
| L3/L5/L6 | held re-open / broad "Apply" / label text | reviewed, intentionally left (correct/safe behavior). |

All changes verified: both files `ast.parse` clean; cache lookups tested against the real `user_profiles/1/profile.json`. The detailed write-up of each issue is preserved below for reference.

---

---

## HIGH — Bug A: resolution cache only reads ONE of the two cache dicts

**Where:** [apply_engine.py:91-93](d:/Projects/Resume_Builder/apply_engine.py#L91-L93)

```python
def _cache_get(profile, label, val):
    cache = profile.get("_resolved") or profile.get("_dropdown_resolved") or {}
    return cache.get(_ck(label, val))
```

**Problem:** `A or B` returns `A` as soon as `_resolved` is non-empty. The profile now has entries in **both** dicts:
- `_resolved` (5 keys) — country code, state, "worked at Amgen", phone device type, country.
- `_dropdown_resolved` (1 key) — **the `MTech → "Master's / Graduate Degree (…)"` mapping** (the semantic match you specifically cared about).

Because `_resolved` is non-empty, `_dropdown_resolved` is **never consulted**, so the degree mapping is invisible. The degree dropdown will miss cache → fall to LLM/hold on every run.

**Repro (confirmed):**
```
degree lookup (current code): None      # should be the Master's option
degree lookup (fixed)       : Y
```

**Proposed fix (one function, safe):**
```python
def _cache_get(profile, label, val):
    key = _ck(label, val)
    for d in (profile.get("_resolved"), profile.get("_dropdown_resolved")):
        if d and key in d:
            return d[key]
    return None
```
(Writes still go to `_resolved`; this only makes the legacy dict readable. Alternatively, one-time migrate `_dropdown_resolved` into `_resolved` and drop it.)

---

## MEDIUM — Bug C: dropdown cache keys are unstable (label includes the displayed value)

**Where:** key built by `_ck(label, value)` at [apply_engine.py:88-89](d:/Projects/Resume_Builder/apply_engine.py#L88-L89); labels come from `_COLLECT_JS` value/innerText extraction in [auto_agent.py](d:/Projects/Resume_Builder/auto_agent.py).

**Evidence:** the cached degree key is
`"degree master's / graduate degree (5-6 year…) required|mtech"`
— the **label already contains the selected option text**. On a fresh page where the same dropdown shows "Select One", the collected label would be e.g. `"degree select one required"`, producing a **different key** → cache miss even after Bug A is fixed.

By contrast the country-code key is clean (`"country / territory phone code*|india +91"`) because that widget exposes a real `aria-label`. So cache reliability depends on whether a given widget has a clean accessible label vs. falling back to innerText (which includes the current value).

**Impact:** the degree dropdown (and any prompt whose label falls back to innerText) may re-resolve / re-hold on every fresh run instead of hitting cache — i.e., the "confirm once, deterministic forever" promise breaks for those fields.

**Proposed direction (needs a decision, not a one-liner):**
- Normalize the label used for cache keys: strip a trailing selected-value/"select one"/"required" tail, OR
- Key the dropdown cache on something stable — e.g. `(section_label, profile-value)` or a normalized "field stem" — rather than the raw rendered label.
- Lowest-risk interim: when caching, store under BOTH the raw label and a normalized stem; look up by stem first.

**To verify quickly:** on My Experience/Education, watch whether the degree dropdown logs `[cached]` or re-prints a `[CONFIRM NEEDED]` block after Bug A is fixed. If it still re-holds, it's Bug C.

---

## LOW — robustness notes (review, likely fine for now)

### L1. `execute_action` does `creds["email"]` unconditionally
[auto_agent.py:813-814](d:/Projects/Resume_Builder/auto_agent.py#L813-L814)
```python
if isinstance(val, str):
    val = val.replace(EMAIL_TOKEN, creds["email"]).replace(PASS_TOKEN, creds["password"])
```
`_delete_phantom_rows` calls this with `creds={}` ([apply_engine.py:840-841](d:/Projects/Resume_Builder/apply_engine.py#L840-L841)). Currently **safe** only because that action has no string `value` (the `isinstance` guard skips the line). If a valued action is ever sent with `creds={}` → `KeyError`. **Fix:** `creds.get("email","")` / `creds.get("password","")`.

### L2. Already-filled guard now lets ALL selects through (incl. native `<select>`)
[apply_engine.py:1010-1012](d:/Projects/Resume_Builder/apply_engine.py#L1010-L1012) — I excluded `select` so a previously **mis-picked** dropdown (e.g. leftover `Afghanistan`) can be corrected. Within a run the per-run memo prevents re-opening; across fresh runs the value-aware pre-check skips a matching chip. Edge case: a native `<select>` (rare in Workday) whose `el.value` is a code (not the visible text) may not match the profile value in the pre-check → one wasteful re-select per fresh run (re-picks the same correct value, so not *wrong*). **Optional tighten:** restrict the exclusion to `widget == "typeahead"` rather than all selects.

### L3. Held dropdowns re-open every pass within a run
A `select` that resolves to "held" isn't memoized (correctly — so it reappears in the final held summary), so each convergence pass re-opens it and re-prints the `[CONFIRM NEEDED]` block. Noisy but not harmful; converges to `max_attempts` then stops. Only relevant for genuinely ambiguous dropdowns awaiting user input.

### L4. `_click_popup_option` uses a loose substring match
[apply_engine.py:521](d:/Projects/Resume_Builder/apply_engine.py#L521): `t == want or want in t or t in want`. The `t in want` arm means a very short option text that is a substring of the target could match the wrong row. In practice the resolver passes the exact option text and the `==` arm wins first, so low risk — but `t in want` could be dropped or length-guarded.

### L5. `advance()` gateway tier matches bare "Apply"
[apply_engine.py:762-803](d:/Projects/Resume_Builder/apply_engine.py#L762-L803): `_GATEWAY_TEXTS` includes `"Apply"`. Only reached when no NEXT/SUBMIT/AUTH button exists, and `_GATEWAY_BAD` excludes autofill/linkedin/last/resume. On data pages the NEXT automation-id matches first, so this won't fire there. Low risk; flagged only because "Apply" is broad if a data page ever lacks the standard NEXT id.

### L6. `_field_display_text` can return the field's label text for an empty dropdown
[apply_engine.py:577-598](d:/Projects/Resume_Builder/apply_engine.py) — when a prompt is empty, container `innerText` may be the label. Handled safely (the value-word match won't hit the label, and the wrong-chip removal is gated on `_selected_chip` being truthy), so no false skip/remove. Noted for awareness only.

---

## Verified OK (traced, no issue found)

- **Toggle fix holds:** correctly-filled dropdown is skipped on later passes via (a) per-run memo ([apply_engine.py:995-1001](d:/Projects/Resume_Builder/apply_engine.py#L995-L1001)) and (b) value-aware pre-check ([apply_engine.py:893-907](d:/Projects/Resume_Builder/apply_engine.py#L893-L907)). No re-open path remains for a matching chip.
- **Afghanistan fix:** Enter removed; poll-for-filter then click-exact ([apply_engine.py:921-934](d:/Projects/Resume_Builder/apply_engine.py#L921-L934)); chip-aware success verify ([apply_engine.py:951-958](d:/Projects/Resume_Builder/apply_engine.py)); wrong-chip removal before re-pick.
- **`_COLLECT_JS` ordering fix:** widget detection now precedes chip extraction ([auto_agent.py:175-205](d:/Projects/Resume_Builder/auto_agent.py#L175-L205)); the `tag==='input' && widget==='typeahead'` chip block is no longer dead code.
- **Soft alerts:** capitalization "Alert-…" warnings are filtered by `scan_page_errors` (`kind=='alert'`) ([apply_engine.py:744-749](d:/Projects/Resume_Builder/apply_engine.py#L744-L749)) — they won't block advancement or trigger correction loops.
- **Radio memo key** uses the group question `q`, consistent between the skip-check and the add, so all members of an answered group skip on later passes.
- **Sign-out guard** in `execute_action` ([auto_agent.py:823-825](d:/Projects/Resume_Builder/auto_agent.py#L823-L825)) still protects against destroying the session.
- **Sensitive/EEO decline rule**, **password same-value fill+blur**, **phantom-row delete by section_label**, **free-text always-held** — all intact on their paths.
- Both files parse (`ast.parse`).

---

## Suggested order of fixes (when you're ready)

1. **Bug A** — 5-line `_cache_get` change. Restores the degree mapping immediately. Safe.
2. **Bug C** — decide on a label-normalization / stable-key strategy; this is what makes dropdown caching durable across runs.
3. **L1** — trivial `.get()` hardening.
4. L2/L4 — optional tightening.

I can apply Bug A + L1 right away if you want a quick win before tomorrow — they're low-risk one-liners. Bug C deserves a short design decision first.
