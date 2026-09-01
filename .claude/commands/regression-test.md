# /regression-test — Run generate + QA for all custom-template cohorts

Runs `scripts/regression_test.py`, which generates a population for each cohort
that has its own source template and runs `auto_qa.py` on the result.

**Usage:**
```
/regression-test
/regression-test --count 50
/regression-test --cohorts ckd_progression long_covid
/regression-test --model gpt-4o-mini
/regression-test --skip-generate
```

Arguments are passed verbatim to `scripts/regression_test.py`.

---

## Instructions

Parse `$ARGUMENTS` for:
- `--count <n>` (optional, default 200) — patients per cohort; use 50 for a fast smoke test
- `--cohorts <key> [<key>...]` (optional) — limit to specific cohort keys
- `--model <name>` (optional, default gpt-4o) — OpenAI model for QA review
- `--skip-generate` (optional) — skip generation, review existing output

Build the command:
```bash
python3 scripts/regression_test.py [--count N] [--cohorts K1 K2 ...] [--model M] [--skip-generate]
```

Run it from the project root and stream its output to the user.

**After it completes:**
- If exit code 0: report "All cohorts passed."
- If exit code 1: list the failing cohorts and remind the user to run `/auto-qa` for each one.
- If exit code 2 or higher: report the error output.

**Important:** Do not attempt to auto-fix issues found during regression testing.
The purpose of this command is to detect regressions, not repair them.
Repairs are done with `/auto-qa` on the specific failing cohort.
