#!/usr/bin/env python3
"""
SDA3 Population Generator
Generates synthetic SDA3 XML patient files at scale using an OpenAI-compatible LLM API.

Modes:
  async    - concurrent requests, immediate results (good for <200 patients)
  batch    - OpenAI Batches API at 50% cost (OpenAI only; not supported on internal LLM)
  validate - validate and fix existing files in output-dir

Usage:
  # Quick async test - 20 patients, 10 at a time
  python generate_population.py --count 20 --mode async

  # Large batch - 1000 patients at half price
  python generate_population.py --count 1000 --mode batch

  # Resume (skips patients whose files already exist)
  python generate_population.py --count 1000 --mode async --resume

  # Use a population config file to control cohort mix, demographics, counties
  python generate_population.py --count 1000 --mode async --config ohio_demo.json

  # Use the internal ISC LLM instead of OpenAI (no API key needed)
  python generate_population.py --count 20 --mode async \
      --base-url https://apps-llm-1.iscinternal.com/v1 --model Qwen/Qwen3-32B-AWQ

Model cost guidance (per 1000 patients, ~1K tokens output each):
  gpt-4o-mini  : ~$0.001 async / ~$0.0005 batch  (recommended for bulk)
  gpt-4o       : ~$0.015 async / ~$0.0075 batch

Requires OPENAI_API_KEY environment variable (not needed for --base-url on the internal LLM).
"""

# Default LLM — OpenAI. Empty base URL means "use the OpenAI SDK default endpoint".
DEFAULT_BASE_URL = ""
DEFAULT_MODEL = "gpt-4o-mini"

# Internal ISC LLM endpoint — pass via --base-url / --model to use instead of OpenAI
ISC_LLM_BASE_URL = "https://apps-llm-1.iscinternal.com/v1"
ISC_LLM_DEFAULT_MODEL = "Qwen/Qwen3-32B-AWQ"

import asyncio
import argparse
import copy
import json
import random
import re
import sys
import time
from pathlib import Path

try:
    import openai  # noqa: F401
except ImportError:
    print("ERROR: Install the OpenAI SDK:  pip install openai")
    sys.exit(1)

MAX_RETRIES = 3   # validation fix attempts before giving up on a patient

# ---------------------------------------------------------------------------
# XSD validation helpers
# ---------------------------------------------------------------------------

def load_schema():
    """Load SDA.xsd for validation. Returns None if lxml is not installed.

    SDA.xsd only defines complexTypes — it has no root element declaration.
    We auto-generate a thin wrapper (SDA_validator.xsd) that adds one so lxml
    can validate <Container> documents against the schema.
    """
    try:
        from lxml import etree
    except ImportError:
        print("WARNING: lxml not installed — XSD validation disabled. Run: pip install lxml",
              flush=True)
        return None

    base = Path(__file__).parent
    schema_path = base / "SDA.xsd"
    if not schema_path.exists():
        print(f"WARNING: SDA.xsd not found at {schema_path} — validation disabled", flush=True)
        return None

    # SDA.xsd has no top-level element declaration, so lxml rejects <Container> as a root.
    # Create a wrapper that includes SDA.xsd and adds the missing element declaration.
    wrapper_path = base / "SDA_validator.xsd"
    if not wrapper_path.exists():
        wrapper_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<s:schema xmlns:s="http://www.w3.org/2001/XMLSchema">\n'
            '  <s:include schemaLocation="SDA.xsd"/>\n'
            '  <s:element name="Container" type="Container"/>\n'
            '</s:schema>\n',
            encoding="utf-8",
        )

    try:
        with open(wrapper_path, "rb") as f:
            schema_doc = etree.parse(f)
        return etree.XMLSchema(schema_doc)
    except Exception as e:
        print(f"WARNING: Could not build XSD validator ({e}) — validation disabled", flush=True)
        return None


