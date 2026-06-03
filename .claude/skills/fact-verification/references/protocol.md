# Fact-Verification — Protocol Detail & Integration Map

## Integration map (existing assets the driver reuses — no reinvention)

| Asset | Role in this skill | Layer |
|-------|--------------------|-------|
| `scripts/execution/p1/extract_claims_with_markers.py --extract` | Stage 1 claim+marker extraction (CE3 canonical) | internal/deterministic |
| `scripts/execution/p1/evidence_chain.py --check resolve` | reference for marker→record; the driver builds an in-memory index for speed | internal/deterministic |
| `data/raw/{date}/all_articles.jsonl` | source-of-truth corpus (evidence_id → record) | internal |
| `scripts/fact_verify.py` | Stages 1-5 orchestration + verdict JSON + web worklist | internal/deterministic |
| `@fact-checker` agent (Read/WebSearch/WebFetch) | Stage 5 external independent verification | external/web |
| `@fact-triangulator` (WF5) | optional multi-source triangulation for newspaper claims | external |
| `src/dci/ensemble/re_verification.py`, `sg_superhuman.py` | optional NLI entailment (claim ⊨ source) for deep semantic check | internal/model |
| `@translator` | KO rendering of the fact-check report (text artifacts only) | post |

## Driver output schema (`fact_verify.py` → JSON)

```
{
  report, jsonl, total_claims,
  summary: { claims_with_markers, markers_total, markers_resolved,
             markers_unresolved, internal_resolution_rate,
             hallucination_flags, severe_hallucination_flags,
             untraceable_numbers, high_risk_claims },
  decision: "PASS" | "REVIEW",          # REVIEW = blocking; remediate
  blocking: bool,
  claims: [ { id, text, markers,
              internal:{all_resolved,resolved,unresolved},
              untraceable_numbers, hallucination_flags,
              risk_score, web_verify_needed } ],
  web_verify_worklist: [ {id, text, risk_score, reason} ]
}
```
- exit 0 = PASS (no blocking); exit 1 = REVIEW (remediate); exit 2 = error (STOP+report).

## Blocking criteria (deterministic)
- ANY unresolved `[ev:xxx]` marker (cited source does not exist).
- ANY severe hallucination flag: `absolute_overclaim`, `unhedged_prediction`,
  `hype_dream`.
Non-blocking REVIEW signals: `untraceable_numbers` (verify CE3 provenance),
`unsupported_superlative`, `universal_quantifier`, high `risk_score`.

## Hybrid verification rule (internal vs external)
- **Internal (always)**: marker resolution + number traceability + fallacy scan.
  Sufficient verdict for CE3-computed metric claims (provenance = Python metrics).
- **External (high-risk worklist)**: world-fact claims (named entities, events,
  geopolitical/economic assertions) MUST be cross-checked against an *independent*
  source via `@fact-checker`. A high-risk world-claim is NEVER marked "verified"
  on internal evidence alone.

Worklist prioritisation for the external pass:
1. `reason == unresolved_markers` (critical)
2. world-fact claims with `untraceable_numbers` lacking Python provenance
3. `reason == high_factual_density` (entities/events)
Deprioritise: claims whose only numbers are corpus aggregates from a known
metrics file (w1/w2/w3-metrics.json) — these are CE3-trustworthy.

## Remediation ladder (ANCHOR ②: self-correct → remove/flag → report)
1. **Self-correct** — regenerate the passage with the failing claim removed or
   correctly hedged/qualified; re-run `fact_verify.py`. (Up to 3 attempts.)
2. **Strike/flag** — if still unverifiable: delete the claim, or inline-mark it
   `[UNVERIFIED]` / `[출처 미확인]` so no reader mistakes it for confirmed fact.
3. **Report** — `verification-logs/factcheck-{artifact}-{date}.md`:
   per-claim verdict table, external sources cited, remediations applied,
   residual UNVERIFIED list, internal_resolution_rate, severe-flag count.
4. **Halt condition** — if the verification system itself cannot run (missing
   corpus / extractor failure / repeated external-agent failure), STOP and report
   to the user; do NOT ship the artifact (ANCHOR: ② integrity outranks ①③).

## Per-artifact application notes
- **W3 insight / W4 Master**: marker-dense; expect high internal resolution.
  Untraceable numbers are mostly CE3 metrics → verify provenance, low web-priority.
- **DCI final report**: daily format has few/no inline markers (template prose) —
  rely on the corpus-level claims + heavier external web check on world-facts.
- **Public Narrative (L1/L2/L3)**: facts_pool whitelist already constrains numbers;
  fact-verify adds the fallacy/overclaim + external world-fact layer. L3 (future)
  must be fully hedged — unhedged predictions are BLOCKING.
- **Newspaper sections**: highest external-verification priority (public-facing).
  Triangulate multi-source claims via `@fact-triangulator`; strike single-source
  sensational claims (P15).
