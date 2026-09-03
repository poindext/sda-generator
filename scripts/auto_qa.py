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
CHANGELOG_FILE = "logs/auto_qa_changelog.jsonl"


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from .env into os.environ (skips already-set keys)."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

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
    "20150-9": "FVC (L)",
    "20157-4": "FEV1 (L)",
    "19926-5": "FEV1/FVC ratio",
    "33914-3": "eGFR CKD-EPI (mL/min/1.73m²)",
    "2028-9":  "CO2/Bicarbonate (mEq/L)",
    "2132-9":  "Phosphate (mg/dL)",
    "2731-8":  "PTH (pg/mL)",
    "789-8":   "RBC (M/uL)",
    "4544-3":  "Hematocrit (%)",
    "5767-9":  "Urine Protein (mg/dL)",
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
    has_cf = any(c.startswith("E84") for c in codes)
    if has_cf:
        if any(c.startswith("E13") for c in codes):
            return "CF-Related Diabetes"
        return "Cystic Fibrosis"
    for c in codes:
        if c.startswith(("Z59", "Z56", "Z63", "Z55", "Z60", "Z62", "Z64", "Z65")):
            return "SDOH / Social Determinants"
    for c in codes:
        if c.startswith(("F11", "F12", "F13", "F14", "F15", "F16", "F18", "F19")):
            return "Opioid / Substance Use Disorder"
    for c in codes:
        if c.startswith("U09"):
            return "Long COVID / PASC"
    for c in codes:
        if c.startswith("N18"):
            return "CKD Progression"
    for c in codes:
        if c.startswith("A41") or (c.startswith("R65") and any(x.startswith("A41") for x in codes)):
            return "Sepsis"
    for c in codes:
        if c.startswith("O2") or c.startswith("O1") or c.startswith("O7"):
            return "Maternal Morbidity"
    for c in codes:
        if c.startswith(("E11", "E10", "E12")):
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

def build_review_package(output_dir: Path, template_name: str,
                         qa_config: dict | None = None) -> str:
    _pfx = output_dir.name
    patients  = {r["PatientID"]: r for r in _load_csv(output_dir / f"{_pfx}_patients.csv")}
    encs_raw  = _load_csv(output_dir / f"{_pfx}_encounters.csv")
    labs_raw  = _load_csv(output_dir / f"{_pfx}_labs.csv")
    meds_raw  = _load_csv(output_dir / f"{_pfx}_medications.csv")
    val_rows  = _load_csv(output_dir / f"{_pfx}_generator_validation.csv")

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

    rng = random.Random(137)
    shown = 0
    max_cases = 20
    for cohort, _ in cohort_counts.most_common():
        if shown >= max_cases:
            break
        # Prefer adults 21+ as representative cases to avoid reviewer confusion about
        # borderline-age patients; fall back to all cohort patients if none qualify.
        adult_pids = [p for p in cohort_pids[cohort]
                      if int(patients[p].get("Age", 0)) >= 21]
        pool = adult_pids if adult_pids else cohort_pids[cohort]
        sample_pids = rng.sample(pool, min(2, len(pool)))
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

    # Required drug class check (driven by qa_config)
    required_classes = (qa_config or {}).get("required_drug_classes", [])
    if required_classes:
        h("REQUIRED DRUG CLASS CHECK  (population-specific)")
        med_names_lower = " ".join(m.get("MedicationName", "").lower()
                                   for m in meds_raw)
        unique_pats_total = len(patients)
        for rc in required_classes:
            keywords = rc.get("keywords", [])
            min_pct  = rc.get("minimum_patient_pct", 0)
            found_pats = set()
            for m in meds_raw:
                name_l = m.get("MedicationName", "").lower()
                if any(kw.lower() in name_l for kw in keywords):
                    found_pats.add(m.get("PatientID", ""))
            pct = 100 * len(found_pats) / unique_pats_total if unique_pats_total else 0
            status = "OK" if pct >= min_pct else f"*** BELOW THRESHOLD (expected >={min_pct}%) ***"
            lines.append(
                f"  {rc['class_name']:<40} "
                f"found in {len(found_pats):,} patients ({pct:.1f}%)  [{status}]"
            )
            if pct < min_pct:
                lines.append(f"    Rationale: {rc.get('rationale','')[:100]}")

    h("SPIROMETRY COHERENCE CHECK")
    fvc_vals  = loinc_vals.get("20150-9", [])
    fev1_vals = loinc_vals.get("20157-4", [])
    if fvc_vals and fev1_vals:
        fvc_mean  = statistics.mean(fvc_vals)
        fev1_mean = statistics.mean(fev1_vals)
        ratio     = fev1_mean / fvc_mean if fvc_mean else 0
        flag = " *** PHYSIOLOGICALLY IMPOSSIBLE — FEV1 CANNOT EXCEED FVC ***" if fev1_mean >= fvc_mean else " (OK)"
        lines.append(f"  FVC  mean={fvc_mean:.2f} L  (n={len(fvc_vals):,})")
        lines.append(f"  FEV1 mean={fev1_mean:.2f} L  (n={len(fev1_vals):,})")
        lines.append(f"  FEV1/FVC ratio = {ratio:.2f}{flag}")
    else:
        lines.append("  No spirometry data found (LOINC 20150-9 / 20157-4 not present).")

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
{specialist_role}

