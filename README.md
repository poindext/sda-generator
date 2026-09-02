# SDA3 Synthetic Patient Population Generator

Generates realistic synthetic patient populations in InterSystems HealthShare **SDA3 XML** format for use in healthcare IT demonstrations, analytics testing, and platform evaluation.

Populations are defined by cohort templates (diabetes, hypertension, asthma, COPD, tick-borne diseases, etc.) and produced entirely in Python with no LLM or API key required. An optional automated clinical QA loop uses GPT-4o to review the output for clinical plausibility and iterates until the population passes review.

---

## Project Layout

```
sda_generator/
├── scripts/          Python tools (see Scripts section below)
├── templates/        Population templates, QA configs, and design notes
├── config/           Shared data libraries (lab panel reference data)
├── schema/           SDA3 XSD validation schemas
├── populations/      Generated output directories (gitignored)
├── sample_data/      Reference SDA3 XML samples (gitignored)
├── logs/             auto_qa_changelog.jsonl and other audit trails
└── .claude/commands/ /auto-qa slash command for Claude Code
```

---

## Prerequisites

- **Python 3.11+**
- **pip packages:**

```bash
pip install openai lxml requests
```

- **OpenAI API key** (required for `design_population.py`, `auto_qa.py`, `population_wizard.py`; not needed for `generate_population.py`)

---

## Setup

1. Clone the repository and install dependencies:

```bash
git clone <repo-url>
cd sda_generator
python3 -m venv .venv
source .venv/bin/activate
pip install openai lxml requests
```

2. Create a `.env` file in the project root with your OpenAI API key:

```
OPENAI_API_KEY=sk-proj-...
```

The `.env` file is gitignored and must never be committed.

3. To use the OpenAI key in your shell session:

```bash
export OPENAI_API_KEY=$(grep OPENAI_API_KEY .env | cut -d= -f2-)
```

---

## Workflow Overview

The typical workflow for a new population is:

```
describe → design → generate → QA review → load into HealthShare
  (.txt)    (.template.json)   (CSVs + XML)   (auto_qa.py)
```

All scripts are run from the **project root directory**.

---

## Scripts

### `scripts/population_wizard.py` — Interactive population designer

An interview-driven wizard that asks questions about the desired population and produces a structured description document. Optionally runs `design_population.py` immediately after.

```bash
# Interactive wizard — produces a .txt description
python3 scripts/population_wizard.py --output templates/my_population.txt

# Wizard + auto-generate the full JSON template in one step
python3 scripts/population_wizard.py \
  --output templates/my_population.txt \
  --generate-template \
  --template-output templates/my_population.template.json
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--output` | `population_design.txt` | Path for the generated description document |
| `--generate-template` | off | Also run `design_population.py` after the wizard |
| `--template-output` | `<output>.template.json` | Path for the JSON template (with `--generate-template`) |
| `--model` | `gpt-4o` | LLM model |
| `--base-url` | (OpenAI) | OpenAI-compatible base URL for internal LLM endpoints |

---

### `scripts/design_population.py` — Text description → JSON template

Converts a `.txt` population description into a rich JSON template that controls cohort composition, diagnoses, medications, lab panels, and demographics. Uses the lab panels library in `config/` to populate realistic lab ranges.

```bash
python3 scripts/design_population.py templates/fl_demo.txt \
  --output templates/fl_demo.template.json
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--output` | `<input>.template.json` | Output path for the template JSON |
| `--model` | `gpt-4o` | LLM model |
| `--library` | `config/lab_panels_library.json` | Lab panels reference library |
| `--base-url` | (OpenAI) | OpenAI-compatible base URL |

---

### `scripts/generate_population.py` — Generate patients from a template

The main generator. Runs entirely in Python (no LLM, no API key required) and produces SDA3 XML files plus a set of analysis CSVs.

```bash
# Generate 1000 patients from the Florida template
python3 scripts/generate_population.py \
  --template templates/fl_demo.template.json \
  --output populations/population-fl_1000 \
  --count 1000
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--template` | — | Template JSON file (required) |
| `--output` | `Population/` | Output directory for XML + CSVs |
| `--count` | `10` | Number of patients to generate |
| `--concurrency` | `10` | Parallel workers |
| `--resume` | off | Skip patients whose files already exist |
| `--no-validate` | off | Skip XSD validation (faster) |

