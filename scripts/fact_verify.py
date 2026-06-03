#!/usr/bin/env python3
"""fact_verify.py — Fact-Verification driver (ABSOLUTE ANCHOR ②a hallucination safeguard).

Deterministic core of the `fact-verification` sub-skill. Verifies a narrative
markdown output against its source corpus and surfaces hallucination risk so the
orchestrator can self-correct / flag / report.

Pipeline (deterministic, P1 — code doesn't lie):
  1. Claim extraction        — reuse extract_claims_with_markers.py --extract
  2. Internal evidence        — resolve every [ev:xxx] marker to a raw JSONL record
                                (in-memory evidence_id->record index)
  3. Number traceability      — numbers in a claim that do NOT appear in its
                                resolved source record are flagged REVIEW
                                (may be a legit CE3-computed metric, or a fabrication)
  4. Hallucination scan       — overclaim/absolute, false-confidence, unhedged
                                prediction, unsupported superlative, hype/"dream" words
  5. Risk scoring             — numeric + named-entity + assertion + prediction density
                                -> HIGH-risk claims go to the external web worklist
                                (consumed by the @fact-checker agent — hybrid mode)

Hybrid verification: this script does the INTERNAL + deterministic layer and emits a
worklist for the EXTERNAL web layer (@fact-checker). It NEVER fabricates verdicts —
every number/flag is computed from the text + source.

Exit codes: 0 = no blocking issue; 1 = issues found (unresolved markers OR
overclaim/false-confidence flags) — orchestrator must remediate; 2 = error.

Usage:
  fact_verify.py --report <md> --jsonl <raw_corpus.jsonl> [--out <report.json>]
                 [--label <name>] [--high-risk-threshold 3]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Hallucination lexicons (ANCHOR ②b — block exaggeration / false confidence /
# dreaming. Flags mark text for review/softening, NOT automatic falsity.)
# --------------------------------------------------------------------------
_ABSOLUTE = [
    # Korean absolutes / overclaim
    "반드시", "확실히", "절대", "명백히", "틀림없이", "분명히", "완벽하게",
    "의심의 여지", "전혀", "결코", "100%", "100 %", "백퍼센트",
    # English absolutes
    "certainly", "definitely", "undoubtedly", "guaranteed", "proven fact",
    "undeniable", "without doubt", "100%", "absolutely", "categorically",
]
_UNIVERSAL = [
    "모든 ", "항상", "언제나", "전부", "예외 없이",
    "always", "never ", "every single", "all of them", "none of",
]
_HYPE = [  # dream / delusion promoters (ANCHOR ②b)
    "혁명적", "판도를 바꾸", "게임체인저", "전례 없는", "역사상 최초",
    "revolutionary", "game-chang", "paradigm shift", "unprecedented",
    "groundbreaking", "world-first", "once-in-a-generation",
]
_SUPERLATIVE = [
    "최고의", "유일한", "가장 큰", "최대의", "최악의",
    "the best", "the only", "the most", "the largest", "the worst",
]
# Future-prediction cue (KO/EN) — flagged ONLY when no hedge nearby.
_PREDICTION = re.compile(
    r"(will\s+\w+|is going to|예상된다|될 것이다|할 것이다|전망이다|가능성이 높다|"
    r"이어질 것|확대될 것|심화될 것)", re.IGNORECASE)
_HEDGE = re.compile(
    r"(may|might|could|likely|possibly|expected|projected|appears|suggests|"
    r"risk that|risk of|the risk|conditional|if\b|should\b|"  # risk-framed / conditional hedges
    r"가능성|전망|예상|추정|시사|~로 보인다|불확실|may be|could be|"
    r"라면|다면|경우|조건부|위험이|리스크|"  # KO conditional / risk hedges
    r"belief that|expectation that|forecast that|prediction that|not a prediction|"  # attributed / disclaimed
    r"reflects .{0,30}belief|is not a forecast|믿음|기대|전망이다)", re.IGNORECASE)

_NUM = re.compile(r"-?\d[\d,]*\.?\d*%?")
_EVID = re.compile(r"ev:[a-f0-9]{6,}")  # real markers are 16 hex; {6,} excludes HTML-parse fragments
# Capitalised multi-word (proper-noun) heuristic for named-entity density.
_PROPER = re.compile(r"\b[A-Z][a-zA-Z]+(?:[\s-][A-Z][a-zA-Z]+)*\b")


def _norm_num(tok: str) -> str:
    return tok.replace(",", "").rstrip("%").rstrip(".")


def _load_evidence_index(jsonl: Path) -> dict:
    idx = {}
    with open(jsonl, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            eid = rec.get("evidence_id")
            if isinstance(eid, str) and eid:
                key = eid if eid.startswith("ev:") else f"ev:{eid}"
                idx[key] = rec
    return idx


def _extract_claims(report: Path, project_dir: Path) -> dict:
    """Reuse the canonical CE3 claim extractor (deterministic)."""
    out = project_dir / ".tmp_factverify_claims.json"
    script = project_dir / "scripts" / "execution" / "p1" / "extract_claims_with_markers.py"
    py = project_dir / ".venv" / "bin" / "python"
    py = str(py) if py.exists() else sys.executable
    try:
        subprocess.run(
            [py, str(script), "--extract", "--report", str(report), "--output", str(out)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if out.exists():
            data = json.loads(out.read_text(encoding="utf-8"))
            out.unlink(missing_ok=True)
            return data
    except Exception as exc:  # noqa: BLE001
        return {"_error": str(exc), "claims": {}}
    return {"claims": {}}


_FINDING_TAG = re.compile(r"\[(GEOP|TEMP|CL-?\d?|NF-?\d?|EA-?\d?|TP-?\d?|GI-?\d?|EI-?\d?|FI-?\d?)[^\]]*\]")
_MD_STRIP = re.compile(r"^[\s>*#\-•\d.)\]\[]+")


def _extract_markdown_claims(report_path: Path) -> list[dict]:
    """Fallback claim extraction for marker-less reports (W3 template, DCI daily,
    public narrative, newspaper). Pulls factual-bearing lines — bullets, finding-tagged
    lines, or sentences carrying a number or proper noun — so the hallucination scan +
    external worklist actually run on reports that lack inline [ev:xxx] markers.
    """
    claims = []
    try:
        text = report_path.read_text(encoding="utf-8")
    except Exception:
        return claims
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 20 or line.startswith("|") or line.startswith("```"):
            continue
        is_bullet = bool(re.match(r"^[\-*•]\s+|^\d+\.\s+", line))
        has_tag = bool(_FINDING_TAG.search(line))
        body = _MD_STRIP.sub("", line).strip()
        if len(body) < 15:
            continue
        # Factual signal: a number, OR a multi-word proper noun, OR a finding tag.
        factual = bool(_NUM.search(body)) or bool(_PROPER.search(body)) or has_tag
        if not (is_bullet or has_tag or (factual and body.endswith((".", "다", "다.")))):
            continue
        if not factual:
            continue
        key = body[:120]
        if key in seen:
            continue
        seen.add(key)
        claims.append({"text": body, "evidence_ids": []})
    return claims


def _source_text(rec: dict) -> str:
    parts = [str(rec.get(k, "")) for k in ("title", "body", "source_name", "source_domain")]
    return " ".join(parts)


def _scan_hallucination(text: str) -> list[dict]:
    flags = []
    low = text.lower()
    for term in _ABSOLUTE:
        if term.lower() in low:
            flags.append({"type": "absolute_overclaim", "term": term})
    for term in _UNIVERSAL:
        if term.lower() in low:
            flags.append({"type": "universal_quantifier", "term": term})
    for term in _HYPE:
        if term.lower() in low:
            flags.append({"type": "hype_dream", "term": term})
    for term in _SUPERLATIVE:
        if term.lower() in low:
            flags.append({"type": "unsupported_superlative", "term": term})
    if _PREDICTION.search(text) and not _HEDGE.search(text):
        flags.append({"type": "unhedged_prediction", "term": _PREDICTION.search(text).group(0)})
    return flags


def verify(report: Path, jsonl: Path, project_dir: Path, high_risk_threshold: int) -> dict:
    evidence = _load_evidence_index(jsonl)
    extracted = _extract_claims(report, project_dir)
    claims_raw = extracted.get("claims", {}) or {}
    # Normalise to a list
    if isinstance(claims_raw, dict):
        items = [(k, v) for k, v in claims_raw.items()]
    else:
        items = list(enumerate(claims_raw))
    # Supplement with markdown factual lines so marker-less reports (W3 template,
    # DCI daily, public narrative, newspaper) are actually verified, not trivially
    # passed. Marker-based claims take priority; markdown claims fill the gap.
    marker_texts = {((v.get("text") if isinstance(v, dict) else str(v)) or "")[:120]
                    for _, v in items}
    md_added = 0
    for j, mc in enumerate(_extract_markdown_claims(report)):
        if mc["text"][:120] in marker_texts:
            continue
        items.append((f"md-{j}", mc))
        md_added += 1

    claims_out = []
    worklist = []
    n_with_markers = 0
    n_markers = 0
    n_resolved = 0
    n_unresolved = 0
    n_halluc = 0
    n_numbers_untraceable = 0

    for cid, c in items:
        text = (c.get("text") if isinstance(c, dict) else str(c)) or ""
        markers = (c.get("evidence_ids") if isinstance(c, dict) else None) or _EVID.findall(text)
        markers = list(dict.fromkeys(markers))
        if markers:
            n_with_markers += 1
        # Internal evidence resolution
        resolved, unresolved, src_blob = [], [], []
        for m in markers:
            n_markers += 1
            rec = evidence.get(m if m.startswith("ev:") else f"ev:{m}")
            if rec is not None:
                resolved.append(m)
                n_resolved += 1
                src_blob.append(_source_text(rec))
            else:
                unresolved.append(m)
                n_unresolved += 1
        source_text = " ".join(src_blob)
        source_nums = {_norm_num(x) for x in _NUM.findall(source_text)}
        # Number traceability (numbers in claim not present in source -> REVIEW)
        claim_nums = [x for x in _NUM.findall(text)]
        untraceable_nums = [
            x for x in claim_nums
            if _norm_num(x) and _norm_num(x) not in source_nums and len(_norm_num(x)) >= 2
        ]
        if untraceable_nums:
            n_numbers_untraceable += len(untraceable_nums)
        # Hallucination scan
        halluc = _scan_hallucination(text)
        if halluc:
            n_halluc += len(halluc)
        # Risk scoring
        risk = 0
        risk += min(3, len(claim_nums))            # numeric density
        risk += min(2, len(set(_PROPER.findall(text))) // 2)  # named-entity density
        if _PREDICTION.search(text):
            risk += 1
        if untraceable_nums:
            risk += 2
        if unresolved:
            risk += 3
        # Unsourced factual claim (no marker but carries numbers/entities) — must be
        # externally verified; absence of an internal source is itself a risk.
        unsourced_factual = (not markers) and (bool(claim_nums) or bool(_PROPER.search(text)))
        if unsourced_factual:
            risk += 2
        high_risk = risk >= high_risk_threshold
        entry = {
            "id": cid,
            "text": text,
            "markers": markers,
            "internal": {
                "all_resolved": (not unresolved) and bool(markers),
                "resolved": resolved,
                "unresolved": unresolved,
            },
            "untraceable_numbers": untraceable_nums,
            "hallucination_flags": halluc,
            "risk_score": risk,
            "web_verify_needed": high_risk,
        }
        claims_out.append(entry)
        if high_risk:
            worklist.append({"id": cid, "text": text, "risk_score": risk,
                             "reason": ("unresolved_markers" if unresolved else
                                        "unsourced_factual" if unsourced_factual else
                                        "untraceable_numbers" if untraceable_nums else
                                        "high_factual_density")})

    total = len(claims_out)
    # Blocking decision: unresolved markers OR overclaim/false-confidence flags.
    severe_halluc = sum(
        1 for c in claims_out for f in c["hallucination_flags"]
        if f["type"] in ("absolute_overclaim", "unhedged_prediction", "hype_dream")
    )
    blocking = (n_unresolved > 0) or (severe_halluc > 0)
    return {
        "report": str(report),
        "jsonl": str(jsonl),
        "total_claims": total,
        "summary": {
            "total_claims_extracted": total,
            "markdown_claims_added": md_added,
            "claims_with_markers": n_with_markers,
            "markers_total": n_markers,
            "markers_resolved": n_resolved,
            "markers_unresolved": n_unresolved,
            "internal_resolution_rate": round(n_resolved / n_markers, 4) if n_markers else None,
            "hallucination_flags": n_halluc,
            "severe_hallucination_flags": severe_halluc,
            "untraceable_numbers": n_numbers_untraceable,
            "high_risk_claims": len(worklist),
        },
        "decision": "REVIEW" if blocking else "PASS",
        "blocking": blocking,
        "claims": claims_out,
        "web_verify_worklist": worklist,
        "note": (
            "Internal+deterministic layer. HIGH-risk claims in web_verify_worklist "
            "must be cross-checked by @fact-checker (external web). decision=REVIEW "
            "means the orchestrator must self-correct/flag/report per ANCHOR ②."
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fact-Verification driver (ANCHOR ②a)")
    ap.add_argument("--report", required=True, help="narrative markdown to verify")
    ap.add_argument("--jsonl", required=True, help="source raw corpus JSONL")
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--out", default=None, help="write JSON report here")
    ap.add_argument("--label", default=None)
    ap.add_argument("--high-risk-threshold", type=int, default=3)
    args = ap.parse_args()

    report = Path(args.report)
    jsonl = Path(args.jsonl)
    pdir = Path(args.project_dir).resolve()
    if not report.exists():
        print(json.dumps({"error": f"report not found: {report}"})); sys.exit(2)
    if not jsonl.exists():
        print(json.dumps({"error": f"jsonl not found: {jsonl}"})); sys.exit(2)

    result = verify(report, jsonl, pdir, args.high_risk_threshold)
    if args.label:
        result["label"] = args.label
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    # Console summary (compact)
    s = result["summary"]
    print(json.dumps({
        "label": args.label, "decision": result["decision"],
        "total_claims": result["total_claims"], "summary": s,
        "out": args.out,
    }, ensure_ascii=False))
    sys.exit(1 if result["blocking"] else 0)


if __name__ == "__main__":
    main()