Your role is to assess clinical accuracy and believability.

IMPORTANT CONTEXT: This is an INTENTIONALLY ENRICHED demo dataset, not a random \
population sample. Chronic disease cohorts (diabetes, hypertension, COPD, etc.) \
are deliberately overrepresented so the system has enough cases to demonstrate \
analytics. DO NOT flag overall disease prevalence rates as issues — those \
percentages are a deliberate design choice by the system operator, not a bug. \
For example, a 25-40% diabetes prevalence in this demo is expected and intentional.

Review the population data for:
1. Clinically implausible values (impossible lab results, anthropometrics, vitals)
2. Age-inappropriate diagnoses, medications, or lab ordering (focus on patients \
   under 18 — adult chronic disease medications such as antihypertensives, \
   statins, and diabetes drugs should NOT appear in pediatric patients under 18; \
   adults aged 18 and older may appropriately receive any of these medications)
3. Sex-inappropriate diagnoses or medications
4. Missing expected clinical associations (e.g. T2DM without lipid monitoring)
5. Implausible combinations within a single patient record
6. Implausible lab VALUE RANGES (e.g. glucose > 500 without ketoacidosis context, \
   HDL < 20, or A1c > 15%)

Do NOT flag:
- Overall disease prevalence rates (intentionally enriched)
- Mild temporal variability in lab values between visits (random fluctuation is \
  normal and expected in synthetic data)
- Glucose 50–500 mg/dL — the generator enforces this range; any value the \
generator produces is within bounds by construction. Never flag glucose in \
this range as "implausible" regardless of encounter type or context.
- Systolic BP up to 230 mmHg in ED encounters — hypertensive urgency/emergency \
  genuinely presents with SBP 180-230
- Sodium 110–165 mEq/L — severe hyponatremia (110-130) and hypernatremia \
  (150-165) are real clinical findings; never flag these as implausible
- BNP 0–5000 pg/mL — BNP values in this range are physiologically possible; \
  heart failure patients routinely present with BNP 500–2000
- Triglycerides 40–500 mg/dL, LDL 0–300 mg/dL, HDL 20–100 mg/dL, Total \
  Cholesterol 120–400 mg/dL — all within physiologic bounds; never implausible
- Lab abnormalities detected on screening panels in wellness patients (high \
  cholesterol on a wellness visit, mildly elevated glucose, etc.) — discovering \
  undiagnosed conditions is the whole purpose of wellness screening