**Output files** (all in the output directory):

| File | Description |
|------|-------------|
| `patients.csv` | Demographics, age, sex, BMI, primary facility |
| `encounters.csv` | All encounter records with type, date, diagnoses |
| `medications.csv` | All prescriptions with dose, frequency, start/end dates |
| `labs.csv` | All lab results with LOINC codes, values, reference ranges |
| `patient_facilities.csv` | Patient–facility associations |
| `generator_validation.csv` | Any structural validation failures (empty = clean) |
| `<MRN>.xml` | Individual SDA3 XML file per patient |

---

### `scripts/auto_qa.py` — Clinical QA reviewer

Builds a clinical review package from the population CSVs and sends it to GPT-4o for clinical plausibility review. Reports issues by severity (CRITICAL → HIGH → MEDIUM → LOW) and logs results to `logs/auto_qa_changelog.jsonl`.

```bash
export OPENAI_API_KEY=$(grep OPENAI_API_KEY .env | cut -d= -f2-)

python3 scripts/auto_qa.py \
  --template templates/fl_demo.template.json \
  --output populations/population-fl_1000
```

**Exit codes:** `0` = approved, `1` = issues found (see `<output>/qa_issues.json`), `2` = runtime error.

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--template` | — | Template file (used to load matching `.qa_config.json`) |
| `--output` | — | Population output directory |
| `--model` | `gpt-4o` | OpenAI model for review |
| `--generate` | off | Run `generate_population.py` before reviewing |
| `--count` | 0 | Patient count (with `--generate`) |
| `--changelog` | `logs/auto_qa_changelog.jsonl` | Changelog path |

**Template-specific QA config:** Place a `<template_stem>.qa_config.json` file in `templates/` alongside the template to inject population-specific context into the reviewer prompt. See `templates/fl_demo.qa_config.json` for an example. The config supports two keys:
- `description` — one-line description of the population shown to the reviewer
- `clarifications` — list of dataset-specific notes (e.g., "A1c is only ordered for confirmed diabetic patients")

---

### `scripts/extract_cohort_csvs.py` — Extract CSVs from XML shards

Parses SDA3 XML files and extracts structured CSVs. Used when you have XML files but not the CSVs (e.g., after loading into HealthShare and exporting).

```bash
python3 scripts/extract_cohort_csvs.py \
  --input populations/population-fl_1000 \
  --output populations/population-fl_1000/extracted_csvs
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--input` | — | Directory containing SDA3 XML files |
| `--output` | — | Output directory for CSV files |
| `--workers` | `8` | Parallel workers |
| `--limit` | `0` (all) | Process only first N files (for testing) |

---

## Automated QA Loop (`/auto-qa`)

The `/auto-qa` Claude Code slash command runs the full generate → review → fix → regenerate loop autonomously until the population achieves a clean pass or reaches 5 rounds.

```
/auto-qa --template templates/fl_demo.template.json \
         --output populations/population-fl_1000 \
         --count 1000
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `--template` | Template file |
| `--output` | Population output directory |
| `--count` | Patient count |
| `--review-only` | Skip generation; review existing output only |
| `--model` | OpenAI model (default: `gpt-4o`) |

The loop logs every fix applied to `logs/auto_qa_changelog.jsonl`.

---

## Template Files

Templates live in `templates/`. Two file types are associated with each population:

---

### `<name>.template.json` — Population template

The primary input to `generate_population.py`. Produced by `design_population.py` and hand-editable. Controls every aspect of the generated population.

**Top-level sections:**

| Section | Type | Description |
|---------|------|-------------|
| `meta` | object | Population-level metadata |
| `demographics` | object | Age, sex, race, and insurance distributions |
| `geography` | object | County/city location pool with weights |
| `facilities` | array | Healthcare facilities patients can be assigned to |
| `providers` | array | Clinicians (linked to facilities) |
| `insurance_plans` | array | Insurance plan definitions |
| `name_pools` | object | First-name, last-name, and middle-initial pools |
| `sending_facility` | string | Default `SendingFacility` code for all SDA3 records |
| `multi_facility` | object | Multi-facility visit assignment rules |
| `cohorts` | array | Clinical cohort definitions (the core section) |
| `catalogs` | object | Shared allergy, vaccination, and encounter pattern catalogs |

