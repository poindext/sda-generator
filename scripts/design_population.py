#!/usr/bin/env python3
"""
SDA3 Population Template Designer

Reads a free-text population description and uses AI (once) to produce a
comprehensive JSON template. The template contains everything needed for
AI-free patient generation: clinical catalogs, name pools, lab value ranges,
document templates, cohort rules, facilities, providers, and insurance plans.

Phases (each is one or more LLM calls):
  1. Population structure  — meta, geography, demographics, cohort list,
                             facilities, providers, insurance plans
  2. Name pools            — first/last names by race/ethnicity
  3. Cohort catalogs       — diagnoses, meds, labs, vitals, procedures,
                             illness/social/family histories, rad orders,
                             document templates  (concurrent, one call per cohort)
  4. Shared catalogs       — allergies, vaccinations, encounter patterns

Usage:
  python design_population.py ohio_demo.txt
  python design_population.py ohio_demo.txt --output ohio_template.json
  python design_population.py ohio_demo.txt --model gpt-4o
  python design_population.py ohio_demo.txt \\
      --base-url https://apps-llm-1.iscinternal.com/v1 --model Qwen/Qwen3-32B-AWQ
  python design_population.py ohio_demo.txt --library lab_panels_library.json

Requires OPENAI_API_KEY environment variable unless --base-url points to an
unauthenticated endpoint.

IMPORTANT — lab_panels_library.json
  If a lab_panels_library.json file is present (default: same directory as this
  script), its QA-hardened reference ranges are automatically merged into every
  generated cohort catalog after the LLM runs. The LLM selects which panels to
  use and sets abnormal_pct/weight; the library provides correct LOINC codes and
  physiologically validated normal/abnormal ranges. This prevents the LLM from
  hallucinating implausible values (e.g. HDL 932 mg/dL).

  The library was produced from ohio_demo.template.json after extensive clinical
  QA. It is portable across all Ohio (and other state) deployments — do not
  regenerate it via LLM. To add a new panel, add it directly to the library JSON.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_BASE_URL = ""
DEFAULT_MODEL = "gpt-4o"
ISC_LLM_BASE_URL = "https://apps-llm-1.iscinternal.com/v1"
ISC_LLM_DEFAULT_MODEL = "Qwen/Qwen3-32B-AWQ"

MAX_RETRIES = 3

# Low Risk Preventive / wellness cohort names that should have low abnormal rates.
# Any cohort whose name contains one of these strings will have its lab
# abnormal_pct capped at PREVENTIVE_MAX_ABN_PCT after the LLM runs.
_PREVENTIVE_COHORT_KEYWORDS = ("preventive", "wellness", "low risk", "healthy")
_PREVENTIVE_MAX_ABN_PCT = 0.05

try:
    import openai  # noqa: F401
except ImportError:
    print("ERROR: Install the OpenAI SDK:  pip install openai")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Lab panel library helpers
# ---------------------------------------------------------------------------

def load_lab_library(path: str | None) -> dict:
    """Load lab_panels_library.json. Returns empty dict if path is None/missing."""
    if path is None:
        default = Path(__file__).parent.parent / "config" / "lab_panels_library.json"
        path = str(default) if default.exists() else None
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        panels = data.get("panels", data)  # support both {panels:{}} and flat dict
        print(f"  Loaded lab library: {len(panels)} vetted panels from {path}", flush=True)
        return panels
    except Exception as e:
        print(f"  [WARN] Could not load lab library {path}: {e}", flush=True)
        return {}


def _library_panel_summary(library: dict) -> str:
    """Build a compact panel list for injection into the Phase 3 prompt."""
    if not library:
        return ""
    lines = ["Vetted lab panels (use these order_codes exactly — ranges will be applied from library):"]
    for code, panel in sorted(library.items(), key=lambda x: x[1].get("order_description", "")):
        tags = ", ".join(panel.get("disease_tags", [])) or "general"
        lines.append(f"  {code}: {panel['order_description']} [{tags}]")
    return "\n".join(lines)


def load_cohort_hints(path: str | None = None) -> dict:
    """Load cohort_hints_library.json. Returns empty dict if path is None/missing."""
    if path is None:
        default = Path(__file__).parent.parent / "config" / "cohort_hints_library.json"
        path = str(default) if default.exists() else None
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        n = len(data.get("cohort_hints", []))
        print(f"  Loaded cohort hints: {n} cohort types from {path}", flush=True)
        return data
    except Exception as e:
        print(f"  [WARN] Could not load cohort hints {path}: {e}", flush=True)
        return {}


def _lab_constraints_text(cohort_hints_lib: dict) -> str:
    """Build the LAB VALUE CONSTRAINTS block from config."""
    constraints = cohort_hints_lib.get("lab_constraints", {})
    rules = constraints.get("rules", [])
    if not rules:
        return ""
    note = constraints.get("note", "Generator enforces hard limits")
    lines = [f"LAB VALUE CONSTRAINTS ({note}):"]
    for r in rules:
        lines.append(f"  - {r['analyte']}: {r['field']} must be {r['operator']} {r['value']} {r['unit']}")
    return "\n".join(lines)


def _matched_cohort_hints(cohort_name: str, cohort_hints_lib: dict, library: dict) -> str:
    """Return targeted hints + vetted panel suggestions for matching cohort type(s)."""
    if not cohort_hints_lib:
        return ""
    name_lower = cohort_name.lower()
    matched = [
        h for h in cohort_hints_lib.get("cohort_hints", [])
        if any(kw in name_lower for kw in h.get("keywords", []))
    ]
    if not matched:
        return ""

    blocks = []
    for hint in matched:
        lines = [f"  [{hint['cohort_type'].upper()}]"]

        # Proactive panel suggestions from disease_tags in lab_panels_library
        tag = hint.get("disease_tag")
        if tag and library:
            tagged = [
                f"    {code} — {p['order_description']}"
                for code, p in library.items()
                if tag in p.get("disease_tags", [])
            ]
            if tagged:
                lines.append("  Vetted panels for this cohort type (use these order_codes):")
                lines.extend(tagged)

        if hint.get("labs"):
            lab_strs = [f"{l['order_code']} ({l['description']})" for l in hint["labs"]]
            lines.append(f"  Key labs: {', '.join(lab_strs)}")

        if hint.get("medications"):
            lines.append("  Key medications:")
            for m in hint["medications"]:
                lines.append(f"    - {m['name']} [RxNorm {m['rxnorm']}] — {m['indication']}")

        if hint.get("comorbidities"):
            com_strs = [
                f"{c['code']} {c['description']} ~{int(float(c['prevalence_pct']) * 100)}%"
                for c in hint["comorbidities"]
            ]
            lines.append(f"  Comorbidities: {'; '.join(com_strs)}")

        if hint.get("_note"):
            lines.append(f"  NOTE: {hint['_note']}")

        if hint.get("encounter_pattern_guidance"):
            lines.append(f"  Encounter pattern: {hint['encounter_pattern_guidance']}")

        blocks.append("\n".join(lines))

    return "COHORT-SPECIFIC CLINICAL HINTS (from cohort_hints_library.json):\n" + "\n".join(blocks)


# ---------------------------------------------------------------------------
# Cohort catalog — QA-approved definitions that bypass the Phase 3 LLM call
# ---------------------------------------------------------------------------

# Fields that represent reusable clinical knowledge (everything except population
# meta like weight, min_age, max_age, sex_bias which come from Phase 1).
_CLINICAL_FIELDS = (
    "diagnoses", "comorbidities", "medications", "labs", "observations",
    "procedures", "illness_history_templates", "social_history_templates",
    "family_history_templates", "document_templates", "rad_order_templates",
    "encounter_pattern",
)

_CATALOG_PATH = Path(__file__).parent.parent / "config" / "cohort_catalog.json"


def load_cohort_catalog(path: str | None = None) -> dict:
    """Load cohort_catalog.json. Returns empty dict if missing."""
    p = Path(path) if path else _CATALOG_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        n = len(data.get("cohorts", {}))
        print(f"  Loaded cohort catalog: {n} QA-approved cohorts from {p}", flush=True)
        return data
    except Exception as e:
        print(f"  [WARN] Could not load cohort catalog {p}: {e}", flush=True)
        return {}


def _find_catalog_match(cohort_name: str, catalog: dict) -> dict | None:
    """Return the first catalog entry whose keywords match cohort_name, else None."""
    name_lower = cohort_name.lower()
    for entry in catalog.get("cohorts", {}).values():
        if any(kw in name_lower for kw in entry.get("keywords", [])):
            return entry
    return None


def _cohort_slug(cohort: dict) -> str:
    """Derive a stable catalog key from a cohort dict (id field preferred)."""
    cid = cohort.get("id") or cohort.get("cohort_id", "")
    if cid:
        return str(cid)
    import re
    return re.sub(r"[^a-z0-9]+", "_", cohort.get("name", "unknown").lower()).strip("_")


def update_catalog_from_template(
    template_path: str,
    catalog_path: str | None = None,
    cohort_hints_lib: dict | None = None,
) -> None:
    """
    Extract all cohort clinical definitions from a QA-approved template and
    save them to the cohort catalog. Existing entries are overwritten so the
    catalog always reflects the most recently approved version.
    """
    tpath = Path(template_path)
    if not tpath.exists():
        print(f"[ERROR] Template not found: {tpath}", flush=True)
        return

    cpath = Path(catalog_path) if catalog_path else _CATALOG_PATH

    catalog = (
        json.loads(cpath.read_text(encoding="utf-8"))
        if cpath.exists() else {"version": "1.0", "cohorts": {}}
    )

    template = json.loads(tpath.read_text(encoding="utf-8"))
    added, updated = 0, 0

    for cohort in template.get("cohorts", []):
        slug = _cohort_slug(cohort)
        clinical = {f: cohort[f] for f in _CLINICAL_FIELDS if f in cohort}

        # Keywords: use hints-library entry if found, else derive from cohort name
        import re
        hints_entry = None
        if cohort_hints_lib:
            name_lower = cohort.get("name", "").lower()
            for h in cohort_hints_lib.get("cohort_hints", []):
                if any(kw in name_lower for kw in h.get("keywords", [])):
                    hints_entry = h
                    break
        if hints_entry:
            keywords = hints_entry["keywords"]
        else:
            # Use the full name phrase and the id phrase as keywords.
            # Single-word tokens are too broad (e.g. "chronic" would match COPD);
            # require the full phrase so only near-identical cohort names reuse this entry.
            name_phrase = re.sub(r"[^a-z0-9 ]+", " ",
                                 cohort.get("name", "").lower()).strip()
            id_phrase = slug.replace("_", " ")
            keywords = list(dict.fromkeys([name_phrase, id_phrase]))

        action = "updated" if slug in catalog["cohorts"] else "added"
        if action == "added":
            added += 1
        else:
            updated += 1

        catalog["cohorts"][slug] = {
            "canonical_name": cohort.get("name", slug),
            "keywords": keywords,
            "qa_status": "approved",
            "source_template": tpath.name,
            "clinical_catalog": clinical,
        }
        print(f"  [{action}] {cohort.get('name', slug)}", flush=True)

    cpath.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Catalog saved -> {cpath}", flush=True)
    print(f"  {added} added, {updated} updated | total: {len(catalog['cohorts'])} cohorts",
          flush=True)


def sync_configs_from_template(
    template_path: str,
    catalog_path: str | None = None,
    library_path: str | None = None,
) -> None:
    """
    Push QA-approved template definitions back into every relevant config file:
      1. cohort_catalog.json  — cohort clinical definitions
      2. lab_panels_library.json — any lab panels not already present

    Call this after every successful QA pass so configs stay current.
    """
    print(f"\nSyncing configs from {template_path}...", flush=True)

    hints = load_cohort_hints()

    # 1. cohort_catalog.json
    update_catalog_from_template(template_path,
                                 catalog_path=catalog_path,
                                 cohort_hints_lib=hints)

    # 2. lab_panels_library.json
    lib_path = Path(library_path) if library_path else (
        Path(__file__).parent.parent / "config" / "lab_panels_library.json"
    )
    template = json.loads(Path(template_path).read_text(encoding="utf-8"))

    if not lib_path.exists():
        print(f"  [SKIP] lab_panels_library.json not found at {lib_path}", flush=True)
        return

    lib_data = json.loads(lib_path.read_text(encoding="utf-8"))
    panels = lib_data.get("panels", lib_data)

    added_panels = 0
    for cohort in template.get("cohorts", []):
        for lab in cohort.get("labs", []):
            code = lab.get("order_code", "")
            if not code or code in panels:
                continue
            # Only add if it has result_items (enough structure to be useful)
            if not lab.get("result_items"):
                continue
            panels[code] = {
                "order_code": code,
                "order_description": lab.get("order_description", code),
                "specimen": lab.get("specimen", "Blood"),
                "result_items": lab["result_items"],
                "disease_tags": [],
                "_source": "auto-synced from " + Path(template_path).name,
            }
            print(f"  [lab panel added] {code} — {lab.get('order_description', code)}",
                  flush=True)
            added_panels += 1

    if added_panels:
        if "panels" in lib_data:
            lib_data["panels"] = panels
        else:
            lib_data = panels
        lib_path.write_text(json.dumps(lib_data, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        print(f"  Lab library saved ->{lib_path}  ({added_panels} panel(s) added)",
              flush=True)
    else:
        print("  Lab library: no new panels to add.", flush=True)


async def _resolve_cohort(
    client, model: str, cohort: dict, description: str,
    library: dict, cohort_hints_lib: dict, catalog: dict,
) -> dict:
    """
    Return clinical catalog for a cohort. Checks the catalog first (no LLM cost);
    falls back to a Phase 3 LLM call when no approved definition exists.
    """
    entry = _find_catalog_match(cohort["name"], catalog)
    if entry:
        return entry["clinical_catalog"]
    return await phase3_cohort(client, model, cohort, description,
                                library=library, cohort_hints_lib=cohort_hints_lib)


def apply_library_overrides(cohort_catalog: dict, library: dict, cohort_name: str) -> dict:
    """
    Replace LLM-generated result_items with library versions where order_code matches.
    Also caps abnormal_pct for preventive/wellness cohorts.
    """
    if not library:
        return cohort_catalog

    is_preventive = any(
        kw in cohort_name.lower() for kw in _PREVENTIVE_COHORT_KEYWORDS
    )

    for lab in cohort_catalog.get("labs", []):
        code = lab.get("order_code", "")
        if code in library:
            lib = library[code]
            lab["result_items"] = lib["result_items"]
            lab["order_description"] = lib["order_description"]
            lab["specimen"] = lib.get("specimen", lab.get("specimen", "Blood"))

        if is_preventive:
            abn = float(lab.get("abnormal_pct", 0.3))
            if abn > _PREVENTIVE_MAX_ABN_PCT:
                lab["abnormal_pct"] = _PREVENTIVE_MAX_ABN_PCT

    return cohort_catalog


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------

def _make_async_client(base_url: str):
    from openai import AsyncOpenAI
    if base_url and "openai.com" not in base_url:
        return AsyncOpenAI(base_url=base_url, api_key="none")
    return AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))


def _extra_body(model: str) -> dict | None:
    if "qwen" in model.lower():
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return None


def strip_json_fences(text: str) -> str:
    """Strip markdown code fences and return the JSON content."""
    text = text.strip()
    m = re.match(r'^```(?:json)?\s*\n?(.*?)\n?```\s*$', text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # Find outermost JSON object or array
    for ch in ('{', '['):
        idx = text.find(ch)
        if idx != -1:
            text = text[idx:]
            break
    return text


async def _llm_once(client, model: str, messages: list) -> str:
    response = await client.chat.completions.create(
        model=model,
        max_tokens=16384,
        temperature=0.2,
        messages=messages,
        extra_body=_extra_body(model),
    )
    return response.choices[0].message.content


async def call_llm_json(client, model: str, system: str, user: str, label: str) -> dict | list:
    """Call the LLM and return parsed JSON. Uses multi-turn repair on parse failures."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user + "\n\nRespond with ONLY valid JSON — no markdown fences, no explanation."},
    ]
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = await _llm_once(client, model, messages)
            return json.loads(strip_json_fences(raw))
        except json.JSONDecodeError as e:
            last_exc = e
            print(f"  [JSON RETRY {attempt}/{MAX_RETRIES}] {label}: {e}", flush=True)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                f"Your response was not valid JSON ({e}). "
                "Respond with ONLY the corrected JSON."})
        except Exception as e:
            last_exc = e
            print(f"  [API RETRY {attempt}/{MAX_RETRIES}] {label}: {e}", flush=True)
    Path(f"debug_{label.replace(' ', '_')}.json").write_text(str(last_exc))
    raise RuntimeError(f"{label}: failed after {MAX_RETRIES} attempts — {last_exc}")