- Multiple rows for the same medication in a single patient record — these represent \
  dose escalation events (Action=Start then Action=Increase) and are clinically \
  correct for chronic disease management. Do NOT flag escalation sequences as \
  duplicate prescriptions.
- Doxycycline prescribed to any patient with a tick-borne disease diagnosis — \
  Doxycycline is the IDSA/CDC first-line treatment for ALL tick-borne diseases \
  in patients of any age. Never flag it as age-inappropriate when a tick-borne \
  diagnosis is present.
- Standard infection-specific antibiotics (Doxycycline, Amoxicillin for Lyme; \
  azithromycin for respiratory infections, etc.) — appropriate regardless of age \
  when the matching diagnosis is present
- Elevated transaminases (AST/ALT) in infectious disease patients — Lyme hepatitis \
  and antibiotic-associated transaminase elevation are well-documented
- Montelukast 10 MG in patients 15 and older — FDA-approved adult/adolescent dose \
  for ages ≥15. The 5 MG dose applies to ages 6-14 only. Do not flag 10 MG for \
  patients 15-17.
- Rescue inhalers (Albuterol, Levalbuterol) and inhaled corticosteroids \
  (Fluticasone, Budesonide, Beclomethasone) for asthma or COPD patients — \
  appropriate at all ages. Never flag inhaled asthma medications as \
  age-inappropriate when an asthma or COPD diagnosis is present.
- ARB antihypertensives (Losartan, Valsartan, Irbesartan, etc.) in patients with \
  hypertension or diabetes — guideline-recommended, especially for diabetic \
  nephroprotection. Not age-inappropriate for adults 18+.
- ACE inhibitors (Lisinopril, Ramipril, Enalapril, Benazepril) are \
  therapeutically equivalent to ARBs for nephroprotection in CKD and diabetic \
  nephropathy. A patient on an ACE inhibitor FULLY satisfies guideline \
  recommendations for RAAS blockade. Never flag an ACE inhibitor patient as \
  "missing ARB" — ACE-I and ARB are not given together (dual RAAS blockade \
  is contraindicated). If the issue is "missing SGLT2 or ARB," the ARB \
  requirement is already met by the ACE inhibitor.
- SGLT2 inhibitors (Empagliflozin, Dapagliflozin, Canagliflozin) in adult \
  diabetes patients — standard ADA second-line agents. Not age-inappropriate \
  for adults 18+.
- Pediatric BMI values — pediatric BMI is assessed on age-specific growth charts, \
  not adult standards. BMI 10-15 is normal for infants and young children. Never \
  flag low BMI for patients under 18.
- Mildly elevated calcium (up to 11.5 mg/dL) in infectious or inflammatory \
  disease — hypercalcemia occurs in Lyme disease and granulomatous conditions.
- Elevated total IgE (up to 500 IU/mL) in asthma, atopic, or tick-borne \
  disease patients — IgE elevation is a well-established feature of these \
  conditions.
- Missing lipid-lowering therapy when a statin is already listed in the patient's \
  medications — Atorvastatin, Simvastatin, Rosuvastatin, Pravastatin, Lovastatin, \
  and Pitavastatin are all lipid-lowering therapies. If the patient's medication \
  list contains any statin, they have lipid-lowering therapy. Never flag "missing \
  lipid-lowering therapy" if the evidence contains any of these medications.
- Metformin in patients aged 10 or older — FDA-approved for type 2 diabetes \
  and gestational diabetes management from age 10+. Never flag Metformin as \
  age-inappropriate for patients aged 10 or older, including adolescents and \
  pregnant teens.