def validate_xml(xml_str: str, schema) -> tuple:
    """Return (is_valid: bool, errors: list[str])."""
    from lxml import etree
    try:
        doc = etree.fromstring(xml_str.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        return False, [f"XML syntax error: {e}"]
    is_valid = schema.validate(doc)
    errors = [str(e) for e in schema.error_log]
    return is_valid, errors


def build_fix_prompt(xml_str: str, errors: list) -> str:
    """Build a user prompt asking the LLM to fix specific validation errors."""
    capped = errors[:20]
    error_text = "\n".join(f"  - {e}" for e in capped)
    if len(errors) > 20:
        error_text += f"\n  ... and {len(errors) - 20} more errors"
    section_order_hint = (
        "REMINDER — mandatory Container section order (do not deviate):\n"
        "  Patient → Encounters → Allergies → IllnessHistories → SocialHistories → FamilyHistories\n"
        "  → Diagnoses → Observations → Problems → Procedures → Documents → LabOrders → RadOrders\n"
        "  → Medications → Vaccinations → MedicalClaims → SocialDeterminants\n"
        "If an 'element not expected' error names Observations/Diagnoses/LabOrders/Medications, "
        "those sections appear AFTER a section that should come later — reorder them.\n\n"
    )
    return (
        "The SDA3 XML below failed XSD validation. Fix ONLY the structural errors listed — "
        "do not change patient identity, dates, or clinical content unless they caused an error.\n\n"
        + section_order_hint
        + f"Validation errors:\n{error_text}\n\n"
        f"XML to fix:\n{xml_str}"
    )


def strip_fences(text: str) -> str:
    """Remove markdown code fences if the model wrapped the XML in them."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    return text.strip()


def sanitize_xml_text_nodes(xml: str) -> str:
    """Escape bare < > & characters that appear inside text content (between tags).

    The LLM frequently writes things like '<150 mg/dL' or 'BP < 130' in NoteText,
    ResultText, etc. This replaces unescaped special chars in text-only segments
    so the XML parses cleanly, without touching tag markup.
    """
    # Split on tag boundaries. Even-indexed segments are text nodes; odd-indexed are tags.
    parts = re.split(r'(<[^>]*>)', xml)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:  # text node
            # Unescape already-escaped sequences first to avoid double-escaping
            part = part.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            part = part.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        result.append(part)
    return ''.join(result)


def inject_container_fields(xml: str, fallback_facility: str = "") -> str:
    """Inject UpdateECRDemographics and SendingFacility into the Container before </Container>.

    SendingFacility is extracted from the first <EnteredAt><Code> in the document so it matches
    the facility already used throughout the record. Falls back to fallback_facility if not found.
    These sit at the end of the Container sequence per the XSD definition.
    """
    m = re.search(r"<EnteredAt>\s*<Code>([^<]+)</Code>", xml)
    facility = m.group(1).strip() if m else fallback_facility
    injection = (
        "  <UpdateECRDemographics>true</UpdateECRDemographics>\n"
        f"  <SendingFacility>{facility}</SendingFacility>\n"
    )
    return xml.replace("</Container>", injection + "</Container>", 1)


def generate_delete_sda(xml: str, fallback_facility: str = "") -> str:
    """Build a DeletePatient SDA from the MRN and facility in a generated patient XML."""
    mrn_m = re.search(
        r"<PatientNumbers>.*?<PatientNumber>\s*<Number>([^<]+)</Number>",
        xml, re.DOTALL
    )
    mrn = mrn_m.group(1).strip() if mrn_m else "UNKNOWN"

    org_code_m = re.search(
        r"<PatientNumbers>.*?<Organization>\s*<Code>([^<]+)</Code>.*?<Description>([^<]+)</Description>",
        xml, re.DOTALL
    )
    if org_code_m:
        org_code = org_code_m.group(1).strip()
        org_desc = org_code_m.group(2).strip()
    else:
        org_code = fallback_facility or "UNKNOWN"
        org_desc = org_code

    facility_m = re.search(r"<SendingFacility>([^<]+)</SendingFacility>", xml)
    facility = facility_m.group(1).strip() if facility_m else (fallback_facility or org_desc)

    return (
        "<Container>\n"
        "  <Action>DeletePatient</Action>\n"
        f"  <EventDescription>Delete patient: {facility} {mrn}</EventDescription>\n"
        "  <UpdateECRDemographics>true</UpdateECRDemographics>\n"
        f"  <SendingFacility>{facility}</SendingFacility>\n"
        "  <Patient>\n"
        "    <PatientNumbers>\n"
        "      <PatientNumber>\n"
        f"        <Number>{mrn}</Number>\n"
        "        <Organization>\n"
        f"          <Code>{org_code}</Code>\n"
        f"          <Description>{org_desc}</Description>\n"
        "        </Organization>\n"
        "        <NumberType>MRN</NumberType>\n"
        "      </PatientNumber>\n"
        "    </PatientNumbers>\n"
        "    <ActionCode>D</ActionCode>\n"
        "  </Patient>\n"
        "</Container>\n"
    )


# ---------------------------------------------------------------------------

# Disease cohort weights (must sum to 1.0)
COHORTS = [
    ("hypertension",              0.28),
    ("diabetes_controlled",       0.07),
    ("diabetes_poor_control",     0.05),
    ("diabetes_no_recent_a1c",    0.04),
    ("asthma",                    0.09),
    ("cardiovascular_disease",    0.08),
    ("multiple_chronic",          0.14),
    ("pregnancy",                 0.02),
    ("healthy",                   0.23),
]

RACES = [
    ("2106-3", "White",                          0.79),
    ("2054-5", "Black or African American",      0.13),
    ("2028-9", "Asian",                          0.02),
    ("1002-5", "American Indian or Alaska Native", 0.01),
    ("2131-1", "Other Race",                     0.05),
]

ETHNICITIES = [
    (None,     None,                    0.96),
    ("2135-2", "Hispanic or Latino",    0.04),
]

# Insurance type: (NumberType, OrgDescription, weight)
INSURANCES = [
    ("COM", None,        0.45),  # commercial — MRN only
    ("MA",  "Medicaid",  0.35),  # org name overridden by config insurances array
    ("MC",  "Medicare",  0.20),
]

COHORT_DESCRIPTIONS = {
    "hypertension": (
        "patient with essential hypertension. Include 1 outpatient encounter and 1-2 antihypertensive "
        "medications. Include a blood pressure observation. No diabetes or other chronic conditions."
    ),
    "diabetes_controlled": (
        "patient with type 2 diabetes, reasonably controlled (A1c between 7.0-8.9%). Include 1-2 "
        "outpatient encounters, metformin and/or one other diabetes medication, and an A1c lab result "
        "within the past 6 months."
    ),
    "diabetes_poor_control": (
        "patient with poorly controlled type 2 diabetes (A1c above 9%). Include 1-2 outpatient "
        "encounters, diabetes medications, and an A1c lab result showing A1c >= 9.1%."
    ),
    "diabetes_no_recent_a1c": (
        "patient with type 2 diabetes on metformin. Include 1 outpatient encounter from 8-10 months ago "
        "with a glucose result but NO A1c result — the patient has not had an A1c in over 6 months. "
        "This represents a monitoring gap."
    ),
    "asthma": (
        "patient with asthma. Include 1 outpatient encounter and possibly 1 ED encounter for an "
        "exacerbation. Medications should include a rescue inhaler (albuterol) and optionally a "
        "controller inhaler."
    ),
    "cardiovascular_disease": (
        "patient with cardiovascular disease (coronary artery disease, prior MI, or heart failure). "
        "Include 1-2 encounters, appropriate cardiac medications (statin, aspirin, beta-blocker or "
        "ACE inhibitor), and a lipid panel lab result."
    ),
    "multiple_chronic": (
        "patient with 2-3 chronic conditions from this set: type 2 diabetes, hypertension, asthma, "
        "obesity, COPD. Include 2 outpatient encounters and appropriate medications for each condition. "
        "Include at least one lab result."
    ),
    "pregnancy": (
        "pregnant patient at a prenatal visit. Include 1-2 prenatal outpatient encounters, appropriate "
        "prenatal diagnoses (Z34.x supervision of pregnancy), and at least one prenatal lab (blood type, "
        "CBC, or glucose screen)."
    ),
    "healthy": (
        "generally healthy patient with no chronic conditions. Include 1 wellness or new-patient "
        "outpatient encounter with normal vital signs. No diagnoses, medications, or labs required "
        "unless clinically appropriate."
    ),
}

# ---------------------------------------------------------------------------
# System prompt — SDA3 generation rules (no markdown fences in output)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
You are generating synthetic InterSystems HealthShare SDA3 XML patient records for a statewide
health information exchange demonstration dataset.

## Output
Output raw XML only. Start with <Container> and end with </Container>. No markdown, no code fences,
no explanation before or after. The XML must be well-formed.

## CRITICAL: Element ordering
SDA3 uses strict XSD <sequence> definitions. Elements MUST appear in the exact order shown in the
examples below. Wrong order = XSD validation failure. Follow each example precisely.

## VALIDATED REFERENCE EXAMPLE — this exact structure passed XSD validation
Study this example carefully. It shows every section type with correct element ordering.
Use it as your structural template. Change names, codes, dates, and values — never change ordering.

<Container>
  <Patient>
    <Name>
      <FamilyName>Johnson</FamilyName>
      <GivenName>William</GivenName>
      <MiddleName>R.</MiddleName>
      <Type>Legal</Type>
    </Name>
    <Gender>
      <SDACodingStandard>HL7</SDACodingStandard>
      <Code>M</Code>
      <Description>Male</Description>
    </Gender>
    <Race>
      <SDACodingStandard>CDCREC</SDACodingStandard>
      <Code>2054-5</Code>
      <Description>Black or African American</Description>
    </Race>
    <EthnicGroup>
      <SDACodingStandard>CDCREC</SDACodingStandard>
      <Code>2186-5</Code>
      <Description>Not Hispanic or Latino</Description>
    </EthnicGroup>
    <BirthTime>1938-03-10T00:00:00Z</BirthTime>
    <PatientNumbers>
      <PatientNumber>
        <Number>MRN830125</Number>
        <NumberType>MRN</NumberType>
        <Organization>
          <Code>LAKEMED</Code>
          <Description>Lake Medical Center</Description>
        </Organization>
      </PatientNumber>
      <PatientNumber>
        <Number>ST-MCD-4471838</Number>
        <NumberType>MA</NumberType>
        <Organization>
          <Code>STMCD</Code>
          <Description>State Medicaid</Description>
        </Organization>
      </PatientNumber>
    </PatientNumbers>
    <Addresses>
      <Address>
        <Street>123 Main Street</Street>
        <City><Code>MENTOR</Code><Description>Mentor</Description></City>
        <State><SDACodingStandard>USPS</SDACodingStandard><Code>ST</Code><Description>State</Description></State>
        <Zip><Code>44060</Code></Zip>
        <Country><SDACodingStandard>ISO 3166</SDACodingStandard><Code>US</Code><Description>United States</Description></Country>
        <County><SDACodingStandard>FIPS</SDACodingStandard><Code>00001</Code><Description>Lake County</Description></County>
      </Address>
    </Addresses>
    <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
    <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ActionCode>A</ActionCode>
  </Patient>
  <Encounters>
    <Encounter>
      <EncounterNumber>ENC-2022091501</EncounterNumber>
      <EncounterType>O</EncounterType>
      <HealthCareFacility>
        <Organization>
          <Code>LAKEMED</Code>
          <Description>Lake Medical Center</Description>
        </Organization>
      </HealthCareFacility>
      <ActionCode>A</ActionCode>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2022-09-15T00:00:00Z</EnteredOn>
      <FromTime>2022-09-15T00:00:00Z</FromTime>
      <ToTime>2022-09-15T00:00:00Z</ToTime>
    </Encounter>
  </Encounters>
  <Allergies>
    <Allergy>
      <Allergy>
        <SDACodingStandard>SNM</SDACodingStandard>
        <Code>372687004</Code>
        <Description>Amoxicillin</Description>
      </Allergy>
      <AllergyCategory>
        <SDACodingStandard>HL7</SDACodingStandard>
        <Code>DA</Code>
        <Description>Drug Allergy</Description>
      </AllergyCategory>
      <Reaction>
        <SDACodingStandard>SNM</SDACodingStandard>
        <Code>271807003</Code>
        <Description>Rash</Description>
      </Reaction>
      <Status>A</Status>
      <ActionCode>A</ActionCode>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2023-07-01T00:00:00Z</EnteredOn>
      <FromTime>2023-07-01T00:00:00Z</FromTime>
      <EncounterNumber>ENC-2023031502</EncounterNumber>
    </Allergy>
  </Allergies>
  <Diagnoses>
    <Diagnosis>
      <FromTime>2022-09-15T00:00:00Z</FromTime>
      <EncounterNumber>ENC-2022091501</EncounterNumber>
      <Diagnosis>
        <SDACodingStandard>ICD10</SDACodingStandard>
        <Code>E11.9</Code>
        <Description>Type 2 diabetes mellitus</Description>
      </Diagnosis>
      <DiagnosisType>
        <SDACodingStandard>HL7</SDACodingStandard>
        <Code>F</Code>
        <Description>Final</Description>
      </DiagnosisType>
      <Status>
        <SDACodingStandard>HL7</SDACodingStandard>
        <Code>A</Code>
        <Description>Active</Description>
      </Status>
      <OnsetTime>2015-01-10T00:00:00Z</OnsetTime>
      <ActionCode>A</ActionCode>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2022-09-15T00:00:00Z</EnteredOn>
    </Diagnosis>
  </Diagnoses>
  <Observations>
    <Observation>
      <ExternalId>Observations_1</ExternalId>
      <EncounterNumber>ENC-2022091501</EncounterNumber>
      <ObservationTime>2022-09-15T10:00:00Z</ObservationTime>
      <ObservationCode>
        <Code>8480-6</Code>
        <Description>Systolic blood pressure</Description>
      </ObservationCode>
      <ObservationValue>135</ObservationValue>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2022-09-15T10:00:00Z</EnteredOn>
    </Observation>
  </Observations>
  <Problems>
    <Problem>
      <EncounterNumber>ENC-2022091501</EncounterNumber>
      <Problem>
        <SDACodingStandard>ICD10</SDACodingStandard>
        <Code>I50.9</Code>
        <Description>Heart failure, unspecified</Description>
      </Problem>
      <Status>
        <SDACodingStandard>SNOMED CT</SDACodingStandard>
        <Code>55561003</Code>
        <Description>Active</Description>
      </Status>
      <ActionCode>A</ActionCode>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2022-09-15T00:00:00Z</EnteredOn>
      <FromTime>2022-09-15T00:00:00Z</FromTime>
    </Problem>
  </Problems>
  <Procedures>
    <Procedure>
      <FromTime>2022-09-15T00:00:00Z</FromTime>
      <EncounterNumber>ENC-2022091501</EncounterNumber>
      <Procedure>
        <SDACodingStandard>CPT</SDACodingStandard>
        <Code>99213</Code>
        <Description>Office visit, moderate complexity</Description>
      </Procedure>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2022-09-15T00:00:00Z</EnteredOn>
      <ActionCode>A</ActionCode>
    </Procedure>
  </Procedures>
  <LabOrders>
    <LabOrder>
      <PlacerId>LAB-20220915-001</PlacerId>
      <OrderItem>
        <SDACodingStandard>LN</SDACodingStandard>
        <Code>4548-4</Code>
        <Description>Hemoglobin A1c panel</Description>
      </OrderItem>
      <OrderedBy><Code>DR789</Code><Description>Dr. Lee</Description></OrderedBy>
      <EnteringOrganization><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteringOrganization>
      <Specimen>Blood</Specimen>
      <SpecimenCollectedTime>2022-09-15T08:00:00Z</SpecimenCollectedTime>
      <Priority><Code>R</Code><Description>Routine</Description></Priority>
      <ActionCode>A</ActionCode>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2022-09-15T00:00:00Z</EnteredOn>
      <FromTime>2022-09-15T00:00:00Z</FromTime>
      <ToTime>2022-09-15T00:00:00Z</ToTime>
      <Result>
        <ResultType>AT</ResultType>
        <ResultItems>
          <LabResultItem>
            <EnteredOn>2022-09-17T14:30:22Z</EnteredOn>
            <TestItemCode>
              <SDACodingStandard>LN</SDACodingStandard>
              <Code>4548-4</Code>
              <Description>Hemoglobin A1c/Hemoglobin.total in Blood</Description>
              <IsNumeric>true</IsNumeric>
            </TestItemCode>
            <ResultValue>7.8</ResultValue>
            <ResultValueUnits>%</ResultValueUnits>
            <ResultNormalRange>&lt;5.7</ResultNormalRange>
            <ResultInterpretation>H</ResultInterpretation>
            <ExternalId>LabResultItem_1</ExternalId>
          </LabResultItem>
        </ResultItems>
        <ResultTime>2022-09-17T14:30:22Z</ResultTime>
        <ResultStatus>F</ResultStatus>
        <ExternalId>Result_1</ExternalId>
      </Result>
      <ExternalId>LabOrder_1</ExternalId>
      <EncounterNumber>ENC-2022091501</EncounterNumber>
    </LabOrder>
  </LabOrders>
  <Medications>
    <Medication>
      <ActionCode>A</ActionCode>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2022-09-15T00:00:00Z</EnteredOn>
      <FromTime>2022-01-15T00:00:00Z</FromTime>
      <EncounterNumber>ENC-2022091501</EncounterNumber>
      <Frequency><Code>QD</Code><Description>Once daily</Description></Frequency>
      <Status>active</Status>
      <DrugProduct>
        <SDACodingStandard>RxNorm</SDACodingStandard>
        <Code>860975</Code>
        <Description>Metformin 500 MG Oral Tablet</Description>
      </DrugProduct>
      <DoseQuantity>500</DoseQuantity>
      <DoseUoM><SDACodingStandard>UCUM</SDACodingStandard><Code>mg</Code></DoseUoM>
      <DosageForm><Code>TAB</Code><Description>Tablet</Description></DosageForm>
      <Route><SDACodingStandard>HL7</SDACodingStandard><Code>PO</Code><Description>Oral</Description></Route>
    </Medication>
  </Medications>
  <Vaccinations>
    <Vaccination>
      <EnteredBy><Code>DR789</Code><Description>Dr. Lee</Description></EnteredBy>
      <EnteredAt><Code>LAKEMED</Code><Description>Lake Medical Center</Description></EnteredAt>
      <EnteredOn>2022-01-15T00:00:00Z</EnteredOn>
      <FromTime>2022-01-15T00:00:00Z</FromTime>
      <ExternalId>Vaccinations_1</ExternalId>
      <EncounterNumber>ENC-2022011501</EncounterNumber>
      <OrderItem><Code>207</Code><Description>COVID-19, mRNA, LNP-S, PF, 100 mcg/0.5 mL dose</Description></OrderItem>
      <OrderedBy><Code>DR789</Code><Description>Dr. Lee</Description></OrderedBy>
      <Frequency><Code>O</Code><Description>Once</Description></Frequency>
      <Status>V</Status>
      <DrugProduct>
        <Code>207</Code>
        <Description>COVID-19, mRNA, LNP-S, PF, 100 mcg/0.5 mL dose</Description>
      </DrugProduct>
      <DoseQuantity>0.5</DoseQuantity>
      <DoseUoM><Code>mL</Code><Description>mL</Description></DoseUoM>
      <DosageForm><Code>I</Code><Description>injection</Description></DosageForm>
      <Route><Code>IM</Code><Description>intramuscular</Description></Route>
      <Indication>vaccination</Indication>
    </Vaccination>
  </Vaccinations>
</Container>

END OF REFERENCE EXAMPLE. Generate new patients using the same structure.

## Container structure — SECTION ORDER IS ENFORCED BY XSD
Clinical sections are direct children of <Container>, placed AFTER </Patient>.
They are NEVER nested inside <Patient>.

MANDATORY SECTION ORDER — this is not a suggestion, it is the XSD sequence:
  1. <Patient>
  2. <Encounters>          ← AttendingClinicians, ReferringClinician, HealthFunds go INSIDE <Encounter>
  3. <Allergies>           ← comes BEFORE IllnessHistories and Diagnoses
  4. <IllnessHistories>    ← BEFORE SocialHistories and Diagnoses (Medical History tab)
  5. <SocialHistories>     ← BEFORE Diagnoses
  6. <FamilyHistories>     ← BEFORE Diagnoses
  7. <Diagnoses>
  8. <Observations>        ← comes BEFORE Medications
  9. <Problems>
  10.<Procedures>
  11.<Documents>           ← comes AFTER Procedures, BEFORE LabOrders
  12.<LabOrders>
  12b.<RadOrders>          ← comes AFTER LabOrders, BEFORE Medications (Diagnostic Studies tab)
  13.<Medications>         ← comes AFTER Observations, Procedures, LabOrders, RadOrders
  14.<Vaccinations>
  (many skipped sections...)
  15.<MedicalClaims>       ← AFTER Vaccinations, BEFORE SocialDeterminants
  16.<SocialDeterminants>  ← comes AFTER MedicalClaims, near end of Container

ENCOUNTER INTERNAL ORDER (fields inside <Encounter>):
  EncounterNumber → EncounterType → AttendingClinicians → ReferringClinician
  → HealthCareFacility → HealthFunds → ActionCode → EnteredBy → EnteredAt
  → EnteredOn → FromTime → ToTime

COMMON MISTAKES THAT CAUSE VALIDATION FAILURE:
  ✗ Putting Documents AFTER LabOrders — INVALID. Documents MUST come BEFORE LabOrders.
  ✗ Putting Medications before Observations — INVALID
  ✗ Putting Allergies after Diagnoses or Medications — INVALID
  ✗ Putting Vaccinations before LabOrders or Medications — INVALID
  ✗ Putting Observations, Diagnoses, or LabOrders AFTER Vaccinations — INVALID
  ✗ Skipping sections and jumping straight to Medications or Vaccinations — INVALID
  ✗ Adding <Clinician> inside a Vaccination — INVALID. Vaccination has NO Clinician field.
  ✗ Adding <UpdatedOn>, <EnteredBy>, <EnteredAt>, <EnteredOn> inside MedicalClaim — INVALID.
  ✗ Emitting empty sections like <Diagnoses></Diagnoses> — DO NOT do this
  ✓ Only include sections that have actual records
  ✓ If a patient has no allergies, omit <Allergies> entirely
  ✓ The sequence is FIXED — always write sections top-to-bottom in the exact order listed above

Every section MUST use its plural wrapper tag even for a single entry:
  <Encounters><Encounter>...</Encounter></Encounters>
  <Diagnoses><Diagnosis>...</Diagnosis></Diagnoses>
  <Medications><Medication>...</Medication></Medications>
  <Allergies><Allergy>...</Allergy></Allergies>
  <LabOrders><LabOrder>...</LabOrder></LabOrders>
  <PatientNumbers><PatientNumber>...</PatientNumber></PatientNumbers>
  <ResultItems><LabResultItem>...</LabResultItem></ResultItems>

## Date format — ISO 8601 REQUIRED (HealthShare rejects HL7 YYYYMMDD format)

ALL TimeStamp fields require the full ISO 8601 datetime format: YYYY-MM-DDTHH:MM:SSZ
Never use date-only YYYY-MM-DD for any field. Never use HL7 YYYYMMDD format.

  All fields: YYYY-MM-DDTHH:MM:SSZ
  Examples: 2022-09-15T00:00:00Z   2022-09-17T14:30:22Z   1968-04-22T00:00:00Z

  When a precise time is not meaningful (e.g. a date of birth, an onset date years ago,
  an encounter date), use T00:00:00Z as the time component.
  When a precise time IS known (e.g. ResultTime, ObservationTime), use the actual time.

Date field names by type (EXACT names — do not invent alternatives):
  Encounter   : FromTime (visit start), ToTime (visit end), EnteredOn
  Diagnosis   : FromTime (= encounter date), EnteredOn (= encounter date),
                IdentificationTime (when diagnosis was made, = encounter date),
                OnsetTime (optional; for chronic conditions, years before current encounter)
                NOTE: There is NO DiagnosisTime field. Use IdentificationTime.
  Medication  : FromTime (prescription start), ToTime (stop date if discontinued), EnteredOn
                NOTE: There is NO StartTime or StopTime field. Use FromTime and ToTime.
  LabOrder    : FromTime (= order date), ToTime (= order date, same as FromTime for most orders;
                for panel orders with expected multi-day processing, may match ResultTime), EnteredOn
                Result.ResultTime (= when resulted; must be >= FromTime)
                NOTE: There is NO OrderedOn field. Use FromTime.
  Observation : ObservationTime (required), EnteredOn
  Allergy     : EnteredOn (required), FromTime (onset, optional)
  Procedure   : FromTime (= procedure date), EnteredOn

Date coherence:
  Result.ResultTime >= LabOrder.FromTime (never result before order)
  Encounter ToTime >= FromTime (never discharge before admit)
  Outpatient: FromTime = ToTime (same day)
  Inpatient:  ToTime = FromTime + 1-7 days
  Each record's EnteredOn = the encounter's FromTime it belongs to
  Chronic medication FromTime = original prescribe date (may be years before current encounter)
  Diagnosis OnsetTime for chronic disease = years before current encounter

## CodeTableDetail — applies to EVERY coded field
Fields of CodeTableDetail type MUST use elements in this EXACT order:
  1. <SDACodingStandard>  ← ALWAYS FIRST
  2. <Code>
  3. <Description>

WRONG:  <Code>F</Code><Description>Female</Description><SDACodingStandard>HL7</SDACodingStandard>
RIGHT:  <SDACodingStandard>HL7</SDACodingStandard><Code>F</Code><Description>Female</Description>

Status fields: some Status fields are PLAIN STRINGS (not CodeTableDetail):
  Allergy.Status    → plain string:  A (Active), I (Inactive), R (Resolved)
  Medication.Status → plain string:  V (Verified), E (Executed), D (Discontinued), C (Cancelled)
  LabOrder.Result.ResultStatus → plain string: F (Final), R (Entered), K (Corrected)
  Diagnosis.Status  → CodeTableDetail (use SDACodingStandard+Code+Description)
  Encounter.Status  → CodeTableDetail

LabResultItem reference range: there are TWO different fields — use ONLY ResultNormalRange:
  <ResultNormalRange>70-99</ResultNormalRange>   ← CORRECT: plain string, use this
  <ReferenceRange>...</ReferenceRange>           ← WRONG for text: it is element-only (QuantityRange type,
                                                    children: Low/High/Text) — never put plain text in it
ResultStatus NEVER goes inside <LabResultItem> — it belongs at <Result> level only:
  <Result>
    <ResultItems>...</ResultItems>   ← LabResultItem children go here
    <ResultTime>...</ResultTime>
    <ResultStatus>F</ResultStatus>   ← RESULT level, never inside LabResultItem
  </Result>

Coding systems:
  Gender/BirthGender/LegalSex  → HL7          M, F, O, U
  Race                         → CDCREC        2106-3 White · 2054-5 Black · 2028-9 Asian · 1002-5 AI/AN
  EthnicGroup                  → CDCREC        2135-2 Hispanic · 2186-5 Non-Hispanic
  State (Address)              → USPS          OH, CA, TX, NY
  Country (Address)            → ISO 3166      US, CA, GB
  County (Address)             → FIPS          use your state's county FIPS codes (e.g. 39049 Franklin OH · 06037 Los Angeles CA)
  City (Address)               → (none)        Code+Description only
  Zip (Address)                → (none)        Code only
  Diagnoses/Problems           → ICD10         E11.9, I10, J45.9
  Lab TestItemCode/ObsCode     → LN            4548-4 A1C · 2160-0 creatinine · 8480-6 systolic BP
  DrugProduct (medications)    → RxNorm        860975 metformin 500mg
  AllergyCode/Reaction         → SNM           372687004 amoxicillin · 271807003 rash
  AllergyCategory              → HL7           DA Drug · FA Food · EA Environmental
  Procedures                   → CPT           99213, 93000
  Vaccines (DrugProduct/OrderItem) → CVX       140 flu · 207 COVID mRNA
  Route                        → HL7           PO, IV, IM, SC, INH, TOP
  DosageForm                   → NCI           TAB, CAP, SOL, INJ
  DoseUoM                      → UCUM          mg, mL, mcg, mg/kg

## Patient example (follow this element order exactly)
<Patient>
  <Name>
    <FamilyName>Rodriguez</FamilyName>
    <GivenName>Maria</GivenName>
    <MiddleName>L.</MiddleName>
    <Type>Legal</Type>
  </Name>
  <Gender>
    <SDACodingStandard>HL7</SDACodingStandard>
    <Code>F</Code>
    <Description>Female</Description>
  </Gender>
  <Race>
    <SDACodingStandard>CDCREC</SDACodingStandard>
    <Code>2106-3</Code>
    <Description>White</Description>
  </Race>
  <EthnicGroup>
    <SDACodingStandard>CDCREC</SDACodingStandard>
    <Code>2186-5</Code>
    <Description>Not Hispanic or Latino</Description>
  </EthnicGroup>
  <BirthTime>1968-04-22T00:00:00Z</BirthTime>
  <PatientNumbers>
    <PatientNumber>
      <Number>MRN381042</Number>
      <NumberType>MRN</NumberType>
      <Organization>
        <Code>GH001</Code>
        <Description>General Hospital</Description>
      </Organization>
    </PatientNumber>
    <PatientNumber>
      <Number>ST-MCD-4471839</Number>
      <NumberType>MA</NumberType>
      <Organization>
        <Code>STMCD</Code>
        <Description>State Medicaid</Description>
      </Organization>
    </PatientNumber>
  </PatientNumbers>
  <Addresses>
    <Address>
      <Street>742 Maple Street</Street>
      <City><Code>SOMECITY</Code><Description>Some City</Description></City>
      <State><SDACodingStandard>USPS</SDACodingStandard><Code>ST</Code><Description>State Name</Description></State>
      <Zip><Code>43215</Code></Zip>
      <Country><SDACodingStandard>ISO 3166</SDACodingStandard><Code>US</Code><Description>United States</Description></Country>
      <County><SDACodingStandard>FIPS</SDACodingStandard><Code>00049</Code><Description>Some County</Description></County>
    </Address>
  </Addresses>
  <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
  <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
  <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
  <ActionCode>A</ActionCode>
</Patient>
NOTE: Patient has NO SendingFacility field. Do not add one.
MRN format: MRN + 6 digits. NumberType values: MRN, MA (Medicaid), MC (Medicare).

## Encounter example (follow this element order exactly)
CRITICAL internal order: EncounterNumber → EncounterType → AttendingClinicians → ReferringClinician
→ HealthCareFacility → HealthFunds → ActionCode → EnteredBy → EnteredAt → EnteredOn → FromTime → ToTime
EncounterType codes: I=Inpatient, O=Outpatient, E=Emergency, G=Generated.
HealthFunds.HealthFund inner element uses HealthFundCode type: SDACodingStandard FIRST, then Code, then Description.
CRITICAL: <SDACodingStandard> must be the very first child of the inner <HealthFund> element — before <Code> and <Description>.
Priority=1 (primary insurance).
<Encounters>
  <Encounter>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <EncounterType>O</EncounterType>
    <AttendingClinicians>
      <CareProvider>
        <Code>DR456</Code>
        <Description>Dr. Smith</Description>
      </CareProvider>
    </AttendingClinicians>
    <ReferringClinician>
      <Code>DR123</Code>
      <Description>Dr. Jones</Description>
    </ReferringClinician>
    <HealthCareFacility>
      <Organization>
        <Code>GH001</Code>
        <Description>General Hospital</Description>
      </Organization>
    </HealthCareFacility>
    <HealthFunds>
      <HealthFund>
        <HealthFund>
          <SDACodingStandard>QD_HealthPlanCodeList</SDACodingStandard>
          <Code>BCBS</Code>
          <Description>Blue Cross Blue Shield</Description>
        </HealthFund>
        <HealthFundPlan>
          <Code>BCBS-PPO</Code>
          <Description>BCBS PPO Plan</Description>
        </HealthFundPlan>
        <GroupName>Employer Group Plan</GroupName>
        <GroupNumber>GRP-12345</GroupNumber>
        <MembershipNumber>MBR-98765</MembershipNumber>
        <PlanType>PPO</PlanType>
        <Priority>1</Priority>
        <FromTime>2024-01-01T00:00:00Z</FromTime>
        <ToTime>2024-12-31T00:00:00Z</ToTime>
      </HealthFund>
    </HealthFunds>
    <ActionCode>A</ActionCode>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T08:00:00Z</FromTime>
    <ToTime>2024-03-15T14:00:00Z</ToTime>
  </Encounter>
</Encounters>

## MedicalClaim example (comes AFTER Vaccinations, BEFORE SocialDeterminants)
AdjudicatedCoverage is a plain string (insurer name). No ExternalId needed.
ProcedureCode in MedicalClaimLine uses Description only (free text).
CRITICAL: MedicalClaim does NOT use UpdatedOn, EnteredBy, EnteredAt, EnteredOn, or any SuperClass fields.
<MedicalClaims>
  <MedicalClaim>
    <MedicalClaimNumber>CLM-20240315-001</MedicalClaimNumber>
    <FromTime>2024-03-15T08:00:00Z</FromTime>
    <ToTime>2024-03-15T14:00:00Z</ToTime>
    <ClaimProcessedDate>2024-03-20T00:00:00Z</ClaimProcessedDate>
    <ReceivedDate>2024-03-16T00:00:00Z</ReceivedDate>
    <SubmissionDate>2024-03-16T00:00:00Z</SubmissionDate>
    <AdjudicatedCoverage>Blue Cross Blue Shield</AdjudicatedCoverage>
    <AdjudicationStatus><Code>complete</Code><Description>complete</Description></AdjudicationStatus>
    <Priority><Code>N</Code><Description>normal</Description></Priority>
    <Status><Code>active</Code><Description>active</Description></Status>
    <BillingProvider><Code>GH001</Code><Description>General Hospital</Description></BillingProvider>
    <ClaimType><Code>Professional</Code><Description>Professional</Description></ClaimType>
    <Payer><Code>BCBS</Code><Description>Blue Cross Blue Shield</Description></Payer>
    <PrimaryProcedure>
      <Procedure><Description>Office or other outpatient visit, moderate complexity (99213)</Description></Procedure>
    </PrimaryProcedure>
    <PaymentStatus><Code>paid</Code><Description>paid</Description></PaymentStatus>
    <PaymentAmount>120</PaymentAmount>
    <MedicalClaimLines>
      <MedicalClaimLine>
        <LineCounter>1</LineCounter>
        <FromTime>2024-03-15T08:00:00Z</FromTime>
        <ToTime>2024-03-15T14:00:00Z</ToTime>
        <ProcedureCode><Description>Office or other outpatient visit, moderate complexity (99213)</Description></ProcedureCode>
        <AdjudicationDetails>
          <AdjudicationDetail>
            <Category><Code>submitted</Code><Description>submitted</Description></Category>
          </AdjudicationDetail>
        </AdjudicationDetails>
      </MedicalClaimLine>
    </MedicalClaimLines>
  </MedicalClaim>
</MedicalClaims>

## Diagnosis example (follow this element order exactly)
CRITICAL: No SDACodingStandard on nested <Diagnosis> code or <DiagnosisType>.
No ActionCode. DiagnosingClinician comes after EncounterNumber.
ExternalId REQUIRED at the end of each Diagnosis.
DiagnosisType code: C=Chronic, A=Admitting, W=Working, F=Final.
<Diagnoses>
  <Diagnosis>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <DiagnosingClinician><Code>DR456</Code><Description>Dr. Smith</Description></DiagnosingClinician>
    <Diagnosis>
      <Code>I10</Code>
      <Description>Essential hypertension</Description>
    </Diagnosis>
    <DiagnosisType>
      <Code>C</Code>
      <Description>Chronic</Description>
    </DiagnosisType>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ExternalId>Diagnoses_1</ExternalId>
  </Diagnosis>
</Diagnoses>

## Medication example (follow this element order exactly)
NOTE: No ActionCode. No SDACodingStandard on DrugProduct, DoseUoM, or Route.
Use FromTime (not StartTime). Use DoseQuantity+DoseUoM (not Dose).
ExternalId REQUIRED. EncounterNumber comes early (after FromTime/ExternalId).
<Medications>
  <Medication>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
    <ExternalId>Medications_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <OrderedBy><Code>DR456</Code><Description>Dr. Smith</Description></OrderedBy>
    <Frequency><Code>BID</Code><Description>Twice daily</Description></Frequency>
    <Status>active</Status>
    <DrugProduct>
      <Code>860975</Code>
      <Description>Metformin 500 MG Oral Tablet</Description>
    </DrugProduct>
    <DoseQuantity>500</DoseQuantity>
    <DoseUoM><Code>mg</Code><Description>mg</Description></DoseUoM>
    <DosageForm><Code>TAB</Code><Description>Tablet</Description></DosageForm>
    <Route><Code>PO</Code><Description>Oral</Description></Route>
  </Medication>
</Medications>

## LabOrder example (follow this element order exactly)
CRITICAL ordering rules:
- OrderItem, OrderedBy, EnteringOrganization, Specimen come BEFORE ActionCode/EnteredBy/EnteredOn/FromTime/ToTime
- SpecimenCollectedTime and Priority come AFTER Specimen, BEFORE ActionCode
- ToTime REQUIRED: use same value as FromTime for most orders
- ResultType MUST be set: use AT for atomic results (ResultItems). ResultType comes FIRST inside <Result>
- Inside <LabResultItem>: EnteredOn comes BEFORE TestItemCode (XSD sequence positions 2 vs 8)
- ResultNormalRange is a plain string — NEVER use ReferenceRange as plain text
- ResultStatus belongs at <Result> level, NEVER inside <LabResultItem>
- ExternalId REQUIRED at three levels: on LabOrder (before EncounterNumber), on Result, on each LabResultItem
<LabOrders>
  <LabOrder>
    <PlacerId>LAB-20240315-001</PlacerId>
    <OrderItem>
      <SDACodingStandard>LN</SDACodingStandard>
      <Code>4548-4</Code>
      <Description>Hemoglobin A1c panel</Description>
    </OrderItem>
    <OrderedBy><Code>DR456</Code><Description>Dr. Smith</Description></OrderedBy>
    <EnteringOrganization><Code>GH001</Code><Description>General Hospital</Description></EnteringOrganization>
    <Specimen>Blood</Specimen>
    <SpecimenCollectedTime>2024-03-15T08:00:00Z</SpecimenCollectedTime>
    <Priority><Code>R</Code><Description>Routine</Description></Priority>
    <ActionCode>A</ActionCode>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
    <ToTime>2024-03-15T00:00:00Z</ToTime>
    <Result>
      <ResultType>AT</ResultType>
      <ResultItems>
        <LabResultItem>
          <EnteredOn>2024-03-17T14:30:22Z</EnteredOn>
          <TestItemCode>
            <SDACodingStandard>LN</SDACodingStandard>
            <Code>4548-4</Code>
            <Description>Hemoglobin A1c/Hemoglobin.total in Blood</Description>
            <IsNumeric>true</IsNumeric>
          </TestItemCode>
          <ResultValue>7.8</ResultValue>
          <ResultValueUnits>%</ResultValueUnits>
          <ResultNormalRange>&lt;5.7</ResultNormalRange>
          <ResultInterpretation>H</ResultInterpretation>
          <ExternalId>LabResultItem_1</ExternalId>
        </LabResultItem>
      </ResultItems>
      <ResultTime>2024-03-17T14:30:22Z</ResultTime>
      <ResultStatus>F</ResultStatus>
      <ExternalId>Result_1</ExternalId>
    </Result>
    <ExternalId>LabOrder_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
  </LabOrder>
</LabOrders>

## Allergy example (follow this element order exactly)
CRITICAL: No SDACodingStandard on nested <Allergy>, <Reaction>, or <Severity>.
No ActionCode. No AllergyCategory. No SendingFacility.
Status is a plain string: A=Active, I=Inactive, R=Resolved.
ExternalId REQUIRED (before EncounterNumber). EncounterNumber is LAST.
<Allergies>
  <Allergy>
    <Allergy>
      <Code>PCN</Code>
      <Description>Penicillin</Description>
    </Allergy>
    <Reaction>
      <Code>R</Code>
      <Description>Rash</Description>
    </Reaction>
    <Severity>
      <Code>M</Code>
      <Description>Moderate</Description>
    </Severity>
    <Status>A</Status>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2023-11-05T00:00:00Z</EnteredOn>
    <FromTime>2023-11-05T00:00:00Z</FromTime>
    <ExternalId>Allergies_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
  </Allergy>
</Allergies>

## IllnessHistory example (Medical History tab in Clinical Viewer)
IllnessHistories comes AFTER Allergies and BEFORE SocialHistories in the XSD.
Condition.Code and Condition.Description are BOTH the same plain-text description — NOT a coded ICD value.
No SDACodingStandard on Condition. No EncounterNumber (encounter-less). ExternalId REQUIRED.
FromTime = onset date. ToTime = resolution date (omit for ongoing conditions).
<IllnessHistories>
  <IllnessHistory>
    <Condition>
      <Code>Essential hypertension</Code>
      <Description>Essential hypertension</Description>
    </Condition>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <Clinician><Code>DR456</Code><Description>Dr. Smith</Description></Clinician>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2018-04-10T00:00:00Z</FromTime>
    <ExternalId>IllnessHistories_1</ExternalId>
  </IllnessHistory>
  <IllnessHistory>
    <Condition>
      <Code>Appendectomy (remote surgical history)</Code>
      <Description>Appendectomy (remote surgical history)</Description>
    </Condition>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <Clinician><Code>DR456</Code><Description>Dr. Smith</Description></Clinician>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>1995-06-01T00:00:00Z</FromTime>
    <ToTime>1995-06-08T00:00:00Z</ToTime>
    <ExternalId>IllnessHistories_2</ExternalId>
  </IllnessHistory>
</IllnessHistories>

## SocialHistory example
SocialHistories comes AFTER IllnessHistories and BEFORE Diagnoses in the XSD.
No SDACodingStandard on SocialHabit. No EncounterNumber (encounter-less data). ExternalId REQUIRED.
CRITICAL: There is NO <Clinician> field in SocialHistory — never add one. The XSD does not define it.
Common SocialHabit codes: NS=Non Smoker, TOBA=Tobacco/Smoker, ETOH=Alcohol, NOALC=No Alcohol, DRUGS=Drug use.
XSD field order: SocialHabit → SocialHabitComments → Status → EnteredBy → EnteredAt → EnteredOn → FromTime → ExternalId
<SocialHistories>
  <SocialHistory>
    <SocialHabit>
      <Code>NS</Code>
      <Description>Non Smoker</Description>
    </SocialHabit>
    <SocialHabitComments>Patient has never smoked.</SocialHabitComments>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
    <ExternalId>SocialHistories_1</ExternalId>
  </SocialHistory>
</SocialHistories>

## FamilyHistory example
FamilyHistories comes AFTER SocialHistories and BEFORE Diagnoses in the XSD.
FamilyMember Code+Description (no SDACodingStandard). Diagnosis Code+Description (no SDACodingStandard). ExternalId REQUIRED.
Common FamilyMember codes: FTH=Father, MTH=Mother, SIB=Sibling, CHILD=Child, GRNDF=Grandfather, GRNDF=Grandmother.
<FamilyHistories>
  <FamilyHistory>
    <FamilyMember>
      <Code>FTH</Code>
      <Description>Father</Description>
    </FamilyMember>
    <Diagnosis>
      <Code>I10</Code>
      <Description>Essential hypertension</Description>
    </Diagnosis>
    <NoteText>Father had hypertension diagnosed in his 50s.</NoteText>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ExternalId>FamilyHistories_1</ExternalId>
  </FamilyHistory>
</FamilyHistories>

## SocialDeterminants example
SocialDeterminants comes AFTER Vaccinations (near the end of Container) in the XSD.
Status is plain string "Active". SocialDeterminantCode uses Description only (no Code, no SDACodingStandard).
SocialDeterminantValueText holds the answer. No EncounterNumber. ExternalId REQUIRED.
<SocialDeterminants>
  <SocialDeterminant>
    <Status>Active</Status>
    <SocialDeterminantCode>
      <Description>Housing status</Description>
    </SocialDeterminantCode>
    <SocialDeterminantValueText>Owner</SocialDeterminantValueText>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ExternalId>SocialDeterminants_1</ExternalId>
  </SocialDeterminant>
</SocialDeterminants>

## Observation / vital signs example (follow this element order exactly)
XSD field order: ExternalId → EncounterNumber → ObservationTime → ObservationCode →
  ObservationValue → EnteredBy → EnteredAt → EnteredOn
CRITICAL: ExternalId is the VERY FIRST element. ObservationCode has NO SDACodingStandard.
Never place ExternalId after ObservationCode, ObservationValue, EnteredAt, or EnteredOn — it MUST be first.
No ActionCode on Observations.
<Observations>
  <Observation>
    <ExternalId>Observations_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <ObservationTime>2024-03-15T14:00:00Z</ObservationTime>
    <ObservationCode>
      <Code>8480-6</Code>
      <Description>BP Systolic</Description>
    </ObservationCode>
    <ObservationValue>142</ObservationValue>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T14:00:00Z</EnteredOn>
  </Observation>
</Observations>

## Procedure example (follow this element order exactly)
CRITICAL: The procedure code lives in a CHILD <Procedure> element (same name as parent).
Never put <SDACodingStandard> directly inside the outer <Procedure> — wrap it in <Procedure>.
<Procedures>
  <Procedure>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <Procedure>
      <SDACodingStandard>CPT</SDACodingStandard>
      <Code>99213</Code>
      <Description>Office visit, moderate complexity</Description>
    </Procedure>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ActionCode>A</ActionCode>
    <SendingFacility>GH001</SendingFacility>
  </Procedure>
</Procedures>

## Problem example (follow this element order exactly)
CRITICAL: The problem code is in a CHILD <Problem> element (same name as parent) of type BodyPart.
It comes EARLY in the sequence — BEFORE ActionCode, EnteredBy, EnteredOn, and FromTime.
<Problems>
  <Problem>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <Problem>
      <SDACodingStandard>ICD10</SDACodingStandard>
      <Code>I50.9</Code>
      <Description>Heart failure, unspecified</Description>
    </Problem>
    <Status>
      <SDACodingStandard>SNOMED CT</SDACodingStandard>
      <Code>55561003</Code>
      <Description>Active</Description>
    </Status>
    <ActionCode>A</ActionCode>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
  </Problem>
</Problems>

## Document example (clinical notes — progress notes, discharge summaries, etc.)
Documents section goes AFTER <Procedures> and BEFORE <LabOrders>.
NO SDACodingStandard on DocumentType. ExternalId REQUIRED.
NoteText is plain text (not Base64). Use SOAP format for progress notes.
DocumentType codes: PN=Progress Note, DS=Discharge Summary, SV=Sick Visit, H&P=History and Physical.
Include Documents for ALL encounters — outpatient progress notes, inpatient H&P and discharge summaries.
The NoteText should be clinically coherent with the patient's diagnoses, medications, and labs.
<Documents>
  <Document>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <DocumentTime>2024-03-15T14:00:00Z</DocumentTime>
    <NoteText>CHIEF COMPLAINT: Follow-up for hypertension.

HISTORY OF PRESENT ILLNESS: Patient presents for routine follow-up of essential hypertension. Reports good medication compliance. Denies chest pain, shortness of breath, or dizziness.

PHYSICAL EXAMINATION:
Vitals: BP 138/82 mmHg, HR 74 bpm, Weight 185 lbs
General: Well-appearing, no acute distress.
Cardiovascular: Regular rate and rhythm, no murmurs.
Respiratory: Clear to auscultation.

ASSESSMENT AND PLAN:
1. Essential hypertension - Blood pressure slightly elevated. Continue current antihypertensives. Reinforce dietary sodium restriction and exercise.

FOLLOW-UP: Return in 3 months.</NoteText>
    <DocumentType>
      <Code>PN</Code>
      <Description>Progress Note</Description>
    </DocumentType>
    <Clinician><Code>DR456</Code><Description>Dr. Smith</Description></Clinician>
    <DocumentName>Outpatient Progress Note</DocumentName>
    <FileType>Progress Note</FileType>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T14:00:00Z</EnteredOn>
    <ExternalId>Documents_1</ExternalId>
  </Document>
</Documents>

For inpatient encounters (EncounterType=I), include both an H&P on admission and a Discharge Summary:
- H&P DocumentType Code="HP", filed on admission date
- Discharge Summary DocumentType Code="DS", filed on discharge date; text must include hospital course, discharge diagnoses, discharge medications, and follow-up instructions.

## RadOrder example (Diagnostic Studies tab — comes AFTER LabOrders, BEFORE Medications)
XSD field order: PlacerId → OrderItem → OrderedBy → Specimen → SpecimenCollectedTime →
  EnteredBy → EnteredAt → EnteredOn → FromTime → ToTime → Result → ReasonForStudy →
  ExternalId → EncounterNumber
CRITICAL: OrderedBy MUST come BEFORE Specimen. Result and ReasonForStudy come AFTER FromTime/ToTime.
EncounterNumber is LAST. No SDACodingStandard on OrderItem. ResultText is plain narrative text.
<RadOrders>
  <RadOrder>
    <PlacerId>RAD-20240315-001</PlacerId>
    <OrderItem>
      <Code>CXR-PA-LAT</Code>
      <Description>Chest X-Ray PA and Lateral</Description>
    </OrderItem>
    <OrderedBy><Code>DR456</Code><Description>Dr. Smith</Description></OrderedBy>
    <Specimen>Chest radiograph</Specimen>
    <SpecimenCollectedTime>2024-03-15T08:30:00Z</SpecimenCollectedTime>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T08:30:00Z</FromTime>
    <ToTime>2024-03-15T08:30:00Z</ToTime>
    <Result>
      <ResultTime>2024-03-15T11:00:00Z</ResultTime>
      <ResultText>No acute cardiopulmonary process. Borderline cardiac enlargement.</ResultText>
      <EnteredOn>2024-03-15T08:30:00Z</EnteredOn>
      <AuthorizationTime>2024-03-15T11:00:00Z</AuthorizationTime>
    </Result>
    <ReasonForStudy>
      <Description>Evaluate for cardiomegaly</Description>
    </ReasonForStudy>
    <ExternalId>RadOrders_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
  </RadOrder>
</RadOrders>

## Vaccination example (follow this element order exactly)
Vaccination extends SuperClass. SuperClass fields (EnteredBy/EnteredAt/EnteredOn/FromTime/
ExternalId/EncounterNumber) come FIRST in the effective XSD sequence, BEFORE the clinical fields.
XSD field order: EnteredBy → EnteredAt → EnteredOn → FromTime → ExternalId → EncounterNumber
  → OrderItem → OrderedBy → Frequency → Status → DrugProduct → DoseQuantity → DoseUoM
  → DosageForm → Route → Indication
NO ActionCode. NO SDACodingStandard on OrderItem, DrugProduct, DoseUoM, DosageForm, or Route.
OrderItem and DrugProduct use the same CVX code. Status is the plain string "V" (given/verified).
<Vaccinations>
  <Vaccination>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2022-10-01T00:00:00Z</EnteredOn>
    <FromTime>2022-10-01T00:00:00Z</FromTime>
    <ExternalId>Vaccinations_1</ExternalId>
    <EncounterNumber>ENC-2022100101</EncounterNumber>
    <OrderItem><Code>141</Code><Description>Influenza, seasonal, injectable</Description></OrderItem>
    <OrderedBy><Code>DR456</Code><Description>Dr. Smith</Description></OrderedBy>
    <Frequency><Code>O</Code><Description>Once</Description></Frequency>
    <Status>V</Status>
    <DrugProduct>
      <Code>141</Code>
      <Description>Influenza, seasonal, injectable</Description>
    </DrugProduct>
    <DoseQuantity>1</DoseQuantity>
    <DoseUoM><Code>dose</Code><Description>dose</Description></DoseUoM>
    <DosageForm><Code>I</Code><Description>injection</Description></DosageForm>
    <Route><Code>IM</Code><Description>intramuscular</Description></Route>
    <Indication>vaccination</Indication>
  </Vaccination>
</Vaccinations>

## What NOT to include
- CustomPairs, ProvenanceIds, SdaID, SourceFormat — omit
- Extension elements — omit
- MPIID — omit
- Guarantors — omit
- SendingFacility inside <Patient> or <Observation> — these types do not have that field
- SendingFacility inside <LabOrder> — LabOrder does not use SendingFacility; omit it entirely from LabOrder records
- Empty sections like <Diagnoses></Diagnoses> — omit entirely if no records
""".strip()

# ---------------------------------------------------------------------------
# Scenario builder
# ---------------------------------------------------------------------------

def weighted_choice(items_with_weights: list, rng: random.Random = None) -> tuple:
    _rng = rng or random
    total = sum(w for *_, w in items_with_weights)
    r = _rng.uniform(0, total)
    cumulative = 0.0
    for *item, weight in items_with_weights:
        cumulative += weight
        if r <= cumulative:
            return tuple(item)
    return tuple(items_with_weights[-1][:-1])


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

# Active config — populated by load_config(), used by build_scenario()
_config: dict = {}


def load_config(path: str):
    """Load a population config JSON file and merge into active config."""
    global _config
    with open(path) as f:
        _config = json.load(f)
    print(f"Config loaded: {path}")


def _get(key: str, default):
    """Return config value if set, else the built-in default."""
    return _config.get(key, default)


def build_scenario(patient_id: int) -> str:
    rng = random.Random(patient_id)  # deterministic per patient_id

    locations = _get("locations", [])
    if not locations:
        raise ValueError("Config must include a 'locations' array. See ohio_demo.json for an example.")
    county, city, zip_code, region = weighted_choice([(*loc,) for loc in locations], rng)

    cohorts = _get("cohorts", COHORTS)
    cohort = weighted_choice(cohorts, rng)[0]

    races = _get("races", RACES)
    race_code, race_desc = weighted_choice(races, rng)

    ethnicities = _get("ethnicities", ETHNICITIES)
    eth_row = weighted_choice(ethnicities, rng)
    eth_code, eth_desc = eth_row[0], eth_row[1]

    insurances = _get("insurances", INSURANCES)
    ins_row = weighted_choice(insurances, rng)
    ins_type, ins_org = ins_row[0], ins_row[1]

    age_min = _get("age_min", 18)
    age_max = _get("age_max", 85)
    age = rng.randint(age_min, age_max)
    if cohort == "pregnancy":
        age = rng.randint(age_min, min(age_max, 45))

    gender_code = rng.choice(["M", "F"])
    if cohort == "pregnancy":
        gender_code = "F"
    gender_desc = "Male" if gender_code == "M" else "Female"

    # Cohort descriptions: config can override individual cohorts or add custom ones
    cohort_descriptions = copy.copy(COHORT_DESCRIPTIONS)
    cohort_descriptions.update(_get("cohort_descriptions", {}))
    cohort_detail = cohort_descriptions.get(
        cohort, f"patient with condition: {cohort}"
    )

    # Optional extra instructions from config (e.g. facility naming conventions)
    extra = _get("extra_instructions", "")
    extra_note = f"\n- Additional instructions: {extra}" if extra else ""

    ins_note = f"Insurance: {ins_org or 'commercial'}"
    eth_note = f", ethnicity: {eth_desc}" if eth_desc else ""
    rurality_note = f"({region} setting)"

    state = _get("state", "Unknown State")
    state_code = _get("state_code", "XX")

    return (
        f"Generate a single synthetic SDA3 patient record with these characteristics:\n\n"
        f"- County: {county}, City: {city}, {state_code} {zip_code} {rurality_note}\n"
        f"- Age: approximately {age} years old\n"
        f"- Gender: {gender_desc}\n"
        f"- Race: {race_desc} (CDCREC code {race_code}){eth_note}\n"
        f"- {ins_note}\n"
        f"- Clinical scenario: {cohort_detail}"
        f"{extra_note}\n\n"
        f"Patient ID for file tracking: {patient_id:06d}"
    )


# ---------------------------------------------------------------------------
# LLM client helpers
# ---------------------------------------------------------------------------

def _make_async_client(base_url: str):
    """Return an AsyncOpenAI client pointed at base_url."""
    from openai import AsyncOpenAI
    import os
    if base_url and "openai.com" not in base_url:
        return AsyncOpenAI(base_url=base_url, api_key="none")
    return AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))


