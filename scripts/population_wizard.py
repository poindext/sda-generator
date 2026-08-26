#!/usr/bin/env python3
"""
Population Design Wizard

Conducts a structured conversational interview to gather all information
needed to produce a population description document, then optionally feeds
that document into design_population.py to generate a full template.

The wizard covers every topic that design_population.py needs:
  - Population scope and purpose
  - State, geographic regions, and their adjacency
  - Facility count, types, and health systems
  - Disease cohorts and prevalence
  - Demographics and insurance mix
  - Multi-facility HIE configuration
  - Special clinical scenarios

Usage:
  python3 population_wizard.py
  python3 population_wizard.py --output pa_demo.txt
  python3 population_wizard.py --output pa_demo.txt --generate-template
  python3 population_wizard.py --output pa_demo.txt --generate-template \\
      --template-output pa_demo.template.json
  python3 population_wizard.py --model gpt-4o --output ny_demo.txt
  python3 population_wizard.py \\
      --base-url https://apps-llm-1.iscinternal.com/v1 \\
      --model Qwen/Qwen3-32B-AWQ --output ny_demo.txt

Requires OPENAI_API_KEY environment variable (or --base-url for an
unauthenticated internal endpoint).
"""

import argparse
import os
import subprocess
import sys
import textwrap
from pathlib import Path

READY_SIGNAL = "[READY TO GENERATE]"
DEFAULT_MODEL = "gpt-4o"
ISC_LLM_BASE_URL = "https://apps-llm-1.iscinternal.com/v1"

