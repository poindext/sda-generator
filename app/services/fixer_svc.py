"""
Fixer service — LLM-driven auto-fix loop for populations with QA issues.

For each round:
  1. Read qa_issues.json
  2. Ask OpenAI for a targeted patch (only changed key paths + new values)
  3. Apply the patch to the template in memory and save it
  4. Regenerate the population
  5. Re-run QA
  6. If approved → build chunks and stop; else loop up to MAX_ROUNDS
"""
import asyncio
import json
import re
from pathlib import Path

from openai import OpenAI

from app.config import OPENAI_API_KEY, DEFAULT_MODEL
from app.services import job_store, qa_svc, chunker_svc
from app.services.generator_svc import _run_generator_sync, _read_meta, _write_meta

MAX_ROUNDS = 5

_SYSTEM_PROMPT = """\
You are a clinical data template expert for a synthetic patient population generator.

You will be given:
1. A summary of the template structure
2. A TEMPLATE VALIDATION REPORT listing structural problems (if any)
3. A MEDICATION-DIAGNOSIS ANALYSIS showing which medications are blocked per cohort (if drug-class issues exist)
4. A list of QA issues found in the generated population

Your job: return a JSON PATCH — a list of targeted changes only.

Each patch entry has exactly two fields:
  "path"  — dot-notation key path into the template (e.g. "cohorts[0].medications[2].for_diagnosis_code")
  "value" — the corrected value to set at that path

Rules:
- Return ONLY the JSON array of patch entries, e.g.:
  [{"path": "cohorts[0].medications[2].for_diagnosis_code", "value": "E84"}, ...]
- No explanation, no markdown fences, no surrounding text
- Array indices in paths are 0-based integers inside square brackets, e.g. cohorts[0]
- Do not change fields unrelated to the reported issues

MEDICATION THRESHOLD ISSUES — follow these rules:
- If a medication shows as BLOCKED because for_diagnosis_code doesn't match any cohort diagnosis,
  shorten the for_diagnosis_code prefix to the first 3-4 characters (e.g. "E84.0" -> "E84")
  OR set it to "" if the medication should be available to all patients in that cohort.
- Never remove a diagnosis-specific medication filter for medications that are only appropriate
  for specific conditions (e.g., insulin stays gated to diabetes codes, not removed entirely).
- If a QA issue says a required drug class is below threshold, check the BLOCKED list and fix
  the for_diagnosis_code values causing the blocking.

LAB RANGE ISSUES:
- Adjust normal_min/normal_max/abnormal_min/abnormal_max in the relevant result_items.
- FEV1 (LOINC 20157-4) must always have a lower max value than FVC (20150-9) in every range.
"""


def _template_summary(tmpl: dict) -> str:
    """Return a compact structural summary of the template (no large arrays)."""
    MAX_ITEMS = 3

    def summarise(obj, depth=0):
        if depth > 4:
            return "…"
        if isinstance(obj, dict):
            items = []
            for k, v in obj.items():
                items.append(f"{k}: {summarise(v, depth + 1)}")
            return "{" + ", ".join(items) + "}"
        if isinstance(obj, list):
            preview = [summarise(v, depth + 1) for v in obj[:MAX_ITEMS]]
            suffix = f", … ({len(obj)} total)" if len(obj) > MAX_ITEMS else ""
            return "[" + ", ".join(preview) + suffix + "]"
        if isinstance(obj, str) and len(obj) > 60:
            return repr(obj[:57] + "…")
        return repr(obj)

    return summarise(tmpl)


def _apply_patch(tmpl: dict, patches: list) -> tuple[dict, list[str]]:
    """Apply a list of {"path": ..., "value": ...} patches to the template dict.

    Returns (updated_template, list_of_error_messages).
    Paths use dot notation with bracket indexing: "cohorts[0].lab_ranges.wbc.max"
    """
    import copy
    result = copy.deepcopy(tmpl)
    errors: list[str] = []

    # Parse a path string into a list of keys/indices
    def parse_path(path: str):
        parts = []
        for segment in re.split(r"\.", path):
            m = re.fullmatch(r"([^\[]+)((?:\[\d+\])+)", segment)
            if m:
                parts.append(m.group(1))
                for idx in re.findall(r"\[(\d+)\]", m.group(2)):
                    parts.append(int(idx))
            else:
                parts.append(segment)
        return parts

    for entry in patches:
        path = entry.get("path", "")
        value = entry.get("value")
        if not path:
            continue
        parts = parse_path(path)
        node = result
        try:
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = value
        except (KeyError, IndexError, TypeError) as e:
            errors.append(f"patch '{path}': {e}")

    return result, errors


