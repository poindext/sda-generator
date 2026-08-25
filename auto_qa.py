#!/usr/bin/env python3
"""
auto_qa.py — Clinical QA review for synthetic patient populations.

Builds a compact but comprehensive clinical review package from population
CSVs and submits it to OpenAI for clinical accuracy and believability review.

The script is designed to be called by the /auto-qa Claude Code skill, which
handles the fix-application and regeneration loop.

Usage:
  # Review an already-generated population:
  python3 auto_qa.py --template fl_demo.template.json --output population-fl_1000

  # Generate then review:
  python3 auto_qa.py --template fl_demo.template.json --output population-fl_1000 --generate

Exit codes:
  0  approved — population passes clinical review
  1  issues found — qa_issues.json written to output directory
  2  runtime error
"""

import argparse
import csv
import json
import os
import random
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_MODEL = "gpt-4o"
ISSUES_FILE = "qa_issues.json"
CHANGELOG_FILE = "auto_qa_changelog.jsonl"

# Human-readable names for LOINC codes used in the generator
_LOINC_NAMES = {
    "4548-4":  "HbA1c (%)",
    "88294-4": "eGFR (mL/min/1.73m²)",
    "3094-0":  "BUN (mg/dL)",
    "2160-0":  "Creatinine (mg/dL)",
    "2085-9":  "HDL (mg/dL)",
    "13457-7": "LDL (mg/dL)",
    "2093-3":  "Total Cholesterol (mg/dL)",
    "2571-8":  "Triglycerides (mg/dL)",
    "6690-2":  "WBC (K/uL)",
    "777-3":   "Platelets (K/uL)",
    "718-7":   "Hemoglobin (g/dL)",
    "6768-6":  "Alk Phos (U/L)",
    "1742-6":  "ALT (U/L)",
    "1920-8":  "AST (U/L)",
    "1975-2":  "Bilirubin Total (mg/dL)",
    "2951-2":  "Sodium (mEq/L)",
    "2823-3":  "Potassium (mEq/L)",
    "17861-6": "Calcium (mg/dL)",
    "2339-0":  "Glucose (mg/dL)",
    "33762-6": "BNP (pg/mL)",
    "42757-5": "Troponin (ng/mL)",
    "14959-1": "uACR (mg/g)",
    "11579-0": "Total IgE (IU/mL)",
    "20565-8": "Eosinophils (%)",
    "32623-1": "PFT FEV1/FVC (%)",
}