def _make_sync_client(base_url: str):
    """Return a synchronous OpenAI client pointed at base_url."""
    from openai import OpenAI
    import os
    if base_url and "openai.com" not in base_url:
        return OpenAI(base_url=base_url, api_key="none")
    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))


def _extra_body(model: str) -> dict | None:
    """Disable Qwen3 thinking mode so the model returns plain XML without reasoning tokens."""
    if "qwen" in model.lower():
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return None


# ---------------------------------------------------------------------------
# Async mode
# ---------------------------------------------------------------------------

async def generate_one_async(
    patient_id: int,
    output_dir: Path,
    delete_dir: Path,
    model: str,
    semaphore: asyncio.Semaphore,
    resume: bool,
    schema,
    base_url: str = DEFAULT_BASE_URL,
) -> bool:
    output_file = output_dir / f"patient_{patient_id:06d}.xml"
    if resume and output_file.exists():
        return True

    scenario = build_scenario(patient_id)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": scenario},
    ]

    async with semaphore:
        oai = _make_async_client(base_url)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await oai.chat.completions.create(
                    model=model,
                    max_tokens=16384,
                    messages=messages,
                    extra_body=_extra_body(model),
                )
                xml = sanitize_xml_text_nodes(strip_fences(response.choices[0].message.content))

                if schema is not None:
                    is_valid, errors = validate_xml(xml, schema)
                    if not is_valid:
                        if attempt < MAX_RETRIES:
                            print(
                                f"  [RETRY {attempt}/{MAX_RETRIES}] patient_{patient_id:06d}: "
                                f"{len(errors)} error(s) — sending back for correction",
                                flush=True,
                            )
                            for e in errors[:5]:
                                print(f"    ERR: {e}", flush=True)
                            messages = [
                                {"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user",   "content": build_fix_prompt(xml, errors)},
                            ]
                            continue
                        else:
                            print(
                                f"  [FAIL] patient_{patient_id:06d}: "
                                f"still invalid after {MAX_RETRIES} attempts — "
                                f"saving as .invalid.xml",
                                flush=True,
                            )
                            (output_dir / f"patient_{patient_id:06d}.invalid.xml").write_text(
                                xml, encoding="utf-8"
                            )
                            return False

                sending_facility = _get("sending_facility", "")
                if sending_facility:
                    xml = inject_container_fields(xml, sending_facility)
                output_file.write_text(xml, encoding="utf-8")
                delete_file = delete_dir / f"patient_{patient_id:06d}_delete.xml"
                delete_file.write_text(generate_delete_sda(xml, sending_facility), encoding="utf-8")
                tag = f" (fixed in {attempt} attempts)" if attempt > 1 else ""
                print(f"  [OK{tag}] {output_file.name}", flush=True)
                return True

            except Exception as exc:
                print(f"  [ERR] patient_{patient_id:06d}: {exc}", flush=True)
                return False

    return False


