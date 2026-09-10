"""
sync_catalog.py — rebuild clinical_catalog entries in config/cohort_catalog.json
from their source template files.

Called automatically by the pre-commit hook when any *.template.json is staged.
Also safe to run manually: python3 scripts/sync_catalog.py
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATH = BASE_DIR / "config" / "cohort_catalog.json"
TEMPLATES_DIR = BASE_DIR / "templates"

CLINICAL_KEYS = [
    "diagnoses", "comorbidities", "medications", "labs",
    "observations", "procedures", "encounter_pattern",
]


def main() -> int:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    updated = 0
    errors = 0

    for cohort_id, entry in catalog["cohorts"].items():
        src = entry.get("source_template")
        if not src:
            continue

        tpl_path = TEMPLATES_DIR / src
        if not tpl_path.exists():
            print(f"  WARN: template not found for {cohort_id}: {tpl_path}", file=sys.stderr)
            continue

        try:
            tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ERROR reading {tpl_path}: {exc}", file=sys.stderr)
            errors += 1
            continue

        cohorts = tpl.get("cohorts", [])
        # Match by cohort id, then fall back to first cohort
        cohort = next((c for c in cohorts if c.get("id") == cohort_id), None)
        if cohort is None and cohorts:
            cohort = cohorts[0]
        if cohort is None:
            continue

        clinical = {k: cohort[k] for k in CLINICAL_KEYS if k in cohort}
        if entry.get("clinical_catalog") != clinical:
            entry["clinical_catalog"] = clinical
            updated += 1

    CATALOG_PATH.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"sync_catalog: {updated} cohort(s) updated, {errors} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