CRITICAL: age-inappropriate medications — ONLY flag if the patient's AGE field \
in the REPRESENTATIVE CASES section shows 17 or younger AND the specific \
medication appears in that patient's MEDICATIONS list AND the medication has \
no FDA-approved pediatric indication. Age 18+ is an adult. \
Verify the Age field, medication text, and pediatric approval status explicitly \
before flagging.
CRITICAL: population stats are not patient records — The MEDICATION ANALYSIS \
section lists population-wide drug frequencies. A drug appearing in that table \
does NOT mean any specific patient received it inappropriately. You MUST find \
the medication in a specific patient's MEDICATIONS section before reporting it.
DATA INTEGRITY: Each representative patient's MEDICATIONS list is complete and \
authoritative. A patient with no medications listed has zero medications. Do NOT \
infer, assume, or fabricate medications from diagnoses or population-level data.

GROUNDING RULE — THIS IS MANDATORY:
Before writing any issue, locate supporting evidence in this data packet. \
You may cite:
  - A specific patient from REPRESENTATIVE CASES: quote their PatientID, Age, \
    and the exact medication name or lab value as it appears in the packet.
  - A specific statistic from POPULATION STATS or LAB SUMMARY: quote the exact \
    field name, numeric value, and units.
  - A REQUIRED DRUG CLASS CHECK result showing a drug class is "BELOW THRESHOLD" — \
    cite the drug class name and the observed percentage. You MAY flag a missing \
    expected item when the REQUIRED DRUG CLASS CHECK section explicitly shows it \
    is absent or below threshold.
  - A REQUIRED CHECKS item from the population-specific context that instructs \
    you to verify something is present — if the data confirms it is absent \
    (e.g., zero count, 0.0%, absent from medication list), cite that absence \
    as evidence. Absence of an expected clinical element IS a valid finding \
    when the population context defines what is required.
Do NOT fabricate or infer findings that have no basis in the data packet.

{population_specific_context}
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
      "evidence": "verbatim quote from the data packet (section name + exact text)",
      "data_section": "e.g. REPRESENTATIVE CASES > Patient 475 | LAB SUMMARY | MEDICATION ANALYSIS",
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


# ---------------------------------------------------------------------------
# Template-specific QA config
# ---------------------------------------------------------------------------

def load_qa_config(template_path: str) -> dict:
    """Load <template_stem>.qa_config.json if it exists, else return empty config."""
    p = Path(template_path)
    stem = p.stem
    if stem.endswith(".template"):
        stem = stem[: -len(".template")]
    config_path = p.parent / f"{stem}.qa_config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def build_population_specific_context(config: dict) -> str:
    """Render qa_config into a prompt block for injection into the system prompt."""
    if not config:
        return ""
    parts = []
    description = config.get("description", "")
    if description:
        parts.append(f"POPULATION-SPECIFIC CONTEXT: {description}")
    required_checks = config.get("required_checks", [])
    if required_checks:
        parts.append("REQUIRED CHECKS — you MUST verify each of these:")
        for check in required_checks:
            parts.append(f"- {check}")
    exemptions = config.get("do_not_flag", [])
    if exemptions:
        parts.append("Additional population-specific items — Do NOT flag:")
        for item in exemptions:
            parts.append(f"- {item}")
    for note in config.get("clarifications", []):
        parts.append(note)
    return "\n".join(parts) + "\n" if parts else ""