# ---------------------------------------------------------------------------
# Phase 1 — Population structure
# ---------------------------------------------------------------------------

_P1_SYSTEM = """\
You are a health informatics architect building synthetic patient population
configurations for a clinical data generator. Output ONLY valid JSON.
All codes must be real codes from the specified coding systems.
Weights within each list must sum to 1.0."""

_P1_SCHEMA = """\
{
  "meta": {
    "name": "short population name",
    "description": "1-2 sentence summary",
    "state": "full state name — use 'National' if the description says national/nationwide/multi-state",
    "state_code": "2-letter USPS code — use 'US' if national/nationwide/multi-state",
    "medicaid_field": "state Medicaid program name — use 'Medicaid' if national",
    "medicaid_id_prefix": "prefix for Medicaid IDs — use 'MEDICAID-' if national",
    "total_patients": integer,
    "history_months": integer
  },
  "geography": {
    "locations": [
      {
        "county": "County Name",
        "county_fips": "5-digit FIPS code for the actual county",
        "state_code": "2-letter USPS code matching the description",
        "region": "Region Name (derive from description geography)",
        "weight": 0.15,
        "rurality": "urban|suburban|rural",
        "cities": ["City1", "City2"]
      }
    ]
  },
  "demographics": {
    "age_distribution": [
      {"min": 18, "max": 34, "weight": 0.20, "label": "Young Adult"}
    ],
    "sex_distribution": [
      {"sex": "F", "weight": 0.51},
      {"sex": "M", "weight": 0.49}
    ],
    "race_distribution": [
      {
        "race_code": "2106-3",
        "race_description": "White",
        "ethnicity_code": "2186-5",
        "ethnicity_description": "Not Hispanic or Latino",
        "weight": 0.70
      }
    ],
    "insurance_distribution": [
      {"plan_code": "OHMED", "weight": 0.35}
    ]
  },
  "facilities": [
    {
      "code": "FAC001",
      "name": "Facility Name matching the description geography",
      "type": "hospital",
      "health_system_code": "HS001",
      "health_system_name": "Health System Name",
      "city": "City matching the description",
      "region": "Region Name matching a location region above",
      "county_fips": "5-digit FIPS for this facility's county",
      "address": "Street Address",
      "zip": "ZIP code",
      "weight": 0.12
    }
  ],
  "providers": [
    {
      "code": "DR001",
      "name": "Dr. Jane Smith",
      "specialty": "Internal Medicine",
      "facility_code": "CLECLINIC"
    }
  ],
  "insurance_plans": [
    {
      "code": "STMED",
      "name": "State Medicaid Program (name based on description state)",
      "coding_standard": "QD_HealthPlanCodeList",
      "type": "Medicaid",
      "plan_code": "XX-MED-PLAN",
      "plan_name": "State Medicaid Managed Care Plan",
      "group_name": "State Medicaid Program",
      "priority": 1
    }
  ],
  "cohorts": [
    {
      "id": "diabetes_t2",
      "name": "Type 2 Diabetes",
      "weight": 0.12,
      "min_age": 30,
      "max_age": 85,
      "sex_bias": null,
      "description": "Patients with type 2 diabetes, managed with oral agents and/or insulin"
    }
  ],
  "sending_facility": "CLECLINIC",
  "multi_facility": {
    "enabled": true,
    "distribution": {
      "one_facility_pct": 0.55,
      "two_facility_pct": 0.30,
      "three_plus_facility_pct": 0.15,
      "max_facilities": 3
    },
    "encounter_type_outside_prob": {
      "O": 0.08,
      "E": 0.40,
      "I": 0.20
    },
    "geographic_affinity": {
      "same_region_weight": 0.70,
      "adjacent_region_weight": 0.20,
      "other_region_weight": 0.10
    },
    "adjacent_regions": {
      "Region A": ["Region B", "Region C"],
      "Region B": ["Region A", "Region D"],
      "Region C": ["Region A", "Region D"],
      "Region D": ["Region B", "Region C"]
    },
    "ed_ip_same_facility_lock": true,  // false when description mentions transfers/cross-org journeys
    "facility_mrn_prefix": true
  }
}"""


