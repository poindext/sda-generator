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
    "state": "full state name",
    "state_code": "2-letter USPS code",
    "medicaid_field": "state Medicaid program name",
    "medicaid_id_prefix": "prefix for Medicaid IDs e.g. OH-MEDICAID-",
    "total_patients": integer,
    "history_months": integer
  },
  "geography": {
    "locations": [
      {
        "county": "County Name",
        "county_fips": "5-digit FIPS",
        "state_code": "OH",
        "region": "NE Ohio",
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
      "code": "CLECLINIC",
      "name": "Cleveland Clinic Main Campus",
      "type": "hospital",
      "health_system_code": "CCHS",
      "health_system_name": "Cleveland Clinic Health System",
      "city": "Cleveland",
      "region": "NE Ohio",
      "county_fips": "39035",
      "address": "9500 Euclid Ave",
      "zip": "44195",
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
      "code": "OHMED",
      "name": "Ohio Medicaid",
      "coding_standard": "QD_HealthPlanCodeList",
      "type": "Medicaid",
      "plan_code": "OH-MED-PLAN",
      "plan_name": "Ohio Medicaid Managed Care Plan",
      "group_name": "State of Ohio Medicaid Program",
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
      "NE Ohio": ["Central Ohio", "NW Ohio"],
      "Central Ohio": ["NE Ohio", "SW Ohio", "SE Ohio"],
      "SW Ohio": ["Central Ohio", "SE Ohio"],
      "NW Ohio": ["NE Ohio"],
      "SE Ohio": ["Central Ohio", "SW Ohio"]
    },
    "ed_ip_same_facility_lock": true,
    "facility_mrn_prefix": true
  }
}"""


async def phase1_structure(client, model: str, description: str) -> dict:
    print("Phase 1: Population structure...", flush=True)
    result = await call_llm_json(client, model, _P1_SYSTEM, f"""\
Population description:
{description}

Generate a JSON object matching the schema below. Include:
- 8-15 geographic locations, each with a "region" label grouping them
  (e.g. "NE Ohio", "Central Ohio") — 3-6 distinct regions total
- 8-15 facilities, each with a health_system_code/name (group related facilities
  under the same health system), a region matching one of the location regions,
  and a realistic weight (larger/academic centers get higher weight)
- 10-20 providers spread across facilities
- 3-8 insurance plans
- 6-12 clinical cohorts
- A multi_facility block: set adjacent_regions to reflect actual geographic
  adjacency between the regions you defined above; keep all other values as shown
All weights within each array must sum to 1.0.

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
    result = await call_llm_json(client, model, _P3_SYSTEM, f"""\
Population context (brief): {description[:400]}

Generate a complete clinical data catalog for the "{cohort['name']}" cohort.
{cohort.get('description', '')}
Patient age range: {cohort.get('min_age', 18)}–{cohort.get('max_age', 85)} years.

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
{lib_hint}
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
                   library: dict | None = None) -> dict:
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
        "ed_ip_same_facility_lock": True,
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

    return {
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

    # Phase 3 — concurrent (one call per cohort)
    library = load_lab_library(getattr(args, "library", None))
    cohort_hints_lib = load_cohort_hints()
    cohorts = structure.get("cohorts", [])
    print(f"Phase 3: Clinical catalogs for {len(cohorts)} cohorts (concurrent)...", flush=True)
    results = await asyncio.gather(
        *[phase3_cohort(client, args.model, c, description,
                        library=library, cohort_hints_lib=cohort_hints_lib)
          for c in cohorts],
        return_exceptions=True,
    )
    cohort_catalogs = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"  [WARN] Cohort '{cohorts[i]['name']}' failed: {r}", flush=True)
            cohort_catalogs.append({})
        else:
            print(f"  ✓ {cohorts[i]['name']}", flush=True)
            cohort_catalogs.append(r)

    # Phase 4
    shared = await phase4_shared(client, args.model, description)

    # Merge and write
    print("\nMerging and writing template...", flush=True)
    template = merge_template(structure, names, cohort_catalogs, shared, library=library)
    output_path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")

    n_cohorts   = len(template.get("cohorts", []))
    n_facilities = len(template.get("facilities", []))
    n_counties  = len(template.get("geography", {}).get("locations", []))
    n_allergies = len(template.get("catalogs", {}).get("allergies", []))

    print(f"\n✓ Template written → {output_path}")
    print(f"  {n_cohorts} cohorts  |  {n_facilities} facilities  |  {n_counties} counties")
    print(f"  {n_allergies} shared allergies")
    print(f"\nNext step: python generate_population.py --template {output_path} --count 100")


def main():
    parser = argparse.ArgumentParser(
        description="Convert a population text description into a rich JSON template."
    )
    parser.add_argument("input",
                        help="Path to the .txt file describing the population")
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
    args = parser.parse_args()
    if getattr(args, "library", None) and args.library.lower() == "none":
        args.library = ""  # disable
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
