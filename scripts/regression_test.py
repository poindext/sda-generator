#!/usr/bin/env python3
"""
regression_test.py — Run generate + QA review for every custom-template cohort.

Reads approved cohorts from config/cohort_catalog.json, generates a population
for each one, runs auto_qa.py, and prints a pass/fail summary table.

Exit code: 0 if all cohorts pass, 1 if any fail.

Usage:
    python3 scripts/regression_test.py
    python3 scripts/regression_test.py --count 50      # faster smoke test
    python3 scripts/regression_test.py --cohorts ckd_progression long_covid
    python3 scripts/regression_test.py --model gpt-4o-mini
    python3 scripts/regression_test.py --skip-generate  # review existing output
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
CATALOG_PATH = ROOT / "config" / "cohort_catalog.json"
TEMPLATES_DIR = ROOT / "templates"
POPULATIONS_DIR = ROOT / "populations"
REGRESSION_DIR = POPULATIONS_DIR / "regression"
CHANGELOG = ROOT / "logs" / "auto_qa_changelog.jsonl"

# Templates shared across many cohorts — skip for regression (no 1:1 output dir)
SKIP_TEMPLATES = {
    "ohio_demo.template.json",
    "template.template.json",
    "ohio_demo.json",
}

GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD  = "\033[1m"


def load_catalog() -> dict:
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def cohorts_to_run(catalog: dict, only: list[str] | None) -> list[dict]:
    results = []
    for key, entry in catalog["cohorts"].items():
        if only and key not in only:
            continue
        src = entry.get("source_template", "")
        if not src or src in SKIP_TEMPLATES:
            continue
        tmpl_path = TEMPLATES_DIR / src
        if not tmpl_path.exists():
            print(f"  [WARN] {key}: template not found ({src}), skipping")
            continue
        results.append({
            "key": key,
            "canonical_name": entry.get("canonical_name", key),
            "template": str(tmpl_path),
            "source_template": src,
            "qa_status": entry.get("qa_status", "unknown"),
        })
    return results


def run_cmd(cmd: list[str], label: str) -> tuple[int, str]:
    """Run a subprocess and return (exit_code, combined_output)."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    out = result.stdout + result.stderr
    return result.returncode, out