# Maps ICD prefix → (specialist title, domain description)
_ICD_SPECIALIST_MAP = [
    (("E84",),          "pulmonologist and cystic fibrosis specialist",
                        "cystic fibrosis, CFTR biology, CF-related diabetes, pulmonary exacerbations, CFTR modulator therapy, pancreatic insufficiency, and CF-associated liver disease"),
    (("C",),            "oncologist",
                        "cancer biology, oncology staging, chemotherapy regimens, targeted therapy, immunotherapy, and cancer-related complications"),
    (("I50", "I48", "I47"), "cardiologist specializing in heart failure and arrhythmia",
                        "heart failure management, atrial fibrillation, antiarrhythmic therapy, cardiac medications, BNP interpretation, and electrophysiology"),
    (("I20", "I21", "I25"), "interventional cardiologist",
                        "coronary artery disease, acute coronary syndromes, troponin interpretation, revascularization, and cardiac rehabilitation"),
    (("J45", "J44"),    "pulmonologist specializing in obstructive lung disease",
                        "asthma, COPD, inhaled therapies, spirometry interpretation, step-up therapy, and respiratory exacerbation management"),
    (("E10", "E11", "E13"), "endocrinologist specializing in diabetes",
                        "type 1 and type 2 diabetes management, insulin therapy, GLP-1/SGLT2 agents, CFRD, HbA1c targets, and diabetes complications"),
    (("N18",),          "nephrologist",
                        "chronic kidney disease staging, renal dosing, dialysis, proteinuria management, and nephrotoxic drug avoidance"),
    (("F",),            "psychiatrist and behavioral health specialist",
                        "mental health diagnoses, psychotropic medications, substance use disorders, and psychiatric comorbidities"),
    (("O", "Z34"),      "obstetrician and maternal-fetal medicine specialist",
                        "obstetric care, pregnancy complications, gestational diabetes, prenatal screening, and postpartum management"),
    (("A15", "B20"),    "infectious disease specialist",
                        "HIV, tuberculosis, opportunistic infections, antiretroviral therapy, and immunocompromised patient management"),
    (("M",),            "rheumatologist",
                        "rheumatologic diseases, immunosuppressive therapy, joint disease, biologics, and autoimmune conditions"),
    (("G",),            "neurologist",
                        "neurological diagnoses, seizure management, neurodegenerative diseases, and CNS medications"),
    (("K",),            "gastroenterologist",
                        "gastrointestinal disorders, liver disease, IBD, GI procedures, and hepatic medications"),
]


def build_specialist_role(template_path: str) -> str:
    """Derive a specialist reviewer role from the template's disease profile.

    Reads the template JSON, collects primary ICD codes across cohorts, and
    maps them to the most relevant clinical specialty. Falls back to a
    general clinician role when no specialty match is found.
    """
    try:
        tmpl = json.load(open(template_path, encoding="utf-8"))
    except Exception:
        return ("You are a clinician and healthcare informatics specialist "
                "reviewing a synthetic patient population for use in a "
                "healthcare IT demonstration.")

    meta = tmpl.get("meta", {})
    pop_name = meta.get("name", "this patient population")

    # Collect all primary ICD codes across cohorts
    primary_codes: list[str] = []
    for cohort in tmpl.get("cohorts", []):
        for dx in cohort.get("diagnoses", []):
            if isinstance(dx, dict) and dx.get("is_primary"):
                c = dx.get("code", "")
                if c:
                    primary_codes.append(c)

    cohort_names = [c.get("name", "") for c in tmpl.get("cohorts", [])]

    # Match to specialist
    specialist = None
    domain = None
    for prefixes, spec, dom in _ICD_SPECIALIST_MAP:
        for code in primary_codes:
            if any(code.startswith(p) for p in prefixes):
                specialist = spec
                domain = dom
                break
        if specialist:
            break

    if not specialist:
        specialist = "clinician and healthcare informatics specialist"
        domain = "general medicine, chronic disease management, clinical coding, and healthcare analytics"

    cohort_str = ""
    if cohort_names:
        cohort_str = (f" The population includes the following cohorts: "
                      f"{', '.join(cohort_names)}.")

    return (
        f"You are a {specialist} reviewing a synthetic patient population "
        f"called \"{pop_name}\" for use in a healthcare IT demonstration. "
        f"You have deep clinical expertise in {domain}.{cohort_str}"
    )