def _p1_scale_hints(description: str) -> str:
    """Extract scale guidance from description text to override prompt defaults."""
    import re as _re
    hints = []

    # Geography: detect explicit county/location count
    m = _re.search(r'all\s+(\d+)\s+count(?:y|ies)', description, _re.IGNORECASE)
    if m:
        hints.append(
            f"GEOGRAPHY: The description says 'all {m.group(1)} counties'. "
            f"Generate exactly {m.group(1)} locations (one per county), NOT 8-15."
        )
    else:
        hints.append(
            "GEOGRAPHY: Generate 8-15 geographic locations unless the description "
            "specifies a larger count. Derive region names from the description's geography."
        )

    # Facilities: detect explicit facility count
    mf = _re.search(r'(\d+)\s+facilit', description, _re.IGNORECASE)
    if mf:
        hints.append(
            f"FACILITIES: The description specifies {mf.group(1)} facilities. "
            f"Generate exactly {mf.group(1)} facilities, NOT 8-15. "
            "All facility names must be completely FICTIONAL — do not use the names of "
            "real hospitals, health systems, or clinics."
        )
    else:
        hints.append(
            "FACILITIES: Generate 8-15 facilities. "
            "All facility names must be completely FICTIONAL — do not use real institution names."
        )

    # Providers: detect explicit provider count
    mp = _re.search(r'(\d+)\s+provider', description, _re.IGNORECASE)
    if mp:
        hints.append(
            f"PROVIDERS: The description specifies approximately {mp.group(1)} providers. "
            f"Generate approximately {mp.group(1)} providers, NOT 10-20."
        )
    else:
        hints.append("PROVIDERS: Generate 10-20 providers spread across facilities.")

    # Cohort weights: detect disease prevalence percentage
    mpct = _re.search(
        r'(\d+(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?(?:patients|population)',
        description, _re.IGNORECASE
    )
    if mpct:
        pct = float(mpct.group(1))
        hints.append(
            f"COHORT WEIGHTS: The description states the disease affects {pct}% of patients. "
            f"Cohort weights are FRACTIONS OF ALL PATIENTS (they must sum to 1.0 across ALL cohorts). "
            f"The disease stage cohort weights must sum to {pct/100:.4f} (= {pct}% / 100). "
            f"Example: if there are 4 disease stages (45%/30%/18%/7% of affected), their population "
            f"weights are {pct*0.45/100:.4f} / {pct*0.30/100:.4f} / {pct*0.18/100:.4f} / {pct*0.07/100:.4f}."
        )
    else:
        hints.append(
            "COHORT WEIGHTS: Weights are FRACTIONS OF ALL PATIENTS and must sum to 1.0 across "
            "all cohorts. A disease affecting 10% of patients means disease cohort weights sum to 0.10."
        )

    # Age restrictions
    if _re.search(r'all ages', description, _re.IGNORECASE):
        hints.append(
            "AGE RANGES: The description says the disease 'affects all ages'. "
            "Set min_age=0, max_age=120 for those cohorts — do NOT restrict them to 20-64."
        )

    # ed_ip_same_facility_lock
    if _re.search(r'transfer|cross.organiz|cross.facilit|cross-facilit', description, _re.IGNORECASE):
        hints.append(
            "ED/IP TRANSFER: The description mentions patient transfers. "
            "Set ed_ip_same_facility_lock=false in the multi_facility block."
        )

    return "\n".join(f"  • {h}" for h in hints)


_LARGE_SCALE_COUNTY_THRESHOLD = 20   # split into sub-calls above this


def _p1_extract_counts(description: str) -> tuple[int, int, int]:
    """Return (county_count, facility_count, provider_count) from description, or 0 if not stated."""
    import re as _re
    m_county   = _re.search(r'all\s+(\d+)\s+count(?:y|ies)', description, _re.IGNORECASE)
    m_facility = _re.search(r'(\d+)\s+facilit', description, _re.IGNORECASE)
    m_provider = _re.search(r'(\d+)\s+provider', description, _re.IGNORECASE)
    return (
        int(m_county.group(1))   if m_county   else 0,
        int(m_facility.group(1)) if m_facility else 0,
        int(m_provider.group(1)) if m_provider else 0,
    )


# Compact schema for Phase 1a — omits geography.locations, facilities, providers
_P1A_SCHEMA = """\
{
  "meta": { "name": "...", "description": "...", "state": "...", "state_code": "XX",
            "medicaid_field": "...", "medicaid_id_prefix": "...",
            "total_patients": 25000, "history_months": 36 },
  "region_definitions": [
    { "name": "NE Ohio", "adjacent": ["Central Ohio", "NW Ohio"] }
  ],
  "demographics": {
    "age_distribution":   [{"min": 0, "max": 14, "weight": 0.08, "label": "Child"}],
    "sex_distribution":   [{"sex": "F", "weight": 0.51}, {"sex": "M", "weight": 0.49}],
    "race_distribution":  [{"race_code": "2106-3", "race_description": "White",
                             "ethnicity_code": "2186-5",
                             "ethnicity_description": "Not Hispanic or Latino", "weight": 0.70}],
    "insurance_distribution": [{"plan_code": "OHMED", "weight": 0.35}]
  },
  "insurance_plans": [
    {"code": "OHMED", "name": "Ohio Medicaid", "coding_standard": "QD_HealthPlanCodeList",
     "type": "Medicaid", "plan_code": "OH-MED-PLAN",
     "plan_name": "Ohio Medicaid Managed Care Plan", "group_name": "Ohio Medicaid Program",
     "priority": 1}
  ],
  "cohorts": [
    {"id": "diabetes_t2", "name": "Type 2 Diabetes", "weight": 0.12,
     "min_age": 30, "max_age": 85, "sex_bias": null,
     "description": "Patients with type 2 diabetes"}
  ],
  "sending_facility": "FAC001",
  "multi_facility": {
    "enabled": true,
    "distribution": {"one_facility_pct": 0.55, "two_facility_pct": 0.30,
                     "three_plus_facility_pct": 0.15, "max_facilities": 3},
    "encounter_type_outside_prob": {"O": 0.08, "E": 0.40, "I": 0.20},
    "geographic_affinity": {"same_region_weight": 0.70, "adjacent_region_weight": 0.20,
                             "other_region_weight": 0.10},
    "adjacent_regions": {"NE Ohio": ["Central Ohio", "NW Ohio"]},
    "ed_ip_same_facility_lock": true,
    "facility_mrn_prefix": true
  }
}"""