async def run_async(count: int, output_dir: Path, delete_dir: Path, model: str, concurrency: int,
                    resume: bool, schema, base_url: str = DEFAULT_BASE_URL):
    semaphore = asyncio.Semaphore(concurrency)
    validate_note = "with XSD validation" if schema is not None else "validation disabled"
    print(f"Async mode: {count} patients, concurrency={concurrency}, model={model}, "
          f"{validate_note}")
    tasks = [
        generate_one_async(i, output_dir, delete_dir, model, semaphore, resume, schema, base_url)
        for i in range(1, count + 1)
    ]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r)
    print(f"\nDone. {ok}/{count} valid (or already existed).")


# ---------------------------------------------------------------------------
# Validate-and-fix mode — repair existing files in the output directory
# ---------------------------------------------------------------------------

async def _fix_one_file(filepath: Path, xml_str: str, errors: list, model: str,
                        semaphore: asyncio.Semaphore, schema,
                        base_url: str = DEFAULT_BASE_URL) -> bool:
    async with semaphore:
        oai = _make_async_client(base_url)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": build_fix_prompt(xml_str, errors)},
        ]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await oai.chat.completions.create(
                    model=model, max_tokens=16384, messages=messages,
                    extra_body=_extra_body(model),
                )
                xml = sanitize_xml_text_nodes(strip_fences(response.choices[0].message.content))
                is_valid, new_errors = validate_xml(xml, schema)
                if not is_valid:
                    if attempt < MAX_RETRIES:
                        print(
                            f"  [RETRY {attempt}] {filepath.name}: "
                            f"{len(new_errors)} error(s) remain",
                            flush=True,
                        )
                        messages = [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user",   "content": build_fix_prompt(xml, new_errors)},
                        ]
                        continue
                    print(f"  [FAIL] {filepath.name}: still invalid after {MAX_RETRIES} attempts",
                          flush=True)
                    return False
                sending_facility = _get("sending_facility", "")
                if sending_facility:
                    xml = inject_container_fields(xml, sending_facility)
                filepath.write_text(xml, encoding="utf-8")
                delete_dir = filepath.parent / "Delete"
                delete_dir.mkdir(exist_ok=True)
                delete_file = delete_dir / filepath.name.replace(".xml", "_delete.xml")
                delete_file.write_text(generate_delete_sda(xml, sending_facility), encoding="utf-8")
                print(f"  [FIXED] {filepath.name} (attempt {attempt})", flush=True)
                return True
            except Exception as exc:
                print(f"  [ERR] {filepath.name}: {exc}", flush=True)
                return False
    return False