---

**`meta` object:**

| Field | Description |
|-------|-------------|
| `name` | Population display name |
| `description` | One-line description |
| `state` / `state_code` | State name and two-letter code |
| `medicaid_field` | Medicaid plan label used in insurance assignment |
| `medicaid_id_prefix` | Prefix for generated Medicaid ID strings |
| `total_patients` | Target population size (informational; `--count` overrides at generation time) |
| `history_months` | Number of months of clinical history to generate |

---

**`demographics` object:**

Each sub-key holds a weighted distribution array. Weights are normalized by the generator and need not sum to 1.

| Sub-key | Item fields |
|---------|-------------|
| `age_distribution` | `min_age`, `max_age`, `weight` |
| `sex_distribution` | `sex` (`"M"` / `"F"`), `weight` |
| `race_distribution` | `description` (plain English label), `weight` |
| `insurance_distribution` | `plan_code` (matches `insurance_plans[].code`), `weight` |

---

**`geography.locations` array:**

| Field | Description |
|-------|-------------|
| `county` | County name |
| `county_fips` | 5-digit FIPS code |
| `state_code` | Two-letter state code |
| `weight` | Relative patient-assignment weight for this county |
| `rurality` | `"urban"`, `"suburban"`, or `"rural"` |
| `region` | Region label (used by `multi_facility.adjacent_regions`) |
| `cities` | Array of `{name, zips[]}` — a city is picked at address generation time |

---

**`facilities` array:**

| Field | Description |
|-------|-------------|
| `code` | Facility identifier used in SDA3 `EnteredAt` and `SendingFacility` |
| `name` | Display name |
| `health_system_code` / `health_system_name` | Parent health system |
| `type` | `"hospital"`, `"clinic"`, etc. |
| `city`, `county_fips`, `address`, `zip` | Location fields |
| `weight` | Relative weight for home-facility assignment |
| `region` | Region label (must match a key used in `multi_facility.adjacent_regions`) |

**`providers` array:** `code`, `name`, `specialty`, `facility_code` (must match a facility `code`).

**`insurance_plans` array:** `code`, `name`, `coding_standard`, `type`, `plan_code`, `plan_name`, `group_name`, `priority`.

---

**`multi_facility` object:**

Controls whether patients visit more than one facility and how alternate facilities are chosen.

| Field | Description |
|-------|-------------|
| `enabled` | `true` / `false` |
| `distribution` | `{one_facility_pct, two_facility_pct, three_plus_facility_pct, max_facilities}` |
| `encounter_type_outside_prob` | Probability a given encounter type occurs at a non-home facility (`O`, `E`, `I` keys) |
| `geographic_affinity` | `{same_region_weight, adjacent_region_weight, other_region_weight}` |
| `adjacent_regions` | Map of region → list of neighboring region labels |
| `ed_ip_same_facility_lock` | `true` locks ED→inpatient admission pairs to the same facility |
| `facility_mrn_prefix` | `true` prefixes the MRN with the facility code |

---

**`cohorts` array** — the core section:

Each cohort defines a clinical phenotype. The generator assigns each patient to exactly one cohort using the `weight` field.

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Stable internal identifier |
| `name` | string | Display name |
| `weight` | number | Relative probability of being assigned this cohort |
| `min_age` / `max_age` | integer | Age eligibility range |
| `sex_bias` | string | `"M"`, `"F"`, or `"any"` |
| `description` | string | One-line clinical description |
| `diagnoses` | array | Primary and comorbid diagnoses |
| `comorbidities` | array | Additional diagnoses added with some probability |
| `medications` | array | Drug prescriptions with weighted selection |
| `labs` | array | Lab panels ordered at eligible encounters |
| `observations` | array | Vital sign observations (BP, HR, etc.) |
| `procedures` | array | CPT procedure codes added to encounters |
| `illness_history_templates` | array | Past medical history entries |
| `social_history_templates` | array | Social history entries (smoking, alcohol, etc.) |
| `family_history_templates` | array | Family history entries |
| `document_templates` | array | Clinical note templates with `note_template` strings |
| `rad_order_templates` | array | Radiology order + result text templates |
| `encounter_pattern` | object | Encounter frequency and type distribution |