# ---------------------------------------------------------------------------
# System prompt — drives the interview
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = f"""\
You are a population design specialist helping users create synthetic patient \
population configurations for clinical HIE demonstration and testing.

Your goal is to interview the user and collect enough information to write a \
comprehensive population description document. That document will be fed into \
an AI template generator that produces a JSON configuration used to generate \
thousands of synthetic SDA3 patient records.

════════════════════════════════════════════════════════════
INFORMATION CHECKLIST — cover ALL of these before signaling readiness
════════════════════════════════════════════════════════════

1. PURPOSE & SCOPE
   □ What is this population for? (dashboard demo, load testing, training, etc.)
   □ Which state or region?
   □ Total patient count (suggest ranges if unsure: small 500–5k, medium 10–50k,
     large 100k–500k)
   □ History period in months (12–36 typical)

2. GEOGRAPHY
   □ Which counties or areas to include (all counties, metro only, specific regions)
   □ 3–6 named geographic REGIONS that group the counties
     (e.g. "NE Ohio", "Central Ohio", "SW Ohio") — these are used for
     HIE facility-routing logic
   □ Which regions are ADJACENT to each other — ask explicitly, e.g.:
     "Is NE Ohio adjacent to Central Ohio? Is it adjacent to NW Ohio?"
     This is structural: it controls which facilities patients are routed to
     for out-of-region encounters
   □ Urban/rural/suburban distribution
   □ Whether to oversample rural counties so small counties have enough records
     for demographic analysis

3. FACILITIES & HEALTH SYSTEMS
   □ Approximate facility count (8–20 for regional demo; 40–50 for statewide)
   □ Facility types (academic medical centers, community hospitals, FQHCs,
     urgent care, specialty clinics, independent labs, long-term care)
   □ Health system groupings — which facilities share an umbrella organization
     (e.g. "OhioHealth" owns 6 hospitals; "SOMC" is independent)
     Suggest well-known health systems for the state if user is unsure
   □ Geographic spread of facilities across regions
   □ Whether any facilities are multi-specialty vs. single-specialty

4. PROVIDERS
   □ Approximate provider count (2–3 per facility is typical)
   □ Specialties to represent (primary care, cardiology, endocrinology, OB/GYN,
     pulmonology, infectious disease, etc.)

5. DISEASE COHORTS
   □ Which clinical cohorts to include
   □ Approximate weight or prevalence for each (percentages of total population)
   □ Age range restrictions per cohort (e.g. pregnancy: 15–45)
   □ Sub-population detail within cohorts where relevant:
       - Diabetes: % uncontrolled (A1c > 9%), % with care gap (no A1c > 6 months)
       - HTN: % uncontrolled
       - CHF: % with recent readmission
       - Reportable disease: any outbreak scenario?
   □ Whether a "Low Risk Preventive / Wellness" cohort is needed for healthy patients

6. DEMOGRAPHICS
   □ Age distribution (suggest state-specific breakdown)
   □ Sex distribution
   □ Race/ethnicity (suggest state census demographics if user is unsure)
   □ Insurance/payer mix: Medicaid %, Medicare %, commercial %, uninsured %
   □ Name of the state Medicaid program (e.g. "Ohio Medicaid", "Medi-Cal",
     "TennCare", "Georgia Pathways")

7. MULTI-FACILITY HIE CONFIGURATION
   □ What % of patients should appear in more than one facility's records
     (typical: 15–25% for a regional HIE demo; up to 40% for a statewide HIE)
   □ Geographic affinity strength (patients mostly stay regional vs. travel freely)
   □ Should ED-to-inpatient admission always stay at the same facility? (yes/no)
     (almost always yes for realistic HIE data)

8. SPECIAL SCENARIOS & DELIBERATE IMPERFECTIONS
   □ Any reportable-disease outbreak scenario (seeded cluster, spread to adjacent
     county, lab results lagging ED visits by days)
   □ Any trend scenarios (one county improving, another deteriorating)
   □ Any care-gap demonstrations (patients overdue for screening)
   □ Any incomplete feed scenarios (one facility stops reporting partway through —
     demonstrates the "missing feed" detection use case)
   □ Any specific acute event clusters to highlight

════════════════════════════════════════════════════════════
INTERVIEW APPROACH
════════════════════════════════════════════════════════════

- Ask 1–2 related questions per turn — do not dump all questions at once
- Be conversational; offer specific suggestions when the user is uncertain
  (e.g. "For Ohio, common health systems are OhioHealth, Cleveland Clinic,
   University Hospitals, and Bon Secours Mercy. Would you like to use those?")
- Use your geographic knowledge to suggest realistic region names,
  major health systems, and county groupings for the given state
- For ADJACENT REGIONS: after regions are confirmed, always ask explicitly
  about each adjacency pair — do not assume or skip this
- Push for specifics on cohort prevalence (percentages, not just "yes")
- If the user is designing for a specific real-world use case, use that to
  infer realistic values and confirm them rather than asking redundantly

════════════════════════════════════════════════════════════
SIGNALING COMPLETION
════════════════════════════════════════════════════════════

When you have covered ALL checklist items above, end your message with exactly:
  {READY_SIGNAL}

Do NOT produce the full document yet — just signal that you are ready.
The system will then ask you to generate the document.
"""

# ---------------------------------------------------------------------------
# Synthesis prompt — sent once after READY_SIGNAL is detected
# ---------------------------------------------------------------------------

