"""
Fix cleared for_diagnosis_code on protected medications in a template file.

Usage:
    python scripts/fix_med_gates.py templates/fl_population.template.json

Scans all cohort medications and restores the diagnosis gate for any
protected drug that had its for_diagnosis_code cleared to "" or removed.
"""
import json
import sys

# Protected drug → correct ICD-10 prefix to restore
_PROTECTED = {
    "metformin":     "E11",
    "glipizide":     "E11",
    "glimepiride":   "E11",
    "glyburide":     "E11",
    "sitagliptin":   "E11",
    "empagliflozin": "E11",
    "dapagliflozin": "E11",
    "semaglutide":   "E11",
    "liraglutide":   "E11",
    "insulin":       "E1",   # covers E10 and E11
    "warfarin":      "I48",  # AF anticoagulation (most common indication)
    "rivaroxaban":   "I48",
    "apixaban":      "I48",
    "dabigatran":    "I48",
}


def _match(drug_description: str) -> str | None:
    desc = drug_description.lower()
    for keyword, prefix in _PROTECTED.items():
        if keyword in desc:
            return prefix
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/fix_med_gates.py <template.json>")
        sys.exit(1)

    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        tmpl = json.load(f)

    fixed = []
    for ci, cohort in enumerate(tmpl.get("cohorts", [])):
        for mi, med in enumerate(cohort.get("medications", [])):
            fdc = med.get("for_diagnosis_code", None)
            if fdc is None or fdc == "":
                correct = _match(med.get("drug_description", ""))
                if correct:
                    med["for_diagnosis_code"] = correct
                    fixed.append(
                        f"  cohorts[{ci}] {cohort.get('name','?')} | "
                        f"{med.get('drug_description','?')} -> for_diagnosis_code={repr(correct)}"
                    )

    if not fixed:
        print("No cleared gates found for protected medications.")
        return

    print(f"Restoring {len(fixed)} cleared gate(s):")
    for line in fixed:
        print(line)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(tmpl, f, indent=2)
    print(f"\nSaved: {path}")


if __name__ == "__main__":
    main()
