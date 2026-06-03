---
name: fact-verification
description: Dedicated source-authenticity + evidence + logical-fallacy + fact-check verification layer for narrative outputs, with hallucination safeguards. Hybrid (internal evidence-chain rigor + external web cross-check). Use to verify any narrative artifact (W3 insight, W4 Master, DCI, Public Narrative, Newspaper) before it is treated as trustworthy. Triggers — "팩트체크", "출처 검증", "환각 검증", "fact-check this report", "verify claims", "source verification".
---

# Fact-Verification — Hallucination Safeguard for Narrative Outputs

> Born from ABSOLUTE ANCHOR ②a: *where fact-checking is essential, a dedicated
> sub-skill must secure verification rigor, evaluation reliability, and
> hallucination safeguards.* This skill is that safeguard. **Garbage-in is
> blocked here** — a contaminated foundation only makes falsehood more polished.

## WHY this skill exists

The pipeline produces narrative artifacts that make **claims about real-world
events**. An eloquent-but-unverified claim is worse than no claim: it launders a
hallucination into authority. This skill subjects every narrative output to an
**adversarial, claim-by-claim verification** that defaults to *doubt* and only
upgrades a claim to *trustworthy* on evidence.

It does **not** trust the artifact, and it does **not** trust itself — every
number and verdict is computed deterministically from the text + the source
corpus (P1: code doesn't lie), and high-risk factual claims are cross-checked
against **independent external sources** (web).

## Inherited DNA (genome from AgenticWorkflow)

| Gene | Application here |
|------|------------------|
| 절대 기준 1 (Quality) | Verification depth/accuracy is the only criterion. No time/token budget. |
| 절대 기준 2 (SOT) | Verdicts written as read-only artifacts (`*-factcheck.json`); never mutate the source artifact except via the explicit remediation step. |
| P1 (Hallucination Prevention) | Claim extraction, evidence resolution, number traceability, fallacy lexicon scan are **Python-deterministic**. The LLM is used ONLY for semantic web verification and prose softening — never to invent a verdict or a number. |
| Adversarial Review | Skeptic-by-default. A claim is UNVERIFIED until independently confirmed. |
| Decision Log | Every REVIEW/FAIL and every remediation is logged with rationale. |

## When to use

After ANY narrative artifact is produced and before it is consumed/published:
W3 insight report · W4 Master integrated report · DCI final report ·
Public Narrative L1/L2/L3 · Newspaper sections.

## Verification Protocol (6 stages — execute in order)

### Stage 1 — Claim extraction (deterministic)
`scripts/fact_verify.py` reuses `extract_claims_with_markers.py --extract` to
pull every claim (`text`, `evidence_ids`, `source_line`) from the artifact.

### Stage 2 — Internal evidence resolution (deterministic)
For every `[ev:xxx]` marker, resolve to a raw-corpus record via an in-memory
`evidence_id → record` index built from `data/raw/{date}/all_articles.jsonl`.
- **Unresolved marker → BLOCKING** (the claim cites a source that does not exist).

### Stage 3 — Number traceability (deterministic)
Numbers in a claim that do **not** appear in its resolved source record are
flagged `untraceable_numbers` (REVIEW). Two legitimate cases vs one bad case:
- legit: a CE3 Python-computed aggregate (corpus count, coverage %, mean) — these
  are trustworthy by construction (verify they came from a Python metrics file).
- legit: a value present in a *different* part of the source.
- **bad: a number with no computational provenance and not in source → likely
  fabrication → BLOCKING after confirmation.**

### Stage 4 — Hallucination / fallacy scan (deterministic; ANCHOR ②b)
Lexicon scan for: absolute overclaim (반드시/확실히/100%/guaranteed),
universal quantifiers (모든/항상/always/never), hype-dream words
(혁명적/game-changing/unprecedented), unsupported superlatives (최고/the only),
and **unhedged future predictions** (will/될 것이다 with no may/might/가능성/전망).
- severe flags (overclaim, unhedged-prediction, hype) → **BLOCKING** (must be
  softened/qualified or removed).

### Stage 5 — Risk scoring → external web worklist (hybrid)
Each claim is risk-scored (numeric + named-entity + assertion + prediction
density + unresolved/untraceable penalties). Claims at/above threshold enter
`web_verify_worklist`. **Run the external layer on the worklist:**
- Invoke `@fact-checker` (Read+WebSearch+WebFetch, opus; on opus quota use a
  Sonnet fallback) on the worklist claims — *independent* source verification
  (cite a source OTHER than what the artifact cites). Claim-by-claim verdicts:
  Verified / Unverified / False / Unable.
- Prioritise factual world-claims (entities, events, geopolitics) over
  CE3-computed metric claims.

### Stage 6 — Verdict + remediation (ANCHOR ②: self-correct → remove/flag → report)
1. Aggregate deterministic + web verdicts.
2. **Self-correct**: regenerate the offending passage with the failing claim
   removed or correctly qualified; re-run Stages 1-5.
3. If still unverifiable after self-correction: **strike or mark `[UNVERIFIED]`**
   inline (never silently keep an unverified claim — ANCHOR ②b garbage-in block).
4. **Report**: write `verification-logs/factcheck-{artifact}-{date}.md` with
   per-claim verdicts, web sources, remediations, and residual UNVERIFIED items.
5. If verification *itself* cannot run (no corpus, extractor broken): **STOP and
   report** (ANCHOR: if ② is shaken, halt ①③).

## How to run

```bash
# Deterministic internal layer + worklist (exit 1 ⇒ remediation required)
.venv/bin/python scripts/fact_verify.py \
  --report <artifact.md> --jsonl data/raw/<date>/all_articles.jsonl \
  --label <artifact-name> --out verification-logs/factcheck-<artifact>-<date>.json

# External web layer (on the worklist) — orchestrator invokes the agent:
#   @fact-checker, verify each claim in <worklist> against INDEPENDENT sources.
```

## NEVER DO (ANCHOR ②b)
- Never pass an unresolved-marker claim downstream.
- Never keep an overclaim/unhedged-prediction unqualified.
- Never invent a verdict or a number — read them from `fact_verify.py` / the
  agent's report.
- Never declare "verified" on internal evidence alone for a high-risk world-claim
  — external independent confirmation is required (hybrid).
- Never produce dreamy/grandiose prose. Calibrated, sourced, hedged — or struck.

## Absolute-standard conflict
Quality (①) over speed always. If hallucination-prevention (②) cannot be assured
for an artifact, **stop and report** rather than ship a polished falsehood —
②'s integrity outranks ① and ③ completion (per the user's ABSOLUTE ANCHOR).

See `references/protocol.md` for the full per-stage detail and the integration
map of existing verification assets.