_P1B_GEO_SCHEMA = """\
{
  "locations": [
    {
      "county": "County Name",
      "county_fips": "39035",
      "state_code": "OH",
      "region": "NE Ohio",
      "weight": 0.095,
      "rurality": "urban",
      "cities": ["Cleveland", "Parma"]
    }
  ]
}"""

_P1C_FAC_SCHEMA = """\
{
  "facilities": [
    {
      "code": "FAC001",
      "name": "Fictional Hospital Name",
      "type": "academic",
      "health_system_code": "HS001",
      "health_system_name": "Fictional Health System Name",
      "city": "City",
      "region": "NE Ohio",
      "county_fips": "39035",
      "address": "123 Main St",
      "zip": "44101",
      "weight": 0.05
    }
  ],
  "providers": [
    {
      "code": "DR001",
      "name": "Dr. Fictional Name",
      "specialty": "Internal Medicine",
      "facility_code": "FAC001"
    }
  ]
}"""


async def _phase1a_structure(client, model: str, description: str,
                              scale_hints: str) -> dict:
    """Phase 1a: meta, demographics, cohorts, insurance, multi_facility, region_definitions."""
    national_hint = ""
    if re.search(r'\bnational\b|\bnationwide\b|\bmulti.state\b', description, re.IGNORECASE):
        national_hint = (
            "\nNATIONAL POPULATION: Set meta.state='National', meta.state_code='US'. "
            "Use US Census division names as regions. "
            "Include national insurance plans.\n"
        )
    return await call_llm_json(client, model, _P1_SYSTEM, f"""\
Population description:
{description}

Generate a JSON object with meta, demographics, insurance_plans, cohorts, \
sending_facility, and multi_facility ONLY (no geography locations or facilities).

SCALE AND CONFIGURATION RULES:
{scale_hints}
{national_hint}
Additional rules:
- Include a "region_definitions" array listing each region name and its adjacent regions.
  These will be used in Phase 1b/1c to generate geography and facilities.
- 3-8 insurance plans.
- 6-12 clinical cohorts (or as many as the description specifies).
- All weights must sum to 1.0 within each array.

Schema (output exactly these keys):
{_P1A_SCHEMA}""", "phase1a")


async def _phase1b_geography(client, model: str, description: str,
                              regions: list[dict], county_count: int,
                              state_name: str, state_code: str,
                              existing_counties: list[str] | None = None) -> dict:
    """Phase 1b: geography locations for large-scale populations."""
    region_names = [r["name"] for r in regions]
    if existing_counties:
        need = county_count - len(existing_counties)
        existing_note = (
            f"\nAlready generated {len(existing_counties)} counties: {sorted(existing_counties)}\n"
            f"Generate ONLY the {need} MISSING counties NOT in that list above.\n"
            f"Do NOT repeat any county already listed. Output EXACTLY {need} new locations."
        )
        target_n = need
    else:
        existing_note = ""
        target_n = county_count
    return await call_llm_json(client, model, _P1_SYSTEM, f"""\
Population description (brief): {description[:600]}

Generate ONLY the geography.locations array for a {state_name} population.
Regions defined: {region_names}
{existing_note}
Requirements:
- Generate EXACTLY {target_n} location(s) — one entry per county, no more, no less.
  Do not stop early. Every county is required even if small or rural.
- Each location must have county, county_fips (5-digit), state_code="{state_code}",
  region (must match one of the defined regions above), rurality (urban/suburban/rural),
  cities (1-2 representative cities), and weight.
- Distribute counties across regions appropriately for {state_name} geography.
- Weights do NOT need to sum to 1.0 in supplemental calls — they will be renormalized.
- Urban counties with large cities get higher weight; rural counties get lower weight
  (minimum 0.001 each). Small counties with low weight are still required.

Schema:
{_P1B_GEO_SCHEMA}""", "phase1b")


async def _phase1b_complete(client, model: str, description: str,
                             regions: list[dict], county_count: int,
                             state_name: str, state_code: str) -> dict:
    """
    Phase 1b with automatic gap-filling loop.
    Keeps making targeted supplemental calls listing already-received counties
    until the target is reached or MAX_SUPPLEMENT_ROUNDS is exhausted.
    """
    MAX_SUPPLEMENT_ROUNDS = 5

    raw = await _phase1b_geography(client, model, description, regions,
                                   county_count, state_name, state_code)
    locs = _dedup_locations(raw.get("locations", []))

    round_n = 0
    while len(locs) < county_count and round_n < MAX_SUPPLEMENT_ROUNDS:
        round_n += 1
        short_by = county_count - len(locs)
        existing = [l["county"] for l in locs]
        print(f"  Phase 1b: {len(locs)} counties, need {short_by} more — "
              f"supplement round {round_n}/{MAX_SUPPLEMENT_ROUNDS}...", flush=True)
        supplement = await _phase1b_geography(
            client, model, description, regions,
            county_count, state_name, state_code,
            existing_counties=existing,
        )
        extra = _dedup_locations(supplement.get("locations", []))
        merged = {l["county"]: l for l in locs}
        added = 0
        for l in extra:
            if l["county"] not in merged:
                merged[l["county"]] = l
                added += 1
        locs = list(merged.values())
        print(f"  Phase 1b supplement {round_n} ✓  +{added} counties ->{len(locs)} total",
              flush=True)
        if added == 0:
            # LLM returned only duplicates — no progress, stop early
            print("  Phase 1b: no new counties in last supplement — stopping.", flush=True)
            break

    if len(locs) > county_count:
        locs = locs[:county_count]

    return {"locations": locs}


def _dedup_locations(locs: list) -> list:
    """Deduplicate locations by (county, state_code); keep first occurrence."""
    seen: dict = {}
    for loc in locs:
        key = (loc.get("county", ""), loc.get("state_code", ""))
        if key not in seen:
            seen[key] = loc
    return list(seen.values())


async def _phase1c_facilities(client, model: str, description: str,
                               regions: list[dict], facility_count: int,
                               provider_count: int, state_name: str) -> dict:
    """Phase 1c: facilities and providers for large-scale populations."""
    region_names = [r["name"] for r in regions]
    return await call_llm_json(client, model, _P1_SYSTEM, f"""\
Population description (brief): {description[:600]}

Generate ONLY facilities and providers for a {state_name} population.
Regions defined: {region_names}

Requirements:
- Generate exactly {facility_count} facilities (FAC001–FAC{facility_count:03d}).
- ALL facility names and health system names must be COMPLETELY FICTIONAL.
  Do not use real institution names (no Cleveland Clinic, OSU, etc.).
- Spread facilities across all defined regions proportionally.
- Include a mix of types: academic/tertiary, community hospital, outpatient clinic,
  urgent care, specialty clinic, FQHC/community health center, rural/critical-access.
- Group related facilities under the same health_system_code/health_system_name.
- Facility weights must sum to 1.0.
- Generate approximately {provider_count} providers (DR001–DR{provider_count:03d}).
- Spread providers across facilities (~{provider_count // facility_count} per facility average).
- Include specialties: Internal Medicine, Emergency Medicine, Infectious Disease,
  Pulmonology, Neurology, Critical Care, Cardiology, Nephrology, Family Medicine,
  Pediatrics, Behavioral Health, Pathology, Hospitalist.

Schema:
{_P1C_FAC_SCHEMA}""", "phase1c")


