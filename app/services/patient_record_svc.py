"""
Patient Record service — generates a single SDA3 patient record using the
generate-sda3 skill as a system prompt and an OpenAI model as the generator.
Optionally enriches the prompt with clinical context extracted from an existing
population template cohort (validated ICD-10, RxNorm, LOINC codes).
"""
from __future__ import annotations

import copy
import json
import re
import zipfile
from pathlib import Path
from typing import AsyncIterator
from xml.etree import ElementTree as ET

from app.config import BASE_DIR, OPENAI_API_KEY, POPULATIONS_DIR, TEMPLATES_DIR

_SKILL_PATH = BASE_DIR / ".claude" / "commands" / "generate-sda3.md"
_RECORDS_DIR = POPULATIONS_DIR / "single-records"
_SCHEMA_PATH = BASE_DIR / "schema" / "SDA_validator.xsd"

# Lazy-loaded lxml schema — None if lxml or schema file unavailable
_xsd_schema = None


def _get_xsd_schema():
    global _xsd_schema
    if _xsd_schema is not None:
        return _xsd_schema
    try:
        from lxml import etree
        if _SCHEMA_PATH.exists():
            _xsd_schema = etree.XMLSchema(etree.parse(str(_SCHEMA_PATH)))
    except Exception:
        pass
    return _xsd_schema


def _validate_file(path: Path) -> list[str]:
    """Return a list of XSD validation error strings for a written XML file."""
    schema = _get_xsd_schema()
    if schema is None:
        return []
    try:
        from lxml import etree
        doc = etree.parse(str(path))
        if not schema.validate(doc):
            return [f"L{e.line}: {e.message}" for e in schema.error_log]
    except Exception as exc:
        return [str(exc)]
    return []


# --------------------------------------------------------------------------
# Cohort template listing
# --------------------------------------------------------------------------