async def validate_and_fix_dir(output_dir: Path, model: str, concurrency: int, schema,
                               base_url: str = DEFAULT_BASE_URL):
    if schema is None:
        print("XSD validation not available. Install lxml and ensure SDA.xsd is present.")
        return

    xml_files = sorted(
        f for f in output_dir.glob("patient_*.xml")
        if not f.name.endswith(".invalid.xml")
    )
    if not xml_files:
        print(f"No patient XML files found in {output_dir}/")
        return

    print(f"Validating {len(xml_files)} file(s)...")
    invalid = []
    for f in xml_files:
        try:
            content = f.read_text(encoding="utf-8")
            is_valid, errors = validate_xml(content, schema)
            if is_valid:
                print(f"  [OK] {f.name}", flush=True)
            else:
                print(f"  [INVALID] {f.name}: {len(errors)} error(s)", flush=True)
                invalid.append((f, content, errors))
        except Exception as e:
            print(f"  [READ ERR] {f.name}: {e}", flush=True)

    if not invalid:
        print(f"\nAll {len(xml_files)} file(s) are valid.")
        return

    print(f"\nFixing {len(invalid)} invalid file(s)...")
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _fix_one_file(f, content, errors, model, semaphore, schema, base_url)
        for f, content, errors in invalid
    ]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r)
    print(f"\nValidation pass complete. Fixed: {ok}/{len(invalid)}")