async def phase1_structure(client, model: str, description: str) -> dict:
    print("Phase 1: Population structure...", flush=True)
    scale_hints = _p1_scale_hints(description)
    county_count, facility_count, provider_count = _p1_extract_counts(description)

    if county_count > _LARGE_SCALE_COUNTY_THRESHOLD:
        # Large-scale: split into three sub-calls to stay within token limits
        print(f"  Large-scale population detected "
              f"({county_count} counties, {facility_count} facilities, "
              f"{provider_count} providers) — using split Phase 1...", flush=True)

        result_a = await _phase1a_structure(client, model, description, scale_hints)
        print(f"  Phase 1a ✓  {len(result_a.get('cohorts', []))} cohorts", flush=True)

        regions = result_a.get("region_definitions", [])
        state_name = result_a.get("meta", {}).get("state", "Ohio")
        state_code = result_a.get("meta", {}).get("state_code", "OH")

        n_fac = facility_count or 15
        n_prov = provider_count or 30

        result_b, result_c = await asyncio.gather(
            _phase1b_complete(client, model, description, regions,
                              county_count, state_name, state_code),
            _phase1c_facilities(client, model, description, regions,
                                n_fac, n_prov, state_name),
        )
        print(f"  Phase 1b ✓  {len(result_b.get('locations', []))} counties", flush=True)
        print(f"  Phase 1c ✓  {len(result_c.get('facilities', []))} facilities  "
              f"{len(result_c.get('providers', []))} providers", flush=True)

        # Build adjacent_regions from region_definitions
        adjacent_regions = {
            r["name"]: r.get("adjacent", []) for r in regions
        }
        if result_a.get("multi_facility", {}).get("adjacent_regions"):
            adjacent_regions = result_a["multi_facility"]["adjacent_regions"]

        result = {
            **result_a,
            "geography": {"locations": result_b.get("locations", [])},
            "facilities": result_c.get("facilities", []),
            "providers":  result_c.get("providers", []),
        }
        result.setdefault("multi_facility", {})["adjacent_regions"] = adjacent_regions
        result.pop("region_definitions", None)

    else:
        result = await call_llm_json(client, model, _P1_SYSTEM, f"""\
Population description:
{description}

Generate a JSON object matching the schema below.

SCALE AND CONFIGURATION RULES (derived from the description above — follow exactly):
{scale_hints}

Additional rules:
- Group facilities under fictional health systems (never real names).
- Set each facility's region to match one of the location regions above.
- Larger/academic centers get higher facility weight.
- 3-8 insurance plans appropriate for the description's state.
- 6-12 clinical cohorts (or as many as the description specifies).
- In multi_facility.adjacent_regions, list every region defined above as a key,
  with its geographically adjacent regions as values.
- All weights within each array must sum to 1.0.

IMPORTANT — if the description says "national", "nationwide", or "multi-state":
- Set meta.state = "National" and meta.state_code = "US"
- Spread locations across multiple US states (each location's state_code reflects
  its actual state — do NOT set every location to the same state)
- Use regions based on US Census divisions (Northeast, Southeast, Midwest, Southwest,
  West, etc.) rather than intra-state regions
- Include national insurance plans (commercial, Medicare, Medicaid by state)

Schema:
{_P1_SCHEMA}""", "phase1")

    print(f"  ✓ {len(result.get('cohorts',[]))} cohorts  "
          f"{len(result.get('facilities',[]))} facilities  "
          f"{len(result.get('geography',{}).get('locations',[]))} counties", flush=True)
    return result


# ---------------------------------------------------------------------------
# Phase 2 — Name pools
# ---------------------------------------------------------------------------

_P2_SYSTEM = """\
Generate realistic synthetic US patient name pools for a clinical data
generator. Names must be culturally appropriate for each group.
Output ONLY valid JSON."""

_P2_SCHEMA = """\
{
  "name_pools": {
    "male_first": {
      "White":           ["James","John","Robert"," ...20 total"],
      "Black":           ["Marcus","Darius","Andre"," ...20 total"],
      "Hispanic":        ["Carlos","Miguel","Jose"," ...20 total"],
      "Asian":           ["Kevin","Daniel","Wei"," ...20 total"],
      "Native American": ["Joseph","Raymond","Thomas"," ...15 total"],
      "Other":           ["Alex","Jordan","Chris"," ...15 total"]
    },
    "female_first": {
      "White":           ["Mary","Patricia","Jennifer"," ...20 total"],
      "Black":           ["Aaliyah","Keisha","Tamara"," ...20 total"],
      "Hispanic":        ["Maria","Sofia","Isabella"," ...20 total"],
      "Asian":           ["Mei","Yuki","Ji-Young"," ...20 total"],
      "Native American": ["Sarah","Rebecca","Angela"," ...15 total"],
      "Other":           ["Taylor","Morgan","Quinn"," ...15 total"]
    },
    "last_names": {
      "White":           ["Smith","Johnson","Williams"," ...25 total"],
      "Black":           ["Washington","Jackson","Robinson"," ...25 total"],
      "Hispanic":        ["Garcia","Martinez","Rodriguez"," ...25 total"],
      "Asian":           ["Kim","Chen","Nguyen"," ...25 total"],
      "Native American": ["Runningwater","Littlefeather","Bearcloud"," ...20 total"],
      "Other":           ["Brown","Taylor","Davis"," ...20 total"]
    },
    "middle_initials": ["A","B","C","D","E","F","G","H","J","K","L","M","N","P","R","S","T","W"]
  }
}"""


async def phase2_names(client, model: str) -> dict:
    print("Phase 2: Name pools...", flush=True)
    result = await call_llm_json(client, model, _P2_SYSTEM, f"""\
Generate realistic US patient name pools. Provide the exact number of names
shown in the schema (20/20/15 for first names per group, 25/20 for last names).
Return real names — replace the placeholder strings with actual names.

Schema:
{_P2_SCHEMA}""", "phase2")
    print("  ✓ Name pools generated", flush=True)
    return result


# ---------------------------------------------------------------------------
# Phase 3 — Per-cohort clinical catalogs
# ---------------------------------------------------------------------------

_P3_SYSTEM = """\
You are a clinical informatics specialist generating synthetic clinical data
catalogs for a US patient population generator.
All codes must be real, valid codes from the specified coding standard.
Output ONLY valid JSON."""

_P3_SCHEMA = """\
{
  "diagnoses": [
    {
      "code": "E11.9",
      "description": "Type 2 diabetes mellitus without complications",
      "is_primary": true,
      "onset_age_min": 30,
      "onset_age_max": 85,
      "only_encounter_types": []
    }
  ],
  "comorbidities": [
    {
      "code": "I10",
      "description": "Essential hypertension",
      "prevalence_pct": 0.65
    }
  ],
  "medications": [
    {
      "drug_code": "860975",
      "drug_description": "Metformin 500 MG Oral Tablet",
      "dose_quantity": "500",
      "dose_uom": "mg",
      "dosage_form_code": "TAB",
      "dosage_form_description": "Tablet",
      "route_code": "PO",
      "route_description": "Oral",
      "frequency_code": "BID",
      "frequency_description": "Twice daily",
      "status": "active",
      "for_diagnosis_code": "E11.9",
      "weight": 0.3
    }
  ],
  "labs": [
    {
      "order_code": "4548-4",
      "order_description": "Hemoglobin A1c panel",
      "specimen": "Blood",
      "result_items": [
        {
          "code": "4548-4",
          "description": "Hemoglobin A1c/Hemoglobin.total in Blood",
          "is_numeric": true,
          "units": "%",
          "normal_range_text": "<5.7",
          "normal_min": 4.5,
          "normal_max": 5.6,
          "abnormal_min": 6.5,
          "abnormal_max": 12.0,
          "abnormal_flag": "H",
          "text_values": []
        }
      ],
      "abnormal_pct": 0.55,
      "weight": 0.4
    }
  ],
  "observations": [
    {
      "code": "8480-6",
      "description": "Systolic blood pressure",
      "units": "mmHg",
      "normal_min": 90,
      "normal_max": 119,
      "abnormal_min": 130,
      "abnormal_max": 175,
      "abnormal_flag": "H",
      "abnormal_pct": 0.45,
      "weight": 0.2
    }
  ],
  "procedures": [
    {
      "code": "99213",
      "description": "Office or other outpatient visit, moderate complexity",
      "encounter_type": "O",
      "weight": 0.5
    }
  ],
  "illness_history_templates": [
    {
      "condition": "Type 2 diabetes mellitus, diagnosed at age 45",
      "onset_at_age_min": 40,
      "onset_at_age_max": 55,
      "is_resolved": false,
      "weight": 1.0
    }
  ],
  "social_history_templates": [
    {
      "habit_code": "NS",
      "habit_description": "Non Smoker",
      "comments": [
        "Patient has never smoked.",
        "Former smoker, quit 10 years ago."
      ],
      "weight": 0.6
    }
  ],
  "family_history_templates": [
    {
      "condition": "Type 2 diabetes mellitus",
      "relationship": "Mother",
      "weight": 0.5
    }
  ],
  "document_templates": [
    {
      "type_code": "PN",
      "type_description": "Progress Note",
      "encounter_type": "O",
      "chief_complaints": [
        "Follow-up for diabetes management",
        "Routine monitoring of type 2 diabetes",
        "Diabetes follow-up with medication review"
      ],
      "note_template": "CHIEF COMPLAINT: {chief_complaint}\\n\\nHISTORY OF PRESENT ILLNESS: {patient_name} is a {age}-year-old {sex} presenting for routine follow-up of {diagnosis}. Patient reports {compliance_statement}. Denies chest pain, shortness of breath, or dizziness.\\n\\nPHYSICAL EXAMINATION:\\nVitals: BP {bp} mmHg, HR {hr} bpm, Weight {weight} lbs\\nGeneral: Well-appearing, no acute distress.\\n\\nASSESSMENT AND PLAN:\\n1. {diagnosis} — {plan}\\n\\nFOLLOW-UP: Return in 3 months."
    }
  ],
  "rad_order_templates": [
    {
      "order_code": "CXR-PA-LAT",
      "order_description": "Chest X-Ray PA and Lateral",
      "reason_description": "Evaluate for cardiac enlargement and pulmonary edema",
      "result_texts": [
        "No acute cardiopulmonary process. Mild cardiomegaly noted.",
        "Lungs are clear. No pleural effusion. Heart size upper limits of normal."
      ],
      "weight": 0.3
    }
  ],
  "encounter_pattern": {
    "encounters_per_year": 4.0,
    "encounter_type_weights": {"O": 0.90, "E": 0.05, "I": 0.05},
    "lab_encounter_rate": 0.75,
    "rad_encounter_rate": 0.15
  }
}"""