def list_cohort_options() -> list[dict]:
    """
    Return a flat list of selectable cohort options from all template files.
    Each entry: {label, template_file, cohort_id}
    """
    options = []

    # Cohort-specific templates (templates/cohorts/*.template.json)
    for path in sorted((TEMPLATES_DIR / "cohorts").glob("*.template.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        template_name = data.get("meta", {}).get("name", path.stem)
        cohorts = data.get("cohorts", [])
        rel = str(path.relative_to(TEMPLATES_DIR))
        for c in cohorts:
            options.append({
                "label": c.get("name") or template_name,
                "template_file": rel,
                "cohort_id": c.get("id", ""),
            })

    return sorted(options, key=lambda o: o["label"].lower())


# --------------------------------------------------------------------------
# Template context builder
# --------------------------------------------------------------------------

def _build_template_context(cohort_refs: list[dict]) -> str:
    """
    Extract clinical reference material from one or more template cohorts and
    merge into a single structured prompt block for the model.
    """
    all_sections: list[str] = []
    for ref in cohort_refs:
        section = _build_single_cohort_context(ref.get("template_file", ""), ref.get("cohort_id", ""))
        if section:
            all_sections.append(section)
    return "\n\n".join(all_sections)


def _build_single_cohort_context(template_file: str, cohort_id: str) -> str:
    if not template_file:
        return ""
    path = TEMPLATES_DIR / template_file
    if not path.exists():
        return ""
    data = json.loads(path.read_text(encoding="utf-8"))

    cohorts = data.get("cohorts", [])
    cohort = next((c for c in cohorts if c.get("id") == cohort_id), None)
    if not cohort:
        return ""

    lines = [
        "━━━ CLINICAL REFERENCE — use these validated codes for this patient ━━━",
        f"COHORT: {cohort.get('name', cohort_id)}",
        "",
    ]

    # Diagnoses
    dx = cohort.get("diagnoses", [])
    comorbidities = cohort.get("comorbidities", [])
    if dx or comorbidities:
        lines.append("DIAGNOSES (ICD-10-CM):")
        for d in dx:
            tag = " [primary]" if d.get("is_primary") else ""
            lines.append(f"  • {d['code']} — {d['description']}{tag}")
        for d in comorbidities:
            pct = f" ({int(d['prevalence_pct']*100)}% prevalence)" if "prevalence_pct" in d else ""
            lines.append(f"  • {d['code']} — {d['description']}{pct}")
        lines.append("")

    # Medications
    meds = cohort.get("medications", [])
    if meds:
        lines.append("MEDICATIONS (RxNorm):")
        seen = set()
        for m in meds:
            key = m.get("drug_code", "")
            if key in seen:
                continue
            seen.add(key)
            dose = f"{m.get('dose_quantity','')} {m.get('dose_uom','')}".strip()
            freq = m.get("frequency_description") or m.get("frequency_code", "")
            route = m.get("route_description") or m.get("route_code", "")
            lines.append(f"  • {m['drug_code']} — {m['drug_description']}  ({dose} {route} {freq})".rstrip())
        lines.append("")

    # Labs
    labs = cohort.get("labs", [])
    if labs:
        lines.append("LABORATORY ORDERS (LOINC):")
        for lab in labs:
            lines.append(f"  • {lab['order_code']} — {lab['order_description']} [{lab.get('specimen','')}]")
            for ri in lab.get("result_items", [])[:6]:
                rng = ""
                if "normal_min" in ri and "normal_max" in ri:
                    rng = f"  normal {ri['normal_min']}–{ri['normal_max']} {ri.get('units','')}"
                lines.append(f"      {ri['code']} {ri['description']}{rng}")
        lines.append("")

    # Procedures
    procs = cohort.get("procedures", [])
    if procs:
        lines.append("PROCEDURES (CPT):")
        seen_p = set()
        for p in procs:
            if p["code"] in seen_p:
                continue
            seen_p.add(p["code"])
            lines.append(f"  • {p['code']} — {p['description']}")
        lines.append("")

    # Encounter pattern
    ep = cohort.get("encounter_pattern", {})
    if ep:
        visits = ep.get("encounters_per_year", "")
        weights = ep.get("encounter_type_weights", {})
        _type_labels = {'O': 'outpatient', 'E': 'ED', 'I': 'inpatient'}
        type_str = ", ".join(
            f"{_type_labels.get(k, k)} {int(v*100)}%"
            for k, v in weights.items()
        )
        lines.append(f"ENCOUNTER PATTERN: ~{visits} visits/year ({type_str})")
        lines.append("")

    lines.append("━━━ Use the codes above. The scenario below provides patient-specific details. ━━━")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# System prompt loader
# --------------------------------------------------------------------------

def _load_system_prompt() -> str:
    """Return the Rules section of the skill file as the system prompt."""
    text = _SKILL_PATH.read_text(encoding="utf-8")
    parts = text.split("---", 1)
    return parts[1].strip() if len(parts) > 1 else text


# --------------------------------------------------------------------------
# Record generation
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Facility splitter
# --------------------------------------------------------------------------

# XSD-required order of direct children of <Patient>
_PATIENT_FIELD_ORDER = [
    "Name", "BlankNameReason", "MothersMaidenName", "MothersFullName", "Aliases",
    "PrimaryLanguage", "OtherLanguages", "Religion", "MaritalStatus",
    "Gender", "BirthGender", "LegalSex", "Race", "Races", "EthnicGroup", "EthnicGroups",
    "SupportContacts", "BirthTime", "BirthPlace", "BirthOrder",
    "IsProtected", "DeathTime", "IsDead",
    "PatientNumbers", "PriorPatientNumbers", "Addresses", "ContactInfo",
    "FamilyDoctor", "InactiveMRNs", "Occupation",
    "PublicityCode", "PublicityEffectiveDate", "Comments",
    "ImmunizationRegistryStatus", "CommunicationPreference",
    "EnteredBy", "EnteredAt", "EnteredOn", "ActionCode", "EncounterNumber", "ExternalId",
]
_PATIENT_FIELD_RANK = {tag: i for i, tag in enumerate(_PATIENT_FIELD_ORDER)}


def _reorder_patient(patient_el):
    """Re-order direct children of a Patient element to match XSD sequence."""
    children = sorted(patient_el, key=lambda e: _PATIENT_FIELD_RANK.get(e.tag, 999))
    patient_el[:] = children


def _split_by_facility(
    xml: str, base_name: str, out_dir: Path
) -> tuple[list[dict], Path]:
    """
    Parse generated SDA3 XML and write one add + one delete file per
    unique SendingFacility found in clinical records.  Returns a list of
    file-info dicts and the path to the ZIP package.
    """
    # Strip XML declaration before parsing (ET.fromstring chokes on it)
    raw = xml.strip()
    if raw.startswith("<?xml"):
        raw = raw[raw.index("?>") + 2:].strip()

    # Remove XML-invalid control characters (keep tab \x09, LF \x0a, CR \x0d)
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
    # Escape bare & not already part of a valid XML entity reference
    raw = re.sub(
        r"&(?!(?:amp|lt|gt|apos|quot|#\d+|#x[0-9a-fA-F]+);)",
        "&amp;",
        raw,
    )
    # Escape bare < that is NOT the start of a tag, comment, or PI
    # (e.g. "EFW < 10th percentile" in note text)
    raw = re.sub(r"<(?![a-zA-Z/!?])", "&lt;", raw)

    root = ET.fromstring(raw)
    patient_el = root.find("Patient")

    # Build encounter map: number -> FromTime (for nearest-date remapping)
    enc_date_map: dict[str, str] = {}
    for e in root.findall(".//Encounters/Encounter"):
        num = (e.findtext("EncounterNumber") or "").strip()
        ft = (e.findtext("FromTime") or e.findtext("EnteredOn") or "").strip()
        if num:
            enc_date_map[num] = ft

    def _nearest_encounter(record_date: str) -> str | None:
        """Return the EncounterNumber of the encounter closest to record_date."""
        if not enc_date_map:
            return None
        # Use lexicographic ISO 8601 comparison — close enough for date proximity
        best = min(enc_date_map.items(), key=lambda kv: abs(
            _iso_ordinal(kv[1]) - _iso_ordinal(record_date)
        ))
        return best[0]

    def _iso_ordinal(ts: str) -> int:
        """Convert ISO 8601 timestamp to an integer for distance comparison."""
        # Strip time portion and non-digit chars; treat missing as epoch
        digits = "".join(c for c in (ts or "")[:10] if c.isdigit())
        try:
            from datetime import date
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).toordinal()
        except Exception:
            return 0

    def _record_date(record) -> str:
        """Best date field from a clinical record."""
        for field in ("FromTime", "EnteredOn", "ObservationTime",
                      "SpecimenCollectedTime", "ProcedureTime"):
            v = record.findtext(field)
            if v:
                return v.strip()
        return ""

    # Remap any orphaned EncounterNumber to the nearest existing encounter
    for section in root:
        if section.tag in ("Patient", "Encounters"):
            continue
        for record in section:
            en = (record.findtext("EncounterNumber") or "").strip()
            if en and en not in enc_date_map:
                en_el = record.find("EncounterNumber")
                if en_el is not None:
                    nearest = _nearest_encounter(_record_date(record))
                    if nearest:
                        en_el.text = nearest
                    else:
                        record.remove(en_el)

    # Group records by SendingFacility
    fac_records: dict[str, dict[str, list]] = {}
    for section in root:
        if section.tag == "Patient":
            continue
        for record in section:
            fac = (
                record.findtext("SendingFacility")
                or record.findtext("EnteredAt/Code")
                or "FACILITY"
            ).strip()
            fac_records.setdefault(fac, {}).setdefault(section.tag, []).append(record)

    if not fac_records:
        fac_records["FACILITY"] = {}

    out_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale XML files from prior generations so the output only reflects
    # the current generation (avoids ghost facilities when GPT drops a facility)
    for stale in out_dir.glob("*.xml"):
        stale.unlink()

    written: list[dict] = []
    header = '<?xml version="1.0" encoding="UTF-8"?>\n'

    for fac in sorted(fac_records):
        sections = fac_records[fac]

        # Build add container — Patient with MRN filtered to this facility only
        add_root = ET.Element("Container")
        if patient_el is not None:
            add_patient = copy.deepcopy(patient_el)
            pn_wrapper = add_patient.find("PatientNumbers")
            if pn_wrapper is not None:
                for pn in list(pn_wrapper):
                    org_code = pn.findtext("Organization/Code") or ""
                    if org_code and org_code != fac:
                        pn_wrapper.remove(pn)
            _reorder_patient(add_patient)
            add_root.append(add_patient)
        for stag, recs in sections.items():
            sec = ET.SubElement(add_root, stag)
            for r in recs:
                sec.append(copy.deepcopy(r))

        # Build delete container — Patient section only, MRN filtered to this
        # facility, ActionCode=D on the Patient element
        del_root = ET.Element("Container")
        if patient_el is not None:
            del_patient = copy.deepcopy(patient_el)
            pn_wrapper = del_patient.find("PatientNumbers")
            if pn_wrapper is not None:
                for pn in list(pn_wrapper):
                    org_code = pn.findtext("Organization/Code") or ""
                    if org_code and org_code != fac:
                        pn_wrapper.remove(pn)
            ac = del_patient.find("ActionCode")
            if ac is None:
                ac = ET.SubElement(del_patient, "ActionCode")
            ac.text = "D"
            _reorder_patient(del_patient)
            del_root.append(del_patient)

        # Container-level SendingFacility — required by HealthShare MPI Update Manager
        ET.SubElement(add_root, "SendingFacility").text = fac
        ET.SubElement(del_root, "SendingFacility").text = fac

        ET.indent(add_root, space="  ")
        ET.indent(del_root, space="  ")

        add_name = f"{base_name}_{fac}.xml"
        del_name = f"{base_name}_{fac}_DELETE.xml"

        (out_dir / add_name).write_text(
            header + ET.tostring(add_root, encoding="unicode"), encoding="utf-8"
        )
        (out_dir / del_name).write_text(
            header + ET.tostring(del_root, encoding="unicode"), encoding="utf-8"
        )

        def rel(n):
            return str((out_dir / n).relative_to(BASE_DIR))

        add_errors = _validate_file(out_dir / add_name)
        del_errors = _validate_file(out_dir / del_name)
        written.append({
            "name": add_name, "path": rel(add_name), "facility": fac, "type": "add",
            "xsd_errors": add_errors,
        })
        written.append({
            "name": del_name, "path": rel(del_name), "facility": fac, "type": "delete",
            "xsd_errors": del_errors,
        })

    zip_path = out_dir / f"{base_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in written:
            zf.write(BASE_DIR / f["path"], f["name"])

    return written, zip_path


# --------------------------------------------------------------------------
# Record generation
# --------------------------------------------------------------------------

_REFINE_SYSTEM_PROMPT = """\
You are a clinical data quality reviewer for InterSystems HealthShare SDA3 patient records.

You will be given:
1. The original scenario description used to generate a patient record
2. The generated SDA3 XML

Your job: return an improved, more detailed scenario description that corrects every clinical \
incoherence, temporal inconsistency, medication lifecycle error, missing structured data element, \
and cross-facility identity problem you find in the XML.

CRITICAL: You are amending the original scenario, NOT rewriting it from scratch.
- Preserve EXACTLY: the patient's name, date of birth, sex, race/ethnicity, and all demographics.
- Preserve EXACTLY: all facility codes, facility names, specialty providers, and clinical relationships.
- Preserve all clinical content that is correct in the XML.
- Only ADD detail or CORRECT errors — do not replace or simplify existing content.

The improved scenario must explicitly state:
- Exact encounter dates, facility codes, facility names, and encounter types for every encounter
- Each facility's local MRN for the patient (every source system must have its own)
- Which facility prescribes each medication, with start date, stop date, and completion status
- When each condition begins and resolves, with explicit dates that fall within the relevant encounter
- Required structured observations and approximate values (vitals trajectory, labs at each time point, \
functional test measurements)
- Telehealth vs in-person encounter distinction if specimens are collected

Return ONLY the improved scenario text — no explanation, no XML, no commentary.\
"""


async def _xml_generation_pass(
    client,
    model: str,
    system_prompt: str,
    user_message: str,
) -> AsyncIterator[dict]:
    """
    Single XML generation pass. Yields token/status events, then a final
    {"type": "xml_complete", "xml": str} event with the full collected text.
    """
    max_tokens = 32000 if model.startswith("gpt-4.1") else 16000
    try:
        stream = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0.4,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            stream=True,
        )
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}
        return

    full_text = ""
    async for event in stream:
        delta = event.choices[0].delta.content
        if delta:
            full_text += delta
            yield {"type": "token", "content": delta}

    # Continuation passes if output was cut off before </Container>
    for _cont in range(3):
        if full_text.strip().rstrip("`").rstrip().endswith("</Container>"):
            break
        yield {"type": "status", "message": f"Output limit reached — requesting continuation (pass {_cont + 2})…"}
        assistant_content = full_text.strip()
        if assistant_content.startswith("```"):
            assistant_content = re.sub(r"^```(?:xml)?\s*\n?", "", assistant_content)
        last_close = assistant_content.rfind(">")
        if last_close > 0:
            assistant_content = assistant_content[: last_close + 1]
            full_text = assistant_content
        try:
            cont_stream = await client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0.4,
                messages=[
                    {"role": "system",    "content": system_prompt},
                    {"role": "user",      "content": user_message},
                    {"role": "assistant", "content": assistant_content},
                    {"role": "user",      "content": (
                        "The XML was cut off before </Container>. "
                        "Continue exactly where you left off — output raw XML only, "
                        "no markdown code fences, ending with </Container>. "
                        "Do not repeat any content already written."
                    )},
                ],
                stream=True,
            )
        except Exception as exc:
            yield {"type": "error", "message": f"Continuation failed: {exc}"}
            return
        async for event in cont_stream:
            delta = event.choices[0].delta.content
            if delta:
                full_text += delta

    yield {"type": "xml_complete", "xml": full_text}