**`diagnoses` / `comorbidities` item:**
```json
{ "code": "E11.9", "description": "...", "is_primary": true, "onset_age_min": 18, "onset_age_max": 85 }
```

**`medications` item:**
```json
{
  "drug_code": "197361", "drug_description": "Lisinopril 10 MG Oral Tablet",
  "dose_quantity": "10", "dose_uom": "mg",
  "dosage_form_code": "TAB", "dosage_form_description": "Tablet",
  "route_code": "PO", "route_description": "Oral",
  "frequency_code": "QD", "frequency_description": "Once daily",
  "status": "active", "for_diagnosis_code": "I10", "weight": 0.25
}
```

**`labs` item:** Same structure as a `lab_panels_library.json` panel entry plus `weight` and `abnormal_pct`.

**`observations` item:**
```json
{ "code": "8480-6", "description": "Systolic blood pressure", "units": "mmHg",
  "normal_min": 90, "normal_max": 119, "abnormal_min": 130, "abnormal_max": 175,
  "abnormal_flag": "H", "abnormal_pct": 0.45, "weight": 0.33 }
```

**`document_templates` item:**
```json
{ "type_code": "PN", "type_description": "Progress Note", "encounter_type": "O",
  "chief_complaints": ["Follow-up for hypertension management", ...],
  "note_template": "CHIEF COMPLAINT: {chief_complaint}\n\nHPI: ..." }
```
Available template variables: `{chief_complaint}`, `{patient_name}`, `{age}`, `{sex}`, `{diagnosis}`, `{plan}`, `{compliance_statement}`, `{bp}`, `{hr}`, `{weight}`.

**`rad_order_templates` item:**
```json
{ "order_code": "CXR-PA-LAT", "order_description": "Chest X-Ray PA and Lateral",
  "reason_description": "...", "result_texts": ["...", "..."], "weight": 0.6 }
```

**`illness_history_templates` item:**
```json
{ "condition": "Type 2 diabetes mellitus, diagnosed at age 45",
  "onset_at_age_min": 40, "onset_at_age_max": 55, "is_resolved": false, "weight": 0.56 }
```

**`social_history_templates` item:**
```json
{ "habit_code": "NS", "habit_description": "Non Smoker",
  "comments": ["Patient has never smoked.", "Former smoker, quit 10 years ago."], "weight": 0.6 }
```

**`encounter_pattern` object:**
```json
{ "encounters_per_year": 4.0,
  "encounter_type_weights": { "O": 0.9, "E": 0.05, "I": 0.05 },
  "lab_encounter_rate": 0.75,
  "rad_encounter_rate": 0.15 }
```

---

**`catalogs` object:**

| Sub-key | Description |
|---------|-------------|
| `allergies` | Shared allergy pool; each patient gets 0–2 allergies drawn from this list |
| `vaccinations` | Shared vaccination pool |
| `encounter_patterns` | Named encounter pattern objects (referenced by cohort encounter_pattern) |

**`catalogs.allergies` item:** `allergen_code`, `allergen_description`, `category_code`, `category_description`, `reaction_code`, `reaction_description`, `severity_code`, `severity_description`, `weight`.

---

### `<name>.qa_config.json` — QA reviewer context

Optional companion file placed alongside a template. `auto_qa.py` loads it automatically when it finds a file named `<template_stem>.qa_config.json` in the same directory. Its contents are injected into the GPT-4o reviewer prompt to provide population-specific context that prevents false-positive QA failures.

**Structure:**

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | One-line description of the population shown to the reviewer at the top of the prompt |
| `clarifications` | array of strings | Dataset-specific notes that explain expected patterns which might otherwise look like clinical errors (e.g. "A1c is only ordered for confirmed diabetic patients") |

**Example:**
```json
{
  "description": "Florida 1000-patient demo — cohorts: preventive_wellness, diabetes_t2, hypertension, ...",
  "clarifications": [
    "HbA1c showing 100% abnormal rate — in this dataset HbA1c is only ordered for patients with a confirmed diabetes diagnosis.",
    "Tick-borne disease cohort intentionally has no chronic medications."
  ]
}
```

Without a `.qa_config.json`, the reviewer uses only generic clinical heuristics and may flag patterns that are intentional design decisions in the template.

---

## Creating a New Population