_NOVEL_DISEASE_KEYWORDS = (
    "novel", "emerging", "unknown", "undiscovered", "fictional", "ovmis",
    "novel disease", "new disease", "mystery", "syndrome under investigation",
)


def _is_novel_cohort(cohort: dict) -> bool:
    text = (cohort.get("name", "") + " " + cohort.get("description", "")).lower()
    return any(kw in text for kw in _NOVEL_DISEASE_KEYWORDS)


async def phase3_cohort(client, model: str, cohort: dict, description: str,
                       library: dict | None = None,
                       cohort_hints_lib: dict | None = None) -> dict:
    label = f"phase3-{cohort['id']}"
    lib_hint = ""
    if library:
        lib_hint = f"""
LAB PANELS — IMPORTANT: Use order_codes from this vetted library where applicable.
Set only abnormal_pct (0.0–1.0) and weight for each panel you include; do NOT invent
result_items — they will be replaced from the library automatically.
You may add cohort-specific panels not in this list, but provide full result_items for those.

{_library_panel_summary(library)}
"""
    novel_hint = ""
    if _is_novel_cohort(cohort):
        novel_hint = """
NOVEL/FICTIONAL DISEASE GUIDANCE — this cohort represents a novel or not-yet-named disease:
- Do NOT use ICD-10 codes for established named diseases (asthma, sepsis, pneumonia, AKI, etc.)
  as primary diagnoses. Use symptom/syndrome codes (R-codes, M35.x, G-codes, J70.x, M31.x)
  that reflect what clinicians would code BEFORE the disease was named.
- Mild/early: R53.83 (fatigue), R50.9 (fever), G54.9 (nerve disorder), M79.3 (panniculitis)
- Moderate: M35.9 (systemic connective tissue disorder), R21 (rash), R09.02 (hypoxemia), G90.9 (autonomic)
- Severe: J70.2 (acute interstitial pneumonitis), G62.9 (polyneuropathy), M35.9
- Critical: M31.9 (necrotizing vasculopathy), I41 (myocarditis in classified diseases), G61.9
- Medications must NOT include asthma inhalers (albuterol, fluticasone, montelukast) unless the
  cohort specifically calls for them. Use appropriate anti-inflammatory, supportive, or
  investigational therapies instead.
- Document templates must describe the OVMIS/novel-disease narrative: unexplained inflammatory
  symptoms, diagnostic uncertainty, clinical investigation — not routine chronic disease management.
- Illness history: use "recurrent unexplained inflammatory episodes" not named chronic diseases.
"""

    result = await call_llm_json(client, model, _P3_SYSTEM, f"""\
Population context (brief): {description[:400]}

Generate a complete clinical data catalog for the "{cohort['name']}" cohort.
{cohort.get('description', '')}
Patient age range: {cohort.get('min_age', 18)}–{cohort.get('max_age', 85)} years.
NOTE: Only restrict diagnoses/history age ranges if clinically justified. If the cohort
serves all ages (min_age=0), do not copy min_age/max_age into onset_age_min/max for every
diagnosis — use the full patient age range or the specific age of typical onset for each.

Requirements:
- 2-4 diagnoses (real ICD-10-CM codes; mark the primary one; set
  only_encounter_types to ["I"] for codes that should only appear on inpatient
  encounters, e.g. delivery codes like O80; leave as [] for all others)
- 2-4 common comorbidities with realistic prevalence percentages
  (see COHORT-SPECIFIC CLINICAL HINTS below for comorbidity recommendations)
- 5-10 medications (real RxNorm codes, various drug classes for the condition)
- 3-6 lab panels (see LAB PANELS note below)
- 4-8 vital sign / observation types (real LOINC codes; realistic min/max ranges)
- 2-4 procedures (real CPT codes)
- 2-3 illness history templates (plain English description of condition; use
  "onset_at_age_min" and "onset_at_age_max" fields, NOT "onset_years_ago",
  so onset dates anchor to patient birth year — e.g. CAD diagnosed at age 52–58)
- 2-3 social history templates (use standard habit codes: NS/TOBA/ETOH/NOALC/DRUGS/EXER)
- 1-2 family history templates
- 1-2 progress note document templates with realistic clinical text
- 1-2 radiology order templates if clinically relevant (e.g. CXR for cardiac/pulmonary patients)
- 1 encounter_pattern block that reflects this condition's realistic care frequency:
  * encounters_per_year: float (e.g. 2.0 for healthy, 4.0 for hypertension, 8.0 for CHF/ESRD)
  * encounter_type_weights: object with keys "O" (outpatient), "E" (emergency), "I" (inpatient)
    summing to 1.0 (e.g. a CHF patient has more ED/inpatient visits than a hypertension patient)
  * lab_encounter_rate: float 0–1, probability a lab order is placed at a given encounter
  * rad_encounter_rate: float 0–1, probability a radiology order is placed at a given encounter

All weights within each list must sum to 1.0 (or be proportional — they will be normalized).
note_template placeholders available: {{patient_name}}, {{age}}, {{sex}}, {{diagnosis}},
{{provider}}, {{chief_complaint}}, {{bp}}, {{hr}}, {{weight}}, {{compliance_statement}}, {{plan}}

{_lab_constraints_text(cohort_hints_lib or {})}

{_matched_cohort_hints(cohort["name"], cohort_hints_lib or {}, library or {})}
{novel_hint}{lib_hint}
Schema:
{_P3_SCHEMA}""", label)
    return result


# ---------------------------------------------------------------------------
# Phase 4 — Shared catalogs
# ---------------------------------------------------------------------------

_P4_SYSTEM = """\
Generate shared clinical data catalogs for a US patient population generator.
All codes must be real codes from the specified standards.
Output ONLY valid JSON."""

_P4_SCHEMA = """\
{
  "allergies": [
    {
      "allergen_code": "372687004",
      "allergen_description": "Amoxicillin",
      "category_code": "DA",
      "category_description": "Drug Allergy",
      "reaction_code": "247472004",
      "reaction_description": "Hives",
      "severity_code": "MO",
      "severity_description": "Moderate",
      "weight": 0.08
    }
  ],
  "vaccinations": [
    {
      "cvx_code": "141",
      "description": "Influenza, seasonal, injectable",
      "min_age_years": 0,
      "max_age_years": 120,
      "weight": 0.25
    }
  ],
  "encounter_patterns": [
    {
      "id": "routine_outpatient",
      "label": "Routine Outpatient Visit",
      "encounter_type": "O",
      "duration_days": 0,
      "encounters_per_year": 3.0,
      "weight": 0.5
    }
  ]
}"""


async def phase4_shared(client, model: str, description: str) -> dict:
    print("Phase 4: Shared catalogs...", flush=True)
    result = await call_llm_json(client, model, _P4_SYSTEM, f"""\
Population context: {description[:400]}

Generate shared clinical catalogs for a US patient population.

Requirements:
- 12-18 common allergies (mix of drug, food, environmental; real SNOMED CT codes)
- 8-12 common US vaccinations with appropriate age ranges (real CVX codes)
- 4-6 encounter patterns covering outpatient, inpatient, emergency, and generated types

All weights within each list must sum to 1.0.

Schema:
{_P4_SCHEMA}""", "phase4")
    print(f"  ✓ {len(result.get('allergies',[]))} allergies  "
          f"{len(result.get('vaccinations',[]))} vaccinations  "
          f"{len(result.get('encounter_patterns',[]))} encounter patterns", flush=True)
    return result


# ---------------------------------------------------------------------------
# Phase 5 — Scenario engine
# ---------------------------------------------------------------------------

_SCENARIO_KEYWORDS = (
    r"outbreak", r"spread", r"geographic expand", r"seeding", r"origin county",
    r"diagnostic delay", r"diagnostic learning", r"awareness grow",
    r"feed gap", r"feed outage", r"missing.*feed", r"lab feed",
    r"result delay", r"48.{0,5}hour", r"72.{0,5}hour", r"delayed.*result",
    r"acute cluster", r"acute surge", r"acute event", r"case surge",
    r"data imperfect", r"duplicate.*mrn", r"data quality", r"feed imperfect",
)

_P5_SYSTEM = """\
You are a health informatics architect building a synthetic patient population
scenario configuration. Extract structured scenario parameters from the
population description. Output ONLY valid JSON."""

