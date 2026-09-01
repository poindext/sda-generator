#!/usr/bin/env python3
"""
generate_cohort_templates.py — Produce a standalone {cohort_id}.template.json
for every cohort that currently points at ohio_demo.template.json or a missing
template file.

Each output template is self-contained:
  - Ohio infrastructure (geography, demographics, facilities, providers,
    insurance_plans, sending_facility, name_pools, catalogs) copied from
    ohio_demo.template.json.
  - Cohort demographic fields (min_age, max_age, sex_bias, weight) pulled
    from the ohio_demo cohort block (or from defaults when absent).
  - Clinical content (diagnoses, comorbidities, medications, labs, etc.)
    pulled from clinical_catalog in cohort_catalog.json.

After writing templates, updates cohort_catalog.json so that each cohort's
source_template points at its new individual file.

Usage:
    python3 scripts/generate_cohort_templates.py             # write all
    python3 scripts/generate_cohort_templates.py --dry-run   # report only
    python3 scripts/generate_cohort_templates.py --cohorts diabetes_t2 stroke
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CATALOG_PATH   = ROOT / "config" / "cohort_catalog.json"
OHIO_TMPL_PATH = ROOT / "templates" / "ohio_demo.template.json"
TEMPLATES_DIR  = ROOT / "templates"
COHORTS_DIR    = TEMPLATES_DIR / "cohorts"

# Templates that trigger extraction (either shared or missing)
EXTRACT_SOURCES = {"ohio_demo.template.json", "template.template.json"}

# Clinical fields that live inside the cohort block
CLINICAL_FIELDS = [
    "diagnoses", "comorbidities", "medications", "labs",
    "observations", "procedures", "encounter_pattern",
    "document_templates", "illness_history_templates",
    "social_history_templates", "family_history_templates",
    "rad_order_templates",
]

# Cohort-level flags to copy if present
FLAG_FIELDS = [
    "ckd_progression", "initial_egfr_min", "initial_egfr_max",
    "progression_trajectories",
]

# Maps catalog key → ohio_demo cohort name, for cohorts without an id field
NAME_OVERRIDES = {
    "htn_type_2_diabetes": "HTN + Type 2 Diabetes",
    "low_risk_preventive": "Low Risk Preventive",
}

# Demographic defaults when ohio_demo block has None
DEFAULT_MIN_AGE = 18
DEFAULT_MAX_AGE = 85


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def build_ohio_index(ohio_tmpl: dict) -> dict[str, dict]:
    """Return {cohort_id_or_name → cohort_block} for all ohio_demo cohorts."""
    index: dict[str, dict] = {}
    for c in ohio_tmpl.get("cohorts", []):
        cid = c.get("id")
        if cid:
            index[cid] = c
        else:
            # Index by name for cohorts without id
            index[c.get("name", "")] = c
    return index


def build_template(
    catalog_key: str,
    catalog_entry: dict,
    ohio_index: dict[str, dict],
    ohio_tmpl: dict,
) -> dict:
    """Assemble a complete standalone template for one cohort."""
    cc = catalog_entry.get("clinical_catalog", {})

    # Find the matching ohio_demo block for demographic fields
    ohio_block = ohio_index.get(catalog_key)
    if ohio_block is None:
        # Try the name override
        override_name = NAME_OVERRIDES.get(catalog_key, "")
        ohio_block = ohio_index.get(override_name, {})

    min_age  = ohio_block.get("min_age") or DEFAULT_MIN_AGE
    max_age  = ohio_block.get("max_age") or DEFAULT_MAX_AGE
    sex_bias = ohio_block.get("sex_bias")
    weight   = ohio_block.get("weight", 1.0)
    description = ohio_block.get("description", catalog_entry.get("canonical_name", catalog_key))

    canonical = catalog_entry.get("canonical_name", catalog_key)

    # Build cohort block
    cohort_block: dict = {
        "id":       catalog_key,
        "name":     canonical,
        "weight":   1.0,  # single-cohort template — full weight
        "min_age":  min_age,
        "max_age":  max_age,
        "sex_bias": sex_bias,
    }
    # Merge clinical content from clinical_catalog
    for field in CLINICAL_FIELDS + FLAG_FIELDS:
        if field in cc:
            cohort_block[field] = cc[field]

    # Build meta section
    meta = {
        "name":               f"{canonical} — Synthetic Population",
        "description":        description,
        "state":              "Ohio",
        "state_code":         "OH",
        "medicaid_field":     "Ohio Medicaid",
        "medicaid_id_prefix": "OH-MEDICAID-",
        "total_patients":     200,
        "history_months":     24,
    }

    # Build the full template by copying infrastructure from ohio_demo
    template = {
        "meta":            meta,
        "geography":       ohio_tmpl.get("geography", {}),
        "demographics":    ohio_tmpl.get("demographics", {}),
        "facilities":      ohio_tmpl.get("facilities", []),
        "providers":       ohio_tmpl.get("providers", []),
        "insurance_plans": ohio_tmpl.get("insurance_plans", []),
        "sending_facility":ohio_tmpl.get("sending_facility", {}),
        "multi_facility":  ohio_tmpl.get("multi_facility", False),
        "name_pools":      ohio_tmpl.get("name_pools", {}),
        "cohorts":         [cohort_block],
        "catalogs":        ohio_tmpl.get("catalogs", {}),
    }
    return template


def run(dry_run: bool = False, only: list[str] | None = None) -> None:
    catalog   = load_json(CATALOG_PATH)
    ohio_tmpl = load_json(OHIO_TMPL_PATH)
    ohio_index = build_ohio_index(ohio_tmpl)

    written  = []
    skipped  = []

    for key, entry in catalog["cohorts"].items():
        if only and key not in only:
            continue

        src = entry.get("source_template", "")
        tmpl_path = TEMPLATES_DIR / src if src else None

        # Skip cohorts that already have their own working template
        if src and src not in EXTRACT_SOURCES and tmpl_path and tmpl_path.exists():
            print(f"  [SKIP]  {key}: already has own template ({src})")
            skipped.append(key)
            continue

        out_name = f"cohorts/{key}.template.json"
        out_path = COHORTS_DIR / f"{key}.template.json"

        template = build_template(key, entry, ohio_index, ohio_tmpl)

        cohort_block = template["cohorts"][0]
        dx_count   = len(cohort_block.get("diagnoses", []))
        meds_count = len(cohort_block.get("medications", []))
        labs_count = len(cohort_block.get("labs", []))
        print(
            f"  [{'DRY' if dry_run else 'WRITE'}]  {key:<28} → {out_name}"
            f"  dx:{dx_count}  meds:{meds_count}  labs:{labs_count}"
            f"  age:{cohort_block['min_age']}-{cohort_block['max_age']}"
            + (f"  sex:{cohort_block['sex_bias']}" if cohort_block.get("sex_bias") else "")
        )

        if not dry_run:
            COHORTS_DIR.mkdir(exist_ok=True)
            save_json(out_path, template)
            # Update catalog source_template
            entry["source_template"] = out_name
            written.append(key)

    if not dry_run and written:
        save_json(CATALOG_PATH, catalog)
        print(f"\nWrote {len(written)} template(s) and updated cohort_catalog.json.")
        print("Skipped (already have own template):", len(skipped))
        print("\nNext step:")
        print("  python3 scripts/regression_test.py")
    elif dry_run:
        print(f"\nDry run complete. {len([k for k in catalog['cohorts'] if catalog['cohorts'][k].get('source_template','') in EXTRACT_SOURCES or not (TEMPLATES_DIR / catalog['cohorts'][k].get('source_template','')).exists()])} cohort(s) would be written.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--cohorts", nargs="+", metavar="KEY", help="Limit to specific cohort keys")
    args = parser.parse_args()
    run(dry_run=args.dry_run, only=args.cohorts)