1. Write a plain-English description of the population in a `.txt` file, or use the wizard:
   ```bash
   python3 scripts/population_wizard.py --output templates/my_pop.txt
   ```

2. Generate the template:
   ```bash
   python3 scripts/design_population.py templates/my_pop.txt
   ```

3. Generate the population:
   ```bash
   python3 scripts/generate_population.py \
     --template templates/my_pop.template.json \
     --output populations/population-my_pop_1000 \
     --count 1000
   ```

4. Run the QA loop until approved:
   ```
   /auto-qa --template templates/my_pop.template.json \
            --output populations/population-my_pop_1000 \
            --count 1000
   ```

---

## Configuration Files

All five files live in `config/`. Together they form a **three-layer flywheel** that improves population quality over time:

```
Layer 1 — cohort_hints_library.json   (guidance injected before the LLM runs)
Layer 2 — lab_panels_library.json     (authoritative ranges applied after the LLM runs)
Layer 3 — cohort_catalog.json         (approved definitions that bypass the LLM entirely)
```

`inpatient_scenarios.json` and `phrase_pools.json` are runtime libraries loaded by the generator itself.

---

### `config/lab_panels_library.json`

**Purpose:** 27 QA-vetted lab panel definitions. Each panel carries reference ranges, abnormal ranges, and disease-association tags.

**Used by:**
- `design_population.py` — applies panel definitions to every cohort template after the LLM generates cohort clinical data (`apply_library_overrides()`). The LLM-generated lab section names panels by LOINC order code; the library replaces the `result_items` array with vetted values so LLM-hallucinated ranges never reach a template.
- `design_population.py` Phase 3 prompt — injects lab value constraints (normal/abnormal min/max bounds) to steer the LLM toward plausible ranges before generation.

**When to edit:** Add a new panel when a new lab test appears in templates; fix a reference range; add or update `disease_tags` to improve which panels get selected for a given cohort type.

**Top-level structure:**
```json
{
  "_comment": "...",
  "panels": {
    "<LOINC-order-code>": { ... }
  }
}
```

**Panel object:**
| Field | Type | Description |
|-------|------|-------------|
| `order_code` | string | LOINC order code (same as the key) |
| `order_description` | string | Human-readable panel name |
| `specimen` | string | `"Blood"`, `"Urine"`, etc. |
| `disease_tags` | array of strings | Cohort types this panel is appropriate for (e.g. `"diabetes"`, `"cardiovascular"`) |
| `result_items` | array | One object per analyte (see below) |

**Result item object:**
| Field | Type | Description |
|-------|------|-------------|
| `code` | string | LOINC code for this analyte |
| `description` | string | Analyte name |
| `units` | string | Reporting units |
| `is_numeric` | boolean | `false` for text-result analytes |
| `normal_min` / `normal_max` | number | In-range value bounds |
| `abnormal_min` / `abnormal_max` | number | Out-of-range value bounds |
| `text_values` | array (optional) | Fixed result strings for non-numeric analytes |

---

### `config/cohort_hints_library.json`

**Purpose:** Clinical hints for 15 cohort types, injected verbatim into `design_population.py` Phase 3 prompts before the LLM generates cohort clinical data. Guides the LLM toward realistic diagnoses, medications, and comorbidities for each cohort type.

**Used by:** `design_population.py` only. A cohort's name is matched against each entry's `keywords` list; the best match is selected and its `labs`, `medications`, `comorbidities`, and `encounter_pattern_guidance` are formatted into the Phase 3 prompt.

**When to edit:** When a cohort type consistently produces clinically implausible output, add or strengthen its hint entry. Improvements here benefit every future template that includes that cohort type.

**Top-level structure:**
```json
{
  "version": "1.0",
  "_comment": "...",
  "lab_constraints": { "note": "...", "rules": [ ... ] },
  "cohort_hints": [ ... ]
}
```

**`lab_constraints.rules` item** (hard bounds injected into every Phase 3 prompt):
| Field | Description |
|-------|-------------|
| `analyte` | Lab test name |
| `field` | Which range field the rule constrains (`normal_min`, `abnormal_max`, etc.) |
| `operator` | `">="` or `"<="` |
| `value` | Numeric limit |
| `unit` | Reporting unit |