_P5_SCHEMA = """\
{
  "scenarios": {
    "outbreak_seeding": {
      "enabled": true,
      "description": "one-sentence summary of the outbreak origin and spread pattern",
      "origin_county_fips": "5-digit FIPS of the origin county",
      "origin_region": "region name matching the origin",
      "phases": [
        {
          "months": [1, 6],
          "origin_region_share": 0.70,
          "adjacent_region_share": 0.25,
          "other_share": 0.05
        }
      ]
    },
    "diagnostic_learning": {
      "enabled": true,
      "description": "how clinician awareness and coding improve over time",
      "phases": [
        {
          "months": [1, 12],
          "label": "pre-recognition",
          "primary_dx_strategy": "symptom_codes",
          "oap_assay_available": false,
          "oap_ordering_rate": 0.0,
          "specialist_referral_rate": 0.05,
          "note_language": "unexplained inflammatory syndrome, etiology under investigation"
        },
        {
          "months": [13, 18],
          "label": "emerging-recognition",
          "primary_dx_strategy": "mixed",
          "oap_assay_available": true,
          "oap_assay_available_facility_types": ["academic", "tertiary"],
          "oap_ordering_rate": 0.30,
          "specialist_referral_rate": 0.25,
          "note_language": "possible novel inflammatory syndrome; OAP assay ordered"
        },
        {
          "months": [19, 36],
          "label": "established-recognition",
          "primary_dx_strategy": "syndrome_codes",
          "oap_assay_available": true,
          "oap_assay_available_facility_types": ["academic","tertiary","community","specialty"],
          "oap_ordering_rate": 0.80,
          "specialist_referral_rate": 0.55,
          "note_language": "confirmed novel inflammatory syndrome"
        }
      ]
    },
    "lab_feed_gap": {
      "enabled": true,
      "description": "summary of the lab feed outage",
      "start_month_offset": 18,
      "duration_days": 45,
      "affected_facility_types": ["community", "rural"],
      "affected_data_types": ["labs"],
      "gap_type": "complete_absence"
    },
    "result_delay": {
      "enabled": true,
      "description": "summary of systematic result delays",
      "affected_facility_types": ["academic", "tertiary"],
      "delay_hours_min": 48,
      "delay_hours_max": 72,
      "affected_data_types": ["labs", "rad_orders"],
      "applies_throughout": true
    },
    "acute_cluster": {
      "enabled": true,
      "description": "summary of the acute surge event",
      "county_name": "county where surge occurs",
      "county_fips": "5-digit FIPS",
      "start_month": 28,
      "end_month": 36,
      "affected_cohorts": ["novel_disease_severe", "novel_disease_critical"],
      "incidence_multiplier": 3.0,
      "transfer_rate_override": 0.40,
      "transfer_destination_facility_types": ["academic", "tertiary"]
    },
    "data_imperfections": {
      "enabled": true,
      "description": "summary of data quality issues",
      "duplicate_mrn_across_facilities_pct": 0.03,
      "outdated_demographics_pct": 0.06,
      "missing_identifier_pct": 0.02,
      "conflicting_demographics_pct": 0.01
    }
  }
}"""


def _has_scenarios(description: str) -> bool:
    """Return True if the description contains scenario-type language."""
    return any(
        re.search(kw, description, re.IGNORECASE)
        for kw in _SCENARIO_KEYWORDS
    )


async def phase5_scenarios(client, model: str, description: str,
                            structure: dict) -> dict:
    """Phase 5: extract scenario configuration from description."""
    print("Phase 5: Scenario engine...", flush=True)

    # Pass facility context so LLM can reference real facility codes/types
    fac_summary = [
        {"code": f["code"], "type": f.get("type", ""), "region": f.get("region", "")}
        for f in structure.get("facilities", [])[:20]  # first 20 to keep prompt small
    ]
    cohort_ids = [c["id"] for c in structure.get("cohorts", [])]
    history_months = structure.get("meta", {}).get("history_months", 36)

    result = await call_llm_json(client, model, _P5_SYSTEM, f"""\
Population description:
{description}

Extract a structured scenario configuration from the description above.
The simulation history is {history_months} months.

Available cohort IDs: {cohort_ids}
Facility sample (first 20): {json.dumps(fac_summary)}

Rules:
- Set enabled=false for any scenario not mentioned in the description.
- For outbreak_seeding: derive origin county from the description (e.g. "SE/Appalachian Ohio"
  -> use the most rural/remote county in that region). Set phase month ranges to match any
  described timeline; if vague, use reasonable quarterly steps across the history window.
- For diagnostic_learning: map the described awareness timeline to phase month ranges.
  If a specific assay or biomarker is mentioned, set oap_assay_available accordingly.
- For lab_feed_gap: extract the duration (e.g. "45 days"), when it occurs
  (start_month_offset from month 1), and which facility types are affected.
- For result_delay: extract the delay hours and which facility types (usually high-volume
  academic/tertiary centers).
- For acute_cluster: identify the county, the period (months from start), and the severity
  multiplier implied by "acute surge" or similar language.
- For data_imperfections: extract any stated percentages; if only described qualitatively
  use reasonable small percentages (0.01–0.06).
- Use real county FIPS codes where applicable.

Schema:
{_P5_SCHEMA}""", "phase5")

    n_enabled = sum(
        1 for v in result.get("scenarios", {}).values()
        if isinstance(v, dict) and v.get("enabled")
    )
    print(f"  ✓ {n_enabled} scenarios enabled", flush=True)
    return result


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _normalize_weights(items: list, key: str = "weight") -> list:
    """Re-normalize weights in a list so they sum to 1.0."""
    total = sum(item.get(key, 1.0) for item in items)
    if total <= 0:
        return items
    for item in items:
        item[key] = round(item.get(key, 1.0) / total, 6)
    return items


def merge_template(structure: dict, names: dict, cohort_catalogs: list, shared: dict,
                   library: dict | None = None, description: str = "") -> dict:
    cohort_defs = structure.get("cohorts", [])
    merged_cohorts = []
    for cohort_def, catalog in zip(cohort_defs, cohort_catalogs):
        merged = {**cohort_def, **catalog}
        # Apply QA-hardened library ranges before normalizing weights
        if library:
            merged = apply_library_overrides(merged, library, merged.get("name", ""))
        # Normalize weights within catalog sub-lists
        for key in ("medications", "labs", "observations", "procedures",
                    "illness_history_templates", "social_history_templates",
                    "family_history_templates", "rad_order_templates"):
            if isinstance(merged.get(key), list) and merged[key]:
                _normalize_weights(merged[key])
        merged_cohorts.append(merged)

    # multi_facility: merge LLM output over stable defaults so no field is ever missing.
    # Default ed_ip_same_facility_lock: False when description mentions transfers.
    import re as _re
    _desc_has_transfers = bool(
        _re.search(r'transfer|cross.organiz|cross.facilit', description or "", _re.IGNORECASE)
    )
    _mf_defaults = {
        "enabled": True,
        "distribution": {
            "one_facility_pct": 0.55,
            "two_facility_pct": 0.30,
            "three_plus_facility_pct": 0.15,
            "max_facilities": 3,
        },
        "encounter_type_outside_prob": {"O": 0.08, "E": 0.40, "I": 0.20},
        "geographic_affinity": {
            "same_region_weight": 0.70,
            "adjacent_region_weight": 0.20,
            "other_region_weight": 0.10,
        },
        "adjacent_regions": {},
        "ed_ip_same_facility_lock": not _desc_has_transfers,
        "facility_mrn_prefix": True,
    }
    _mf_llm = structure.get("multi_facility", {})
    _multi_facility = {**_mf_defaults, **_mf_llm}
    # Deep-merge sub-dicts so a partial LLM response doesn't drop defaults
    for _sub in ("distribution", "encounter_type_outside_prob", "geographic_affinity"):
        _multi_facility[_sub] = {**_mf_defaults[_sub], **_mf_llm.get(_sub, {})}
    # If LLM didn't generate adjacent_regions, derive them from facility regions:
    # each region is adjacent to every other region (safe default).
    if not _multi_facility.get("adjacent_regions"):
        _regions = sorted({f.get("region", "") for f in structure.get("facilities", [])
                           if f.get("region")})
        _multi_facility["adjacent_regions"] = {
            r: [x for x in _regions if x != r] for r in _regions
        }

    template = {
        "meta":             structure.get("meta", {}),
        "geography":        structure.get("geography", {}),
        "demographics":     structure.get("demographics", {}),
        "facilities":       structure.get("facilities", []),
        "providers":        structure.get("providers", []),
        "insurance_plans":  structure.get("insurance_plans", []),
        "sending_facility": structure.get("sending_facility", ""),
        "multi_facility":   _multi_facility,
        "name_pools":       names.get("name_pools", {}),
        "cohorts":          merged_cohorts,
        "catalogs": {
            "allergies":          _normalize_weights(shared.get("allergies", [])),
            "vaccinations":       _normalize_weights(shared.get("vaccinations", [])),
            "encounter_patterns": _normalize_weights(shared.get("encounter_patterns", [])),
        },
    }
    _fix_template(template)
    _warn_template(template)
    return template


_BACKGROUND_COHORT = {
    "id": "general_population",
    "name": "General Population",
    "min_age": 0,
    "max_age": 120,
    "sex_bias": None,
    "description": "General background population — healthy adults and minor acute conditions",
    "diagnoses": [
        {"code": "Z00.00",
         "description": "Encounter for general adult medical exam without abnormal findings",
         "is_primary": True, "onset_age_min": 0, "onset_age_max": 120,
         "only_encounter_types": []},
    ],
    "comorbidities": [],
    "medications": [],
    "labs": [],
    "observations": [],
    "procedures": [
        {"code": "99213",
         "description": "Office or other outpatient visit, established patient",
         "encounter_type": "O", "weight": 1.0},
    ],
    "illness_history_templates": [],
    "social_history_templates": [],
    "family_history_templates": [],
    "document_templates": [],
    "rad_order_templates": [],
    "encounter_pattern": {
        "encounters_per_year": 1.5,
        "encounter_type_weights": {"O": 0.90, "E": 0.08, "I": 0.02},
        "lab_encounter_rate": 0.25,
        "rad_encounter_rate": 0.05,
    },
}