def log_event(event: dict) -> None:
    CHANGELOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CHANGELOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def run_regression(
    count: int = 200,
    only: list[str] | None = None,
    model: str = "gpt-4o",
    skip_generate: bool = False,
) -> int:
    catalog = load_catalog()
    cohorts = cohorts_to_run(catalog, only)

    if not cohorts:
        print("No cohorts to test.")
        return 0

    print(f"\n{BOLD}Regression test — {len(cohorts)} cohort(s) — {count} patients each{RESET}")
    print(f"Model: {model}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("─" * 60)

    results = []
    start_all = time.time()

    for cohort in cohorts:
        key = cohort["key"]
        name = cohort["canonical_name"]
        tmpl = cohort["template"]
        REGRESSION_DIR.mkdir(exist_ok=True)
        out_dir = str(REGRESSION_DIR / f"population-{key}")

        print(f"\n[{key}]  {name}")

        # ── Step 1: Generate ──────────────────────────────────────────
        gen_ok = True
        if not skip_generate:
            print(f"  Generating {count} patients...", end=" ", flush=True)
            t0 = time.time()
            code, out = run_cmd(
                [
                    sys.executable, "scripts/generate_population.py",
                    "--template", tmpl,
                    "--output", out_dir,
                    "--count", str(count),
                ],
                key,
            )
            elapsed = time.time() - t0
            if code != 0:
                print(f"{RED}FAILED{RESET} ({elapsed:.0f}s)")
                last_lines = [l for l in out.splitlines() if l.strip()][-5:]
                for l in last_lines:
                    print(f"    {l}")
                results.append({
                    "key": key,
                    "name": name,
                    "status": "GEN_FAIL",
                    "issues": [],
                })
                log_event({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "event": "regression_gen_fail",
                    "cohort": key,
                    "exit_code": code,
                })
                continue
            # Extract summary from output
            enc_line = next(
                (l for l in out.splitlines() if "encounters.csv" in l), ""
            )
            print(f"{GREEN}OK{RESET} ({elapsed:.0f}s)  {enc_line.strip()}")
            gen_ok = True
        else:
            if not Path(out_dir).exists():
                print(f"  {YELLOW}SKIP_GEN{RESET} — output dir not found, skipping review")
                results.append({
                    "key": key,
                    "name": name,
                    "status": "NO_OUTPUT",
                    "issues": [],
                })
                continue
            print(f"  Skipping generation (--skip-generate)")

        # ── Step 2: QA Review ─────────────────────────────────────────
        print(f"  Running QA review...", end=" ", flush=True)
        t0 = time.time()
        code, out = run_cmd(
            [
                sys.executable, "scripts/auto_qa.py",
                "--template", tmpl,
                "--output", out_dir,
                "--model", model,
            ],
            key,
        )
        elapsed = time.time() - t0

        issues_path = Path(out_dir) / "qa_issues.json"
        issues = []
        if issues_path.exists():
            try:
                qa_data = json.loads(issues_path.read_text())
                issues = qa_data.get("issues", [])
            except Exception:
                pass

        if code == 0:
            print(f"{GREEN}APPROVED{RESET} ({elapsed:.0f}s)")
            status = "PASS"
        elif code == 1:
            crit = sum(1 for i in issues if i.get("severity") == "CRITICAL")
            high = sum(1 for i in issues if i.get("severity") == "HIGH")
            med  = sum(1 for i in issues if i.get("severity") == "MEDIUM")
            low  = sum(1 for i in issues if i.get("severity") == "LOW")
            counts = "  ".join(
                f"{BOLD}{s}{RESET}:{n}"
                for s, n in [("C", crit), ("H", high), ("M", med), ("L", low)]
                if n > 0
            )
            print(f"{RED}ISSUES{RESET} ({elapsed:.0f}s)  {counts}")
            for issue in issues:
                sev = issue.get("severity", "?")
                color = RED if sev in ("CRITICAL", "HIGH") else YELLOW
                print(f"    {color}[{sev}]{RESET} {issue.get('title', '?')}")
            status = "FAIL"
        else:
            print(f"{YELLOW}ERROR{RESET} ({elapsed:.0f}s) — auto_qa exited {code}")
            status = "QA_ERROR"

        results.append({
            "key": key,
            "name": name,
            "status": status,
            "issues": issues,
        })
        log_event({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": "regression_result",
            "cohort": key,
            "status": status,
            "issue_count": len(issues),
            "issue_severities": [i.get("severity") for i in issues],
        })

    # ── Summary table ─────────────────────────────────────────────────
    total_elapsed = time.time() - start_all
    print(f"\n{'─' * 60}")
    print(f"{BOLD}REGRESSION SUMMARY  ({total_elapsed:.0f}s total){RESET}\n")

    col_w = max(len(r["key"]) for r in results) + 2
    passes = 0
    for r in results:
        status = r["status"]
        if status == "PASS":
            marker = f"{GREEN}✓ PASS{RESET}"
            passes += 1
        elif status == "FAIL":
            crit = sum(1 for i in r["issues"] if i.get("severity") == "CRITICAL")
            high = sum(1 for i in r["issues"] if i.get("severity") == "HIGH")
            marker = (
                f"{RED}✗ FAIL{RESET}  "
                f"{len(r['issues'])} issue(s)"
                + (f"  {RED}{crit}C{RESET}" if crit else "")
                + (f"  {RED}{high}H{RESET}" if high else "")
            )
        else:
            marker = f"{YELLOW}⚠ {status}{RESET}"

        print(f"  {r['key']:<{col_w}}  {marker}")

    print(f"\n  {passes}/{len(results)} cohorts passed")
    if passes < len(results):
        failed = [r["key"] for r in results if r["status"] != "PASS"]
        print(f"\n  To repair failing cohorts, run:")
        for key in failed:
            cohort = next((c for c in cohorts if c["key"] == key), None)
            if cohort:
                out_dir = f"populations/regression/population-{key}"
                print(
                    f"    /auto-qa --template {cohort['source_template']}"
                    f" --output {out_dir} --count {count}"
                )

    print()
    return 0 if passes == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count", type=int, default=200,
        help="Patients per cohort (default 200; use 50 for a quick smoke test)",
    )
    parser.add_argument(
        "--cohorts", nargs="+", metavar="KEY",
        help="Limit to specific cohort keys (e.g. ckd_progression long_covid)",
    )
    parser.add_argument(
        "--model", default="gpt-4o",
        help="OpenAI model for QA review (default gpt-4o)",
    )
    parser.add_argument(
        "--skip-generate", action="store_true",
        help="Skip generation; review existing population output",
    )
    args = parser.parse_args()
    sys.exit(run_regression(
        count=args.count,
        only=args.cohorts,
        model=args.model,
        skip_generate=args.skip_generate,
    ))