_SYNTHESIS_PROMPT = """\
You have collected all the necessary information. Now write the final population \
description document.

Requirements:
- 500–900 words of narrative prose with clear section headers
- State every region name explicitly, with a complete adjacency list
  (e.g. "NE Ohio is adjacent to Central Ohio and NW Ohio")
- Name every health system and list its member facilities
- State every cohort with a percentage or patient-count range
- Demographic percentages that sum to 100% within each category
- Multi-facility percentages that sum to 100%
- Any special scenarios described specifically enough to be reproduced
- No vague language ("some", "a few") — use numbers and named entities throughout

Write only the document itself. No preamble or meta-commentary.
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extra_body(model: str) -> dict | None:
    if "qwen" in model.lower():
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return None


def _wrap(text: str, width: int = 78) -> str:
    lines = []
    for para in text.split("\n"):
        if para.strip() == "":
            lines.append("")
        else:
            lines.extend(textwrap.wrap(para, width) or [""])
    return "\n".join(lines)


def _print_assistant(text: str) -> None:
    print()
    for line in _wrap(text).split("\n"):
        print(f"  {line}")
    print()


def _print_status(text: str) -> None:
    bar = "─" * 62
    print(f"\n  {bar}")
    for line in textwrap.wrap(text, 58):
        print(f"  │  {line}")
    print(f"  {bar}\n")


def _get_input() -> str:
    try:
        return input("You: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

def run_wizard(
    output_path: str,
    generate_template: bool,
    template_output: str | None,
    model: str,
    base_url: str,
) -> None:
    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: Install the OpenAI SDK:  pip3 install openai")
        sys.exit(1)

    if base_url and "openai.com" not in base_url:
        client = OpenAI(base_url=base_url, api_key="none")
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            print("ERROR: Set OPENAI_API_KEY environment variable.")
            sys.exit(1)
        client = OpenAI(api_key=api_key)

    def _chat(messages: list) -> str:
        kwargs: dict = dict(
            model=model,
            max_tokens=1024,
            temperature=0.3,
            messages=messages,
        )
        eb = _extra_body(model)
        if eb:
            kwargs["extra_body"] = eb
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    history: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]

    _print_status(
        f"Population Design Wizard  ·  Model: {model}  ·  "
        f"Output: {output_path}  ·  Type 'quit' to exit"
    )

    # Opening turn
    history.append({
        "role": "user",
        "content": "Hello — I need to design a synthetic patient population.",
    })
    opening = _chat(history)
    history.append({"role": "assistant", "content": opening})
    _print_assistant(opening)

    # Interview loop
    ready = False
    while not ready:
        user_input = _get_input()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("\n  Wizard cancelled.")
            sys.exit(0)

        history.append({"role": "user", "content": user_input})
        reply = _chat(history)
        history.append({"role": "assistant", "content": reply})

        if READY_SIGNAL in reply:
            display = reply.replace(READY_SIGNAL, "").strip()
            if display:
                _print_assistant(display)
            ready = True
        else:
            _print_assistant(reply)

    # Generate document
    _print_status("All information collected — generating description document...")

    history.append({"role": "user", "content": _SYNTHESIS_PROMPT})
    gen_kwargs: dict = dict(
        model=model,
        max_tokens=2048,
        temperature=0.2,
        messages=history,
    )
    eb = _extra_body(model)
    if eb:
        gen_kwargs["extra_body"] = eb
    synthesis = client.chat.completions.create(**gen_kwargs)
    document = synthesis.choices[0].message.content

    Path(output_path).write_text(document, encoding="utf-8")
    _print_status(f"Document saved → {output_path}")
    print(document)

    # Optionally chain into design_population.py
    if generate_template:
        design_script = Path(__file__).parent / "design_population.py"
        if not design_script.exists():
            print(f"\n  ERROR: design_population.py not found at {design_script}")
            sys.exit(1)

        t_out = template_output or str(Path(output_path).with_suffix(".template.json"))
        _print_status(f"Running design_population.py → {t_out}")

        cmd = [sys.executable, str(design_script), output_path, "--output", t_out]
        if base_url:
            cmd += ["--base-url", base_url, "--model", model]
        subprocess.run(cmd, check=True)
        _print_status(f"Template complete → {t_out}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interview-driven wizard that generates a population "
                    "description document and optionally runs design_population.py."
    )
    parser.add_argument(
        "--output", default="population_design.txt",
        help="Path for the generated description document "
             "(default: population_design.txt)"
    )
    parser.add_argument(
        "--generate-template", action="store_true",
        help="After generating the document, automatically run "
             "design_population.py to produce the full JSON template"
    )
    parser.add_argument(
        "--template-output", default=None,
        help="Path for the JSON template output. "
             "Only used with --generate-template. "
             "Defaults to <output>.template.json"
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"LLM model to use (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--base-url", default="",
        help="OpenAI-compatible base URL. Omit for OpenAI; "
             f"use {ISC_LLM_BASE_URL} for internal ISC LLM"
    )
    args = parser.parse_args()

    run_wizard(
        output_path=args.output,
        generate_template=args.generate_template,
        template_output=args.template_output,
        model=args.model,
        base_url=args.base_url,
    )


if __name__ == "__main__":
    main()
