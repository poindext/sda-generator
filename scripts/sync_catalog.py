#!/usr/bin/env python3
"""
sync_catalog.py — Update cohort_catalog.json clinical_catalog sections
                  from their source template files.

Syncs: diagnoses, comorbidities, medications, labs, observations,
       encounter_pattern, and any cohort-level flags (e.g. ckd_progression).

Cohorts backed by ohio_demo.template.json are skipped (they're manually
curated and share a multi-cohort template with no 1:1 mapping).

Usage:
    python3 scripts/sync_catalog.py             # update in place
    python3 scripts/sync_catalog.py --dry-run   # report changes, no write
    python3 scripts/sync_catalog.py --check     # exit 1 if catalog is stale
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CATALOG_PATH = ROOT / "config" / "cohort_catalog.json"
TEMPLATES_DIR = ROOT / "templates"

# Fields extracted from a template cohort into clinical_catalog
CLINICAL_FIELDS = [
    "diagnoses",
    "comorbidities",
    "medications",
    "labs",
    "observations",
    "encounter_pattern",
    "procedures",
]

# Extra cohort-level flags that should also be reflected in clinical_catalog
# so downstream tooling (e.g. design_population.py) knows about special behaviour.
FLAG_FIELDS = [
    "ckd_progression",
    "initial_egfr_min",
    "initial_egfr_max",
    "progression_trajectories",
]

# Templates that hold many cohorts and are NOT synced per-catalog-key
SKIP_TEMPLATES = {
    "ohio_demo.template.json",
    "template.template.json",
    "ohio_demo.json",
}


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def find_cohort_in_template(template: dict, catalog_key: str) -> dict | None:
    """Return the first cohort in template whose id matches catalog_key, or
    fall back to the first cohort if the template only has one."""
    cohorts = template.get("cohorts", [])
    for c in cohorts:
        if c.get("id", "") == catalog_key:
            return c
    if len(cohorts) == 1:
        return cohorts[0]
    return None


def merge_cohorts(cohorts: list[dict]) -> dict:
    """Merge multiple cohort dicts into a union, deduplicating by code where
    relevant (for diagnoses, comorbidities, medications, labs).  Scalar fields
    (encounter_pattern, flags) are taken from the first cohort."""
    merged: dict = {}
    for field in CLINICAL_FIELDS + FLAG_FIELDS:
        items = []
        seen_codes: set = set()
        for cohort in cohorts:
            val = cohort.get(field)
            if val is None:
                continue
            if isinstance(val, list):
                for item in val:
                    code = item.get("code") or item.get("drug_code") or item.get("order_code")
                    if code and code in seen_codes:
                        continue
                    if code:
                        seen_codes.add(code)
                    items.append(item)
            elif field not in merged:
                # Take scalar/dict value from first cohort that has it
                merged[field] = val
        if items:
            merged[field] = items
    return merged


def build_clinical_catalog(cohort: dict) -> dict:
    """Extract the clinical fields from a template cohort dict."""
    catalog = {}
    for field in CLINICAL_FIELDS:
        if field in cohort:
            catalog[field] = cohort[field]
    for flag in FLAG_FIELDS:
        if flag in cohort:
            catalog[flag] = cohort[flag]
    return catalog


def sync(dry_run: bool = False, check: bool = False) -> None:
    catalog = load_json(CATALOG_PATH)
    changed = []

    for catalog_key, entry in catalog["cohorts"].items():
        source_template = entry.get("source_template", "")
        if not source_template or source_template in SKIP_TEMPLATES:
            continue

        tmpl_path = TEMPLATES_DIR / source_template
        if not tmpl_path.exists():
            print(f"  [WARN] {catalog_key}: template not found: {tmpl_path}", file=sys.stderr)
            continue

        template = load_json(tmpl_path)
        cohort = find_cohort_in_template(template, catalog_key)
        if cohort is None:
            all_cohorts = template.get("cohorts", [])
            if not all_cohorts:
                print(
                    f"  [WARN] {catalog_key}: no cohorts in {source_template}",
                    file=sys.stderr,
                )
                continue
            # Multi-cohort template: merge all sub-cohorts into one catalog entry
            print(
                f"  [INFO] {catalog_key}: merging {len(all_cohorts)} sub-cohorts"
                f" from {source_template}"
            )
            new_clinical = merge_cohorts(all_cohorts)
        else:
            new_clinical = build_clinical_catalog(cohort)
        old_clinical = entry.get("clinical_catalog", {})

        # Compare by JSON serialisation to detect drift
        if json.dumps(old_clinical, sort_keys=True) == json.dumps(new_clinical, sort_keys=True):
            print(f"  [OK]   {catalog_key}: up to date")
            continue

        print(f"  [SYNC] {catalog_key}: updating clinical_catalog from {source_template}")
        # Report what changed at the top level
        old_keys = set(old_clinical.keys())
        new_keys = set(new_clinical.keys())
        for k in sorted(new_keys - old_keys):
            print(f"           + {k} (added)")
        for k in sorted(old_keys - new_keys):
            print(f"           - {k} (removed)")
        for k in sorted(old_keys & new_keys):
            if json.dumps(old_clinical.get(k), sort_keys=True) != json.dumps(new_clinical.get(k), sort_keys=True):
                old_count = len(old_clinical[k]) if isinstance(old_clinical[k], list) else "~"
                new_count = len(new_clinical[k]) if isinstance(new_clinical[k], list) else "~"
                print(f"           ~ {k}: {old_count} → {new_count} items")

        if not dry_run:
            entry["clinical_catalog"] = new_clinical
        changed.append(catalog_key)

    if not changed:
        print("\nAll synced catalog entries are up to date.")
        return

    if check:
        print(
            f"\n❌ Catalog is stale — {len(changed)} cohort(s) need updating:"
            f" {', '.join(changed)}"
        )
        print("Run  python3 scripts/sync_catalog.py  to fix.")
        sys.exit(1)
    elif dry_run:
        print(
            f"\nDry run: {len(changed)} cohort(s) would be updated:"
            f" {', '.join(changed)}"
        )
    else:
        save_json(CATALOG_PATH, catalog)
        print(f"\nSaved. {len(changed)} cohort(s) updated: {', '.join(changed)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report changes without writing",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Exit 1 if catalog is stale (for CI / git hooks)",
    )
    args = parser.parse_args()
    sync(dry_run=args.dry_run, check=args.check)