_BACKGROUND_KEYWORDS = ("general", "background", "unaffected", "healthy", "wellness",
                         "preventive", "low risk")


def _fix_template(template: dict) -> None:
    """
    In-place corrections applied after LLM generation:
    1. Deduplicate geography locations by county name; re-normalize weights.
    2. Re-normalize facility weights.
    3. Auto-fill missing cohort weight with a generic background cohort.
    """
    # 1. Geography dedup + normalize
    locs = template.get("geography", {}).get("locations", [])
    seen = {}
    for loc in locs:
        key = (loc.get("county", ""), loc.get("state_code", ""))
        if key not in seen:
            seen[key] = loc
    deduped = list(seen.values())
    if len(deduped) != len(locs):
        print(f"  [FIX] Deduplicated geography: {len(locs)} -> {len(deduped)} locations",
              flush=True)
    total_geo = sum(l.get("weight", 0) for l in deduped)
    if deduped and abs(total_geo - 1.0) > 0.001:
        for l in deduped:
            l["weight"] = round(l.get("weight", 0) / total_geo, 6)
        print(f"  [FIX] Normalized geography weights (was {total_geo:.4f})", flush=True)
    template["geography"]["locations"] = deduped

    # 2. Facility weight normalize
    facs = template.get("facilities", [])
    total_fac = sum(f.get("weight", 0) for f in facs)
    if facs and abs(total_fac - 1.0) > 0.001:
        for f in facs:
            f["weight"] = round(f.get("weight", 0) / total_fac, 6)
        print(f"  [FIX] Normalized facility weights (was {total_fac:.4f})", flush=True)

    # 3. Cohort weight fill
    cohorts = template.get("cohorts", [])
    total_coh = sum(c.get("weight", 0) for c in cohorts)
    remaining = round(1.0 - total_coh, 6)
    if remaining > 0.05:
        # Always add a new General Population cohort for the unspecified background.
        # Never inflate an existing preventive/wellness cohort — its stated weight is correct.
        has_genpop = any("general" in c.get("name", "").lower() or
                         "background" in c.get("name", "").lower()
                         for c in cohorts)
        if not has_genpop:
            bg = {**_BACKGROUND_COHORT, "weight": remaining}
            cohorts.append(bg)
            print(f"  [FIX] Added 'General Population' cohort (weight={remaining:.4f})",
                  flush=True)

    # Final cohort weight re-check and hard normalize if still off
    total_coh2 = sum(c.get("weight", 0) for c in cohorts)
    if abs(total_coh2 - 1.0) > 0.001:
        for c in cohorts:
            c["weight"] = round(c.get("weight", 0) / total_coh2, 6)
        print(f"  [FIX] Hard-normalized cohort weights (was {total_coh2:.4f})", flush=True)


def _warn_template(template: dict) -> None:
    """Print summary and flag any remaining anomalies after _fix_template."""
    cohorts = template.get("cohorts", [])
    total_w = sum(c.get("weight", 0) for c in cohorts)
    if cohorts and abs(total_w - 1.0) > 0.01:
        print(f"  [WARN] Cohort weights sum to {total_w:.4f} — should be 1.0", flush=True)

    # Flag any single cohort that dominates in a multi-cohort template
    if len(cohorts) >= 4:
        for c in cohorts:
            w = c.get("weight", 0)
            if w > 0.5:
                print(f"  [WARN] Cohort '{c.get('name')}' weight={w:.3f} (>50%)"
                      " — possible decimal-place error if disease prevalence is stated",
                      flush=True)

    # Count summary
    locs = template.get("geography", {}).get("locations", [])
    facs = template.get("facilities", [])
    print(f"  Cohorts: {len(cohorts)}  Facilities: {len(facs)}"
          f"  Counties: {len(locs)}  Providers: {len(template.get('providers', []))}",
          flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args):
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: File not found: {args.input}")
        sys.exit(1)

    description = input_path.read_text(encoding="utf-8").strip()
    output_path = Path(args.output) if args.output else input_path.with_suffix(".template.json")

    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(f"Model:  {args.model}  |  Base URL: {args.base_url or '(OpenAI default)'}\n")

    client = _make_async_client(args.base_url)

    # Phase 1
    structure = await phase1_structure(client, args.model, description)

    # Phase 2
    names = await phase2_names(client, args.model)

    # Phase 3 — concurrent (catalog hits skip the LLM entirely)
    library = load_lab_library(getattr(args, "library", None))
    cohort_hints_lib = load_cohort_hints()
    catalog = load_cohort_catalog()
    cohorts = structure.get("cohorts", [])
    _catalog_hits = {c["name"]: _find_catalog_match(c["name"], catalog) for c in cohorts}
    n_cached = sum(1 for v in _catalog_hits.values() if v)
    print(
        f"Phase 3: Clinical catalogs for {len(cohorts)} cohorts "
        f"({n_cached} from catalog, {len(cohorts) - n_cached} via LLM)...",
        flush=True,
    )
    results = await asyncio.gather(
        *[_resolve_cohort(client, args.model, c, description, library, cohort_hints_lib, catalog)
          for c in cohorts],
        return_exceptions=True,
    )
    cohort_catalogs = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"  [WARN] Cohort '{cohorts[i]['name']}' failed: {r}", flush=True)
            cohort_catalogs.append({})
        else:
            hit = _catalog_hits.get(cohorts[i]["name"])
            source = f"[catalog: {hit['canonical_name']}]" if hit else "[LLM]"
            print(f"  ✓ {cohorts[i]['name']} {source}", flush=True)
            cohort_catalogs.append(r)

    # Phase 4
    shared = await phase4_shared(client, args.model, description)

    # Phase 5 — scenarios (only when description contains scenario language)
    scenarios_result = None
    if _has_scenarios(description):
        scenarios_result = await phase5_scenarios(client, args.model, description, structure)
    else:
        print("Phase 5: No scenario keywords detected — skipping.", flush=True)

    # Merge and write
    print("\nMerging and writing template...", flush=True)
    template = merge_template(structure, names, cohort_catalogs, shared,
                              library=library, description=description)
    if scenarios_result and "scenarios" in scenarios_result:
        template["scenarios"] = scenarios_result["scenarios"]
    output_path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")

    n_cohorts   = len(template.get("cohorts", []))
    n_facilities = len(template.get("facilities", []))
    n_counties  = len(template.get("geography", {}).get("locations", []))
    n_allergies = len(template.get("catalogs", {}).get("allergies", []))

    n_scenarios = len(template.get("scenarios", {}))

    print(f"\n✓ Template written -> {output_path}")
    print(f"  {n_cohorts} cohorts  |  {n_facilities} facilities  |  {n_counties} counties")
    print(f"  {n_allergies} shared allergies  |  {n_scenarios} scenario(s)")
    print(f"\nNext step: python generate_population.py --template {output_path} --count 100")


def main():
    parser = argparse.ArgumentParser(
        description="Convert a population text description into a rich JSON template."
    )
    parser.add_argument("input", nargs="?",
                        help="Path to the .txt file describing the population "
                             "(not required when --update-catalog is used)")
    parser.add_argument("--output",
                        help="Output path for the template JSON "
                             "(default: <input>.template.json)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"LLM model (default: {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="OpenAI-compatible base URL. Omit for OpenAI; "
                             f"use {ISC_LLM_BASE_URL} for internal ISC LLM")
    parser.add_argument("--library", default=None,
                        help="Path to lab_panels_library.json. "
                             "Defaults to lab_panels_library.json in the script directory "
                             "if present. Pass 'none' to disable.")
    parser.add_argument("--update-catalog", metavar="TEMPLATE",
                        help="Extract QA-approved cohort definitions from TEMPLATE into "
                             "config/cohort_catalog.json and exit. Run this after every "
                             "successful QA pass to feed learning back into future "
                             "template generation. "
                             "Example: --update-catalog templates/ohio_demo.template.json")
    parser.add_argument("--sync-configs", metavar="TEMPLATE",
                        help="Push all QA-approved definitions from TEMPLATE back into "
                             "config/cohort_catalog.json AND config/lab_panels_library.json "
                             "in one pass. Preferred over --update-catalog when you want "
                             "both catalogs updated. "
                             "Example: --sync-configs templates/ohio_demo.template.json")
    args = parser.parse_args()

    # --sync-configs mode: update catalog + lab library in one pass
    if args.sync_configs:
        sync_configs_from_template(args.sync_configs)
        return

    # --update-catalog mode: seed/refresh the catalog from a QA-approved template
    if args.update_catalog:
        print(f"Updating cohort catalog from {args.update_catalog}...", flush=True)
        hints = load_cohort_hints()
        update_catalog_from_template(args.update_catalog, cohort_hints_lib=hints)
        return

    if not args.input:
        parser.error("input is required unless --update-catalog is specified")

    if getattr(args, "library", None) and args.library.lower() == "none":
        args.library = ""  # disable
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