**`cohort_hints` item:**
| Field | Type | Description |
|-------|------|-------------|
| `cohort_type` | string | Internal identifier |
| `keywords` | array | Substrings matched against cohort name (case-insensitive) |
| `disease_tag` | string | Corresponding tag in `lab_panels_library.json` |
| `labs` | array | Recommended lab panels with LOINC codes and ordering notes |
| `medications` | array | Typical medication classes with RxNorm examples |
| `comorbidities` | array | Common comorbidities with ICD-10 codes |
| `encounter_pattern_guidance` | string | Free-text guidance on encounter frequency and type |

**15 cohort types:** cardiovascular, diabetes, hypertension, copd, heart_failure, sickle_cell, behavioral_health, opioid_use_disorder, respiratory_illness, stroke, stis, obstetric, multiple_chronic, preventive, reportable_disease.

---

### `config/cohort_catalog.json`

**Purpose:** QA-approved cohort clinical definitions. When `design_population.py` encounters a cohort whose name matches a catalog entry, it skips the LLM Phase 3 call entirely and uses the stored clinical definition instead. This is the "flywheel" — approved output feeds future generation so quality accumulates over time.

**Used by:** `design_population.py`. After every successful QA pass, run:
```bash
python3 scripts/design_population.py --update-catalog templates/<template>.template.json
```
This reads all approved cohorts from the template and upserts them into the catalog.

**When to edit:** Entries are managed automatically via `--update-catalog`. Manual edits are appropriate to make targeted corrections to a cohort's clinical definition (e.g., fix an ICD code, add a missing medication) without regenerating the full template.

**Top-level structure:**
```json
{
  "version": "1.0",
  "cohorts": {
    "<cohort-slug>": { ... }
  }
}
```

**Cohort entry:**
| Field | Type | Description |
|-------|------|-------------|
| `canonical_name` | string | The cohort name as it appears in the template |
| `keywords` | array | Substrings used to match future cohort names to this entry |
| `qa_status` | string | `"approved"` — only approved entries are stored |
| `source_template` | string | Template path that produced this entry |
| `clinical_catalog` | object | Full clinical definition (see below) |

**`clinical_catalog` fields** (mirrors the cohort object in a template):
`diagnoses`, `comorbidities`, `medications`, `labs`, `observations`, `procedures`, `illness_history_templates`, `social_history_templates`, `family_history_templates`, `document_templates`, `rad_order_templates`, `encounter_pattern`

Currently holds **16 Ohio cohort definitions** seeded from the Ohio demo template.

---

### `config/inpatient_scenarios.json`

**Purpose:** 8 acute clinical scenarios used to generate realistic inpatient admissions and ED visits. Each scenario carries clinical narratives, ICD prefix anchors, vital sign ranges, and length-of-stay ranges.

**Used by:** `generate_population.py` exclusively. Loaded at startup into `_INPATIENT_SCENARIOS`. The generator matches a patient's primary diagnoses (ICD prefix) to a scenario and uses it to populate admission notes, ED course notes, discharge plans, and encounter-level vital signs. A filtered subset (`_SAFE_ED_FALLBACK_SCENARIOS`) is used when no ICD prefix matches.

**When to edit:** Add a new scenario to support a new disease cohort; refine clinical narratives for realism; adjust vital sign or LOS ranges.

**Structure:** Array of scenario objects.

**Scenario object:**
| Field | Type | Description |
|-------|------|-------------|
| `icd_prefixes` | array of strings | ICD-10 prefix(es) that trigger this scenario (e.g. `["I50"]`) |
| `chief_complaints` | array of strings | Random-picked chief complaint text |
| `admission_dx` | string | Admission diagnosis text |
| `hospital_course` | string | Inpatient narrative inserted into the H&P document |
| `ed_course` | string \| null | ED narrative (null for delivery scenario) |
| `ed_variants` | array of strings | Alternative ED course texts for ED-only encounters |
| `discharge_plan` | string | Discharge instructions text |
| `bp_sys_range` | [min, max] | Systolic BP range for this scenario's encounters |
| `bp_dia_range` | [min, max] | Diastolic BP range |
| `hr_range` | [min, max] | Heart rate range |
| `los_days_range` | [min, max] | Length-of-stay range in days |
| `scenario_medications` | array (optional) | Medications always added for this scenario (e.g. furosemide for HF, apixaban for AFib) |
| `acute_dx_map` | object (optional) | Per-ICD-prefix acute diagnosis code override (used for diabetes subtypes E10/E11/E12/E13 to ensure DX002 compliance) |