# ---------------------------------------------------------------------------
# Batch mode — submit
# ---------------------------------------------------------------------------

def batch_submit(count: int, output_dir: Path, model: str, resume: bool,
                 base_url: str = DEFAULT_BASE_URL):
    import io
    client = _make_sync_client(base_url)

    patient_ids = [
        i for i in range(1, count + 1)
        if not (resume and (output_dir / f"patient_{i:06d}.xml").exists())
    ]
    skipped = count - len(patient_ids)

    if not patient_ids:
        print("All patients already exist. Nothing to submit.")
        return

    # Build JSONL content for the OpenAI Batch API
    lines = []
    for pid in patient_ids:
        scenario = build_scenario(pid)
        record = {
            "custom_id": f"patient-{pid:06d}",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": model,
                "max_tokens": 16384,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": scenario},
                ],
            },
        }
        lines.append(json.dumps(record))

    print(f"Submitting batch: {len(patient_ids)} patients (skipped {skipped} existing)...")

    # Upload the JSONL file
    jsonl_bytes = "\n".join(lines).encode("utf-8")
    upload = client.files.create(
        file=("batch_input.jsonl", io.BytesIO(jsonl_bytes), "application/jsonl"),
        purpose="batch",
    )

    # Create the batch
    batch = client.batches.create(
        input_file_id=upload.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )

    status_file = output_dir / "batch_status.json"
    status_file.write_text(json.dumps({
        "batch_id": batch.id,
        "input_file_id": upload.id,
        "patient_count": len(patient_ids),
        "model": model,
        "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))

    print(f"\nBatch submitted successfully.")
    print(f"  Batch ID : {batch.id}")
    print(f"  Patients : {len(patient_ids)}")
    print(f"  Status   : {status_file}")
    print(f"\nTo retrieve results when ready:")
    print(f"  python generate_population.py --mode batch --batch-id {batch.id}")


# ---------------------------------------------------------------------------
# Batch mode — retrieve
# ---------------------------------------------------------------------------

def batch_retrieve(batch_id: str, output_dir: Path, base_url: str = DEFAULT_BASE_URL):
    client = _make_sync_client(base_url)

    print(f"Checking batch {batch_id}...")
    while True:
        batch = client.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"  Status: {batch.status}  |  "
            f"completed={counts.completed}  failed={counts.failed}  total={counts.total}",
            flush=True,
        )
        if batch.status == "completed":
            break
        if batch.status in ("failed", "expired", "cancelled"):
            print(f"  Batch ended with status: {batch.status}")
            return
        print("  Not ready yet — waiting 30 seconds...")
        time.sleep(30)

    if not batch.output_file_id:
        print("  No output file — batch may have had no successful results.")
        return

    content = client.files.content(batch.output_file_id).text
    written = 0
    api_errors = 0
    invalid_count = 0

    schema = load_schema()

    for line in content.splitlines():
        if not line.strip():
            continue
        result = json.loads(line)
        custom_id = result["custom_id"]           # e.g. "patient-000042"
        patient_num = int(custom_id.split("-")[1])
        output_file = output_dir / f"patient_{patient_num:06d}.xml"

        if result.get("error"):
            print(f"  [ERR] {custom_id}: {result['error']}", flush=True)
            api_errors += 1
        else:
            xml = strip_fences(
                result["response"]["body"]["choices"][0]["message"]["content"]
            )
            if schema is not None:
                is_valid, val_errors = validate_xml(xml, schema)
                if not is_valid:
                    print(
                        f"  [INVALID] {custom_id}: {len(val_errors)} validation error(s) — "
                        f"saving as .invalid.xml (run --mode validate to fix)",
                        flush=True,
                    )
                    (output_dir / f"patient_{patient_num:06d}.invalid.xml").write_text(
                        xml, encoding="utf-8"
                    )
                    invalid_count += 1
                    continue
            sending_facility = _get("sending_facility", "")
            if sending_facility:
                xml = inject_container_fields(xml, sending_facility)
            output_file.write_text(xml, encoding="utf-8")
            _del_dir = output_dir / "Delete"
            _del_dir.mkdir(exist_ok=True)
            (_del_dir / f"patient_{patient_num:06d}_delete.xml").write_text(
                generate_delete_sda(xml, sending_facility), encoding="utf-8"
            )
            written += 1

    print(f"\nDone. Written: {written}, API errors: {api_errors}, "
          f"XSD invalid (saved as .invalid.xml): {invalid_count}")
    if invalid_count:
        print(f"Run:  python generate_population.py --mode validate  to fix invalid files.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic SDA3 XML patient files using the OpenAI API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--count", type=int, default=10,
                        help="Number of patients to generate (default: 10)")
    parser.add_argument("--output-dir", default="Population",
                        help="Output directory (default: Population/)")
    parser.add_argument("--mode", choices=["async", "batch", "validate"], default="async",
                        help=(
                            "async=concurrent requests with XSD validation+retry; "
                            "batch=Batches API at 50%% cost; "
                            "validate=validate+fix existing files in output-dir"
                        ))
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Parallel requests for async/validate mode (default: 10)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"LLM model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=("OpenAI-compatible base URL. Omit for OpenAI; "
                              f"use {ISC_LLM_BASE_URL} for the internal ISC LLM"))
    parser.add_argument("--resume", action="store_true",
                        help="Skip patients whose output files already exist")
    parser.add_argument("--batch-id",
                        help="Retrieve results of a previously submitted batch (batch mode only)")
    parser.add_argument("--config",
                        help="Path to a population config JSON file (overrides built-in defaults)")
    parser.add_argument("--no-validate", action="store_true",
                        help="Disable XSD validation even if lxml and SDA.xsd are available")
    args = parser.parse_args()

    if args.config:
        load_config(args.config)

    output_dir = Path(args.output_dir)
    delete_dir = output_dir / "Delete"
    output_dir.mkdir(parents=True, exist_ok=True)
    delete_dir.mkdir(exist_ok=True)

    # Clear output and delete dirs before each fresh run (skip when resuming or fixing)
    if not args.resume and args.mode not in ("validate", "batch"):
        for f in output_dir.glob("patient_*.xml"):
            f.unlink()
        for f in output_dir.glob("patient_*.invalid.xml"):
            f.unlink()
        for f in delete_dir.glob("patient_*_delete.xml"):
            f.unlink()
        print(f"Cleared {output_dir}/ and {delete_dir}/")

    schema = None if args.no_validate else load_schema()

    if args.mode == "validate":
        asyncio.run(validate_and_fix_dir(output_dir, args.model, args.concurrency, schema,
                                         args.base_url))
    elif args.mode == "batch":
        if args.batch_id:
            batch_retrieve(args.batch_id, output_dir, args.base_url)
        else:
            batch_submit(args.count, output_dir, args.model, args.resume, args.base_url)
    else:
        asyncio.run(run_async(args.count, output_dir, delete_dir, args.model, args.concurrency,
                              args.resume, schema, args.base_url))


if __name__ == "__main__":
    main()