_SCORE_SYSTEM_PROMPT = """\
You are a clinical data quality reviewer scoring InterSystems HealthShare SDA3 patient records \
for use as HIE/EMPI demonstration data.

Score the record on four dimensions (25 points each, 100 total):

1. Clinical accuracy (25): diagnoses, medications, labs, and procedures are clinically correct \
and coherent with the patient story
2. Temporal coherence (25): all dates are internally consistent; conditions resolve within the \
encounter that documents resolution; medication start/stop dates match the prescribed course; \
no event occurs outside its owning encounter's time window
3. Cross-facility realism (25): every source facility has its own distinct local MRN; medications \
are attributed to the facility that originated them; cross-facility medication reconciliation is correct
4. Structured data completeness (25): every clinically important fact from the notes also exists \
as discrete structured data — not only in note text

SEMANTIC CROSS-CHECK RULES — these catch errors that syntactic validators miss. Apply them \
explicitly before scoring each dimension.

Rule 1 — Active + past ToTime (HARD FAILURE, deduct 10–15 pts from Temporal Coherence):
- Scan every Problem and Diagnosis. If Status=Active AND ToTime is set AND ToTime is earlier \
  than the encounter's FromTime (or the record date), this is a logical contradiction. \
  A condition that has already ended cannot be Active. This rule is deterministic — it is \
  always wrong, no exceptions.
- Example: Problem Status=Active, FromTime=08/12, ToTime=08/19, on an encounter dated 09/05. \
  The ToTime is 17 days before the encounter. The condition is over; Active is wrong.
- Fix: either remove ToTime (if still genuinely active) or change Status to Resolved/Inactive.

Rule 2 — Narrative ↔ structured problem-state (deduct 5–10 pts per violation):
- Read every discharge summary and progress note. Extract all language indicating a condition \
  resolved ("afebrile", "no longer requiring oxygen", "all acute symptoms resolved", \
  "SpO₂ 95% room air at discharge").
- For each such finding, locate the corresponding structured Problem at the SAME facility.
- If the Problem is still Active with no ToTime when the discharge note says it resolved, \
  this is a REAL defect. A hospital whose discharge note says fever resolved must not have \
  an Active Fever problem with no end date.
- If a DIFFERENT facility correctly resolves the problem but the originating facility does not, \
  that is the same defect. The originating source cannot have worse knowledge of its own \
  patient's state than a downstream source.

Rule 3 — Observation ↔ structured problem (deduct 5–8 pts per violation):
- If a measured vital sign or lab value objectively demonstrates resolution (temp 98.6°F, \
  SpO₂ ≥ 94% room air, WBC normalized), any Active structured problem representing that \
  finding must be resolved. Active "Fever" problem alongside a normal final temperature \
  is always a contradiction.

Rule 4 — Source ↔ source consistency (deduct from Cross-Facility Realism):
- If Source A resolves a condition on date D, and Source B (the originating source) has the \
  same condition still Active, deduct from Cross-Facility Realism. The originating source \
  owns the resolution event.

Rule 5 — Clinical precision of finding labels:
- Check that problem/diagnosis descriptions match the clinical specificity of the findings. \
  "Hypoxemia" and "exertional desaturation" are different conditions — a resting SpO₂ of 95% \
  with nadir 92% on exertion is exertional desaturation, not hypoxemia. Using a non-specific \
  label when the structured data supports a more precise one is a Clinical Accuracy deduction.

Do not give 100 unless all semantic cross-checks pass. A structurally clean record with \
any of the above contradictions is NOT a 100.

Then return a refined scenario description that would fix every remaining issue if used to \
regenerate the record.

CRITICAL CONSTRAINTS FOR refined_scenario:
- Start from the ORIGINAL SCENARIO provided in the user message. Do NOT invent a new patient.
- Preserve EXACTLY: patient name, date of birth, sex, race/ethnicity, and all demographic details.
- Preserve EXACTLY: every facility code, facility name, specialty provider, and their clinical roles.
- Preserve ALL clinical content that has no issue (diagnoses, medications, labs, procedures, \
  timeline of events that were scored correctly).
- Only ADD, AMEND, or CLARIFY the specific elements that the identified issues require you to fix.
- The result should read as a targeted amendment of the original scenario — at least 90% of the \
  wording should be unchanged or only lightly edited.
- Do NOT simplify, shorten, or reduce the clinical complexity of the original scenario.

Return ONLY valid JSON in this exact shape — no markdown fences, no commentary:
{
  "score": 82,
  "dimensions": {
    "clinical_accuracy": 22,
    "temporal_coherence": 18,
    "cross_facility_realism": 20,
    "structured_completeness": 22
  },
  "issues": [
    "CMC Fever problem is Active with no ToTime — discharge note says all acute symptoms resolved 08/17",
    "CMC Shortness of breath is Active — discharge note and SpO₂ 95% room air contradict this"
  ],
  "refined_scenario": "Same patient, same facilities, same providers — original scenario text with targeted amendments only to fix the listed issues."
}\
"""