async def start_fix(
    job_id: str,
    template_path: str,
    output_dir: str,
    count: int,
    history_months: int,
) -> None:
    """Fire-and-forget: start the fix-regenerate-QA loop as a background task."""
    asyncio.create_task(
        _fix_pipeline(job_id, template_path, output_dir, count, history_months)
    )


async def _fix_pipeline(
    job_id: str,
    template_path: str,
    output_dir: str,
    count: int,
    history_months: int,
) -> None:
    job_store.update_status(job_id, "running")
    pop_dir = Path(output_dir)

    for round_num in range(1, MAX_ROUNDS + 1):
        job_store.append_progress(job_id, "")
        job_store.append_progress(
            job_id,
            f"── Round {round_num}/{MAX_ROUNDS} ──────────────────────────────────────",
        )

        qa_file = pop_dir / "qa_issues.json"
        if not qa_file.exists():
            job_store.append_progress(job_id, "No qa_issues.json found — nothing to fix.")
            job_store.update_status(job_id, "completed",
                                    result={"qa_status": "needs_review", "rounds": 0})
            return

        issues = json.loads(qa_file.read_text(encoding="utf-8")).get("issues", [])
        if not issues:
            job_store.append_progress(job_id, "No issues in qa_issues.json — population is clean.")
            break

        # Log severity summary
        by_sev: dict[str, int] = {}
        for iss in issues:
            s = iss.get("severity", "?")
            by_sev[s] = by_sev.get(s, 0) + 1
        summary = "  ".join(f"{s}: {n}" for s, n in sorted(by_sev.items()))
        job_store.append_progress(job_id, f"{len(issues)} issue(s) — {summary}")
        job_store.append_progress(job_id, "Calling LLM to compute template patch…")

        try:
            tmpl = json.loads(Path(template_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            job_store.update_status(job_id, "failed", error=f"Cannot read template: {e}")
            return

        patches = await _call_llm_with_heartbeat(job_id, tmpl, issues)
        if patches is None:
            job_store.update_status(
                job_id, "failed",
                error="LLM patch call failed — check OPENAI_API_KEY",
            )
            return

        job_store.append_progress(job_id, f"Applying {len(patches)} patch(es) to template…")
        fixed_tmpl, patch_errors = _apply_patch(tmpl, patches)
        for err in patch_errors:
            job_store.append_progress(job_id, f"  ⚠ {err}")

        Path(template_path).write_text(
            json.dumps(fixed_tmpl, indent=2), encoding="utf-8"
        )
        job_store.append_progress(job_id, "Template updated. Regenerating population…")

        gen_ok = await asyncio.to_thread(
            _run_generator_sync, job_id, template_path, output_dir,
            count, history_months,
        )
        if not gen_ok:
            return  # generator already set status to failed

        job_store.append_progress(job_id, "")
        job_store.append_progress(
            job_id, "── Re-running QA ──────────────────────────────────────",
        )
        approved = await asyncio.to_thread(
            qa_svc.run_qa_sync, job_id, template_path, output_dir
        )

        meta = _read_meta(pop_dir)
        if approved:
            meta["qa_status"] = "approved"
            meta["fix_rounds"] = round_num
            _write_meta(pop_dir, meta)
            job_store.append_progress(job_id, "")
            job_store.append_progress(job_id, "✓ QA APPROVED — building download chunks")
            await asyncio.to_thread(chunker_svc.build_chunks, pop_dir)
            meta["chunks_built"] = True
            _write_meta(pop_dir, meta)
            job_store.update_status(
                job_id, "completed",
                result={"qa_status": "approved", "rounds": round_num},
            )
            return

        meta["qa_status"] = "needs_review"
        _write_meta(pop_dir, meta)
        job_store.append_progress(job_id, f"QA still has issues after round {round_num}.")

    job_store.append_progress(job_id, "")
    job_store.append_progress(
        job_id,
        f"Stopped after {MAX_ROUNDS} round(s). Remaining issues require manual review.",
    )
    job_store.update_status(
        job_id, "completed",
        result={"qa_status": "needs_review", "rounds": MAX_ROUNDS},
    )


async def _call_llm_with_heartbeat(
    job_id: str, tmpl: dict, issues: list
) -> list | None:
    """Run _call_llm_patch in a thread, emitting heartbeat log lines every 10s."""
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(None, _call_llm_patch, tmpl, issues)
    elapsed = 0
    while not future.done():
        await asyncio.sleep(10)
        elapsed += 10
        if not future.done():
            job_store.append_progress(
                job_id, f"  … LLM still working ({elapsed}s elapsed)"
            )
    return await future


def _med_diag_context(tmpl: dict, issues: list) -> str:
    """Return a detailed medication/diagnosis analysis when drug-class issues are present."""
    drug_kw = {"drug", "medication", "trikafta", "cftr", "pert", "pancrelipase",
               "tobramycin", "azithromycin", "prevalence", "threshold", "below"}
    has_drug_issue = any(
        any(kw in str(iss).lower() for kw in drug_kw)
        for iss in issues
    )
    if not has_drug_issue:
        return ""

    lines = ["\nMEDICATION-DIAGNOSIS ANALYSIS (showing blocked medications per cohort):"]
    for i, cohort in enumerate(tmpl.get("cohorts", [])):
        diag_codes = [d.get("code", "") for d in cohort.get("diagnoses", [])]
        meds = cohort.get("medications", [])
        lines.append(f"\n  cohorts[{i}] '{cohort.get('name', '')}' diagnoses={diag_codes}")
        for j, m in enumerate(meds):
            fdc = m.get("for_diagnosis_code", "")
            if not fdc:
                status = "available (no filter)"
            elif any(c.startswith(fdc) for c in diag_codes):
                status = f"available (matches {next(c for c in diag_codes if c.startswith(fdc))})"
            else:
                status = f"*** BLOCKED — no cohort diagnosis starts with '{fdc}' ***"
            lines.append(
                f"    medications[{j}] '{m.get('drug_description', '')[:45]}' "
                f"for_diagnosis_code='{fdc}' -> {status}"
            )
    return "\n".join(lines)


def _validator_context(tmpl: dict) -> str:
    """Run the template validator and format its output for the LLM."""
    import sys
    import os
    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
    sys.path.insert(0, os.path.abspath(scripts_dir))
    try:
        from generate_population import validate_template
        warnings = validate_template(tmpl)
    except Exception:
        return ""
    finally:
        sys.path.pop(0)
    if not warnings:
        return "\nTEMPLATE VALIDATION: No structural issues detected."
    lines = [f"\nTEMPLATE VALIDATION — {len(warnings)} structural issue(s) found:"]
    for w in warnings:
        lines.append(f"  - {w}")
    return "\n".join(lines)


def _call_llm_patch(tmpl: dict, issues: list) -> list | None:
    """Ask the LLM for a targeted patch list rather than a full template rewrite.

    Returns a list of {"path": ..., "value": ...} dicts, or None on failure.
    Output is typically <50 tokens vs 3000+ for a full template rewrite.
    """
    if not OPENAI_API_KEY:
        return None

    summary = _template_summary(tmpl)
    validator_section = _validator_context(tmpl)
    med_diag_section = _med_diag_context(tmpl, issues)

    user_msg = (
        f"Template structure:\n{summary}"
        f"{validator_section}"
        f"{med_diag_section}\n\n"
        f"QA Issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Return the JSON patch array only."
    )
    try:
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=60.0)
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        patches = json.loads(raw.strip())
        if not isinstance(patches, list):
            return None
        return patches
    except Exception:
        return None
