#!/usr/bin/env python3
"""
WF5 Personal Newspaper — Daily Edition Orchestrator (ADR-083)

7-Phase pipeline:
  1. Data ingestion          (W1/W2/W3/W4/DCI/Public/Chart Interp)
  2. Story Clustering        (P6 — DCI simhash + entity overlap)
  3. Structural Organization (country/continent/STEEPS/tier/dark/budget)
  4. Parallel Editorial      (14 desks × Claude CLI via invoke_claude_agent)
  5. Chief Editor Assembly   (17th agent integrates drafts)
  6. Copy Editor Review      (P9/P10/P14/P15 enforcement)
  7. HTML Rendering          (NYTimes-style multi-page)

Usage:
    python3 scripts/reports/generate_newspaper_daily.py \\
        --date 2026-04-14 --project-dir .

    # Skip Phase 4-6 (data + HTML only — for layout testing)
    python3 scripts/reports/generate_newspaper_daily.py \\
        --date 2026-04-14 --skeleton-only --project-dir .

Exit codes:
    0 — full edition generated + NP1-NP12 PASS
    1 — any phase FAIL or validator FAIL
    2 — script error

Runtime: ~60-90 min full run (14 LLM calls for desks + chief + copy).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _emit(obj: dict[str, Any], code: int) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(code)


def _invoke_agent(
    project_dir: Path, agent: str, inputs: dict[str, str],
    output: Path, *, dry_run: bool = False, model: str | None = None,
) -> dict[str, Any]:
    invoker = project_dir / "scripts" / "reports" / "invoke_claude_agent.py"
    if not invoker.exists():
        return {"valid": False, "reason": "invoker missing"}
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(invoker),
        "--agent", agent,
        "--output", str(output),
        "--project-dir", str(project_dir),
        "--inputs", *[f"{k}={v}" for k, v in inputs.items()],
    ]
    if model:
        cmd += ["--model", model]
    if dry_run:
        cmd.append("--dry-run")
    # Retry transient claude CLI failures (intermittent exit 1 / empty stdout
    # observed under sustained load) — up to 4 attempts with 5s backoff.
    last: dict[str, Any] = {"valid": False, "reason": "no attempt"}
    for attempt in range(1, 5):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        except subprocess.TimeoutExpired:
            last = {"valid": False, "reason": "timeout", "attempt": attempt}
            time.sleep(5)
            continue
        if proc.returncode != 0:
            last = {
                "valid": False,
                "returncode": proc.returncode,
                "stderr_tail": proc.stderr[-400:],
                "attempt": attempt,
            }
            time.sleep(5)
            continue
        try:
            res = json.loads(proc.stdout or "{}")
            if isinstance(res, dict):
                res.setdefault("attempt", attempt)
            return res
        except json.JSONDecodeError:
            last = {"valid": False, "reason": "invoker json parse failed", "attempt": attempt}
            time.sleep(5)
            continue
    return last


def main() -> None:
    ap = argparse.ArgumentParser(description="WF5 Newspaper daily orchestrator")
    ap.add_argument("--date", required=True)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--skeleton-only", action="store_true",
                    help="Phases 1-3 + 7 only (skip LLM phases 4-6)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compose prompts, skip Claude CLI")
    ap.add_argument("--desks", default=None,
                    help="Comma-separated desk ids (default: all 14)")
    ap.add_argument("--model", default=None,
                    help="Override model for all agent calls (e.g. claude-sonnet-4-6) — opus quota fallback")
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    sys.path.insert(0, str(project_dir))

    try:
        from src.newspaper import (
            CONTINENTS, STEEPS_SECTIONS,
            DAILY_WORD_BUDGET, TEMPLATE_VERSION,
        )
        from src.newspaper.story_clusterer import (
            load_corpus, cluster_articles, persist_clusters,
        )
        from src.newspaper.organizers import organize_full
        from src.newspaper.country_mapper import all_known_countries
        from src.newspaper.html_renderer import render_edition
    except Exception as exc:
        _emit({"valid": False, "error": f"import failed: {exc}"}, 2)

    t_start = time.time()
    edition = project_dir / "newspaper" / "daily" / args.date
    edition.mkdir(parents=True, exist_ok=True)
    (edition / "drafts").mkdir(exist_ok=True)

    # ---- Phase 1: Ingest ----------------------------------------------------
    t1 = time.time()
    articles = load_corpus(project_dir, args.date)
    phase1_elapsed = time.time() - t1
    if not articles:
        _emit({"valid": False,
               "reason": f"no articles for {args.date} (W1 incomplete)"}, 1)

    # ---- Phase 2: Story Clustering ------------------------------------------
    t2 = time.time()
    clusters = cluster_articles(articles)
    clusters_dict = [c.to_dict() for c in clusters]
    persist_clusters(clusters, edition / "story_clusters.json")
    phase2_elapsed = time.time() - t2

    # ---- Phase 3: Organization ----------------------------------------------
    t3 = time.time()
    org = organize_full(project_dir, args.date, clusters_dict)
    (edition / "editorial_plan.json").write_text(
        json.dumps(org, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    phase3_elapsed = time.time() - t3

    # Evidence anchor map (persisted for editors to consult)
    from src.newspaper.organizers import build_evidence_anchor_map
    evidence_map = build_evidence_anchor_map(clusters_dict)
    (edition / "evidence_anchor_map.json").write_text(
        json.dumps(evidence_map, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    stats: dict[str, Any] = {
        "articles_total": len(articles),
        "clusters_total": len(clusters_dict),
        "clusters_triangulated": sum(
            1 for c in clusters_dict if c.get("triangulated")
        ),
        "countries_covered": sum(
            1 for cnt in org["dark_corners"]["coverage_per_country_sample"]
        ),
        "countries_dark": len(org["dark_corners"]["uncovered"]),
        "evidence_markers": org["evidence_anchor_count"],
        "tier_distribution": org["tier_distribution"],
    }

    if args.skeleton_only or args.dry_run:
        # Skip LLM phases — render with empty drafts for layout preview
        drafts: dict[str, str] = {}
    else:
        # ---- Phase 4: Parallel Editorial ----------------------------------
        t4 = time.time()
        desks = _desk_list(args.desks)
        drafts = {}
        # NP-Fix-prompt-size: story_clusters.json (3.7MB) + evidence_anchor_map.json
        # (784KB) inlined into stdin pipe causes claude CLI exit 1 (prompt overflow).
        # Pre-slice story_clusters per desk and write compact per-desk JSONs;
        # editorial_plan (11KB) is small enough to inline as-is.
        from src.newspaper.organizers import slice_clusters_for_desk  # noqa: E501
        slices_dir = edition / "drafts" / "_slices"
        slices_dir.mkdir(parents=True, exist_ok=True)
        common_inputs_base = {
            "editorial_plan": str(edition / "editorial_plan.json"),
        }
        # Optional upstream artifacts
        for label, path in [
            ("public_future",
             project_dir / "reports" / "public" / args.date / "future.md"),
            ("w4_master",
             project_dir / "reports" / "final"
             / f"integrated-report-{args.date}.md"),
        ]:
            if path.exists():
                common_inputs_base[label] = str(path)

        for desk in desks:
            draft_path = edition / "drafts" / f"{desk}.md"
            # Per-desk story_clusters slice (compact body, drops full text)
            slice_path = slices_dir / f"{desk}-clusters.json"
            try:
                slice_payload = slice_clusters_for_desk(
                    clusters_dict, org, desk,
                )
                slice_path.write_text(
                    json.dumps(slice_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                desk_inputs = dict(common_inputs_base)
                desk_inputs["story_clusters"] = str(slice_path)
            except Exception as exc:  # noqa: BLE001 — fall back to no clusters
                print(
                    f"Phase 4 WARN: slice for {desk} failed: {exc}",
                    file=sys.stderr,
                )
                desk_inputs = dict(common_inputs_base)
            result = _invoke_agent(
                project_dir, desk, desk_inputs, draft_path, model=args.model,
            )
            if result.get("valid"):
                try:
                    drafts[desk] = draft_path.read_text(encoding="utf-8")
                except Exception:
                    drafts[desk] = ""
            else:
                # Surface failure reason for diagnostics — non-blocking
                print(
                    f"Phase 4 WARN: {desk} failed: "
                    f"{result.get('error') or result.get('reason') or result}",
                    file=sys.stderr,
                )
        phase4_elapsed = time.time() - t4

        # Merge any pre-existing desk drafts not regenerated this run (e.g.
        # when --desks targets a subset) so chief assembly + render produce
        # the COMPLETE edition rather than only the regenerated subset.
        for _draft_file in sorted((edition / "drafts").glob("*.md")):
            _name = _draft_file.stem
            if _name in drafts or _name in (
                "newspaper-chief-editor", "newspaper-copy-editor",
            ):
                continue
            try:
                drafts[_name] = _draft_file.read_text(encoding="utf-8")
            except Exception:
                pass

        # ---- Phase 4.5: Budget diagnostics (NP-Fix-D) --------------------
        from src.newspaper.budget_adjuster import (
            diagnose_edition, persist_diagnostics,
        )
        diag = diagnose_edition(edition)
        persist_diagnostics(diag, edition)
        # Chief Editor gets advisory as extra context
        advisory_txt = "\n".join(
            diag.get("chief_editor_advisory", [])
        )

        # ---- Phase 5: Chief Editor Assembly ------------------------------
        t5 = time.time()
        # Chief receives all drafts as additional inputs
        chief_inputs = dict(common_inputs_base)
        for desk, md_text in drafts.items():
            # Reference files, not inline
            chief_inputs[f"draft_{desk}"] = str(edition / "drafts" / f"{desk}.md")
        chief_inputs["budget_diagnostics"] = str(edition / "budget_diagnostics.json")
        chief_out = edition / "drafts" / "newspaper-chief-editor.md"
        chief_result = _invoke_agent(
            project_dir, "newspaper-chief-editor", chief_inputs, chief_out, model=args.model,
        )
        if chief_result.get("valid"):
            drafts["newspaper-chief-editor"] = chief_out.read_text(encoding="utf-8")
        phase5_elapsed = time.time() - t5

        # ---- Phase 6: Copy Editor Review ---------------------------------
        t6 = time.time()
        # Copy editor reads chief's assembly + all desk drafts, outputs final
        copy_inputs = dict(common_inputs_base)
        copy_inputs["chief_assembly"] = str(chief_out)
        copy_out = edition / "drafts" / "newspaper-copy-editor.md"
        copy_result = _invoke_agent(
            project_dir, "newspaper-copy-editor", copy_inputs, copy_out, model=args.model,
        )
        # If copy editor produces valid output, override chief with it
        if copy_result.get("valid") and copy_out.exists():
            drafts["newspaper-chief-editor"] = copy_out.read_text(encoding="utf-8")
        phase6_elapsed = time.time() - t6

    # ---- Phase 7: HTML Rendering -------------------------------------------
    t7 = time.time()
    # Extract watch list (best-effort) from future outlook draft
    watch_list = _extract_watch_list(drafts.get("future-outlook-writer", ""))
    render_result = render_edition(
        project_dir, args.date,
        drafts=drafts, stats=stats, watch_list=watch_list,
    )
    phase7_elapsed = time.time() - t7

    # ---- Phase 7b: Single-page merge (dashboard-friendly) -----------------
    # WF5 renders 16 multi-page HTMLs for NYTimes-style navigation, but
    # Streamlit's `st.components.v1.html()` iframe sandbox cannot follow
    # relative hrefs. Merge all sub-pages into a single scrollable index.html
    # with in-page anchors so the dashboard shows the full edition.
    # Root cause: AUDIT-newspaper-display-2026-04-15.md.
    try:
        from scripts.reports.build_newspaper_single_page import build_single_page
        _merged = build_single_page(edition)
        phase7b_size = _merged.stat().st_size
    except Exception as _exc:  # noqa: BLE001 — best-effort; multi-page files
        # remain on disk regardless of merge outcome.
        phase7b_size = None
        print(f"Phase 7b WARN: single-page merge failed: {_exc}", file=sys.stderr)

    # ---- Final SOT-like metadata -------------------------------------------
    payload = {
        "date": args.date,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "template_version": TEMPLATE_VERSION,
        "skeleton_only": args.skeleton_only,
        "drafts_generated": {k: len(v) for k, v in drafts.items()},
        "stats": stats,
        "phase_elapsed_seconds": {
            "phase1_ingest": round(phase1_elapsed, 2),
            "phase2_cluster": round(phase2_elapsed, 2),
            "phase3_organize": round(phase3_elapsed, 2),
            **(
                {"phase4_editorial": round(locals().get("phase4_elapsed", 0), 2),
                 "phase5_chief": round(locals().get("phase5_elapsed", 0), 2),
                 "phase6_copy": round(locals().get("phase6_elapsed", 0), 2)}
                if not (args.skeleton_only or args.dry_run) else {}
            ),
            "phase7_render": round(phase7_elapsed, 2),
        },
        "total_elapsed_seconds": round(time.time() - t_start, 2),
        "output_dir": str(edition.relative_to(project_dir)),
        "render_manifest": render_result,
    }
    (edition / "newspaper_metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _emit(payload, 0)


def _desk_list(only_arg: str | None) -> list[str]:
    from src.newspaper import (
        CONTINENTAL_DESKS, STEEPS_DESKS, SPECIALTY_AGENTS,
    )
    all_desks = [
        *CONTINENTAL_DESKS,
        *STEEPS_DESKS,
        # Specialty in Phase 4 (chief + copy editor run in Phase 5/6)
        "dark-corner-scout", "fact-triangulator", "future-outlook-writer",
    ]
    if only_arg:
        wanted = {d.strip() for d in only_arg.split(",") if d.strip()}
        return [d for d in all_desks if d in wanted]
    return list(all_desks)


def _extract_watch_list(future_md: str) -> list[str]:
    """Extract bullet items from 'Watch List' section of future_outlook draft."""
    if not future_md:
        return []
    import re
    m = re.search(
        r"(?ims)##\s*.*?(?:Watch\s*List|관찰\s*지표|관찰\s*대상).*?$([\s\S]*?)(?=\n##\s|\Z)",
        future_md,
    )
    if not m:
        return []
    bullets = re.findall(r"(?m)^\s*[-*]\s+(.+)$", m.group(1))
    return [b.strip()[:200] for b in bullets[:12]]


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "valid": False,
            "error": f"script failure: {type(exc).__name__}: {exc}",
        }))
        sys.exit(2)