async def _score_record(
    scenario: str,
    xml_text: str,
    model: str,
    client,
) -> dict | None:
    """
    Ask GPT to score the refined record and return an improved scenario.
    Returns a dict with score/dimensions/issues/refined_scenario, or None on failure.
    """
    xml_excerpt = xml_text[:80_000]
    user_message = (
        f"SCENARIO:\n{scenario}\n\n"
        f"GENERATED XML:\n{xml_excerpt}"
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=4000,
            temperature=0.2,
            messages=[
                {"role": "system", "content": _SCORE_SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        # Strip accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        # Validate expected keys
        if "score" in result and "refined_scenario" in result:
            return result
    except Exception:
        pass
    return None


async def _summarize_scenario(scenario: str, model: str, client) -> str:
    """Return a single-line clinical description (~100 chars) of the scenario."""
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=80,
            temperature=0.0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a clinical informatics assistant. "
                        "Summarize the clinical scenario in one concise line (≤100 characters). "
                        "Format: 'Patient name, age/sex, primary conditions, N facility' (e.g. '2 facilities'). "
                        "Never list facility codes or names — only count them. "
                        "Output only the summary line — no quotes, no explanation."
                    ),
                },
                {"role": "user", "content": scenario},
            ],
        )
        return resp.choices[0].message.content.strip()[:140]
    except Exception:
        return ""