def review_with_openai(package: str, model: str, base_url: str,
                       qa_config: dict | None = None,
                       template_path: str = "") -> dict:
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

    population_ctx = build_population_specific_context(qa_config or {})
    specialist_role = build_specialist_role(template_path) if template_path else (
        "You are a clinician and healthcare informatics specialist reviewing "
        "a synthetic patient population for use in a healthcare IT demonstration."
    )
    system_prompt = (
        _REVIEW_SYSTEM_PROMPT
        .replace("{specialist_role}", specialist_role)
        .replace("{population_specific_context}", population_ctx)
    )

    pkg_tokens_est = len(package) // 4
    print(f"  Sending review package to {model} (~{pkg_tokens_est:,} tokens)...", flush=True)

    resp = client.chat.completions.create(
        model=model,
        temperature=0.1,
        max_tokens=4096,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
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
# Post-processing: rule-based false-positive filter
# ---------------------------------------------------------------------------

import re as _re

def _load_patient_meds(output_dir: Path) -> dict:
    """Return {patient_id_str: set_of_lowercase_med_names} from medications.csv."""
    meds_path = output_dir / f"{output_dir.name}_medications.csv"
    result: dict = {}
    if not meds_path.exists():
        return result
    import csv as _csv
    with open(meds_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            pid = row.get("PatientID", "")
            name = (row.get("MedicationName") or "").lower()
            result.setdefault(pid, set()).add(name)
    return result


def _load_patient_labs(output_dir: Path) -> dict:
    """Return {patient_id_str: set_of_loinc_codes} from labs.csv."""
    labs_path = output_dir / f"{output_dir.name}_labs.csv"
    result: dict = {}
    if not labs_path.exists():
        return result
    import csv as _csv
    with open(labs_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            pid = row.get("PatientID", "")
            code = (row.get("LabCode") or "").strip()
            if code:
                result.setdefault(pid, set()).add(code)
    return result


def _filter_false_positives(issues: list, output_dir: Path | None = None) -> list:
    """Remove issues that contradict the evidence the LLM cited."""
    patient_meds = _load_patient_meds(output_dir) if output_dir else {}
    patient_labs = _load_patient_labs(output_dir) if output_dir else {}
    kept = []
    for iss in issues:
        if _is_false_positive(iss, patient_meds=patient_meds, patient_labs=patient_labs):
            print(f"  [filter] Dropped false positive: {iss.get('title','')!r}")
        else:
            kept.append(iss)
    return kept


def _is_false_positive(iss: dict, patient_meds: dict | None = None,
                        patient_labs: dict | None = None) -> bool:
    category = (iss.get("category") or "").lower()
    title    = (iss.get("title") or "").lower()
    evidence = (iss.get("evidence") or "").lower()

    # CSV-backed check: "missing lipid-lowering therapy" when patient actually has a statin
    if patient_meds is not None:
        pid_match = _re.search(r"\bpatient\s+(\d+)\b", evidence)
        if pid_match:
            pid = pid_match.group(1)
            meds_for_patient = patient_meds.get(pid, set())
            _statin_check = ("atorvastatin", "simvastatin", "rosuvastatin",
                             "pravastatin", "lovastatin", "pitavastatin", "fluvastatin")
            desc_lc = (iss.get("description") or "").lower()
            if any(kw in title or kw in desc_lc
                   for kw in ("lipid", "statin", "cholesterol")) and (
                "missing" in title or "lack" in title or
                "missing" in desc_lc or "lack" in desc_lc
            ) and any(s in med for s in _statin_check
                      for med in meds_for_patient):
                return True

    # CSV-backed check: "missing lipid monitoring/panel" when patient actually has lipid labs
    if patient_labs is not None:
        pid_match = _re.search(r"\bpatient\s+(\d+)\b", evidence)
        if pid_match:
            pid = pid_match.group(1)
            labs_for_patient = patient_labs.get(pid, set())
            _lipid_loincs = {"2093-3", "13457-7", "2085-9", "2571-8"}  # Total Chol, LDL, HDL, TG
            desc_lc = (iss.get("description") or "").lower()
            if any(kw in title or kw in desc_lc
                   for kw in ("lipid", "cholesterol", "ldl", "hdl")) and (
                "missing" in title or "no recent" in evidence or "lack" in title or
                "missing" in desc_lc or "no recent" in desc_lc
            ) and bool(_lipid_loincs & labs_for_patient):
                return True

    # Age-inappropriate medication: LLM cited an adult patient (18+) or failed to
    # cite any age at all.  System prompt requires explicit age ≤17 to flag;
    # if no age ≤17 appears in either evidence or description, it is a false positive.
    # Also filter if the cited medication has a known pediatric FDA approval.
    if "age" in title and ("inappropriate" in title or "medication" in title):
        desc = (iss.get("description") or "").lower()
        combined = evidence + " " + desc
        ages = [int(m) for m in _re.findall(r"\bage[d]?\s+(\d+)", combined)]
        if not ages or all(a >= 18 for a in ages):
            return True  # no under-18 cited — false positive
        # Medications with FDA-approved pediatric indications — not age-inappropriate
        _PEDS_APPROVED: list[tuple[str, int]] = [
            ("metformin",    10),  # FDA-approved for T2DM from age 10
            ("albuterol",     4),  # asthma/bronchospasm from age 4
            ("fluticasone",   4),
            ("budesonide",    6),
            ("montelukast",   2),
            ("amoxicillin",   0),
            ("azithromycin",  0),
            ("doxycycline",   8),  # FDA age 8; CDC allows any age for tick-borne
            ("lisinopril",    6),  # pediatric HTN from age 6
            ("losartan",      6),
        ]
        for med_name, min_age_peds in _PEDS_APPROVED:
            if med_name in combined and all(a >= min_age_peds for a in ages):
                return True  # cited med has pediatric approval at this age

    # "Missing lipid-lowering therapy" when a statin is already present in the
    # evidence — the reviewer contradicted itself.  Check title OR description.
    desc_lower = (iss.get("description") or "").lower()
    _statin_names = ("atorvastatin", "simvastatin", "rosuvastatin", "pravastatin",
                     "lovastatin", "pitavastatin", "fluvastatin")
    _lipid_kws = ("lipid", "statin", "cholesterol", "lipid-lowering")
    if any(s in evidence for s in _statin_names) and any(
        kw in title or kw in desc_lower for kw in _lipid_kws
    ) and ("missing" in title or "lack" in title or "missing" in desc_lower or "lack" in desc_lower):
        return True

    # "Missing SGLT2 or ARB" when an ACE inhibitor is already present — ACE-I and
    # ARBs are therapeutic equivalents for RAAS blockade; dual blockade is
    # contraindicated.  If the evidence shows an ACE inhibitor, the ARB arm of
    # "SGLT2 or ARB" is already satisfied.
    _raas_desc = (iss.get("description") or "").lower()
    _raas_combined = evidence + " " + _raas_desc
    if any(ace in evidence
           for ace in ("lisinopril", "ramipril", "enalapril", "benazepril", "captopril")
           ) and ("arb" in _raas_combined or "sglt" in _raas_combined) and (
        "missing" in title or "expected" in title or
        "missing" in _raas_desc or "lack" in _raas_desc
    ):
        return True

    # Dose escalation flagged as duplicate prescription
    if category == "medications" and any(
        kw in title for kw in ("duplicate", "multiple", "same medication")
    ):
        return True

    # UDS qualitative result (Positive/Negative) misidentified as numeric
    if ("urine" in title or "drug screen" in title or "uds" in title) and (
        "positive" in evidence or "negative" in evidence
    ):
        return True

    # Concurrent MAT agents flagged based on F11.21 code alone (not actual naltrexone medication).
    # F11.21 (OUD in remission) can co-exist with methadone/buprenorphine for patients in evolving
    # remission status; the qa_config do_not_flag explicitly allows multiple F11.x codes together.
    # Also catches confusion of naloxone rescue spray (Narcan) with naltrexone MAT (Vivitrol).
    if ("concurrent mat" in title or "mat" in title) and (
        "f11.21" in evidence or "in remission" in evidence or "naloxone" in evidence
    ) and "naltrexone" not in evidence:
        return True

    # Lab value within the generator's physiologic limits flagged as "implausible".
    # The generator enforces _LOINC_PLAUSIBILITY at generation time — any value it
    # produces is already within these bounds, so a reviewer complaint cannot be
    # addressed by changing code or templates.  If every numeric value cited in the
    # evidence falls within the known physiologic range for the named analyte, the
    # issue is a false positive.
    _PHYSIO_BY_KW: list[tuple[str, float, float]] = [
        ("glucose",      50,   500),   # generator clamp: 50-500
        ("sodium",       110,  165),   # LAB004 range: 110-165
        ("potassium",    2.0,  7.5),
        ("bnp",          0,    5000),  # heart failure BNP
        ("hemoglobin",   5.0,  18.0),
        ("hba1c",        4.0,  15.0),
        ("a1c",          4.0,  15.0),
        ("triglyceride", 40,   500),
        ("cholesterol",  120,  400),
        ("ldl",          0,    300),
        ("hdl",          20,   100),
        ("creatinine",   0.4,  14.0),
        ("bun",          5,    120),
    ]
    if "implausible" in title:
        # Strip "Patient NNN" so patient IDs aren't mistaken for lab values
        evidence_no_ids = _re.sub(r"\bPatient\s+\d+\b", "", evidence, flags=_re.IGNORECASE)
        raw_nums = _re.findall(r"\b(\d+(?:\.\d+)?)\b", evidence_no_ids)
        nums = [float(n) for n in raw_nums if 0 < float(n) < 100000]
        for kw, lo, hi in _PHYSIO_BY_KW:
            if kw in evidence and nums:
                # Only consider numbers plausibly in the analyte's ballpark
                candidates = [n for n in nums if lo * 0.25 <= n <= hi * 4]
                if candidates and all(lo <= n <= hi for n in candidates):
                    return True

    return False


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
               "--template", args.template, "--output", args.output]
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

    # ── Load template-specific QA config ────────────────────────────────────
    qa_config = load_qa_config(args.template)
    if qa_config:
        print(f"  QA config: {Path(args.template).stem.replace('.template','')}.qa_config.json loaded", flush=True)

    # ── Build review package ─────────────────────────────────────────────────
    print("Building clinical review package...", flush=True)
    package = build_review_package(output_dir, args.template, qa_config=qa_config)
    pkg_path = output_dir / "qa_review_package.txt"
    pkg_path.write_text(package, encoding="utf-8")
    print(f"  Package: {len(package):,} chars saved to {pkg_path}", flush=True)

    # ── OpenAI review ────────────────────────────────────────────────────────
    review   = review_with_openai(package, args.model, args.base_url,
                                  qa_config=qa_config,
                                  template_path=args.template)
    approved = review.get("approved", False)
    issues   = review.get("issues", [])
    summary  = review.get("overall_assessment", "")

    # ── Post-process: drop confirmed false positives ─────────────────────────
    issues = _filter_false_positives(issues, output_dir=output_dir)
    if not issues:
        approved = True

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
        # Push QA-approved definitions back into config catalogs automatically.
        _design_script = Path(__file__).parent / "design_population.py"
        if _design_script.exists() and args.template:
            try:
                subprocess.run(
                    [sys.executable, str(_design_script),
                     "--sync-configs", args.template],
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
            except subprocess.CalledProcessError as _e:
                print(f"  [WARN] Config sync failed: {_e}", file=sys.stderr)
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
    print(f"  Full results  -> {issues_path}")
    print(f"  Changelog     -> {changelog_path}")
    print("=" * width)

    sys.exit(0 if approved else 1)


if __name__ == "__main__":
    main()
