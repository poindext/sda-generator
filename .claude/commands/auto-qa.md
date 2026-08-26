# /auto-qa — Automated Clinical QA Loop

Automates the clinical QA loop for a synthetic patient population:
1. Build review package from population CSVs
2. Send to OpenAI for clinical review
3. Read issues → apply code/template fixes
4. Regenerate population
5. Repeat until approved (first clean pass stops the loop)
6. Log all changes to `logs/auto_qa_changelog.jsonl`

**Usage:**
```
/auto-qa --template templates/fl_demo.template.json --output populations/population-fl_1000
/auto-qa --template templates/fl_demo.template.json --output populations/population-fl_1000 --count 1000
/auto-qa --template templates/fl_demo.template.json --output populations/population-fl_1000 --review-only
/auto-qa --template templates/fl_demo.template.json --output populations/population-fl_1000 --model gpt-4o-mini
```

Arguments are passed verbatim via `$ARGUMENTS`.

---

## Instructions

Parse `$ARGUMENTS` to extract:
- `--template <file>` (required) — template JSON file, e.g. `templates/fl_demo.template.json`
- `--output <dir>` (required) — population output directory, e.g. `populations/population-fl_1000`
- `--count <n>` (optional) — patient count override passed to `generate_population.py`
- `--review-only` (optional flag) — skip generation; only run the review on existing output
- `--model <name>` (optional, default `gpt-4o`) — OpenAI model for review

If `--template` or `--output` is missing, stop and ask the user for them.

Set `MAX_ROUNDS=5` (never exceed 5 fix-and-regenerate cycles). Set `CHANGELOG=logs/auto_qa_changelog.jsonl`.

---

### Step 1 — Initial generation (unless --review-only)

Unless `--review-only` was passed, generate the population first:

```bash
python3 scripts/generate_population.py \
  --template <template> \
  --output <output> \
  --mode template \
  [--count <count>]
```

If generation fails (non-zero exit), report the error and stop.

---

### Step 2 — Run QA review

Run the reviewer:

```bash
python3 scripts/auto_qa.py \
  --template <template> \
  --output <output> \
  --model <model>
```

- Exit 0 → **APPROVED** — report success and stop.
- Exit 1 → issues found in `<output>/qa_issues.json` — continue to Step 3.
- Exit 2 → runtime error (missing OPENAI_API_KEY, missing CSV files, etc.) — report and stop.

---

### Step 3 — Read and analyse issues

Read `<output>/qa_issues.json`. Extract the `issues` array. Prioritise by severity:
`CRITICAL` → `HIGH` → `MEDIUM` → `LOW`.

Print a summary:
```
Round N/5 — N issues found  (CRITICAL: X  HIGH: Y  MEDIUM: Z  LOW: W)
```

For each issue, print:
```
  [SEVERITY] title
             Evidence: ...
             Approach: ...
             Fix target: template | generator | both
```

---

### Step 4 — Apply fixes

Work through the issues one by one, most severe first.

**Before touching any file**, note the current issue id, title, and target. After applying each fix, immediately log it to the changelog (see Step 6 format).

**Decision rules:**

- `fix_target == "template"` → edit the template JSON file only (in `templates/`).
- `fix_target == "generator"` → edit `scripts/generate_population.py` only.
- `fix_target == "both"` → fix both.

**How to fix:**

1. Read the relevant section of `scripts/generate_population.py` or the template file using the Read tool (use a specific line range if you know it).
2. Understand the root cause from the issue description and evidence.
3. Apply the minimal change that addresses the issue — do not refactor surrounding code.
4. After each Python edit, verify syntax:
   ```bash
   python3 -m py_compile scripts/generate_population.py
   ```
5. After each JSON template edit, verify JSON validity:
   ```bash
   python3 -c "import json; json.load(open('<template>'))"
   ```
6. If verification fails, fix the syntax error before proceeding to the next issue.

**Important constraints:**
- `scripts/generate_population.py` must remain pure Python with **no LLM calls** — never add imports of `openai`, `anthropic`, or any AI/LLM library.
- Do not change the CSV column names — downstream tooling depends on them.
- Do not change the SDA3 XML schema or validator rule IDs.
- Age and sex appropriateness filters must be preserved.
- Changes should be surgical — fix the reported issue, nothing more.

**When an issue requires deeper investigation:** Use the Bash tool to grep for the relevant variable, function, or ICD prefix in `scripts/generate_population.py` before editing.

---

### Step 5 — Regenerate

After all fixes are applied for this round, regenerate:

```bash
python3 scripts/generate_population.py \
  --template <template> \
  --output <output> \
  --mode template \
  [--count <count>]
```

If generation fails, read the last 50 lines of stderr, diagnose, fix the syntax/logic error, and regenerate before running QA again.

---

### Step 6 — Log all changes to changelog

The changelog is `logs/auto_qa_changelog.jsonl` in the **working directory**. Each line is a JSON object. Append (do not overwrite) using the Bash tool:

```bash
echo '<json>' >> logs/auto_qa_changelog.jsonl
```

Append one entry per fix applied:
```json
{
  "timestamp": "2026-08-24T14:30:00",
  "event": "fix_applied",
  "round": 1,
  "issue_id": 3,
  "issue_severity": "HIGH",
  "issue_title": "A1c values implausibly high for non-diabetic patients",
  "fix_target": "generator",
  "fix_summary": "Added cohort check before assigning A1c; non-diabetic patients now skip A1c ordering unless clinically indicated.",
  "files_changed": ["scripts/generate_population.py"]
}
```

The `scripts/auto_qa.py` script already logs `review_complete` events; you only need to log `fix_applied` events here.

---

### Step 7 — Loop

After regeneration, go back to Step 2 (run QA review again). Track the round counter. If round equals `MAX_ROUNDS` and issues still remain, stop the loop and report:

```
Auto-QA reached 5 rounds without a clean pass. Remaining issues:
[list all unresolved issues]

Stopping. Review the issues above manually and re-run /auto-qa when ready.
```

---

### Final report

When the loop ends (either approved or max rounds reached):

1. Read `logs/auto_qa_changelog.jsonl` and count the `fix_applied` entries.
2. Report:
   ```
   ── Auto-QA Complete ────────────────────────────────────
   Status:       APPROVED  (or: STOPPED after 5 rounds)
   Rounds:       N
   Fixes applied: N
   Changelog:    logs/auto_qa_changelog.jsonl

   Changes made this session:
     Round 1: [fix titles]
     Round 2: [fix titles]
     ...
   ────────────────────────────────────────────────────────
   ```

---

## Notes

- `OPENAI_API_KEY` must be set in the environment. If `scripts/auto_qa.py` exits 2 with a key error, remind the user to set it: `export OPENAI_API_KEY=sk-proj-...`
- The review package is saved to `<output>/qa_review_package.txt` — you can read this to understand what OpenAI saw.
- All intermediate `qa_issues.json` files are overwritten each round; the changelog is the permanent record.
- If the same issue recurs across rounds (same title, same category), it likely needs a more fundamental fix in the generator logic rather than a surface patch. After 2 rounds of the same issue, escalate: read the full relevant function before applying the fix.