async def _refine_scenario(
    original_scenario: str,
    xml_text: str,
    model: str,
    client,
) -> str | None:
    """
    Ask GPT to review the generated XML against the original scenario and return
    an improved scenario description. Returns None if refinement fails or adds
    no meaningful content.
    """
    # Truncate XML to stay within context limits (~80k chars ≈ 20k tokens)
    xml_excerpt = xml_text[:80_000]
    user_message = (
        f"ORIGINAL SCENARIO:\n{original_scenario}\n\n"
        f"GENERATED XML:\n{xml_excerpt}"
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=4000,
            temperature=0.3,
            messages=[
                {"role": "system", "content": _REFINE_SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
        )
        refined = resp.choices[0].message.content.strip()
        # Only use if the refined scenario is meaningfully longer / different
        if refined and len(refined) > max(300, len(original_scenario) * 0.8):
            return refined
    except Exception:
        pass
    return None


def _reconcile_medications(xml: str) -> tuple[str, list[str]]:
    """
    Deterministic post-generation pass on the single-Container combined XML.

    GPT generates ONE <Container> with all clinical records mixed together.
    _split_by_facility routes each Medication to a facility based on its
    SendingFacility field (falling back to EnteredAt/Code).

    This function:
    1. Detects facility codes from encounter numbers (pattern FAC-YYYYMMDD-NN)
    2. Fixes EncounterNumber on any medication whose encounter belongs to a
       different facility than the one it routes to (Rule 6)
    3. Injects a copy of each active medication for every facility that had
       an encounter while it was active but has no routing entry for that drug

    Returns (fixed_xml, list_of_changes_made).
    """
    from collections import defaultdict
    changes: list[str] = []

    # ── Step 1: find facility codes and their latest encounters ───────────────
    # Primary detection: encounter numbers following FAC_CODE-YYYYMMDD-NN
    enc_to_fac: dict[str, str] = {}
    fac_encs: dict[str, list[tuple[str, str]]] = defaultdict(list)  # fac → [(date, enc)]
    for enc in re.findall(r"<EncounterNumber>([^<]+)</EncounterNumber>", xml):
        enc = enc.strip()
        m = re.match(r"^([A-Z][A-Z0-9]*)-(\d{8})-", enc)
        if m:
            fac, date = m.group(1), m.group(2)
            enc_to_fac[enc] = fac
            fac_encs[fac].append((date, enc))

    # Secondary detection: facilities mentioned in EnteredAt/Code or SendingFacility
    # on any record (catches facilities that generated no structured encounters)
    all_fac_codes: set[str] = set(fac_encs.keys())
    for tag in ("SendingFacility", "EnteredAt"):
        if tag == "EnteredAt":
            for code in re.findall(r"<EnteredAt>\s*<Code>([^<]+)</Code>", xml):
                all_fac_codes.add(code.strip())
        else:
            for code in re.findall(r"<SendingFacility>([^<]+)</SendingFacility>", xml):
                all_fac_codes.add(code.strip())

    if len(all_fac_codes) <= 1:
        return xml, changes  # truly single-facility record, nothing to reconcile

    # Latest encounter per facility (by embedded date); facilities without
    # structured encounter numbers get no latest_enc entry — Rule 6 fixes are
    # skipped for them but injection into other facilities still proceeds.
    latest_enc: dict[str, str] = {
        fac: max(pairs, key=lambda p: p[0])[1]
        for fac, pairs in fac_encs.items()
    }

    # ── Step 2: inventory active medications and their routing facilities ─────
    # Key is (drug_desc, from_time) so the same drug prescribed on different
    # dates (e.g. doxycycline 2019 and again 2021) is treated as two independent
    # courses rather than collapsed into one.
    drug_routing: dict[tuple, set[str]] = defaultdict(set)   # (drug, from) → set of fac
    master_meds: dict[tuple, dict] = {}                       # (drug, from) → canonical entry

    for med in re.findall(r"<Medication>(.*?)</Medication>", xml, re.DOTALL):
        drug_m = re.search(
            r"<DrugProduct>.*?<Description>([^<]+)</Description>", med, re.DOTALL
        )
        if not drug_m:
            continue
        drug_desc = drug_m.group(1).strip()

        sf_m   = re.search(r"<SendingFacility>([^<]+)</SendingFacility>", med)
        at_m   = re.search(r"<EnteredAt>\s*<Code>([^<]+)</Code>", med)
        from_m = re.search(r"<FromTime>([^<]+)</FromTime>", med)
        stat_m = re.search(r"<Status>([^<]+)</Status>", med)

        entered_at   = at_m.group(1).strip() if at_m else ""
        routing_fac  = sf_m.group(1).strip() if sf_m else entered_at
        from_time    = from_m.group(1).strip() if from_m else ""
        status       = stat_m.group(1).strip().lower() if stat_m else "active"

        if status not in ("active", ""):
            continue

        med_key = (drug_desc, from_time)

        if routing_fac:
            drug_routing[med_key].add(routing_fac)

        # Prefer the copy with an explicit EnteredAt as the canonical source
        if med_key not in master_meds or (entered_at and not master_meds[med_key]["entered_at"]):
            master_meds[med_key] = {
                "drug_desc":  drug_desc,
                "entered_at": entered_at,
                "from_time":  from_time,
                "raw":        med,
            }

    # ── Step 3: Rule 6 — fix wrong-facility encounter numbers on medications ──
    def fix_enc_in_med(med_block: str) -> str:
        sf_m  = re.search(r"<SendingFacility>([^<]+)</SendingFacility>", med_block)
        at_m  = re.search(r"<EnteredAt>\s*<Code>([^<]+)</Code>", med_block)
        routing = (sf_m.group(1).strip() if sf_m else
                   (at_m.group(1).strip() if at_m else ""))
        if not routing or routing not in latest_enc:
            return med_block
        enc_m = re.search(r"<EncounterNumber>([^<]+)</EncounterNumber>", med_block)
        if not enc_m:
            return med_block
        enc_val = enc_m.group(1).strip()
        enc_fac = enc_to_fac.get(enc_val, "")
        if enc_fac and enc_fac != routing:
            drug_m = re.search(
                r"<DrugProduct>.*?<Description>([^<]+)</Description>", med_block, re.DOTALL
            )
            drug_name = drug_m.group(1).strip() if drug_m else "?"
            new_enc = latest_enc[routing]
            changes.append(
                f"[{routing}] Fixed EncounterNumber on '{drug_name}': '{enc_val}' → '{new_enc}'"
            )
            return med_block.replace(
                f"<EncounterNumber>{enc_val}</EncounterNumber>",
                f"<EncounterNumber>{new_enc}</EncounterNumber>",
            )
        return med_block

    xml = re.sub(
        r"<Medication>(.*?)</Medication>",
        lambda m: f"<Medication>{fix_enc_in_med(m.group(1))}</Medication>",
        xml,
        flags=re.DOTALL,
    )

    # ── Step 4: Rule 7 — inject missing medications per facility ─────────────
    injected_blocks: list[str] = []
    for med_key, med_info in master_meds.items():
        drug_name = med_info["drug_desc"]
        for fac, fac_enc in latest_enc.items():
            if fac in drug_routing.get(med_key, set()):
                continue  # already routed to this facility
            # Only inject if the facility's latest encounter is on/after the med start
            fac_date_m = re.match(r"^[A-Z][A-Z0-9]*-(\d{8})-", fac_enc)
            fac_date   = fac_date_m.group(1) if fac_date_m else ""
            from_digits = re.sub(r"[^0-9]", "", med_info["from_time"])[:8]
            if fac_date and from_digits and fac_date < from_digits:
                continue  # medication started after this facility's last encounter
            # Build reconciled copy: set SendingFacility to target facility,
            # preserve EnteredAt (originating facility), fix EncounterNumber
            raw = med_info["raw"]
            if re.search(r"<SendingFacility>[^<]+</SendingFacility>", raw):
                raw = re.sub(
                    r"<SendingFacility>[^<]+</SendingFacility>",
                    f"<SendingFacility>{fac}</SendingFacility>",
                    raw,
                )
            else:
                raw = raw.rstrip() + f"\n    <SendingFacility>{fac}</SendingFacility>"
            if re.search(r"<EncounterNumber>[^<]+</EncounterNumber>", raw):
                raw = re.sub(
                    r"<EncounterNumber>[^<]+</EncounterNumber>",
                    f"<EncounterNumber>{fac_enc}</EncounterNumber>",
                    raw,
                )
            else:
                raw = raw.rstrip() + f"\n    <EncounterNumber>{fac_enc}</EncounterNumber>"
            injected_blocks.append(f"    <Medication>{raw}</Medication>")
            changes.append(
                f"[{fac}] Injected missing medication '{drug_name}' "
                f"(originated {med_info['entered_at']}), enc={fac_enc}"
            )
            drug_routing[med_key].add(fac)  # prevent double-injection

    if injected_blocks:
        inject_str = "\n".join(injected_blocks)
        if "</Medications>" in xml:
            xml = xml.replace("</Medications>", f"\n{inject_str}\n  </Medications>", 1)
        else:
            xml = xml.rstrip() + f"\n  <Medications>\n{inject_str}\n  </Medications>"

    return xml, changes


def _deterministic_checks(xml: str) -> list[str]:
    """
    Regex-based checks that do not rely on GPT.
    Returns confirmed defect strings to inject into the scoring result.
    """
    issues: list[str] = []

    # Collect all facility codes that appear as SendingFacility anywhere in the doc
    all_fac_codes = set(re.findall(r"<SendingFacility>([^<]+)</SendingFacility>", xml))

    # Process each Container independently
    for container in re.findall(r"<Container>(.*?)</Container>", xml, re.DOTALL):
        sf_m = re.search(r"<SendingFacility>([^<]+)</SendingFacility>", container)
        sending_fac = sf_m.group(1).strip() if sf_m else ""

        drug_keys_seen: list[tuple] = []
        for med in re.findall(r"<Medication>(.*?)</Medication>", container, re.DOTALL):
            enc_m  = re.search(r"<EncounterNumber>([^<]+)</EncounterNumber>", med)
            drug_m = re.search(
                r"<DrugProduct>.*?<Description>([^<]+)</Description>", med, re.DOTALL
            )
            if not (enc_m and drug_m):
                continue
            enc_num   = enc_m.group(1).strip()
            drug_name = drug_m.group(1).strip()
            from_m    = re.search(r"<FromTime>([^<]+)</FromTime>", med)
            from_time = from_m.group(1).strip() if from_m else ""

            # Rule 6 — EncounterNumber must belong to this container's facility,
            # not to a different facility's encounter
            if sending_fac:
                for other in all_fac_codes:
                    if other != sending_fac and other in enc_num:
                        issues.append(
                            f"Rule 6 CONFIRMED — [{sending_fac}] '{drug_name}': "
                            f"EncounterNumber '{enc_num}' belongs to {other}, "
                            f"not to {sending_fac}. Must use a {sending_fac} encounter number."
                        )
                        break

            # Rule 8 — true duplicate: same drug AND same start date within same facility
            med_key = (drug_name, from_time)
            if med_key in drug_keys_seen:
                issues.append(
                    f"Rule 8 CONFIRMED — [{sending_fac}] '{drug_name}' (FromTime={from_time}) "
                    f"appears more than once — exact duplicate entry."
                )
            else:
                drug_keys_seen.append(med_key)

    return issues


def _strip_xml_fences(text: str) -> str:
    xml = text.strip()
    if xml.startswith("```"):
        xml = re.sub(r"^```(?:xml)?\s*\n?", "", xml)
        xml = re.sub(r"\n?```\s*$", "", xml)
    xml = re.sub(r"```(?:xml)?\s*", "", xml)
    return xml


async def generate_record(
    scenario: str,
    filename: str | None = None,
    model: str = "gpt-4o",
    cohort_refs: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """
    Stream SDA3 XML generation for a single patient scenario.
    Automatically runs a second pass after clinical coherence review.

    Yields:
        {"type": "token",  "content": str}
        {"type": "status", "message": str}
        {"type": "done",   "files": list, "zip_path": str, "zip_name": str}
        {"type": "error",  "message": str}
    """
    try:
        from openai import AsyncOpenAI
    except ImportError:
        yield {"type": "error", "message": "openai package not installed"}
        return

    if not OPENAI_API_KEY:
        yield {"type": "error", "message": "OPENAI_API_KEY not configured"}
        return

    system_prompt = _load_system_prompt()

    template_context = ""
    if cohort_refs:
        try:
            template_context = _build_template_context(cohort_refs)
        except Exception as exc:
            yield {"type": "error", "message": f"Failed to load template: {exc}"}
            return

    base_user_message = (
        "Generate realistic InterSystems HealthShare SDA3 XML sample data "
        "for the following scenario:\n\n"
    )
    if template_context:
        base_user_message += template_context + "\n\n"
    base_user_message += scenario.strip()

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    # ── Pass 1: initial generation ────────────────────────────────────────────
    first_pass_xml = ""
    async for event in _xml_generation_pass(client, model, system_prompt, base_user_message):
        if event["type"] == "xml_complete":
            first_pass_xml = event["xml"]
        else:
            yield event
            if event["type"] == "error":
                return

    # ── Refinement: improve the scenario based on what was generated ──────────
    yield {"type": "status", "message": "Analyzing clinical coherence…"}
    refined_scenario = await _refine_scenario(scenario, first_pass_xml, model, client)

    if refined_scenario:
        yield {"type": "status", "message": "Regenerating with refined scenario…"}
        refined_user_message = base_user_message.replace(scenario.strip(), refined_scenario)
        final_xml = ""
        async for event in _xml_generation_pass(client, model, system_prompt, refined_user_message):
            if event["type"] == "xml_complete":
                final_xml = event["xml"]
            else:
                yield event
                if event["type"] == "error":
                    return
        active_scenario = refined_scenario
        active_user_message = refined_user_message
    else:
        final_xml = first_pass_xml
        active_scenario = scenario
        active_user_message = base_user_message

    yield {"type": "status", "message": "Reconciling medications…"}

    xml = _strip_xml_fences(final_xml)

    # ── Deterministic medication reconciliation (no GPT) ─────────────────────
    xml, reconcile_changes = _reconcile_medications(xml)
    if reconcile_changes:
        yield {
            "type": "status",
            "message": f"Auto-fixed {len(reconcile_changes)} medication issue(s): "
                       + "; ".join(reconcile_changes[:3])
                       + ("…" if len(reconcile_changes) > 3 else ""),
        }

    yield {"type": "status", "message": "Packaging files…"}

    if not filename:
        from datetime import datetime
        base_name = f"patient_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        base_name = filename.removesuffix(".xml")

    out_dir = _RECORDS_DIR / base_name

    try:
        files, zip_path = _split_by_facility(xml, base_name, out_dir)

        (out_dir / "scenario.txt").write_text(active_scenario.strip(), encoding="utf-8")
        # Ask GPT for a single-line clinical summary; fall back to first line of user input
        gpt_desc = await _summarize_scenario(scenario, model, client)
        desc_line = gpt_desc or next((ln.strip() for ln in scenario.splitlines() if ln.strip()), "")[:140]
        (out_dir / "description.txt").write_text(desc_line, encoding="utf-8")
        if refined_scenario:
            (out_dir / "scenario_original.txt").write_text(scenario.strip(), encoding="utf-8")
            (out_dir / "scenario_refined.txt").write_text(refined_scenario.strip(), encoding="utf-8")

        prompt_content = (
            "=== SYSTEM PROMPT ===\n\n"
            + system_prompt
            + "\n\n=== USER MESSAGE ===\n\n"
            + active_user_message
        )
        (out_dir / "prompt.txt").write_text(prompt_content, encoding="utf-8")

        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.write(out_dir / "prompt.txt", "prompt.txt")
            zf.write(out_dir / "scenario.txt", "scenario.txt")

        # ── Deterministic pre-checks (regex, no GPT) ─────────────────────────────
        det_issues = _deterministic_checks(xml)

        # ── Pass 2 review: score and offer another round before signalling done ─
        yield {"type": "status", "message": "Scoring clinical quality…"}
        review = await _score_record(active_scenario, xml, model, client)

        # Merge deterministic findings — these override GPT's optimism
        if det_issues:
            if review is None:
                review = {"score": 55, "dimensions": {}, "issues": [], "refined_scenario": ""}
            review["issues"] = det_issues + review.get("issues", [])
            # Cap score: confirmed defects cannot yield a passing grade
            if review.get("score", 100) > 65:
                review["score"] = 65

        if review:
            next_scenario = review.get("refined_scenario", "")
            if next_scenario:
                (out_dir / "scenario_next.txt").write_text(next_scenario.strip(), encoding="utf-8")
            yield {
                "type": "review",
                "score": review.get("score"),
                "dimensions": review.get("dimensions", {}),
                "issues": review.get("issues", []),
                "next_scenario": next_scenario,
                "package_name": base_name,
            }

        yield {
            "type": "done",
            "files": files,
            "zip_path": str(zip_path.relative_to(BASE_DIR)),
            "zip_name": zip_path.name,
        }

    except Exception as exc:
        _RECORDS_DIR.mkdir(parents=True, exist_ok=True)
        fallback = _RECORDS_DIR / f"{base_name}.xml"
        fallback.write_text(xml, encoding="utf-8")
        yield {
            "type": "done",
            "files": [{"name": fallback.name,
                        "path": str(fallback.relative_to(BASE_DIR)),
                        "facility": "", "type": "add"}],
            "zip_path": "",
            "zip_name": "",
            "parse_error": str(exc),
        }