def _flt(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None   # filter NaN
    except (TypeError, ValueError):
        return None


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _infer_cohort(codes: list[str]) -> str:
    for c in codes:
        if c.startswith(("E11", "E10", "E12", "E13")):
            return "Type 2 Diabetes"
    for c in codes:
        if c.startswith(("I50", "I48")):
            return "Heart Failure / AFib"
        if c.startswith(("I25", "I20", "I21")):
            return "Cardiovascular / CAD"
    for c in codes:
        if c.startswith("I10"):
            return "Hypertension"
    for c in codes:
        if c.startswith(("J45", "J44")):
            return "Asthma / COPD"
    for c in codes:
        if c.startswith(("A69", "A77", "A79")):
            return "Tick-borne Disease"
    for c in codes:
        if c.startswith(("O", "Z34")):
            return "Pregnancy"
    for c in codes:
        if c.startswith(("A15", "B20", "B18")):
            return "Reportable Disease"
    for c in codes:
        if c.startswith("Z00"):
            return "Low Risk / Preventive"
    return "Other"


# ---------------------------------------------------------------------------
# Review package builder
# ---------------------------------------------------------------------------

def build_review_package(output_dir: Path, template_name: str) -> str:
    patients  = {r["PatientID"]: r for r in _load_csv(output_dir / "patients.csv")}
    encs_raw  = _load_csv(output_dir / "encounters.csv")
    labs_raw  = _load_csv(output_dir / "labs.csv")
    meds_raw  = _load_csv(output_dir / "medications.csv")
    val_rows  = _load_csv(output_dir / "generator_validation.csv")

    # Index by patient
    enc_by_pat: dict[str, list] = defaultdict(list)
    for r in encs_raw:
        enc_by_pat[r["PatientID"]].append(r)

    lab_by_pat: dict[str, list] = defaultdict(list)
    for r in labs_raw:
        lab_by_pat[r["PatientID"]].append(r)

    med_by_pat: dict[str, list] = defaultdict(list)
    for r in meds_raw:
        med_by_pat[r["PatientID"]].append(r)

    # Infer cohort per patient
    cohort_by_pat: dict[str, str] = {}
    for pid, encs in enc_by_pat.items():
        codes = [e.get("PrimaryDiagnosisCode", "") for e in encs]
        all_codes = []
        for e in encs:
            all_codes += [c for c in e.get("AllEncounterDiagnosisCodes", "").split("|") if c]
        cohort_by_pat[pid] = _infer_cohort(codes + all_codes)

    cohort_counts = Counter(cohort_by_pat.values())

    # Demographic arrays
    ages  = [_flt(p.get("Age")) for p in patients.values()]
    ages  = [a for a in ages if a is not None]
    sexes = [p.get("Sex", "") for p in patients.values()]
    bmis  = [_flt(p.get("BaselineBMI")) for p in patients.values()]
    bmis  = [b for b in bmis if b is not None]
    heights = [_flt(p.get("HeightInches")) for p in patients.values()]
    heights = [h for h in heights if h is not None and h > 10]
    weights = [_flt(p.get("BaselineWeightLbs")) for p in patients.values()]
    weights = [w for w in weights if w is not None and w > 5]

    # Encounter stats
    enc_type_counts: Counter = Counter(r.get("EncounterType") for r in encs_raw)

    # Lab stats per LOINC code
    loinc_vals: dict[str, list] = defaultdict(list)
    loinc_abn:  dict[str, int]  = defaultdict(int)
    for r in labs_raw:
        loinc = r.get("LabCode", "")
        val   = _flt(r.get("ResultValue"))
        flag  = r.get("AbnormalFlag", "").strip().upper()
        if loinc and val is not None:
            loinc_vals[loinc].append(val)
            if flag in ("H", "L", "HH", "LL", "A", "C"):
                loinc_abn[loinc] += 1

    # Medication frequency
    med_freq: Counter = Counter()
    for r in meds_raw:
        key = f"{r.get('MedicationName','')[:35].strip()} {r.get('DoseValue','')} {r.get('DoseUnit','')}".strip()
        if key:
            med_freq[key] += 1

    # Vital stats from encounters (InitialSBP/DBP available for ED/IP)
    sbp = [_flt(r.get("InitialSBP")) for r in encs_raw]
    sbp = [v for v in sbp if v]
    dbp = [_flt(r.get("InitialDBP")) for r in encs_raw]
    dbp = [v for v in dbp if v]
    hr  = [_flt(r.get("InitialHR"))  for r in encs_raw]
    hr  = [v for v in hr  if v]
    wt_enc = [_flt(r.get("InitialWeightLbs")) for r in encs_raw]
    wt_enc = [v for v in wt_enc if v]

    # Condition flag counts (from patients.csv boolean columns)
    def _flag_count(col: str) -> int:
        return sum(1 for p in patients.values() if p.get(col, "").lower() in ("yes", "true", "1"))

    # ── Assemble text ────────────────────────────────────────────────────────

    def hr_line():
        return "─" * 65

    lines = []

    def h(title: str) -> None:
        lines.extend(["", hr_line(), f"  {title}", hr_line()])

    n_pat = len(patients)
    f_count = sum(1 for s in sexes if s == "F")
    m_count = len(sexes) - f_count

    lines.append("SYNTHETIC PATIENT POPULATION — CLINICAL QA REVIEW")
    lines.append("=" * 65)
    lines.append(f"  Template:   {template_name}")
    lines.append(f"  Output:     {output_dir}")
    lines.append(f"  Review:     {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"  Patients:   {n_pat:,}  |  Encounters: {len(encs_raw):,}  |  Labs: {len(labs_raw):,}  |  Medications: {len(meds_raw):,}")

    h("DEMOGRAPHICS")
    if ages:
        lines.append(f"  Age:  mean {statistics.mean(ages):.1f} yrs  (range {int(min(ages))}–{int(max(ages))})")
        lines.append(f"        0–17: {sum(1 for a in ages if a < 18)}  |  18–44: {sum(1 for a in ages if 18 <= a < 45)}  |  45–64: {sum(1 for a in ages if 45 <= a < 65)}  |  65+: {sum(1 for a in ages if a >= 65)}")
    lines.append(f"  Sex:  F {f_count} ({100*f_count/len(sexes):.1f}%)  |  M {m_count} ({100*m_count/len(sexes):.1f}%)")
    if bmis:
        lines.append(f"  BMI:  mean {statistics.mean(bmis):.1f}  (range {min(bmis):.1f}–{max(bmis):.1f})")
        lines.append(f"        <16 (severely underweight): {sum(1 for b in bmis if b < 16)}  |  >50 (severely obese): {sum(1 for b in bmis if b > 50)}")
    if heights:
        lines.append(f"  Height: mean {statistics.mean(heights):.1f} in  (range {min(heights):.1f}–{max(heights):.1f})")
    if weights:
        lines.append(f"  Weight: mean {statistics.mean(weights):.1f} lbs  (range {min(weights):.1f}–{max(weights):.1f})")
    lines.append(f"  Condition flags (from patients.csv):")
    for col, label in [("HasDiabetes","Diabetes"), ("HasHypertension","Hypertension"),
                       ("HasCAD","CAD"), ("HasHeartFailure","Heart Failure"),
                       ("HasAFib","AFib"), ("HasAsthmaCOPD","Asthma/COPD")]:
        cnt = _flag_count(col)
        lines.append(f"    {label:<20} {cnt:4d}  ({100*cnt/n_pat:.1f}%)")

    h("COHORT DISTRIBUTION  (inferred from encounter diagnoses)")
    for cohort, count in cohort_counts.most_common():
        lines.append(f"  {cohort:<40} {count:4d}  ({100*count/n_pat:.1f}%)")

    h("ENCOUNTER SUMMARY")
    enc_per_pat = [len(enc_by_pat[pid]) for pid in patients]
    if enc_per_pat:
        lines.append(f"  Per patient:  mean {statistics.mean(enc_per_pat):.1f}  (range {min(enc_per_pat)}–{max(enc_per_pat)})")
    for etype, label in [("O","Outpatient"), ("E","Emergency"), ("I","Inpatient")]:
        cnt = enc_type_counts.get(etype, 0)
        if cnt:
            lines.append(f"  {label:<15} {cnt:6,}  ({100*cnt/len(encs_raw):.1f}%)")
    acute = sum(1 for r in encs_raw if r.get("IsAcuteEncounter", "").lower() in ("yes","true","1"))
    lines.append(f"  Acute encounters flagged:  {acute}")

    h("VITAL SIGN RANGES  (from ED/IP initial vitals)")
    def _vs_line(label: str, vals: list, unit: str) -> None:
        if vals:
            lines.append(f"  {label:<25} mean {statistics.mean(vals):.0f}  "
                         f"(range {min(vals):.0f}–{max(vals):.0f}) {unit}")
    _vs_line("Systolic BP", sbp, "mmHg")
    _vs_line("Diastolic BP", dbp, "mmHg")
    _vs_line("Heart Rate", hr, "bpm")
    _vs_line("Weight (encounter)", wt_enc, "lbs")

    h("LAB VALUE RANGES  (all encounters)")
    # Sort by volume descending; show top 25 LOINC codes
    for loinc, vals in sorted(loinc_vals.items(), key=lambda x: -len(x[1]))[:25]:
        if len(vals) < 5:
            continue
        name = _LOINC_NAMES.get(loinc, loinc)
        abn_pct = 100 * loinc_abn[loinc] / len(vals)
        pct10  = sorted(vals)[int(len(vals) * 0.10)]
        pct90  = sorted(vals)[int(len(vals) * 0.90)]
        lines.append(
            f"  {name:<38} n={len(vals):5,}  "
            f"mean={statistics.mean(vals):7.1f}  "
            f"min={min(vals):7.1f}  max={max(vals):7.1f}  "
            f"p10={pct10:.1f}  p90={pct90:.1f}  "
            f"abn={abn_pct:.1f}%"
        )

    h("MEDICATION ANALYSIS  (top 35 by frequency)")
    for drug, count in med_freq.most_common(35):
        lines.append(f"  {drug:<55} {count:4d}")

    h("REPRESENTATIVE CASES  (2 complete records per cohort)")
    lines.append("  Each record includes: demographics, all encounters with diagnoses,")
    lines.append("  all medications, and key lab results (most recent per LOINC code).")
    lines.append("")

    cohort_pids: dict[str, list] = defaultdict(list)
    for pid in patients:
        cohort_pids[cohort_by_pat.get(pid, "Other")].append(pid)

    rng = random.Random(42)
    shown = 0
    max_cases = 20
    for cohort, _ in cohort_counts.most_common():
        if shown >= max_cases:
            break
        sample_pids = rng.sample(cohort_pids[cohort], min(2, len(cohort_pids[cohort])))
        for pid in sample_pids:
            p = patients[pid]
            age_val = p.get("Age", "?")
            dob_val = p.get("DOB", "")
            h_in    = p.get("HeightInches", "")
            wt_lb   = p.get("BaselineWeightLbs", "")
            bmi_val = p.get("BaselineBMI", "")
            lines.append(f"  ┌─ Patient {pid}  |  Cohort: {cohort}")
            lines.append(f"  │  Age {age_val}  |  DOB {dob_val}  |  Sex {p.get('Sex','')}  |  {p.get('Race','')}  |  {p.get('City','')} {p.get('ZIP','')}")
            if h_in or wt_lb:
                lines.append(f"  │  Height {h_in} in  |  Weight {wt_lb} lbs  |  BMI {bmi_val}")
            lines.append(f"  │  Facility: {p.get('PrimaryFacilityName','')}")

            encs = sorted(enc_by_pat[pid], key=lambda e: e.get("EncounterStart", ""))
            lines.append(f"  │  Encounters ({len(encs)}):")
            for e in encs[:10]:
                etype  = {"O":"Outpt","E":"ED","I":"Inpt"}.get(e.get("EncounterType","?"), e.get("EncounterType","?"))
                dx     = f"{e.get('PrimaryDiagnosisCode','')} {e.get('PrimaryDiagnosisDescription','')[:40]}"
                sec    = e.get("SecondaryDiagnosisCodes", "")
                date_s = e.get("EncounterStart", "")[:10]
                acute_flag = " [ACUTE]" if e.get("IsAcuteEncounter","").lower() in ("yes","true","1") else ""
                lines.append(f"  │    {date_s}  {etype}  {dx}{acute_flag}")
                if sec:
                    secondary_codes = [c for c in sec.split("|") if c][:4]
                    lines.append(f"  │          Secondary: {', '.join(secondary_codes)}")
            if len(encs) > 10:
                lines.append(f"  │    ... ({len(encs)-10} more encounters)")

            meds = med_by_pat[pid]
            if meds:
                lines.append(f"  │  Medications ({len(meds)}):")
                for m in meds[:8]:
                    name = m.get("MedicationName","")[:35]
                    dose = f"{m.get('DoseValue','')} {m.get('DoseUnit','')}".strip()
                    freq = m.get("Frequency","")
                    cls  = m.get("DrugClass","")[:25]
                    lines.append(f"  │    {name:<35} {dose:<12} {freq:<10} [{cls}]")

            labs = lab_by_pat[pid]
            if labs:
                latest: dict[str, tuple] = {}
                for lab in labs:
                    loinc = lab.get("LabCode", "")
                    dt    = lab.get("ResultDateTime", "")
                    if loinc and (loinc not in latest or dt > latest[loinc][0]):
                        latest[loinc] = (dt, lab)
                lines.append(f"  │  Key labs (most recent per test, {len(labs)} total):")
                for loinc, (dt, lab) in sorted(latest.items())[:12]:
                    name  = _LOINC_NAMES.get(loinc, lab.get("LabName", loinc))
                    val   = lab.get("ResultValue", "")
                    unit  = lab.get("Unit", "")
                    flag  = lab.get("AbnormalFlag", "")
                    flag_s = f"  [{flag}]" if flag else ""
                    lines.append(f"  │    {dt[:10]}  {name:<38} {val} {unit}{flag_s}")

            lines.append("  └─")
            lines.append("")
            shown += 1

    h("VALIDATOR FINDINGS")
    if not val_rows:
        lines.append("  0 findings — all built-in validation rules passed.")
    else:
        rule_counts = Counter(r.get("ValidationRuleID", "") for r in val_rows)
        sev_counts  = Counter(r.get("Severity", "") for r in val_rows)
        lines.append(f"  Total: {len(val_rows)} findings  {dict(sev_counts)}")
        for rule, count in rule_counts.most_common():
            lines.append(f"    {rule}: {count}")
        lines.append("")
        for r in val_rows[:30]:
            lines.append(
                f"  [{r.get('Severity',''):8}] {r.get('ValidationRuleID',''):8} "
                f"P{r.get('PatientID',''):6}: {r.get('Description','')[:75]}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenAI review
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM_PROMPT = """\
You are a clinician and healthcare informatics specialist reviewing a synthetic \
patient population for use in a healthcare IT demonstration. Your role is to \
assess clinical accuracy and believability.

Review the population data for:
1. Clinically implausible values (impossible lab results, anthropometrics, vitals)
2. Age-inappropriate diagnoses, medications, or lab ordering
3. Sex-inappropriate diagnoses or medications
4. Incoherent clinical trajectories (e.g. worsening symptoms but labs improving)
5. Statistically implausible disease prevalence vs. real-world expectations
6. Missing expected clinical associations (e.g. T2DM without lipid monitoring)
7. Implausible combinations (pediatric patient with adult medications, etc.)
8. Any temporal inconsistencies visible in the data

Respond ONLY with a valid JSON object — no prose, no markdown code fences:
{
  "approved": true or false,
  "overall_assessment": "one concise paragraph",
  "issues": [
    {
      "id": 1,
      "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "category": "labs|vitals|medications|demographics|diagnoses|temporal|anthropometrics|comorbidities|other",
      "title": "brief title under 60 chars",
      "description": "specific detailed description",
      "evidence": "concrete example with patient ID or observed value",
      "fix_target": "template|generator|both",
      "suggested_approach": "specific suggestion pointing at cohort, field, or code section"
    }
  ]
}

Set approved=true and issues=[] when the population is clinically accurate and \
believable. Prioritize CRITICAL and HIGH issues; include MEDIUM/LOW only when \
no higher-severity issues exist. Be specific and actionable — vague findings \
are not useful.
"""


def review_with_openai(package: str, model: str, base_url: str) -> dict:
    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: pip3 install openai", file=sys.stderr)
        sys.exit(2)

    if base_url and "openai.com" not in base_url:
        client = OpenAI(base_url=base_url, api_key="none")
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            print("ERROR: OPENAI_API_KEY environment variable not set.", file=sys.stderr)
            sys.exit(2)
        client = OpenAI(api_key=api_key)

    pkg_tokens_est = len(package) // 4
    print(f"  Sending review package to {model} (~{pkg_tokens_est:,} tokens)...", flush=True)

    resp = client.chat.completions.create(
        model=model,
        temperature=0.1,
        max_tokens=4096,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
            {"role": "user",   "content": package},
        ],
    )
    raw = resp.choices[0].message.content
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Wrap raw text as a single unstructured issue
        return {
            "approved": False,
            "overall_assessment": raw[:500],
            "issues": [{"id": 1, "severity": "HIGH", "category": "other",
                        "title": "Unparseable review response",
                        "description": raw[:1000], "evidence": "", "fix_target": "both",
                        "suggested_approach": "Review raw response manually."}],
        }


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------

def _log(changelog_path: Path, entry: dict) -> None:
    with open(changelog_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clinical QA review for synthetic patient populations"
    )
    parser.add_argument("--template", required=True,
                        help="Template JSON file (e.g. fl_demo.template.json)")
    parser.add_argument("--output", required=True,
                        help="Population output directory (e.g. population-fl_1000)")
    parser.add_argument("--generate", action="store_true",
                        help="Run generate_population.py before reviewing")
    parser.add_argument("--count", type=int, default=0,
                        help="Patient count override (0 = use template default)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"OpenAI model (default: {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default="",
                        help="OpenAI-compatible base URL (omit for openai.com)")
    parser.add_argument("--changelog", default=CHANGELOG_FILE,
                        help=f"Changelog file path (default: {CHANGELOG_FILE})")
    args = parser.parse_args()

    output_dir      = Path(args.output)
    changelog_path  = Path(args.changelog)

    # ── Optionally generate ──────────────────────────────────────────────────
    if args.generate:
        cmd = [sys.executable, "generate_population.py",
               "--template", args.template, "--output", args.output,
               "--mode", "template"]
        if args.count:
            cmd += ["--count", str(args.count)]
        print(f"Generating: {' '.join(cmd)}")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print("ERROR: generation failed.", file=sys.stderr)
            sys.exit(2)

    if not output_dir.exists():
        print(f"ERROR: output directory not found: {output_dir}", file=sys.stderr)
        sys.exit(2)

    # ── Build review package ─────────────────────────────────────────────────
    print("Building clinical review package...")
    package = build_review_package(output_dir, args.template)
    pkg_path = output_dir / "qa_review_package.txt"
    pkg_path.write_text(package, encoding="utf-8")
    print(f"  Package: {len(package):,} chars saved to {pkg_path}")

    # ── OpenAI review ────────────────────────────────────────────────────────
    review   = review_with_openai(package, args.model, args.base_url)
    approved = review.get("approved", False)
    issues   = review.get("issues", [])
    summary  = review.get("overall_assessment", "")

    # ── Write qa_issues.json ─────────────────────────────────────────────────
    issues_payload = {
        "timestamp":          datetime.now().isoformat(),
        "template":           args.template,
        "output":             args.output,
        "model":              args.model,
        "approved":           approved,
        "overall_assessment": summary,
        "issues":             issues,
    }
    issues_path = output_dir / ISSUES_FILE
    issues_path.write_text(json.dumps(issues_payload, indent=2), encoding="utf-8")

    # ── Append to changelog ──────────────────────────────────────────────────
    _log(changelog_path, {
        "timestamp":          datetime.now().isoformat(),
        "event":              "review_complete",
        "template":           args.template,
        "output":             args.output,
        "model":              args.model,
        "approved":           approved,
        "issue_count":        len(issues),
        "overall_assessment": summary,
    })

    # ── Print summary ────────────────────────────────────────────────────────
    width = 65
    print()
    print("=" * width)
    if approved:
        print("  APPROVED — population passes clinical review")
    else:
        print(f"  ISSUES FOUND — {len(issues)} issue(s) require attention")
    print()
    print(f"  {summary[:200]}")
    print()
    if issues:
        for iss in issues:
            sev   = iss.get("severity", "")
            title = iss.get("title", "")
            tgt   = iss.get("fix_target", "")
            lines = [f"  [{sev:<8}] {title}  (fix: {tgt})"]
            evid  = iss.get("evidence", "")
            if evid:
                lines.append(f"             Evidence: {evid[:80]}")
            lines.append(f"             Approach: {iss.get('suggested_approach','')[:80]}")
            print("\n".join(lines))
        print()
    print(f"  Full results  → {issues_path}")
    print(f"  Changelog     → {changelog_path}")
    print("=" * width)

    sys.exit(0 if approved else 1)


if __name__ == "__main__":
    main()
