"""
Patient Record service — generates a single SDA3 patient record using the
generate-sda3 skill as a system prompt and an OpenAI model as the generator.
Optionally enriches the prompt with clinical context extracted from an existing
population template cohort (validated ICD-10, RxNorm, LOINC codes).
"""
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
        if len(cohorts) == 1:
            options.append({
                "label": cohorts[0].get("name", template_name),
                "template_file": rel,
                "cohort_id": cohorts[0].get("id", ""),
            })
        else:
            for c in cohorts:
                options.append({
                    "label": f"{template_name} — {c.get('name', c.get('id', ''))}",
                    "template_file": rel,
                    "cohort_id": c.get("id", ""),
                })

    # Main population templates (templates/*.template.json)
    for path in sorted(TEMPLATES_DIR.glob("*.template.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        template_name = data.get("meta", {}).get("name", path.stem)
        cohorts = data.get("cohorts", [])
        rel = str(path.relative_to(TEMPLATES_DIR))
        for c in cohorts:
            options.append({
                "label": f"{template_name} — {c.get('name', c.get('id', ''))}",
                "template_file": rel,
                "cohort_id": c.get("id", ""),
            })

    return sorted(options, key=lambda o: o["label"].lower())


# --------------------------------------------------------------------------
# Template context builder
# --------------------------------------------------------------------------

def _build_template_context(template_file: str, cohort_id: str) -> str:
    """
    Extract clinical reference material from a template cohort and format it
    as a structured prompt block for the model.
    """
    path = TEMPLATES_DIR / template_file
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
# Truncation repair
# --------------------------------------------------------------------------

_SDA3_SECTION_TAGS = [
    "Encounters", "Diagnoses", "Problems", "Medications", "Allergies",
    "LabOrders", "Observations", "Documents", "Procedures", "Vaccinations",
    "PhysicalExams", "Appointments", "Consents", "NoteTexts",
    "IllnessHistories", "SocialHistories", "FamilyHistories",
    "CarePlans", "Goals", "Results",
]


def _repair_truncated_xml(raw: str) -> str:
    """
    Salvage XML that was cut off mid-generation (token limit).
    Strategy: strip any trailing incomplete open-tag, then truncate to the
    last complete SDA3 section closing tag and re-close <Container>.
    """
    # Drop anything from a '<' that never got its closing '>'
    raw = re.sub(r"<[^>]*$", "", raw).rstrip()

    last_pos = -1
    for tag in _SDA3_SECTION_TAGS:
        close = f"</{tag}>"
        pos = raw.rfind(close)
        if pos >= 0:
            candidate = pos + len(close)
            if candidate > last_pos:
                last_pos = candidate

    if last_pos > 0:
        raw = raw[:last_pos] + "\n</Container>"

    return raw


# --------------------------------------------------------------------------
# Facility splitter
# --------------------------------------------------------------------------

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

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raw = _repair_truncated_xml(raw)
        root = ET.fromstring(raw)
    patient_el = root.find("Patient")

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
    written: list[dict] = []
    header = '<?xml version="1.0" encoding="UTF-8"?>\n'

    for fac in sorted(fac_records):
        sections = fac_records[fac]

        # Build add container (unindented first so deepcopy is clean)
        add_root = ET.Element("Container")
        if patient_el is not None:
            add_root.append(copy.deepcopy(patient_el))
        for stag, recs in sections.items():
            sec = ET.SubElement(add_root, stag)
            for r in recs:
                sec.append(copy.deepcopy(r))

        # Build delete container before indenting
        del_root = copy.deepcopy(add_root)
        for sec in del_root:
            if sec.tag == "Patient":
                continue
            for rec in sec:
                ac = rec.find("ActionCode")
                if ac is None:
                    ac = ET.SubElement(rec, "ActionCode")
                ac.text = "D"

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

        rel = lambda n: str((out_dir / n).relative_to(BASE_DIR))
        written.append({"name": add_name, "path": rel(add_name), "facility": fac, "type": "add"})
        written.append({"name": del_name, "path": rel(del_name), "facility": fac, "type": "delete"})

    zip_path = out_dir / f"{base_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in written:
            zf.write(BASE_DIR / f["path"], f["name"])

    return written, zip_path


# --------------------------------------------------------------------------
# Record generation
# --------------------------------------------------------------------------

async def generate_record(
    scenario: str,
    filename: str | None = None,
    model: str = "gpt-4o",
    template_file: str = "",
    cohort_id: str = "",
) -> AsyncIterator[dict]:
    """
    Stream SDA3 XML generation for a single patient scenario.

    Yields:
        {"type": "token",  "content": str}
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

    # Build user message, optionally prefixed with template clinical context
    template_context = ""
    if template_file and cohort_id:
        try:
            template_context = _build_template_context(template_file, cohort_id)
        except Exception as exc:
            yield {"type": "error", "message": f"Failed to load template: {exc}"}
            return

    user_message = (
        "Generate realistic InterSystems HealthShare SDA3 XML sample data "
        "for the following scenario:\n\n"
    )
    if template_context:
        user_message += template_context + "\n\n"
    user_message += scenario.strip()

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    try:
        stream = await client.chat.completions.create(
            model=model,
            max_tokens=32000 if model.startswith("gpt-4.1") else 16000,
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

    # Strip markdown fence to get clean XML
    xml = full_text.strip()
    if xml.startswith("```"):
        xml = re.sub(r"^```(?:xml)?\s*\n?", "", xml)
        xml = re.sub(r"\n?```\s*$", "", xml)

    # Derive base name from optional filename param
    if not filename:
        from datetime import datetime
        base_name = f"patient_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        base_name = filename.removesuffix(".xml")

    out_dir = _RECORDS_DIR / base_name

    try:
        files, zip_path = _split_by_facility(xml, base_name, out_dir)

        # Save scenario (for history display) and full prompt (for reproducibility)
        (out_dir / "scenario.txt").write_text(scenario.strip(), encoding="utf-8")
        prompt_content = (
            "=== SYSTEM PROMPT ===\n\n"
            + system_prompt
            + "\n\n=== USER MESSAGE ===\n\n"
            + user_message
        )
        (out_dir / "prompt.txt").write_text(prompt_content, encoding="utf-8")

        # Append both metadata files to the existing ZIP
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.write(out_dir / "prompt.txt", "prompt.txt")
            zf.write(out_dir / "scenario.txt", "scenario.txt")

        yield {
            "type": "done",
            "files": files,
            "zip_path": str(zip_path.relative_to(BASE_DIR)),
            "zip_name": zip_path.name,
        }
    except Exception as exc:
        # Fallback: save single file if XML is unparseable
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