**8 scenarios:** Normal term delivery (O80/O26/Z34), Acute decompensated heart failure (I50), Atrial fibrillation with RVR (I48), Chest pain / ACS rule-out (I25/I20/I21/I22), Hypertensive urgency (I10–I13), Hyperglycemic crisis (E10–E13), Acute asthma / COPD exacerbation (J45/J44), Active pulmonary tuberculosis / reportable disease (A15/B20/B18/A16).

---

### `config/phrase_pools.json`

**Purpose:** Pools of clinical language strings randomly sampled during document generation. Keeps generated clinical notes from sounding repetitive across patients and encounters.

**Used by:** `generate_population.py` exclusively. Loaded at startup into `_PHRASE_POOLS`. Phrases are picked with `rng.choice()` when building outpatient note text, medication compliance summaries, and symptom-denial statements.

**When to edit:** Add new phrases to reduce repetition in large populations; add a new pool key if a new note section is added to the generator; update phrasing to sound more realistic.

**Structure:** Object where each key is a pool name and the value is an array of interchangeable strings.

**Pool keys:**
| Key | Used in |
|-----|---------|
| `compliance` | Medication adherence sentence in outpatient notes |
| `general_exam` | Physical exam finding in outpatient notes |
| `symptom_denial_cardiovascular` | Symptom denial for cardiovascular cohorts |
| `symptom_denial_diabetes` | Symptom denial for diabetes cohorts |
| `symptom_denial_hypertension` | Symptom denial for hypertension cohorts |
| `symptom_denial_asthma` | Symptom denial for asthma / COPD cohorts |
| `symptom_denial_pregnancy` | Symptom denial for obstetric cohorts |
| `symptom_denial_reportable` | Symptom denial for reportable disease cohorts |
| `symptom_denial_default` | Fallback symptom denial for all other cohorts |

---

## IIS Deployment (Windows / HttpPlatformHandler)

Genesis can be served under IIS as a sub-path application (e.g. `http://server/genesis`) using the `web.config` in the project root.

### One-time server setup

1. Install the **HttpPlatformHandler** IIS module.
2. Install Python 3.11+ and run `pip install -r requirements.txt`.
3. Create a `logs\` folder in the project root and grant the IIS application-pool identity write permission to it.
4. In IIS Manager → your site → **Configuration Editor** → `system.webServer/handlers`, unlock the section (set **overrideModeDefault** to **Allow** or click **Unlock Section**). This is required for the `<clear />` in `web.config` to take effect.
5. Create an IIS Application pointing to the project root and set the application pool to **No Managed Code**.

### After every `git pull`

**`web.config` stores `processPath` as `%SystemDrive%\Python311\python.exe`.**
This is a placeholder. After each pull you must update that line to the actual Python executable on the server — for example:

```xml
processPath="C:\Program Files\Python314\python.exe"
```

The correct path will differ by server. Run `where python` or `py -0p` in a command prompt to find it. Updating the repo value is intentionally avoided because the correct path is server-specific.

### Environment variables

`GENESIS_ROOT_PATH` in `web.config` controls the sub-path prefix. Set it to match the IIS Application alias (e.g. `/genesis`). Leave it empty for root deployment.

`OPENAI_API_KEY` is required only for the wizard / design / QA scripts — not for population generation. Leave the `web.config` value empty and inject the key via IIS Application Settings or Windows environment variables so it is not committed to source control.

### Troubleshooting

| Error | Likely cause | Fix |
|-------|-------------|-----|
| IIS 500.19 (win32=33) | `system.webServer/handlers` section locked | Unlock via Configuration Editor (see step 4 above) |
| IIS 500 / no log file | Wrong `processPath` or `logs\` folder missing | Update processPath; create `logs\` folder |
| CSS/JS 404 | `<clear />` missing from handlers, or `<serverRuntime>` element present | Verify `web.config` matches the repo version |

---

## Security Notes

- `.env` is gitignored — never commit API keys
- A pre-commit hook blocks commits containing secret values
- `scripts/generate_population.py` is pure Python with no LLM calls — it never touches the OpenAI or Anthropic APIs
