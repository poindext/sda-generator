# SDA3 Synthetic Patient Population Generator

Generates realistic synthetic patient populations in InterSystems HealthShare **SDA3 XML** format for use in healthcare IT demonstrations, analytics testing, and platform evaluation.

Populations are defined by cohort templates (diabetes, hypertension, asthma, COPD, tick-borne diseases, etc.) and produced entirely in Python — no LLM required for generation. An optional automated clinical QA loop uses GPT-4o to review the output for clinical plausibility and iterates until the population passes review.

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

- **OpenAI API key** (required for `design_population.py`, `auto_qa.py`, `population_wizard.py`; not needed for `generate_population.py --mode template`)

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

The main generator. In `--mode template` it runs entirely in Python (no LLM, no API key required) and produces SDA3 XML files plus a set of analysis CSVs. This is the primary mode for demo populations.

```bash
# Generate 1000 patients from the Florida template
python3 scripts/generate_population.py \
  --template templates/fl_demo.template.json \
  --output populations/population-fl_1000 \
  --mode template \
  --count 1000
```

**Modes:**

| Mode | Description |
|------|-------------|
| `template` | Pure Python generation from a JSON template — fast, reproducible, no LLM |
| `async` | LLM-driven generation with concurrent requests and XSD validation |
| `batch` | LLM-driven via OpenAI Batches API (50% cost, slower turnaround) |
| `validate` | Validate and fix existing XML files |

**Common options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--template` | — | Template JSON file (required for `--mode template`) |
| `--output` | `Population/` | Output directory for XML + CSVs |
| `--count` | `10` | Number of patients to generate |
| `--mode` | — | Generation mode (see table above) |
| `--no-validate` | off | Skip XSD validation (faster) |
| `--model` | `gpt-4o-mini` | LLM model (async/batch modes only) |

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

### `scripts/expand_facilities.py` — Expand facility catalog

One-time utility that patches the Ohio demo template to expand from 8 to ~46 facilities, adds matching providers, and extends geographic coverage. Run once before regenerating the Ohio population.

```bash
python3 scripts/expand_facilities.py
```

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

## Existing Populations

| Template | Description | Count |
|----------|-------------|-------|
| `templates/fl_demo.template.json` | Florida demo — preventive wellness, diabetes, hypertension, asthma, COPD, tick-borne diseases | 1,000 |
| `templates/ohio_demo.template.json` | Ohio statewide OHIP demo — 88 counties, chronic disease cohorts | 10,000 |

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
     --mode template --count 1000
   ```

4. Run the QA loop until approved:
   ```
   /auto-qa --template templates/my_pop.template.json \
            --output populations/population-my_pop_1000 \
            --count 1000
   ```

---

## Security Notes

- `.env` is gitignored — never commit API keys
- A pre-commit hook blocks commits containing secret values
- `scripts/generate_population.py` is pure Python with no LLM calls — it never touches the OpenAI or Anthropic APIs
