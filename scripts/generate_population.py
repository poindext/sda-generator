#!/usr/bin/env python3
"""
SDA3 Population Generator
Generates synthetic SDA3 XML patient files from a JSON template (no LLM required).

Usage:
  python generate_population.py --template templates/fl_demo.template.json \
      --output populations/population-fl_1000 --count 1000
"""

import argparse
import concurrent.futures
import json
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

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

    base = Path(__file__).parent.parent / "schema"
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
# Template-driven pure-Python generator (no LLM)
# ---------------------------------------------------------------------------

_RACE_LABEL_MAP = {
    "white": "White",
    "black or african american": "Black",
    "black": "Black",
    "hispanic or latino": "Hispanic",
    "hispanic": "Hispanic",
    "asian": "Asian",
    "american indian or alaska native": "Native American",
    "native american": "Native American",
    "native hawaiian or other pacific islander": "Other",
}

_FAMILY_MEMBER_MAP = {
    "mother": "MTH", "father": "FTH", "sibling": "SIB",
    "brother": "SIB", "sister": "SIB", "child": "CHILD",
    "grandfather": "GRNDF", "grandmother": "GRNDM",
}

_REFERENCE_DATE = datetime(2026, 8, 24)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _wpick(items: list, weight_key: str, rng: random.Random):
    weights = [float(item.get(weight_key, 1.0)) for item in items]
    return rng.choices(items, weights=weights, k=1)[0]


def _race_label(description: str) -> str:
    return _RACE_LABEL_MAP.get(description.lower(), "Other")


def _xml_e(tag: str, val) -> str:
    return f"<{tag}>{val}</{tag}>"


def _entered_fields(prov_code, prov_name, fac_code, fac_name, dt: datetime) -> str:
    ts = _ts(dt.replace(hour=0, minute=0, second=0))
    return (
        f"    <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
        f"    <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
        f"    <EnteredOn>{ts}</EnteredOn>\n"
    )


def _result_value(item: dict, is_abnormal: bool, rng: random.Random) -> str:
    if item.get("text_values"):
        return rng.choice(item["text_values"])
    if is_abnormal:
        lo, hi = float(item.get("abnormal_min", 0)), float(item.get("abnormal_max", 100))
    else:
        lo, hi = float(item.get("normal_min", 0)), float(item.get("normal_max", 100))
    val = rng.uniform(lo, hi)
    return f"{val:.1f}" if "." in str(lo) or "." in str(hi) else str(int(val))


def _derive_interp(val_str: str, ri: dict) -> str:
    """Derive H/L/N interpretation from actual numeric value vs reference range."""
    if ri.get("text_values"):
        return "N"
    try:
        v = float(val_str)
        lo = ri.get("normal_min")
        hi = ri.get("normal_max")
        if lo is not None and v < float(lo):
            return "L"
        if hi is not None and v > float(hi):
            return "H"
        return "N"
    except (ValueError, TypeError):
        return "N"


# LOINC codes that represent vital signs needing coordinated generation
_VITAL_LOINC = {
    "8480-6": "bp_sys",      # Systolic BP
    "8462-4": "bp_dia",      # Diastolic BP
    "8867-4": "hr",          # Heart rate
    "29463-7": "weight_lb",  # Body weight
    "3141-9":  "weight_lb",  # Body weight (alt)
    "8302-2":  "height_cm",  # Height
    "39156-5": "bmi",        # BMI
    "8310-5":  "temp_c",     # Body temperature
    "59408-5": "spo2",       # SpO2
    "9279-1":  "resp_rate",  # Respiratory rate
}


def _build_enc_vitals(
    obs_list: list,
    n_encounters: int,
    rng: random.Random,
    sex: str = "M",
    age: int = 40,
) -> list:
    """
    Generate one vitals snapshot per encounter.  BP diastolic is always derived
    from systolic via a realistic pulse pressure so sys > dia is guaranteed.
    For adults: weight is derived from a height/BMI calculation.
    For pediatric patients (age < 18): age- and sex-specific height/weight
    tables are used instead of the adult BMI formula.
    """
    if age < 2:
        height_in = max(18.0, min(36.0, rng.gauss(27.0 if sex == "F" else 28.0, 3.0)))
        base_weight = max(8.0, min(30.0, rng.gauss(20.0, 4.0)))
        weight_floor = 8.0
    elif age < 6:
        height_in = max(32.0, min(48.0, rng.gauss(40.0 if sex == "F" else 41.5, 2.5)))
        base_weight = max(24.0, min(55.0, rng.gauss(37.0, 5.0)))
        weight_floor = 24.0
    elif age < 11:
        height_in = max(42.0, min(60.0, rng.gauss(50.0 if sex == "F" else 51.5, 3.0)))
        base_weight = max(38.0, min(100.0, rng.gauss(62.0, 10.0)))
        weight_floor = 38.0
    elif age < 15:
        height_in = max(56.0, min(68.0, rng.gauss(62.0 if sex == "F" else 62.5, 2.5)))
        base_weight = max(60.0, min(155.0, rng.gauss(98.0, 16.0)))
        weight_floor = 60.0
    elif age < 18:
        height_in = max(58.0, min(72.0 if sex == "F" else 76.0,
                                  rng.gauss(64.0 if sex == "F" else 68.0, 2.5)))
        base_weight = max(80.0, min(185.0, rng.gauss(128.0, 18.0)))
        weight_floor = 80.0
    else:
        if sex == "F":
            height_in = max(56.0, min(76.0, rng.gauss(64.0, 2.7)))
        else:
            height_in = max(60.0, min(80.0, rng.gauss(69.3, 2.7)))
        bmi = max(16.0, min(58.0, rng.gauss(29.5, 6.0)))
        base_weight = round(bmi * (height_in ** 2) / 703.0, 1)
        weight_floor = 88.0

    # Enforce minimum BMI of 13 for all pediatric patients so height/weight
    # combinations never produce implausibly low values against adult standards.
    if age < 18:
        _min_ped_weight = round(13.0 * height_in ** 2 / 703.0, 1)
        base_weight = max(base_weight, _min_ped_weight)
        weight_floor = max(weight_floor, _min_ped_weight)

    vitals = []
    for _ in range(n_encounters):
        # Weight: slow ±1.5 lb drift per encounter; floor is age-dependent.
        base_weight = max(weight_floor, min(500.0, base_weight + rng.uniform(-1.5, 1.5)))
        # BP: systolic first, diastolic = systolic − pulse_pressure (30–55 mmHg)
        bp_sys = rng.uniform(112.0, 162.0)
        bp_dia = max(50.0, bp_sys - rng.uniform(30.0, 55.0))
        hr = rng.uniform(58.0, 98.0)
        vitals.append({
            "bp_sys":     round(bp_sys, 1),
            "bp_dia":     round(bp_dia, 1),
            "hr":         round(hr),
            "weight_lb":  round(base_weight, 1),
        })
    return vitals, height_in


_PHRASE_POOLS = {
    "compliance": [
        "good medication compliance",
        "taking all medications as prescribed",
        "generally good adherence to the medication regimen",
        "some difficulty with medication adherence recently but improving",
        "improved medication compliance compared to last visit",
        "inconsistent with medications at times but doing better",
        "adherent to most medications; occasional missed doses",
        "following the medication regimen as directed",
    ],
    "general_exam": [
        "Well-appearing, no acute distress.",
        "Well-nourished and in no acute distress.",
        "Alert and oriented, comfortable-appearing.",
        "Well-appearing adult in no distress.",
        "Alert, cooperative, no acute distress.",
        "Pleasant, well-appearing, in no distress.",
    ],
    "symptom_denial_cardiovascular": [
        "Denies chest pain, exertional dyspnea, or palpitations.",
        "Reports no chest discomfort, orthopnea, or lower extremity edema.",
        "Denies angina, shortness of breath at rest, or presyncope.",
        "No recent chest pain, dyspnea on exertion, or leg swelling.",
        "Denies chest tightness, worsening shortness of breath, or syncope.",
        "Denies exertional chest pressure, paroxysmal nocturnal dyspnea, or palpitations.",
    ],
    "symptom_denial_diabetes": [
        "Denies polyuria, polydipsia, or blurred vision.",
        "Reports no excessive thirst, frequent urination, or tingling in feet.",
        "Denies hypoglycemic episodes, visual changes, or lower extremity numbness.",
        "No complaints of polydipsia, nocturia, or foot discomfort.",
        "Denies blurred vision, excessive fatigue, or foot sores.",
        "Denies dizziness, diaphoresis, or symptoms of hypoglycemia.",
    ],
    "symptom_denial_hypertension": [
        "Denies headache, visual changes, or chest pain.",
        "Reports no headaches, dizziness, or shortness of breath.",
        "Denies epistaxis, blurred vision, or chest tightness.",
        "No complaints of headache, palpitations, or shortness of breath.",
        "Denies morning headaches, visual disturbances, or dyspnea on exertion.",
        "Denies occipital headache, tinnitus, or chest discomfort.",
    ],
    "symptom_denial_asthma": [
        "Denies wheezing, shortness of breath, or nocturnal symptoms.",
        "Reports no recent exacerbations or nighttime awakenings.",
        "Denies cough, chest tightness, or exercise limitation.",
        "No recent episodes of wheezing or dyspnea at rest.",
        "Denies worsening shortness of breath, cough, or chest tightness.",
        "Denies nocturnal cough, rescue inhaler overuse, or dyspnea.",
    ],
    "symptom_denial_pregnancy": [
        "Denies contractions, vaginal bleeding, or leakage of fluid.",
        "Reports no contractions, spotting, or rupture of membranes.",
        "Denies uterine tightening, vaginal discharge, or decreased fetal movement.",
        "No complaints of contractions, bleeding, or abdominal pain.",
        "Denies preterm contractions, spotting, or fluid leakage.",
    ],
    "symptom_denial_reportable": [
        "Denies fever, night sweats, or weight loss.",
        "Reports no fever, chills, or unintentional weight loss.",
        "Denies hemoptysis, night sweats, or new pulmonary symptoms.",
        "No complaints of fever, fatigue, or productive cough.",
        "Denies systemic symptoms including fever, night sweats, or anorexia.",
    ],
    "symptom_denial_default": [
        "Denies chest pain, shortness of breath, or dizziness.",
        "Reports no chest discomfort, dyspnea, or lightheadedness.",
        "Denies chest pain, dyspnea on exertion, or syncope.",
        "No complaints of chest pain, shortness of breath, or palpitations.",
        "Denies significant cardiopulmonary symptoms.",
    ],
}

_INPATIENT_SCENARIOS = [
    {
        "icd_prefixes": ["O80", "O26", "Z34"],
        "chief_complaints": ["active labor", "scheduled delivery", "rupture of membranes"],
        "admission_dx": "Normal term delivery",
        "hospital_course": (
            "Patient presented at 39–40 weeks gestation in active labor. Continuous electronic "
            "fetal monitoring maintained throughout labor. Spontaneous vaginal delivery without "
            "complications. Apgar scores 8 and 9. Maternal vital signs stable postpartum. "
            "Mother and newborn in stable condition."
        ),
        "ed_course": None,
        "ed_variants": [],
        "discharge_plan": (
            "Postpartum care initiated. Lactation consultation provided. Postpartum depression "
            "screening scheduled. Follow-up with obstetrician in 2 weeks."
        ),
        "bp_sys_range": (100, 130),
        "bp_dia_range": (60, 85),
        "hr_range": (70, 95),
        "los_days_range": (1, 2),
    },
    {
        "icd_prefixes": ["I50"],
        "chief_complaints": ["shortness of breath and leg swelling", "worsening dyspnea and weight gain"],
        "admission_dx": "Acute decompensated heart failure",
        "hospital_course": (
            "Patient admitted with acute decompensated heart failure. IV diuresis initiated "
            "with furosemide. Strict fluid restriction and daily weights implemented. BNP and "
            "renal function monitored closely. Patient responded well to diuresis over 2–3 days "
            "with improvement in dyspnea and reduction in lower extremity edema. Transitioned to "
            "oral diuretics prior to discharge."
        ),
        "ed_course": (
            "IV furosemide administered in the ED with brisk diuretic response. Dyspnea and "
            "oxygen requirements improved significantly within 2–3 hours. BNP obtained; "
            "chest X-ray showed pulmonary vascular congestion. Vital signs stabilized. "
            "Decision to admit for further IV diuresis and monitoring."
        ),
        "ed_variants": [
            "Acute dyspnea with hypoxia requiring supplemental oxygen. IV furosemide given; "
            "symptoms partially improved. Admitted for continued diuresis and cardiac monitoring.",
            "Presentation with dyspnea and bilateral crackles. BNP markedly elevated. "
            "IV diuretics initiated; patient improved over several hours and admitted for observation.",
        ],
        "discharge_plan": (
            "Continue oral furosemide. Low-sodium diet (<2 g/day). Daily weights at home; "
            "call if weight increases >3 lb in 2 days. Follow-up with cardiology in 1–2 weeks."
        ),
        "bp_sys_range": (140, 180),
        "bp_dia_range": (85, 110),
        "hr_range": (90, 115),
        "los_days_range": (2, 3),
        "scenario_medications": [
            {
                "drug_code": "313988",
                "drug_description": "Furosemide 40 MG Oral Tablet",
                "dose_qty": 40,
                "dose_uom": "mg",
                "frequency_code": "QD",
                "frequency_desc": "Once daily",
                "route_code": "PO",
                "route_desc": "Oral",
                "dosage_form_code": "TAB",
                "dosage_form_desc": "Tablet",
            },
        ],
    },
    {
        "icd_prefixes": ["I48"],
        "chief_complaints": ["palpitations and shortness of breath", "rapid irregular heartbeat"],
        "admission_dx": "Atrial fibrillation with rapid ventricular response",
        "hospital_course": (
            "Patient presented with symptomatic palpitations. ECG confirmed atrial fibrillation "
            "with ventricular response in the 130–140s. IV metoprolol administered with "
            "transition to oral rate control. Anticoagulation continued and confirmed therapeutic. "
            "Continuous cardiac monitoring throughout hospitalization. Ventricular rate controlled "
            "to 70–80 bpm by discharge."
        ),
        "ed_course": (
            "ECG confirmed atrial fibrillation with rapid ventricular response. IV metoprolol "
            "boluses given with rate reduction to 90–100 bpm within 2 hours. Anticoagulation "
            "assessed and initiated. Patient remained hemodynamically stable throughout ED stay. "
            "Admitted for continued rate control and monitoring."
        ),
        "ed_variants": [
            "New-onset palpitations with irregular rhythm on telemetry. Rate-control medication "
            "administered in ED; rhythm and rate improved. Discharged with cardiology follow-up.",
            "AF with moderate ventricular response. Oral rate-control medication adjusted; "
            "patient monitored in ED for 4 hours and discharged in stable condition.",
        ],
        "discharge_plan": (
            "Continue anticoagulation and rate-control medications. "
            "Outpatient cardiology follow-up in 1–2 weeks for rhythm assessment."
        ),
        "bp_sys_range": (105, 145),
        "bp_dia_range": (65, 95),
        "hr_range": (130, 145),
        "los_days_range": (2, 3),
        "scenario_medications": [
            {
                "drug_code": "1599538",
                "drug_description": "Apixaban 5 MG Oral Tablet",
                "dose_qty": 5,
                "dose_uom": "mg",
                "frequency_code": "BID",
                "frequency_desc": "Twice daily",
                "route_code": "PO",
                "route_desc": "Oral",
                "dosage_form_code": "TAB",
                "dosage_form_desc": "Tablet",
            },
        ],
    },
    {
        "icd_prefixes": ["I25", "I20", "I21", "I22"],
        "chief_complaints": ["chest pressure with exertion", "chest pain and diaphoresis"],
        "admission_dx": "Chest pain evaluation, rule out acute coronary syndrome",
        "hospital_course": (
            "Patient admitted for evaluation of chest discomfort. Serial troponins x3 obtained "
            "and remained negative. Continuous telemetry monitoring; no significant ST changes. "
            "Cardiology consulted; pharmacologic stress test performed showing no significant "
            "ischemia. Antiplatelet therapy and statin continued throughout."
        ),
        "ed_course": (
            "Serial troponins drawn and remained negative x2. Continuous telemetry; no ST "
            "changes. Chest X-ray unremarkable. Pain improved with nitroglycerin and aspirin. "
            "Admitted for repeat troponin and further evaluation per cardiology."
        ),
        "ed_variants": [
            "Chest pain with atypical features. Troponin negative, ECG unchanged from baseline. "
            "Symptoms attributed to musculoskeletal etiology. Discharged with NSAIDs and PCP follow-up.",
            "Chest discomfort consistent with GERD vs. musculoskeletal pain. Troponin negative, "
            "ECG normal. GI cocktail given with relief. Discharged with PPI and follow-up.",
            "Chest tightness in setting of exertion. Troponin negative. Stress test performed "
            "showing no ischemia. Discharged with cardiology outpatient follow-up.",
        ],
        "discharge_plan": (
            "Continue aspirin, statin, and beta-blocker. "
            "Optimize medical management. Outpatient cardiology follow-up in 2 weeks."
        ),
        "bp_sys_range": (140, 190),
        "bp_dia_range": (85, 115),
        "hr_range": (85, 110),
        "los_days_range": (2, 4),
    },
    {
        "icd_prefixes": ["I10", "I11", "I12", "I13"],
        "chief_complaints": ["severe headache and elevated blood pressure", "headache with blurred vision"],
        "admission_dx": "Hypertensive urgency",
        "hospital_course": (
            "Patient admitted with hypertensive urgency. Neurologic exam unremarkable. "
            "Renal function and urinalysis obtained with no evidence of end-organ damage. "
            "Oral antihypertensive regimen adjusted and titrated. BP improved gradually to "
            "acceptable range over 24 hours."
        ),
        "ed_course": (
            "Neurological exam intact. Labs obtained; no evidence of acute end-organ damage. "
            "Oral antihypertensive given in ED with BP trending down from peak over 3–4 hours. "
            "Patient monitored and discharged once BP consistently below 160/100."
        ),
        "ed_variants": [
            "Severely elevated BP with headache. Neurologic exam normal. Oral labetalol given; "
            "BP improved over 2 hours. Discharged with medication adjustment and close follow-up.",
            "Hypertensive urgency without end-organ damage. Antihypertensive medication "
            "administered; BP responded. Observed for 4 hours and discharged.",
        ],
        "discharge_plan": (
            "Antihypertensive regimen adjusted. Home BP log requested. Follow-up within 1 week."
        ),
        "bp_sys_range": (180, 230),
        "bp_dia_range": (110, 140),
        "hr_range": (80, 100),
        "los_days_range": (1, 2),
    },
    {
        "icd_prefixes": ["E10", "E11", "E12", "E13"],
        "chief_complaints": ["markedly elevated blood sugar", "nausea, vomiting, and hyperglycemia"],
        "admission_dx": "Hyperglycemic crisis",
        "hospital_course": (
            "Patient admitted with markedly elevated blood glucose. Aggressive IV fluid "
            "hydration and insulin protocol initiated with hourly glucose monitoring. "
            "Electrolytes monitored and repleted. Endocrinology consulted for diabetes "
            "management optimization. Glucose normalized over 24–36 hours."
        ),
        "ed_course": (
            "IV fluids and subcutaneous insulin administered in the ED. Glucose decreased from "
            "critical level to an acceptable range within 4–6 hours of treatment. Anion gap "
            "resolved. Patient tolerating oral fluids. Admitted for continued monitoring and "
            "diabetes management optimization."
        ),
        "ed_variants": [
            "Hyperglycemia without ketoacidosis. IV fluids given; glucose trending down after "
            "3 hours of treatment. Discharged with insulin regimen adjustment and endocrinology follow-up.",
            "Moderate hyperglycemia with nausea. Antiemetics and IV fluids administered; "
            "glucose improved. Oral intake tolerated. Discharged with diabetes medication adjustment.",
        ],
        "discharge_plan": (
            "Diabetes regimen adjusted. Diabetes education reinforced. "
            "Follow-up with PCP in 3–5 days and endocrinology within 2 weeks for A1c recheck."
        ),
        "bp_sys_range": (130, 175),
        "bp_dia_range": (80, 110),
        "hr_range": (95, 115),
        "los_days_range": (2, 3),
        # Specific acute complication code keyed by patient ICD prefix.
        # Forced into every encounter that fires this scenario (overrides generic
        # prefix-only anchor) so DX002 always finds the exact acute code.
        "acute_dx_map": {
            "E10": {"code": "E10.65",
                    "description": "Type 1 diabetes mellitus with hyperglycemia"},
            "E11": {"code": "E11.65",
                    "description": "Type 2 diabetes mellitus with hyperglycemia"},
            "E12": {"code": "E11.65",
                    "description": "Type 2 diabetes mellitus with hyperglycemia"},
            "E13": {"code": "E13.65",
                    "description": "Other specified diabetes mellitus with hyperglycemia"},
        },
    },
    {
        "icd_prefixes": ["J45", "J44"],
        "chief_complaints": ["acute shortness of breath and wheezing", "asthma attack"],
        "admission_dx": "Acute asthma / COPD exacerbation",
        "hospital_course": (
            "Patient admitted for acute respiratory exacerbation. Continuous nebulized "
            "bronchodilators administered. Systemic corticosteroids initiated. O2 saturation "
            "monitored; supplemental oxygen as needed. Respiratory status improved progressively "
            "over 48 hours. Discharged when breathing comfortable on room air."
        ),
        "ed_course": (
            "Nebulized albuterol and ipratropium administered; oral corticosteroids given. "
            "O2 saturation improved from low 90s to mid-90s on room air within 2 hours. "
            "Peak flow and respiratory exam improved. Admitted for continued respiratory monitoring."
        ),
        "ed_variants": [
            "Mild-to-moderate exacerbation treated with nebulizers and oral steroids in the ED. "
            "Symptoms significantly improved after two treatments. Discharged with steroid taper "
            "and rescue inhaler prescription.",
            "Wheezing and dyspnea with decreased air entry. Responded well to back-to-back "
            "albuterol treatments. O2 saturation normalized. Discharged after 4-hour observation.",
        ],
        "discharge_plan": (
            "Continue controller and rescue inhalers. Oral steroid taper. "
            "Follow-up with pulmonology in 2 weeks."
        ),
        "bp_sys_range": (110, 145),
        "bp_dia_range": (70, 90),
        "hr_range": (95, 115),
        "los_days_range": (2, 4),
    },
    {
        "icd_prefixes": ["A15", "B20", "B18", "A16"],
        "chief_complaints": [
            "productive cough with hemoptysis",
            "persistent night sweats and significant weight loss",
            "fever, fatigue, and productive cough for several weeks",
        ],
        "admission_dx": "Active pulmonary tuberculosis",
        "hospital_course": (
            "Patient admitted for initiation of RIPE therapy (rifampin, isoniazid, pyrazinamide, "
            "ethambutol) under respiratory isolation. Sputum AFB smears obtained on admission; "
            "culture pending. Chest imaging revealed upper lobe infiltrates consistent with active "
            "disease. Infectious disease consulted. Patient tolerated therapy without significant "
            "adverse effects. Negative pressure room maintained throughout stay. Discharged after "
            "clinical improvement with directly observed therapy arrangements confirmed."
        ),
        "ed_course": (
            "Patient evaluated for respiratory symptoms. Chest X-ray showed upper lobe infiltrate. "
            "Placed in respiratory isolation. Sputum samples collected. Infectious disease notified. "
            "Admitted for full work-up, culture results, and initiation of anti-tuberculosis therapy."
        ),
        "ed_variants": [
            "Hemoptysis and productive cough noted. CXR performed, results reviewed with radiology. "
            "TB protocol initiated; patient placed in negative-pressure room. Admitted for further "
            "evaluation and culture results.",
        ],
        "discharge_plan": (
            "Directly observed therapy (DOT) arranged through county health department. "
            "Follow-up at TB clinic in 2 weeks. Remain in respiratory isolation at home until "
            "sputum cultures show no growth. Family members referred for TB testing."
        ),
        "bp_sys_range": (100, 130),
        "bp_dia_range": (60, 85),
        "hr_range": (75, 105),
        "los_days_range": (5, 10),
    },
]


# Broadly applicable ED fallback scenarios — used when no ICD prefix matches the patient's
# diagnoses (e.g. Pregnancy, Low Risk cohorts).  Excludes condition-specific scenarios
# (AF-RVR, hyperglycemic crisis, ADHF, HTN urgency) that require matching diagnoses.
_SAFE_ED_FALLBACK_SCENARIOS = [
    s for s in _INPATIENT_SCENARIOS
    if s["icd_prefixes"][0].startswith(("I25", "I20", "J45", "J44"))
]


def _assign_facilities(
    patient_id: int,
    enc_types: list,
    home_fac: dict,
    eligible_facs: list,
    mf_config: dict,
    rng: random.Random,
    ed_to_ip_pairs: dict,
    is_pregnancy_cohort: bool = False,
) -> dict:
    """
    Assign a facility dict to each encounter index.

    Returns {ei: facility_dict} for every encounter.

    Design (spec MF-001 through MF-009):
    - Draw this patient's facility count from the configured distribution.
    - Pre-select alternate facilities using geographic affinity weights so the
      patient has a stable set of organisations rather than random per-encounter
      draws.
    - Assign each encounter to home or an alternate based on
      encounter_type_outside_prob.
    - ED→inpatient pairs are locked to the same facility (MF-008).
    - Pregnancy cohort always stays at home facility.
    """
    n_enc = len(enc_types)

    # --- shortcircuit: multi-facility disabled or pregnancy cohort ---
    if not mf_config.get("enabled", False) or is_pregnancy_cohort:
        return {i: home_fac for i in range(n_enc)}

    # --- step 1: how many distinct facilities does this patient use? ---
    dist = mf_config.get("distribution", {})
    one_pct = dist.get("one_facility_pct", 0.55)
    two_pct = dist.get("two_facility_pct", 0.30)
    max_facs = dist.get("max_facilities", 3)

    r = rng.random()
    if r < one_pct:
        n_target = 1
    elif r < one_pct + two_pct:
        n_target = 2
    else:
        n_target = max_facs

    if n_target == 1:
        return {i: home_fac for i in range(n_enc)}

    # --- step 2: build geo-affinity weights for alternate facilities ---
    geo = mf_config.get("geographic_affinity", {})
    same_w = geo.get("same_region_weight", 0.70)
    adj_w = geo.get("adjacent_region_weight", 0.20)
    other_w = geo.get("other_region_weight", 0.10)
    adjacent_map = mf_config.get("adjacent_regions", {})

    home_region = home_fac.get("region", "")
    home_code = home_fac.get("code", "")
    alt_pool = [f for f in eligible_facs if f.get("code") != home_code]

    if not alt_pool:
        return {i: home_fac for i in range(n_enc)}

    def _geo_weight(f):
        fr = f.get("region", "")
        base = f.get("weight", 0.05)
        if fr == home_region:
            return same_w * base
        if fr in adjacent_map.get(home_region, []):
            return adj_w * base
        return other_w * base

    raw_weights = [_geo_weight(f) for f in alt_pool]
    total_w = sum(raw_weights)
    if total_w == 0:
        return {i: home_fac for i in range(n_enc)}
    norm_weights = [w / total_w for w in raw_weights]

    # Weighted sample without replacement to get the patient's alternate facilities
    n_alts = min(n_target - 1, len(alt_pool))
    alternates: list = []
    pool = list(alt_pool)
    weights = list(norm_weights)
    for _ in range(n_alts):
        if not pool:
            break
        cumsum = 0.0
        pick = rng.random()
        chosen = len(pool) - 1
        for idx, w in enumerate(weights):
            cumsum += w
            if pick <= cumsum:
                chosen = idx
                break
        alternates.append(pool.pop(chosen))
        weights.pop(chosen)
        s = sum(weights)
        if s > 0:
            weights = [w / s for w in weights]

    if not alternates:
        return {i: home_fac for i in range(n_enc)}

    # --- step 3: assign per encounter ---
    outside_prob = mf_config.get("encounter_type_outside_prob", {})
    # ed_ip_same_facility_lock forces injected IP encounters to share their ED's facility
    ed_ip_lock = mf_config.get("ed_ip_same_facility_lock", True)

    # ip_locked maps new_ip_ei → facility, populated when we assign the triggering ED
    ip_locked: dict = {}
    enc_facs: dict = {}

    for ei, etype in enumerate(enc_types):
        if ed_ip_lock and ei in ip_locked:
            enc_facs[ei] = ip_locked[ei]
            continue

        p_outside = outside_prob.get(etype, 0.0)
        if rng.random() < p_outside:
            fac = rng.choice(alternates)
        else:
            fac = home_fac

        enc_facs[ei] = fac

        # Lock the paired inpatient to this same facility (MF-008)
        if ed_ip_lock and etype == "E" and ei in ed_to_ip_pairs:
            ip_locked[ed_to_ip_pairs[ei]] = fac

    return enc_facs


def _facility_mrn(patient_id: int, facility_code: str, use_prefix: bool = True) -> str:
    """
    Derive a deterministic, facility-specific MRN for a patient.

    Properties guaranteed by design (spec MF-004):
      same patient_id + same facility_code  → identical MRN every run
      same patient_id + different facility  → different MRN
      different patient_id + same facility  → different MRN (collision prob ~1/9M)

    When use_prefix is True the facility code is prepended, making cross-facility
    MRN collision structurally impossible regardless of the numeric component.

    Implementation: SHA-256 of "{patient_id}:{facility_code}" → take the first
    7 hex digits → map to the decimal range [1 000 000, 9 999 999].
    No RNG is involved so there is nothing to seed or replay.
    """
    import hashlib
    digest = hashlib.sha256(
        f"{patient_id}:{facility_code}".encode()
    ).hexdigest()
    # 7 hex chars = 28 bits → 0..268,435,455; map to 7-digit decimal range
    numeric = int(digest[:7], 16) % 9_000_000 + 1_000_000
    if use_prefix:
        return f"{facility_code}-{numeric}"
    return str(numeric)


def _plan_pregnancy_episodes(rng, enc_dates):
    """
    Plan biologically plausible pregnancy episodes for a pregnancy-cohort patient.
    A full-term delivery requires ~39 weeks of gestation preceding it.
    The minimum gap between deliveries is 280 days (one full term).
    Returns a list of episode dicts (may be empty if history window is too short).
    """
    if not enc_dates:
        return []

    history_start = enc_dates[0].date()
    history_end = enc_dates[-1].date()
    gestation = rng.randint(273, 280)

    earliest_del = history_start + timedelta(days=gestation + 14)
    latest_del = history_end - timedelta(days=7)

    if earliest_del > latest_del:
        return []

    del_range = (latest_del - earliest_del).days
    del_1 = earliest_del + timedelta(days=rng.randint(0, del_range))
    ep1 = {
        "episode_num": 1,
        "conception_date": del_1 - timedelta(days=gestation),
        "prenatal_start": del_1 - timedelta(days=gestation - 56),
        "delivery_date": del_1,
        "postpartum_end": del_1 + timedelta(days=42),
    }
    episodes = [ep1]

    # A second pregnancy is biologically possible only after the postpartum period
    # plus adequate recovery; we allow it only when there is room AND with low probability.
    earliest_del_2 = ep1["postpartum_end"] + timedelta(days=180 + gestation)
    if earliest_del_2 < latest_del and rng.random() < 0.12:
        del_2_range = (latest_del - earliest_del_2).days
        del_2 = earliest_del_2 + timedelta(days=rng.randint(0, max(0, del_2_range)))
        episodes.append({
            "episode_num": 2,
            "conception_date": del_2 - timedelta(days=gestation),
            "prenatal_start": del_2 - timedelta(days=gestation - 56),
            "delivery_date": del_2,
            "postpartum_end": del_2 + timedelta(days=42),
        })

    return episodes

_HTN_REGIMENS = [
    {"label": "ACE", "drug": "lisinopril",        "doses": [10, 20, 40], "freq": "QD",
     "rxnorm": {10: ("1049630", "Lisinopril 10 MG Oral Tablet"),
                20: ("1049631", "Lisinopril 20 MG Oral Tablet"),
                40: ("563611",  "Lisinopril 40 MG Oral Tablet")}},
    {"label": "ARB", "drug": "losartan",           "doses": [25, 50, 100], "freq": "QD",
     "rxnorm": {25:  ("979480", "Losartan Potassium 25 MG Oral Tablet"),
                50:  ("979482", "Losartan Potassium 50 MG Oral Tablet"),
                100: ("979485", "Losartan Potassium 100 MG Oral Tablet")}},
    {"label": "CCB", "drug": "amlodipine",         "doses": [5, 10, 10],  "freq": "QD",
     "rxnorm": {5:  ("329526", "Amlodipine 5 MG Oral Tablet"),
                10: ("858810", "Amlodipine 10 MG Oral Tablet")}},
    {"label": "THIA", "drug": "hydrochlorothiazide", "doses": [12, 25, 25], "freq": "QD",
     "rxnorm": {12: ("310798", "Hydrochlorothiazide 12.5 MG Oral Tablet"),
                25: ("310797", "Hydrochlorothiazide 25 MG Oral Tablet")}},
]


def _build_lab_xml(lab, enc_num, enc_date, patient_id, prov_code, prov_name,
                   fac_code, fac_name, lab_idx, rng, a1c_override=None,
                   enc_start_time=None):
    result_dt = enc_date + timedelta(days=rng.randint(1, 3))
    result_ts = _ts(result_dt.replace(hour=14, minute=30, second=0))
    _draw_base = enc_start_time if enc_start_time else enc_date.replace(hour=9, minute=0, second=0)
    enc_ts = _ts(_draw_base + timedelta(minutes=rng.randint(5, 20)))
    entered_on = _ts(enc_date.replace(hour=0, minute=0, second=0))
    result_items_xml = ""
    _result_vals = []
    for ri_idx, ri in enumerate(lab.get("result_items", []), 1):
        if ri.get("code") == "4548-4" and a1c_override is not None:
            val = f"{a1c_override:.1f}"
            is_abn = a1c_override >= 5.7
            interp = "H" if a1c_override >= 5.7 else "N"
        else:
            is_abn = rng.random() < float(lab.get("abnormal_pct", 0.3))
            val = _result_value(ri, is_abn, rng)
            interp = _derive_interp(val, ri)
        _result_vals.append({"code": ri.get("code", ""), "value": val, "interp": interp, "is_abn": is_abn})
        norm_range = ri.get(
            "normal_range_text",
            f"{ri.get('normal_min','')}-{ri.get('normal_max','')}"
        )
        norm_range = (norm_range.replace("&", "&amp;")
                      .replace("<", "&lt;").replace(">", "&gt;"))
        is_numeric_str = "true" if ri.get("is_numeric", True) else "false"
        result_items_xml += (
            f"          <LabResultItem>\n"
            f"            <EnteredOn>{result_ts}</EnteredOn>\n"
            f"            <TestItemCode>\n"
            f"              <SDACodingStandard>LN</SDACodingStandard>\n"
            f"              <Code>{ri['code']}</Code>\n"
            f"              <Description>{ri['description']}</Description>\n"
            f"              <IsNumeric>{is_numeric_str}</IsNumeric>\n"
            f"            </TestItemCode>\n"
            f"            <ResultValue>{val}</ResultValue>\n"
            f"            <ResultValueUnits>{ri.get('units','')}</ResultValueUnits>\n"
            f"            <ResultNormalRange>{norm_range}</ResultNormalRange>\n"
            f"            <ResultInterpretation>{interp}</ResultInterpretation>\n"
            f"            <ExternalId>LabResultItem_{lab_idx}_{ri_idx}</ExternalId>\n"
            f"          </LabResultItem>\n"
        )
    xml_str = (
        f"    <LabOrder>\n"
        f"      <PlacerId>LAB-{enc_date.strftime('%Y%m%d')}-{patient_id:04d}-{lab_idx}</PlacerId>\n"
        f"      <OrderItem>\n"
        f"        <SDACodingStandard>LN</SDACodingStandard>\n"
        f"        <Code>{lab['order_code']}</Code>\n"
        f"        <Description>{lab['order_description']}</Description>\n"
        f"      </OrderItem>\n"
        f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
        f"      <EnteringOrganization><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteringOrganization>\n"
        f"      <Specimen>{lab.get('specimen','Blood')}</Specimen>\n"
        f"      <SpecimenCollectedTime>{enc_ts}</SpecimenCollectedTime>\n"
        f"      <Priority><Code>R</Code><Description>Routine</Description></Priority>\n"
        f"      <ActionCode>A</ActionCode>\n"
        f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
        f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
        f"      <EnteredOn>{entered_on}</EnteredOn>\n"
        f"      <FromTime>{enc_ts}</FromTime>\n"
        f"      <ToTime>{enc_ts}</ToTime>\n"
        f"      <Result>\n"
        f"        <ResultType>AT</ResultType>\n"
        f"        <ResultItems>\n"
        + result_items_xml +
        f"        </ResultItems>\n"
        f"        <ResultTime>{result_ts}</ResultTime>\n"
        f"        <ResultStatus>F</ResultStatus>\n"
        f"        <ExternalId>Result_{lab_idx}</ExternalId>\n"
        f"      </Result>\n"
        f"      <ExternalId>LabOrder_{lab_idx}</ExternalId>\n"
        f"      <EncounterNumber>{enc_num}</EncounterNumber>\n"
        f"    </LabOrder>\n"
    )
    return xml_str, _result_vals


def generate_from_template(patient_id: int, tmpl: dict) -> str:
    rng = random.Random(patient_id)

    meta = tmpl.get("meta", {})
    history_months = int(meta.get("history_months", 24))
    state_code = meta.get("state_code", "OH")

    # ---- Demographics ----
    geo = _wpick(tmpl["geography"]["locations"], "weight", rng)
    dem = tmpl["demographics"]
    age_bucket = _wpick(dem["age_distribution"], "weight", rng)
    age = rng.randint(int(age_bucket["min"]), int(age_bucket["max"]))
    sex = _wpick(dem["sex_distribution"], "weight", rng)["sex"]
    race_row = _wpick(dem["race_distribution"], "weight", rng)
    ins_row = _wpick(dem["insurance_distribution"], "weight", rng)

    ins_plans = {p["code"]: p for p in tmpl.get("insurance_plans", [])}
    ins_plan = ins_plans.get(ins_row.get("plan_code", ""), {})

    # ---- Cohort ----
    cohorts = [c for c in tmpl.get("cohorts", []) if c.get("weight", 0) > 0]
    cohort = _wpick(cohorts, "weight", rng)
    age = max(int(cohort.get("min_age", 18)), min(int(cohort.get("max_age", 85)), age))
    age = min(age, 105)  # hard biological cap — no survivable age beyond 105

    # Compute birth_dt HERE — before enc_dates — so every date in the record can
    # be clamped to >= DOB.  Moving these two rng calls early shifts the sequence
    # but that is intentional; correctness > backward compatibility.
    birth_year = _REFERENCE_DATE.year - age
    birth_dt = datetime(birth_year, rng.randint(1, 12), rng.randint(1, 28))

    if cohort.get("sex_bias") == "F":
        sex = "F"
    elif cohort.get("sex_bias") == "M":
        sex = "M"

    # Select cohort-appropriate symptom-denial phrase pool for note variation.
    _cn = cohort.get("name", "").lower()
    if any(k in _cn for k in ("cardiovascular", "cardiac", "heart")):
        _denial_pool = _PHRASE_POOLS["symptom_denial_cardiovascular"]
    elif any(k in _cn for k in ("diabetes", "type 2")):
        _denial_pool = _PHRASE_POOLS["symptom_denial_diabetes"]
    elif "hypertension" in _cn:
        _denial_pool = _PHRASE_POOLS["symptom_denial_hypertension"]
    elif "asthma" in _cn:
        _denial_pool = _PHRASE_POOLS["symptom_denial_asthma"]
    elif any(k in _cn for k in ("pregnancy", "prenatal", "obstetric")):
        _denial_pool = _PHRASE_POOLS["symptom_denial_pregnancy"]
    elif any(k in _cn for k in ("reportable", "infectious", "tb", "hiv")):
        _denial_pool = _PHRASE_POOLS["symptom_denial_reportable"]
    else:
        _denial_pool = _PHRASE_POOLS["symptom_denial_default"]

    # ---- Names ----
    np_ = tmpl.get("name_pools", {})
    rlabel = _race_label(race_row.get("race_description", ""))
    first_pool = np_.get("male_first" if sex == "M" else "female_first", {})
    last_pool = np_.get("last_names", {})
    initials = np_.get("middle_initials", ["A", "B", "C"])
    given = rng.choice(first_pool.get(rlabel) or first_pool.get("Other") or ["Alex"])
    family = rng.choice(last_pool.get(rlabel) or last_pool.get("Other") or ["Smith"])
    middle = rng.choice(initials)
    sex_desc = "Male" if sex == "M" else "Female"

    # ---- Facility & Provider ----
    # Prioritize: (1) same county, (2) same region, (3) any age-eligible facility.
    # This prevents a patient in Dayton from being routinely assigned to Cleveland Clinic.
    facilities = tmpl.get("facilities", [])
    age_eligible_facs = [f for f in facilities if f.get("max_patient_age", 999) >= age]
    county_facs = [f for f in age_eligible_facs if f.get("county_fips") == geo.get("county_fips")]
    _pat_region = geo.get("region", "")
    region_facs = [f for f in age_eligible_facs if f.get("region") == _pat_region]
    fac = _wpick(county_facs or region_facs or age_eligible_facs or facilities, "weight", rng)
    provs = [p for p in tmpl.get("providers", []) if p.get("facility_code") == fac["code"]]
    prov = rng.choice(provs) if provs else rng.choice(
        tmpl.get("providers", [{"code": "DR001", "name": "Dr. Smith"}])
    )
    fac_code = fac["code"]
    fac_name = fac["name"]
    prov_code = prov["code"]
    prov_name = prov["name"]

    # ---- Encounter schedule from template encounter_pattern ----
    ep = cohort.get("encounter_pattern", {})
    enc_per_year = float(ep.get("encounters_per_year", 3.0))
    enc_type_weights = ep.get("encounter_type_weights", {"O": 1.0})
    lab_rate = float(ep.get("lab_encounter_rate", 0.6))
    rad_rate = float(ep.get("rad_encounter_rate", 0.2))

    # Clamp history window to the patient's actual lifespan so no encounter
    # can precede DOB.  For young patients this reduces both the window and the
    # expected encounter count proportionally.
    _days_since_birth = max(30, (_REFERENCE_DATE - birth_dt).days)
    history_days = min(history_months * 30, _days_since_birth)
    _effective_history_months = history_days / 30.0

    _enc_mean = max(1.0, enc_per_year * _effective_history_months / 12)
    # Poisson-approximate: Gaussian with SD = sqrt(mean) so low-utilizers stay low and
    # high-utilizers spread naturally; clamp to avoid degenerate single-encounter patients.
    n_encounters = max(2, round(rng.gauss(_enc_mean, max(1.0, _enc_mean ** 0.5))))
    n_encounters = min(n_encounters, 20)  # cap for file size

    # Space encounters roughly evenly with jitter; sort chronologically
    interval = history_days // n_encounters
    enc_dates = sorted(
        _REFERENCE_DATE - timedelta(
            days=history_days - i * interval - rng.randint(0, max(1, interval // 2))
        )
        for i in range(n_encounters)
    )
    # Ensure the most recent encounter is within the last 4 months
    if (_REFERENCE_DATE - enc_dates[-1]).days > 120:
        enc_dates[-1] = _REFERENCE_DATE - timedelta(days=rng.randint(14, 90))
        enc_dates = sorted(enc_dates)

    # Shift any outpatient encounter that falls on a major US holiday or Sunday forward 1-3 days
    _HOLIDAYS = {(1, 1), (7, 4), (11, 25), (12, 25), (12, 26)}

    def _is_holiday(dt):
        return (dt.month, dt.day) in _HOLIDAYS or dt.weekday() == 6  # also exclude Sundays

    enc_dates = sorted(
        d + timedelta(days=rng.randint(1, 3)) if _is_holiday(d) else d
        for d in enc_dates
    )
    # Final safety clamp: ensure no encounter precedes DOB even after holiday shifts.
    _day_after_birth = birth_dt + timedelta(days=1)
    enc_dates = [max(d, _day_after_birth) for d in enc_dates]
    # Deduplicate (multiple clamped dates may collapse to the same day) and re-sort.
    _seen_enc = set()
    _deduped = []
    for _d in sorted(enc_dates):
        _key = _d.date()
        if _key not in _seen_enc:
            _seen_enc.add(_key)
            _deduped.append(_d)
    # Pad back to n_encounters if deduplication shrank the list.
    while len(_deduped) < min(2, n_encounters):
        _deduped.append(_deduped[-1] + timedelta(days=rng.randint(14, 60)))
        _seen_enc.add(_deduped[-1].date())
    enc_dates = _deduped
    n_encounters = len(enc_dates)

    def _fu_text(gap_days):
        if gap_days is None or gap_days > 200:
            return "Return as needed or within 6 months."
        elif gap_days <= 21:
            return "Return in 2 weeks for blood-pressure recheck."
        elif gap_days <= 45:
            return "Return in 4 weeks for blood-pressure check."
        elif gap_days <= 75:
            return "Return in 6 weeks; sooner if BP not improving."
        elif gap_days <= 105:
            return "Return in 3 months."
        else:
            return "Return in 6 months."

    # ---- Pregnancy cohort: pre-plan delivery episodes ----
    # Must happen before enc_types so we can force delivery encounters to inpatient.
    _is_pregnancy_cohort = any(
        d.get("code", "").startswith(("O8", "Z34"))
        for d in cohort.get("diagnoses", [])
    )
    _pregnancy_episodes: list = []
    _preg_delivery_enc_indices: set = set()
    if _is_pregnancy_cohort:
        _pregnancy_episodes = _plan_pregnancy_episodes(rng, enc_dates)
        for _pep in _pregnancy_episodes:
            _dlv = _pep["delivery_date"]
            _closest_ei = min(
                range(len(enc_dates)),
                key=lambda _i, _d=_dlv: abs((enc_dates[_i].date() - _d).days),
            )
            _pep["delivery_enc_ei"] = _closest_ei
            # Snap delivery_date to the actual encounter date so OB001's 4-day window
            # always passes (planned date may be several days from nearest encounter).
            _pep["delivery_date"] = enc_dates[_closest_ei].date()
            _preg_delivery_enc_indices.add(_closest_ei)

    # Encounter type per visit (O=outpatient, E=emergency, I=inpatient)
    et_keys = list(enc_type_weights.keys())
    et_vals = [float(enc_type_weights[k]) for k in et_keys]
    enc_types = rng.choices(et_keys, weights=et_vals, k=n_encounters)
    # Force pregnancy delivery encounters to inpatient, and convert all OTHER randomly-assigned
    # inpatient encounters back to outpatient — the only inpatient event in a pregnancy episode
    # should be the delivery itself (arbitrary inpatient encounters would pick up the delivery
    # scenario and trigger OB001).
    if _is_pregnancy_cohort:
        enc_types = [
            "O" if (_et == "I" and _ei not in _preg_delivery_enc_indices) else _et
            for _ei, _et in enumerate(enc_types)
        ]
    for _dlv_ei in _preg_delivery_enc_indices:
        enc_types[_dlv_ei] = "I"

    # Per-encounter pregnancy context used to select trimester-aware codes.
    _pregnancy_enc_context: dict = {}
    if _is_pregnancy_cohort:
        for _pei, _ped in enumerate(enc_dates):
            _ctx: dict = {"type": "inter_pregnancy"}
            for _pep in _pregnancy_episodes:
                if _pei == _pep.get("delivery_enc_ei"):
                    _ctx = {"type": "delivery", "episode": _pep}
                    break
                elif _pep["prenatal_start"] <= _ped.date() <= _pep["delivery_date"]:
                    _wks = (_ped.date() - _pep["conception_date"]).days // 7
                    _tri = 1 if _wks < 14 else (2 if _wks < 27 else 3)
                    _ctx = {"type": "prenatal", "episode": _pep,
                            "trimester": _tri, "weeks": _wks}
                    break
                elif _pep["delivery_date"] < _ped.date() <= _pep["postpartum_end"]:
                    _ctx = {"type": "postpartum", "episode": _pep}
                    break
            _pregnancy_enc_context[_pei] = _ctx

    # Build diag_list here (before scenario pre-selection) so that _pt_diag_codes only
    # contains diagnoses the patient actually has — comorbidities gated by prevalence_pct.
    # Diagnoses and comorbidities are also filtered by min_age/max_age/sex so that
    # adult-onset conditions (e.g. E11.9, I10) are never assigned to pediatric patients.
    def _dx_age_sex_ok(d: dict) -> bool:
        if age < int(d.get("min_age", 0)):
            return False
        if age > int(d.get("max_age", 999)):
            return False
        _req_sex = d.get("sex")
        if _req_sex and _req_sex != sex:
            return False
        return True

    diag_list = [d for d in cohort.get("diagnoses", []) if _dx_age_sex_ok(d)]
    for _comorbidity in cohort.get("comorbidities", []):
        if rng.random() < float(_comorbidity.get("prevalence_pct", 0)):
            if _dx_age_sex_ok(_comorbidity):
                diag_list.append({
                    "code": _comorbidity["code"],
                    "description": _comorbidity["description"],
                    "is_primary": False,
                })
    # Ensure diabetes diagnosis is present when cohort monitors HbA1c.
    _HBA1C_CODE = "4548-4"
    _cohort_has_a1c = any(
        any(ri.get("code") == _HBA1C_CODE for ri in lab.get("result_items", []))
        for lab in cohort.get("labs", [])
    )
    _has_dm_diag_early = any(d.get("code", "").startswith(("E10", "E11", "E12", "E13")) for d in diag_list)

    # Pre-select inpatient and ED scenarios so narrative and structured vitals are
    # consistent, and so the diagnoses loop can guarantee anchor codes appear on
    # scenario-carrying encounters (DX002 prevention).
    _pt_diag_codes = [d.get("code", "") for d in diag_list]
    _inpatient_scenes: dict = {}       # ei (0-based) -> selected scenario dict
    _inpatient_scen_matched: dict = {} # ei -> True if selected via patient-matching ICD prefix
    _ed_scenes: dict = {}              # ei (0-based) -> pre-selected scenario dict for ED encounters
    _ed_scen_matched: dict = {}        # ei -> True if selected via patient-matching ICD prefix
    for _ei, _et in enumerate(enc_types):
        if _et == "I":
            _ip_matching = [
                s for s in _INPATIENT_SCENARIOS
                if any(c.startswith(tuple(s["icd_prefixes"])) for c in _pt_diag_codes)
            ]
            if _ip_matching:
                _inpatient_scenes[_ei] = rng.choice(_ip_matching)
                _inpatient_scen_matched[_ei] = True
            else:
                _ip_fallback = _SAFE_ED_FALLBACK_SCENARIOS or _INPATIENT_SCENARIOS
                _inpatient_scenes[_ei] = rng.choice(_ip_fallback)
                _inpatient_scen_matched[_ei] = False
        elif _et == "E":
            _ed_m = [s for s in _INPATIENT_SCENARIOS
                     if any(c.startswith(tuple(s["icd_prefixes"])) for c in _pt_diag_codes)]
            # Delivery is a planned inpatient-only event; exclude it from the ED pool so
            # pregnancy patients don't get spurious "Normal term delivery" ED encounters.
            if _is_pregnancy_cohort:
                _ed_m = [s for s in _ed_m if s.get("ed_course") is not None]
            if _ed_m:
                _ed_scenes[_ei] = rng.choice(_ed_m)
                _ed_scen_matched[_ei] = True
            else:
                _ed_fallback = _SAFE_ED_FALLBACK_SCENARIOS or _INPATIENT_SCENARIOS
                _ed_scenes[_ei] = rng.choice(_ed_fallback)
                _ed_scen_matched[_ei] = False
    # ---- Finalize schedule: inject paired inpatient encounters for admitting EDs ----
    # Pre-determine which ED encounters will admit by mirroring the document loop's
    # variant-rotation logic, so the diagnosis loop can allocate the anchor code.
    _pre_ed_key_counts: dict = {}
    _ed_will_admit: dict = {}
    for _ei in sorted(_ed_scenes.keys()):
        _scen = _ed_scenes[_ei]
        _key = _scen["icd_prefixes"][0]
        _prior = _pre_ed_key_counts.get(_key, 0)
        _vrnts = _scen.get("ed_variants", [])
        if _prior == 0 or not _vrnts:
            _chk = _scen.get("ed_course") or _scen.get("hospital_course") or ""
        else:
            _chk = _vrnts[(_prior - 1) % len(_vrnts)]
        _ed_will_admit[_ei] = "dmit" in _chk
        _pre_ed_key_counts[_key] = _prior + 1

    _old_to_new_ei: dict = {}
    _new_ip_for_ed: dict = {}
    _new_enc_types: list = []
    _new_enc_dates: list = []
    for _old_ei, (_et, _ed_dt) in enumerate(zip(enc_types, enc_dates)):
        _new_ei = len(_new_enc_types)
        _old_to_new_ei[_old_ei] = _new_ei
        _new_enc_types.append(_et)
        _new_enc_dates.append(_ed_dt)
        if _et == "E" and _ed_will_admit.get(_old_ei):
            _new_ip_ei = len(_new_enc_types)
            _new_ip_for_ed[_old_ei] = _new_ip_ei
            _new_enc_types.append("I")
            _new_enc_dates.append(_ed_dt + timedelta(days=1))
    enc_types = _new_enc_types
    enc_dates = _new_enc_dates
    n_encounters = len(enc_types)

    # Remap all pre-selected scene dicts to new encounter indices.
    _inpatient_scenes = {_old_to_new_ei[k]: v for k, v in _inpatient_scenes.items()}
    _inpatient_scen_matched = {_old_to_new_ei[k]: v for k, v in _inpatient_scen_matched.items()}
    _ed_scenes = {_old_to_new_ei[k]: v for k, v in _ed_scenes.items()}
    _ed_scen_matched = {_old_to_new_ei[k]: v for k, v in _ed_scen_matched.items()}
    # Injected IP inherits its paired ED's scenario.
    for _old_ed_ei, _new_ip_ei in _new_ip_for_ed.items():
        _inpatient_scenes[_new_ip_ei] = _ed_scenes[_old_to_new_ei[_old_ed_ei]]

    # Remap pregnancy context and delivery indices to new encounter positions.
    if _is_pregnancy_cohort and _pregnancy_enc_context:
        _pregnancy_enc_context = {
            _old_to_new_ei[k]: v for k, v in _pregnancy_enc_context.items()
        }
        for _pep in _pregnancy_episodes:
            if "delivery_enc_ei" in _pep:
                _pep["delivery_enc_ei"] = _old_to_new_ei[_pep["delivery_enc_ei"]]

    # Rebuild encounter gaps for follow-up text (must be after injection).
    _next_enc_gaps = [
        (enc_dates[_gi + 1] - enc_dates[_gi]).days
        if _gi + 1 < len(enc_dates) else None
        for _gi in range(len(enc_dates))
    ]

    # Vitals built here (after injection) so n_encounters is final.
    obs_list_for_vitals = cohort.get("observations", [])
    enc_vitals, _patient_height_in = _build_enc_vitals(
        obs_list_for_vitals, n_encounters, rng, sex=sex, age=age
    )

    # Override enc_vitals BP and HR for inpatient encounters to match selected scenario ranges.
    for _ei, _scen in _inpatient_scenes.items():
        _sys_lo, _sys_hi = _scen.get("bp_sys_range", (120, 160))
        _dia_lo, _dia_hi = _scen.get("bp_dia_range", (75, 100))
        _hr_lo, _hr_hi = _scen.get("hr_range", (58, 98))
        _new_sys = round(rng.uniform(_sys_lo, _sys_hi))
        _new_dia = round(rng.uniform(_dia_lo, _dia_hi))
        _new_hr = round(rng.uniform(_hr_lo, _hr_hi))
        enc_vitals[_ei] = {
            **enc_vitals[_ei], "bp_sys": _new_sys, "bp_dia": _new_dia, "hr": _new_hr
        }

    # Pre-select which encounters get labs AND which panels are drawn, so note text and
    # lab records reference exactly the same encounter/value pairs.
    # _lab_enc_plans[ei] = list of panel dicts to draw at that encounter.
    _lab_list = cohort.get("labs", [])
    _lab_enc_plans: dict = {}  # ei -> list[panel dict]

    def _lab_panels_with_code(code):
        return [l for l in _lab_list if any(ri.get("code") == code for ri in l.get("result_items", []))]

    def _lab_panels_by_desc(*keywords):
        return [l for l in _lab_list
                if any(kw.lower() in l.get("order_description", "").lower() for kw in keywords)]

    def _add_panel(ei, panel):
        if panel is None:
            return
        _lab_enc_plans.setdefault(ei, [])
        desc = panel.get("order_description", "")
        if not any(p.get("order_description", "") == desc for p in _lab_enc_plans[ei]):
            _lab_enc_plans[ei].append(panel)

    if _lab_list:
        # Detect this patient's actual disease state from diag_list (not template)
        _has_dm_labs = any(d.get("code", "").startswith(("E10", "E11", "E12", "E13")) for d in diag_list)
        _has_htn_labs = any(d.get("code", "").startswith(("I10", "I11", "I12", "I13")) for d in diag_list)
        _has_cvd_labs = any(d.get("code", "").startswith(("I25", "I20", "I21", "I22", "I50")) for d in diag_list)
        _is_preventive_cohort = cohort.get("name", "") == "Low Risk Preventive"

        # For T2DM: triple the A1c panel weight so the diabetes story reliably surfaces
        _cohort_has_t2dm = _has_dm_labs
        _effective_lab_list = [
            {**lab, "weight": lab.get("weight", 1.0) * (
                3.0 if (_cohort_has_t2dm and
                        any(ri.get("code") == "4548-4" for ri in lab.get("result_items", [])))
                else 1.0
            )}
            for lab in _lab_list
        ] if _cohort_has_t2dm else _lab_list

        # Step 1: base random panel assignment (one per encounter at lab_rate)
        for _li in range(n_encounters):
            if _li == n_encounters - 1 or rng.random() <= lab_rate:
                _lab_enc_plans.setdefault(_li, [])
                _lab_enc_plans[_li].append(_wpick(_effective_lab_list, "weight", rng))

        # Step 2: disease-specific density scheduling
        _outpt_idxs = [i for i, t in enumerate(enc_types) if t == "O"]
        _inpt_idxs  = [i for i, t in enumerate(enc_types) if t == "I"]
        _ed_idxs    = [i for i, t in enumerate(enc_types) if t == "E"]

        _a1c_panels   = _lab_panels_with_code("4548-4")
        _bmp_panels   = _lab_panels_by_desc("basic metabolic", "metabolic panel")
        _cmp_panels   = _lab_panels_by_desc("comprehensive metabolic")
        _lipid_panels = _lab_panels_by_desc("lipid")
        _uacr_panels  = _lab_panels_by_desc("albumin/creatinine", "urine albumin")
        _cbc_panels   = _lab_panels_by_desc("cbc", "blood count")
        _bnp_panels   = _lab_panels_by_desc("bnp", "natriuretic")
        _trop_panels  = _lab_panels_by_desc("troponin")
        _egfr_panels  = _lab_panels_by_desc("glomerular filtration", "egfr")

        _bmp_or_cmp   = _bmp_panels or _cmp_panels
        _a1c_panel_1  = _a1c_panels[0] if _a1c_panels else None
        _bmp_panel_1  = (_bmp_panels or _cmp_panels or [None])[0]
        _lipid_panel_1 = (_lipid_panels or [None])[0]
        _uacr_panel_1  = (_uacr_panels or [None])[0]
        _cbc_panel_1   = (_cbc_panels or [None])[0]
        _egfr_panel_1  = (_egfr_panels or [None])[0]
        _bnp_panel_1   = (_bnp_panels or [None])[0]
        _trop_panel_1  = (_trop_panels or [None])[0]

        if _has_dm_labs:
            # A1c every 3rd outpatient encounter (90-day cadence)
            for _oi, _ei in enumerate(_outpt_idxs):
                if _oi % 3 == 0 and _a1c_panel_1:
                    _add_panel(_ei, _a1c_panel_1)
            # BMP at every outpatient encounter
            for _ei in _outpt_idxs:
                _add_panel(_ei, _bmp_panel_1)
            # Lipid panel first outpatient + every 4th (ADA annual monitoring)
            for _oi, _ei in enumerate(_outpt_idxs):
                if _oi % 4 == 0:
                    _add_panel(_ei, _lipid_panel_1)
            # uACR every 4th outpatient (annual renal screening)
            for _oi, _ei in enumerate(_outpt_idxs):
                if _oi % 4 == 0:
                    _add_panel(_ei, _uacr_panel_1)
            # eGFR at inpatient encounters
            for _ei in _inpt_idxs:
                _add_panel(_ei, _egfr_panel_1 or _bmp_panel_1)

        if _has_htn_labs and not _has_dm_labs:
            # BMP every 2nd outpatient
            for _oi, _ei in enumerate(_outpt_idxs):
                if _oi % 2 == 0:
                    _add_panel(_ei, _bmp_panel_1)
            # Lipid panel first outpatient + every 4th after
            for _oi, _ei in enumerate(_outpt_idxs):
                if _oi % 4 == 0:
                    _add_panel(_ei, _lipid_panel_1)

        if _has_cvd_labs:
            # Lipid panel first outpatient + every 4th
            for _oi, _ei in enumerate(_outpt_idxs):
                if _oi % 4 == 0:
                    _add_panel(_ei, _lipid_panel_1)
            # BNP at inpatient encounters (HF monitoring)
            for _ei in _inpt_idxs:
                _add_panel(_ei, _bnp_panel_1 or _bmp_panel_1)
            # Troponin at ED encounters
            for _ei in _ed_idxs:
                _add_panel(_ei, _trop_panel_1)

        if _is_preventive_cohort and _outpt_idxs:
            # Annual physical bundle at first outpatient encounter
            _first_op = _outpt_idxs[0]
            _add_panel(_first_op, _cbc_panel_1)
            _add_panel(_first_op, _bmp_panel_1 or (_cmp_panels or [None])[0])
            _add_panel(_first_op, _lipid_panel_1)

    # Encounters where any panel contains an A1c result item
    _a1c_enc_set: set = {
        _li for _li, _plist in _lab_enc_plans.items()
        if any(
            any(ri.get("code") == "4548-4" for ri in _p.get("result_items", []))
            for _p in _plist
        )
    }

    # ---- Multi-facility: assign encounters to facilities ----
    _mf_config = tmpl.get("multi_facility", {})
    _mf_prefix = _mf_config.get("facility_mrn_prefix", True)
    _ed_to_ip_pairs: dict = {}
    for _ii in range(len(enc_types) - 1):
        if enc_types[_ii] == "E" and enc_types[_ii + 1] == "I":
            _ed_to_ip_pairs[_ii] = _ii + 1
    _enc_facility_map: dict = _assign_facilities(
        patient_id=patient_id,
        enc_types=enc_types,
        home_fac=fac,
        eligible_facs=age_eligible_facs,
        mf_config=_mf_config,
        rng=rng,
        ed_to_ip_pairs=_ed_to_ip_pairs,
        is_pregnancy_cohort=_is_pregnancy_cohort,
    )

    # Per-facility provider: select one provider per facility from that facility's pool,
    # seeded by the same per-patient RNG so it's deterministic. Home facility uses
    # the provider already selected above (no second RNG call → single-facility patients
    # are completely unaffected by this block).
    _fac_prov_map: dict = {fac_code: (prov_code, prov_name)}  # home pre-populated
    _tmpl_providers = tmpl.get("providers", [])
    for _oi in range(len(enc_types)):
        _ofc = _enc_facility_map[_oi]["code"]
        if _ofc not in _fac_prov_map:
            _ep_pool = [p for p in _tmpl_providers if p.get("facility_code") == _ofc]
            if _ep_pool:
                _ep = rng.choice(_ep_pool)
                _fac_prov_map[_ofc] = (_ep["code"], _ep["name"])
            else:
                _fac_prov_map[_ofc] = (prov_code, prov_name)

    # ---- Static patient fields ----
    # birth_dt was computed before enc_dates (above); no recalculation needed here.
    _home_fac_code = fac_code
    _home_fac_name = fac_name
    _home_prov_code = prov_code
    _home_prov_name = prov_name
    mrn = _facility_mrn(patient_id, _home_fac_code, use_prefix=_mf_prefix)
    county_name = geo.get("county", "County")
    county_fips = geo.get("county_fips", "39049")
    city_entry = rng.choice(geo.get("cities", [geo.get("county", "City")]))
    if isinstance(city_entry, dict):
        city = city_entry["name"]
        zip_code = rng.choice(city_entry.get("zips", ["00000"]))
    else:
        city = city_entry
        zip_code = fac.get("zip", "43215")
    last_enc_date = enc_dates[-1]
    entered_on_date = last_enc_date.replace(hour=0, minute=0, second=0)

    parts = []  # Patient section only (no Container wrapper; assembly adds it per-facility)

    # ---- Patient ----
    ins_number_type = (
        "MA" if ins_plan.get("type") == "Medicaid"
        else ("MC" if ins_plan.get("type") == "Medicare" else "MRN")
    )
    ins_org_code = ins_plan.get("code", "COM")
    ins_org_name = ins_plan.get("name", "Commercial Insurance")

    patient_numbers = (
        f"    <PatientNumbers>\n"
        f"      <PatientNumber>\n"
        f"        <Number>__FAC_MRN__</Number>\n"
        f"        <NumberType>MRN</NumberType>\n"
        f"        <Organization><Code>__FAC_CODE__</Code><Description>__FAC_NAME__</Description></Organization>\n"
        f"      </PatientNumber>\n"
    )
    if ins_number_type != "MRN":
        ins_num = f"{ins_org_code}-{patient_id:07d}"
        patient_numbers += (
            f"      <PatientNumber>\n"
            f"        <Number>{ins_num}</Number>\n"
            f"        <NumberType>{ins_number_type}</NumberType>\n"
            f"        <Organization><Code>{ins_org_code}</Code><Description>{ins_org_name}</Description></Organization>\n"
            f"      </PatientNumber>\n"
        )
    patient_numbers += "    </PatientNumbers>\n"

    eth_code = race_row.get("ethnicity_code", "2186-5")
    eth_desc = race_row.get("ethnicity_description", "Not Hispanic or Latino")
    street_num = rng.randint(100, 9999)
    street_name = rng.choice(["Main", "Oak", "Elm", "Park", "Lake", "Maple", "Cedar"])
    street_type = rng.choice(["St", "Ave", "Blvd", "Dr", "Ln"])

    parts.append(
        f"  <Patient>\n"
        f"    <Name>\n"
        f"      <FamilyName>{family}</FamilyName>\n"
        f"      <GivenName>{given}</GivenName>\n"
        f"      <MiddleName>{middle}.</MiddleName>\n"
        f"      <Type>Legal</Type>\n"
        f"    </Name>\n"
        f"    <Gender>\n"
        f"      <SDACodingStandard>HL7</SDACodingStandard>\n"
        f"      <Code>{sex}</Code>\n"
        f"      <Description>{sex_desc}</Description>\n"
        f"    </Gender>\n"
        f"    <Race>\n"
        f"      <SDACodingStandard>CDCREC</SDACodingStandard>\n"
        f"      <Code>{race_row['race_code']}</Code>\n"
        f"      <Description>{race_row['race_description']}</Description>\n"
        f"    </Race>\n"
        f"    <EthnicGroup>\n"
        f"      <SDACodingStandard>CDCREC</SDACodingStandard>\n"
        f"      <Code>{eth_code}</Code>\n"
        f"      <Description>{eth_desc}</Description>\n"
        f"    </EthnicGroup>\n"
        f"    <BirthTime>{_ts(birth_dt)}</BirthTime>\n"
        + patient_numbers +
        f"    <Addresses>\n"
        f"      <Address>\n"
        f"        <Street>{street_num} {street_name} {street_type}</Street>\n"
        f"        <City><Code>{city.upper().replace(' ','')}</Code><Description>{city}</Description></City>\n"
        f"        <State><SDACodingStandard>USPS</SDACodingStandard><Code>{state_code}</Code><Description>Ohio</Description></State>\n"
        f"        <Zip><Code>{zip_code}</Code></Zip>\n"
        f"        <Country><SDACodingStandard>ISO 3166</SDACodingStandard><Code>US</Code><Description>United States</Description></Country>\n"
        f"        <County><SDACodingStandard>FIPS</SDACodingStandard><Code>{county_fips}</Code><Description>{county_name} County</Description></County>\n"
        f"      </Address>\n"
        f"    </Addresses>\n"
        f"    <EnteredBy><Code>__PROV_CODE__</Code><Description>__PROV_NAME__</Description></EnteredBy>\n"
        f"    <EnteredAt><Code>__FAC_CODE__</Code><Description>__FAC_NAME__</Description></EnteredAt>\n"
        f"    <EnteredOn>{_ts(entered_on_date)}</EnteredOn>\n"
        f"    <ActionCode>A</ActionCode>\n"
        f"  </Patient>\n"
    )

    # ---- ALL Encounters (must precede all clinical sections in XSD) ----
    hf_block = ""
    if ins_plan and ins_plan.get("type") != "Uninsured":
        cs = ins_plan.get("coding_standard", "QD_HealthPlanCodeList")
        pc = ins_plan.get("plan_code", ins_plan.get("code", "PLAN"))
        pname = ins_plan.get("plan_name", ins_plan.get("name", "Health Plan"))
        gname = ins_plan.get("group_name", "Group")
        mbr = f"MBR-{patient_id:07d}"
        yr = last_enc_date.year
        hf_block = (
            f"      <HealthFunds>\n"
            f"        <HealthFund>\n"
            f"          <HealthFund>\n"
            f"            <SDACodingStandard>{cs}</SDACodingStandard>\n"
            f"            <Code>{ins_plan['code']}</Code>\n"
            f"            <Description>{ins_plan['name']}</Description>\n"
            f"          </HealthFund>\n"
            f"          <HealthFundPlan><Code>{pc}</Code><Description>{pname}</Description></HealthFundPlan>\n"
            f"          <GroupName>{gname}</GroupName>\n"
            f"          <MembershipNumber>{mbr}</MembershipNumber>\n"
            f"          <Priority>1</Priority>\n"
            f"          <FromTime>{yr}-01-01T00:00:00Z</FromTime>\n"
            f"          <ToTime>{yr}-12-31T00:00:00Z</ToTime>\n"
            f"        </HealthFund>\n"
            f"      </HealthFunds>\n"
        )

    _fac_enc: dict = {}
    _fac_dx: dict = {}
    _fac_obs: dict = {}
    _fac_proc: dict = {}
    _fac_doc: dict = {}
    _fac_lab: dict = {}
    _fac_rad: dict = {}
    _fac_med: dict = {}
    _fac_vax: dict = {}
    enc_nums = []
    enc_end_times = []    # stored for doc-signing timestamps
    enc_start_times = []  # stored for obs/procedure timestamps
    for ei, (ed, etype) in enumerate(zip(enc_dates, enc_types), 1):
        _ei0 = ei - 1
        _ef = _enc_facility_map[_ei0]
        fac_code = _ef["code"]
        fac_name = _ef["name"]
        prov_code, prov_name = _fac_prov_map[fac_code]
        en = f"ENC-{ed.strftime('%Y%m%d')}-{patient_id:04d}-{ei}"
        enc_nums.append(en)
        # Inpatient: scenario-specific LOS; ED: fixed window; outpatient: random slot 20-90 min
        if etype == "I":
            _ip_scen = _inpatient_scenes.get(ei - 1)
            _los_lo, _los_hi = _ip_scen.get("los_days_range", (1, 5)) if _ip_scen else (1, 5)
            stay_days = rng.randint(_los_lo, _los_hi)
            enc_start = ed.replace(hour=8, minute=0, second=0)
            enc_end = (ed + timedelta(days=stay_days)).replace(hour=10, minute=0, second=0)
        elif etype == "E":
            enc_start = ed.replace(hour=rng.randint(8, 21), minute=rng.choice([0, 15, 30, 45]), second=0)
            enc_end = enc_start + timedelta(hours=rng.randint(2, 6))
            if enc_end.date() != enc_start.date():
                enc_end = enc_start.replace(hour=23, minute=30, second=0)
        else:
            # Outpatient: realistic appointment slot — random start, 20–90-minute visit
            _appt_hour = rng.randint(8, 16)
            _appt_min = rng.choice([0, 10, 15, 20, 30, 40, 45, 50])
            _dur_min = rng.randint(20, 90)
            enc_start = ed.replace(hour=_appt_hour, minute=_appt_min, second=0)
            enc_end = enc_start + timedelta(minutes=_dur_min)
            if enc_end.date() != enc_start.date():
                enc_end = enc_start.replace(hour=23, minute=0, second=0)
        enc_end_times.append(enc_end)
        enc_start_times.append(enc_start)
        ets = _ts(enc_start)
        ete = _ts(enc_end)
        eod = _ts(ed.replace(hour=0, minute=0, second=0))
        this_hf = hf_block if ei == n_encounters else ""
        _fac_enc.setdefault(fac_code, []).append(
            f"    <Encounter>\n"
            f"      <EncounterNumber>{en}</EncounterNumber>\n"
            f"      <EncounterType>{etype}</EncounterType>\n"
            f"      <AttendingClinicians>\n"
            f"        <CareProvider><Code>{prov_code}</Code><Description>{prov_name}</Description></CareProvider>\n"
            f"      </AttendingClinicians>\n"
            f"      <HealthCareFacility>\n"
            f"        <Organization><Code>{fac_code}</Code><Description>{fac_name}</Description></Organization>\n"
            f"      </HealthCareFacility>\n"
            + this_hf +
            f"      <ActionCode>A</ActionCode>\n"
            f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
            f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
            f"      <EnteredOn>{eod}</EnteredOn>\n"
            f"      <FromTime>{ets}</FromTime>\n"
            f"      <ToTime>{ete}</ToTime>\n"
            f"    </Encounter>\n"
        )
    # Restore home facility vars for shared sections (allergies, illness, social, family)
    fac_code = _home_fac_code
    fac_name = _home_fac_name
    prov_code = _home_prov_code
    prov_name = _home_prov_name
    _home_shared_parts: list = []

    # ---- Allergies (catalogs key; once per patient) ----
    catalogs = tmpl.get("catalogs", tmpl.get("shared", {}))
    all_allergies = catalogs.get("allergies", [])
    if all_allergies and rng.random() < 0.4:
        allergy = _wpick(all_allergies, "weight", rng)
        allergy_onset = max(birth_dt, enc_dates[0] - timedelta(days=rng.randint(90, 1800)))
        reaction = allergy.get("reaction_description", allergy.get("reaction", "Rash"))
        _home_shared_parts.append(
            f"  <Allergies>\n"
            f"    <Allergy>\n"
            f"      <Allergy>\n"
            f"        <Code>{allergy.get('allergen_code','PCN')}</Code>\n"
            f"        <Description>{allergy.get('allergen_description','Penicillin')}</Description>\n"
            f"      </Allergy>\n"
            f"      <Reaction><Code>R</Code><Description>{reaction}</Description></Reaction>\n"
            f"      <Severity><Code>M</Code><Description>Moderate</Description></Severity>\n"
            f"      <Status>A</Status>\n"
            f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
            f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
            f"      <EnteredOn>{_ts(allergy_onset.replace(hour=0,minute=0,second=0))}</EnteredOn>\n"
            f"      <FromTime>{_ts(allergy_onset.replace(hour=0,minute=0,second=0))}</FromTime>\n"
            f"      <ExternalId>Allergies_1</ExternalId>\n"
            f"      <EncounterNumber>{enc_nums[0]}</EncounterNumber>\n"
            f"    </Allergy>\n"
            f"  </Allergies>\n"
        )

    # ---- IllnessHistories (once per patient) ----
    illness_tmpls = cohort.get("illness_history_templates", [])
    if illness_tmpls:
        _home_shared_parts.append("  <IllnessHistories>\n")
        eod0 = _ts(enc_dates[0].replace(hour=0, minute=0, second=0))
        # Max onset age: must be before the first encounter date
        _age_at_first_enc = max(1, (enc_dates[0] - birth_dt).days // 365 - 1)
        for idx, ih in enumerate(illness_tmpls[:2], 1):
            if "onset_at_age_min" in ih:
                oa_min = int(ih["onset_at_age_min"])
                oa_max = min(int(ih["onset_at_age_max"]), _age_at_first_enc)
                oa_min = min(oa_min, oa_max)
                onset_age = rng.randint(oa_min, oa_max)
                onset = birth_dt + timedelta(days=onset_age * 365)
            else:
                yrs_ago = rng.randint(int(ih.get("onset_years_ago_min", 1)),
                                      int(ih.get("onset_years_ago_max", 10)))
                _ref_age = max(1, (_REFERENCE_DATE - birth_dt).days // 365)
                onset_age = max(1, min(_ref_age - yrs_ago, _age_at_first_enc))
                onset = birth_dt + timedelta(days=onset_age * 365)
            cond_text = re.sub(r'at age \d+', f'at age {onset_age}',
                                ih.get("condition", "Chronic condition"))
            _home_shared_parts.append(
                f"    <IllnessHistory>\n"
                f"      <Condition><Code>{cond_text}</Code><Description>{cond_text}</Description></Condition>\n"
                f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                f"      <EnteredOn>{eod0}</EnteredOn>\n"
                f"      <FromTime>{_ts(onset.replace(hour=0,minute=0,second=0))}</FromTime>\n"
                f"      <ExternalId>IllnessHistories_{idx}</ExternalId>\n"
                f"    </IllnessHistory>\n"
            )
        _home_shared_parts.append("  </IllnessHistories>\n")

    # ---- SocialHistories ----
    social_tmpls = cohort.get("social_history_templates", [])
    if social_tmpls:
        sh = _wpick(social_tmpls, "weight", rng)
        comment = rng.choice(sh.get("comments", ["No additional information."]))
        eod0 = _ts(enc_dates[0].replace(hour=0, minute=0, second=0))
        _home_shared_parts.append(
            f"  <SocialHistories>\n"
            f"    <SocialHistory>\n"
            f"      <SocialHabit><Code>{sh.get('habit_code','NS')}</Code>"
            f"<Description>{sh.get('habit_description','Non Smoker')}</Description></SocialHabit>\n"
            f"      <SocialHabitComments>{comment}</SocialHabitComments>\n"
            f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
            f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
            f"      <EnteredOn>{eod0}</EnteredOn>\n"
            f"      <FromTime>{eod0}</FromTime>\n"
            f"      <ExternalId>SocialHistories_1</ExternalId>\n"
            f"    </SocialHistory>\n"
            f"  </SocialHistories>\n"
        )

    # ---- FamilyHistories ----
    fam_tmpls = cohort.get("family_history_templates", [])
    if fam_tmpls:
        _home_shared_parts.append("  <FamilyHistories>\n")
        eod0 = _ts(enc_dates[0].replace(hour=0, minute=0, second=0))
        for idx, fh in enumerate(fam_tmpls[:2], 1):
            rel = fh.get("relationship", "Father")
            rel_code = _FAMILY_MEMBER_MAP.get(rel.lower(), "FTH")
            cond = fh.get("condition", "Chronic condition")
            _home_shared_parts.append(
                f"    <FamilyHistory>\n"
                f"      <FamilyMember><Code>{rel_code}</Code><Description>{rel}</Description></FamilyMember>\n"
                f"      <Diagnosis><Code>{cond}</Code><Description>{cond}</Description></Diagnosis>\n"
                f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                f"      <EnteredOn>{eod0}</EnteredOn>\n"
                f"      <ExternalId>FamilyHistories_{idx}</ExternalId>\n"
                f"    </FamilyHistory>\n"
            )
        _home_shared_parts.append("  </FamilyHistories>\n")

    # ---- Diagnoses: primary at every encounter; comorbidities at even-indexed ones ----
    # diag_list was built above (before scenario pre-selection) so scenarios are only
    # chosen for conditions the patient actually has.

    _enc_secondary_diags: dict = {}  # ei -> list of secondary diag dicts for this encounter
    _enc_all_dx_codes: dict = {}    # ei -> set of all applicable diagnosis codes (primary+secondary)
    if diag_list:
        diag_idx = 1
        primary_diags = [d for d in diag_list if d.get("is_primary")]
        secondary_diags = [d for d in diag_list if not d.get("is_primary")]
        # Remove codes already in primary_diags and deduplicate within secondary_diags
        _primary_codes = {d["code"] for d in primary_diags}
        _seen_sec = set()
        _deduped_sec = []
        for _d in secondary_diags:
            if _d["code"] not in _primary_codes and _d["code"] not in _seen_sec:
                _seen_sec.add(_d["code"])
                _deduped_sec.append(_d)
        secondary_diags = _deduped_sec
        for ei, (ed, en, etype) in enumerate(zip(enc_dates, enc_nums, enc_types)):
            _ef_d = _enc_facility_map[ei]
            fac_code = _ef_d["code"]
            fac_name = _ef_d["name"]
            prov_code, prov_name = _fac_prov_map[fac_code]
            eod = _ts(ed.replace(hour=0, minute=0, second=0))
            # Filter primary_diags to those applicable to this encounter type.
            # Diagnoses with only_encounter_types restrict which encounter types they appear on.
            enc_primary = [
                d for d in primary_diags
                if not d.get("only_encounter_types")
                or etype in d.get("only_encounter_types", [])
            ]
            # Pregnancy cohort: replace enc_primary entirely with episode-context-aware code
            # so Z34.90 is never used — trimester-specific codes replace it, and O80 only
            # appears on the planned delivery encounter.
            if _is_pregnancy_cohort and ei in _pregnancy_enc_context:
                _pctx = _pregnancy_enc_context[ei]
                _ptype = _pctx.get("type")
                if _ptype == "delivery":
                    enc_primary = [{"code": "O80",
                                    "description": "Encounter for full-term uncomplicated delivery",
                                    "is_primary": True}]
                elif _ptype == "prenatal":
                    _tri = _pctx.get("trimester", 3)
                    _zmap = {1: ("Z34.01", "first trimester"),
                             2: ("Z34.02", "second trimester"),
                             3: ("Z34.03", "third trimester")}
                    _zcode, _ztail = _zmap[_tri]
                    enc_primary = [{"code": _zcode,
                                    "description": (
                                        f"Encounter for supervision of normal first pregnancy,"
                                        f" {_ztail}"
                                    ),
                                    "is_primary": True}]
                elif _ptype == "postpartum":
                    enc_primary = [{"code": "Z39.2",
                                    "description": "Encounter for routine postpartum follow-up",
                                    "is_primary": True}]
                else:  # inter_pregnancy
                    enc_primary = [{"code": "Z01.419",
                                    "description": (
                                        "Encounter for gynecological examination"
                                        " without abnormal findings"
                                    ),
                                    "is_primary": True}]
            # For postpartum/inter-pregnancy encounters, exclude pregnancy-specific secondary
            # codes; use a per-encounter pool so secondary_diags itself is not mutated.
            if (_is_pregnancy_cohort and ei in _pregnancy_enc_context
                    and _pregnancy_enc_context[ei].get("type") in ("postpartum", "inter_pregnancy")):
                _sec_pool = [d for d in secondary_diags
                             if not d["code"].startswith(("O", "Z34"))]
            else:
                _sec_pool = secondary_diags
            # Always include applicable primary diagnoses; sample a few secondary ones.
            # Inpatient encounters get more diagnoses (up to 4); outpatient 1-2 secondaries.
            max_secondary = rng.randint(1, 4) if etype == "I" else rng.randint(0, 2)
            # For inpatient and ED encounters with a pre-selected scenario, guarantee the
            # scenario-anchor ICD code appears in this encounter so DX002 never fires
            # on an encounter whose scenario was chosen because the patient has the
            # condition — even if that code wasn't sampled into the secondary slot.
            _forced_sec: list = []
            _scen_for_enc = _inpatient_scenes.get(ei) if etype == "I" else _ed_scenes.get(ei)
            if _scen_for_enc and etype in ("I", "E"):
                _scen_prefs = tuple(_scen_for_enc["icd_prefixes"])
                _acute_dx_map = _scen_for_enc.get("acute_dx_map", {})
                if _acute_dx_map:
                    # Scenario requires a specific acute complication code — derive it
                    # from the patient's actual matched ICD prefix and force it in
                    # unconditionally (so E11.65 is always present, not just when
                    # E11.65 happened to be sampled into secondary_diags).
                    _acute_to_force = None
                    for _pt_code in _pt_diag_codes:
                        for _pfx, _adx in _acute_dx_map.items():
                            if _pt_code.startswith(_pfx):
                                _acute_to_force = _adx
                                break
                        if _acute_to_force:
                            break
                    if _acute_to_force:
                        _acode = _acute_to_force["code"]
                        if not any(d["code"] == _acode for d in enc_primary):
                            _forced_sec = [_acute_to_force]
                else:
                    # Standard anchor: ensure at least one matching-prefix code is present.
                    if not any(d["code"].startswith(_scen_prefs) for d in enc_primary):
                        _anchors = [d for d in _sec_pool if d["code"].startswith(_scen_prefs)]
                        if _anchors:
                            _forced_sec = [rng.choice(_anchors)]
            _forced_sec_codes = {d["code"] for d in _forced_sec}
            _avail_for_sample = [d for d in _sec_pool if d["code"] not in _forced_sec_codes]
            _remaining_slots = max(0, max_secondary - len(_forced_sec))
            enc_secondary = _forced_sec + rng.sample(
                _avail_for_sample, min(_remaining_slots, len(_avail_for_sample))
            )
            _enc_secondary_diags[ei] = enc_secondary
            _enc_all_dx_codes[ei] = {d["code"] for d in enc_primary + enc_secondary}
            for d in enc_primary + enc_secondary:
                dtype_code = "F" if d.get("is_primary") else "C"
                dtype_desc = "Final" if d.get("is_primary") else "Chronic"
                _fac_dx.setdefault(fac_code, []).append(
                    f"    <Diagnosis>\n"
                    f"      <EncounterNumber>{en}</EncounterNumber>\n"
                    f"      <DiagnosingClinician><Code>{prov_code}</Code><Description>{prov_name}</Description></DiagnosingClinician>\n"
                    f"      <Diagnosis><Code>{d['code']}</Code><Description>{d['description']}</Description></Diagnosis>\n"
                    f"      <DiagnosisType><Code>{dtype_code}</Code><Description>{dtype_desc}</Description></DiagnosisType>\n"
                    f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                    f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                    f"      <EnteredOn>{eod}</EnteredOn>\n"
                    f"      <ExternalId>Diagnoses_{diag_idx}</ExternalId>\n"
                    f"    </Diagnosis>\n"
                )
                diag_idx += 1

    # COH001 guarantee: if a flagged condition is in diag_list but never reached any encounter
    # (can happen when encounter count is low and RNG never sampled the code), force it into
    # the first outpatient encounter so the COH001 validator never fires a false-alarm ERROR.
    _GUARANTEE_PREFIXES = [
        ("E10", "E11", "E12", "E13"),
        ("I10", "I11", "I12", "I13"),
        ("I25", "I20", "I21", "I22"),
        ("I50",),
        ("I48",),
        ("J45", "J44"),
    ]
    _all_enc_dx_flat: set = set()
    for _adx_set in _enc_all_dx_codes.values():
        _all_enc_dx_flat |= _adx_set
    for _gpfxs in _GUARANTEE_PREFIXES:
        _gd = next((d for d in diag_list if d.get("code", "").startswith(_gpfxs)), None)
        if _gd is None:
            continue
        if any(c.startswith(_gpfxs) for c in _all_enc_dx_flat):
            continue
        _target_ei = next((i for i, t in enumerate(enc_types) if t == "O"), 0)
        _enc_secondary_diags.setdefault(_target_ei, []).append(_gd)
        _enc_all_dx_codes.setdefault(_target_ei, set()).add(_gd["code"])
        _all_enc_dx_flat.add(_gd["code"])
        _ef_g = _enc_facility_map[_target_ei]
        _fpc_g, _fpn_g = _fac_prov_map[_ef_g["code"]]
        _eod_g = _ts(enc_dates[_target_ei].replace(hour=0, minute=0, second=0))
        _fac_dx.setdefault(_ef_g["code"], []).append(
            f"    <Diagnosis>\n"
            f"      <EncounterNumber>{enc_nums[_target_ei]}</EncounterNumber>\n"
            f"      <DiagnosingClinician><Code>{_fpc_g}</Code><Description>{_fpn_g}</Description></DiagnosingClinician>\n"
            f"      <Diagnosis><Code>{_gd['code']}</Code><Description>{_gd['description']}</Description></Diagnosis>\n"
            f"      <DiagnosisType><Code>C</Code><Description>Chronic</Description></DiagnosisType>\n"
            f"      <EnteredBy><Code>{_fpc_g}</Code><Description>{_fpn_g}</Description></EnteredBy>\n"
            f"      <EnteredAt><Code>{_ef_g['code']}</Code><Description>{_ef_g['name']}</Description></EnteredAt>\n"
            f"      <EnteredOn>{_eod_g}</EnteredOn>\n"
            f"      <ExternalId>Diagnoses_{diag_idx}</ExternalId>\n"
            f"    </Diagnosis>\n"
        )
        diag_idx += 1

    # ---- Observations: vitals at every encounter ----
    obs_list = cohort.get("observations", [])
    if obs_list:
        obs_idx = 1
        for ei_o, (ed, en) in enumerate(zip(enc_dates, enc_nums)):
            _ef_o = _enc_facility_map[ei_o]
            fac_code = _ef_o["code"]
            fac_name = _ef_o["name"]
            prov_code, prov_name = _fac_prov_map[fac_code]
            obs_time = _ts(enc_start_times[ei_o] + timedelta(minutes=rng.randint(3, 10)))
            ev = enc_vitals[ei_o]
            for obs in obs_list:
                code = obs.get("code", "")
                vital_key = _VITAL_LOINC.get(code)
                if vital_key and vital_key in ev:
                    val = str(ev[vital_key])
                else:
                    is_abn = rng.random() < float(obs.get("abnormal_pct", 0.2))
                    val = _result_value(obs, is_abn, rng)
                _fac_obs.setdefault(fac_code, []).append(
                    f"    <Observation>\n"
                    f"      <ExternalId>Observations_{obs_idx}</ExternalId>\n"
                    f"      <EncounterNumber>{en}</EncounterNumber>\n"
                    f"      <ObservationTime>{obs_time}</ObservationTime>\n"
                    f"      <ObservationCode><Code>{obs['code']}</Code><Description>{obs['description']}</Description></ObservationCode>\n"
                    f"      <ObservationValue>{val}</ObservationValue>\n"
                    f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                    f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                    f"      <EnteredOn>{obs_time}</EnteredOn>\n"
                    f"    </Observation>\n"
                )
                obs_idx += 1

    # ---- Procedures: one billing code per encounter ----
    proc_list = cohort.get("procedures", [])
    if proc_list:
        for ep_i, (ed, en) in enumerate(zip(enc_dates, enc_nums)):
            _ef_p = _enc_facility_map[ep_i]
            fac_code = _ef_p["code"]
            fac_name = _ef_p["name"]
            prov_code, prov_name = _fac_prov_map[fac_code]
            proc = _wpick(proc_list, "weight", rng)
            ets = _ts(enc_start_times[ep_i] + timedelta(minutes=rng.randint(10, 25)))
            eod = _ts(ed.replace(hour=0, minute=0, second=0))
            _fac_proc.setdefault(fac_code, []).append(
                f"    <Procedure>\n"
                f"      <FromTime>{ets}</FromTime>\n"
                f"      <EncounterNumber>{en}</EncounterNumber>\n"
                f"      <Procedure>\n"
                f"        <SDACodingStandard>CPT</SDACodingStandard>\n"
                f"        <Code>{proc['code']}</Code>\n"
                f"        <Description>{proc['description']}</Description>\n"
                f"      </Procedure>\n"
                f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                f"      <EnteredOn>{eod}</EnteredOn>\n"
                f"      <ActionCode>A</ActionCode>\n"
                f"    </Procedure>\n"
            )

    _enc_rows: list = []  # accumulated inside the document loop below
    _hyperglycemic_enc_data: dict = {}  # ei0 -> glucose_admit for hyperglycemic encounters

    # ---- Documents: progress note at every encounter ----
    # Per-patient antihypertensive regimen (ACE/ARB/CCB/THIA chosen once per patient)
    _htn_regimen = rng.choices(_HTN_REGIMENS, weights=[0.45, 0.25, 0.20, 0.10])[0]
    _HTN_DRUG     = _htn_regimen["drug"]
    _ACE_DOSE_STEPS = _htn_regimen["doses"]  # reused variable name
    _HTN_RXNORM   = _htn_regimen["rxnorm"]

    # Does this patient have an HTN diagnosis (checked against their actual diag_list)?
    _has_htn_diag = any(
        d.get("code", "").startswith(("I10", "I11", "I12", "I13"))
        for d in diag_list
    )

    # Progressive antihypertensive dose-escalation tracking
    # Each time BP triggers a severe escalation AND enough encounters have passed,
    # we advance the dose tier and record a new medication event.
    _ace_dose_idx = 0                 # current tier index
    _last_ace_esc_ei = None           # 0-based index of last escalation encounter
    _ace_esc_events: list = []        # [(enc_idx_0based, new_dose_mg), ...]
    # Second-line add-on: never the same molecule as the primary to avoid
    # impossible "amlodipine 10mg + amlodipine 5mg" notes when CCB is primary.
    if _HTN_DRUG == "amlodipine":
        _second_drug       = "lisinopril"
        _second_dose_steps = [10, 20]
        _second_rxnorm     = {
            10: ("1049630", "Lisinopril 10 MG Oral Tablet"),
            20: ("1049648", "Lisinopril 20 MG Oral Tablet"),
        }
    else:
        _second_drug       = "amlodipine"
        _second_dose_steps = [5, 10]
        _second_rxnorm     = {
            5:  ("329526", "Amlodipine 5 MG Oral Tablet"),
            10: ("858810", "Amlodipine 10 MG Oral Tablet"),
        }
    _second_dose      = 0              # mg currently active (0 = not yet started)
    _second_esc_events: list = []      # [(ei0, enc_date, enc_num, dose_mg), ...]
    _metformin_dose = 500             # current metformin dose mg (500 → 1000)
    _metformin_esc_events: list = []  # [(ei0, enc_date, enc_num, dose_mg), ...]
    _dm_second_agent_event = None     # (ei0, enc_date, enc_num) when empagliflozin is started
    _scenario_triggered_meds: list = []  # [(ei0, enc_date, enc_num, med_dict), ...]
    _scenario_med_codes: set = set()     # drug_codes already added; prevents duplicate records
    _used_ed_scenario_keys: list = []    # scenario icd_prefixes[0] values already used; for variant selection

    # Pre-compute HbA1c trajectory so notes and lab records share the same values.
    # Patients with A1c monitoring get a scenario-driven trajectory (stable / worsen-then-improve).
    _a1c_by_enc: dict = {}
    _has_a1c_monitoring = any(
        any(ri.get("code") == "4548-4" for ri in lab.get("result_items", []))
        for lab in cohort.get("labs", [])
    )
    _has_dm_diag = any(d.get("code", "").startswith(("E10", "E11")) for d in diag_list)
    if _has_a1c_monitoring:
        a1c_scenario = rng.choices(
            ["stable", "worsen_then_improve", "improving"],
            weights=[0.25, 0.50, 0.25],
        )[0]
        for _i in range(n_encounters):
            frac = _i / max(1, n_encounters - 1)
            if a1c_scenario == "stable":
                _a1c_by_enc[_i] = round(rng.uniform(6.2, 7.8), 1)
            elif a1c_scenario == "worsen_then_improve":
                if frac < 0.35:
                    _a1c_by_enc[_i] = round(rng.uniform(6.5, 8.2), 1)
                elif frac < 0.65:
                    _a1c_by_enc[_i] = round(rng.uniform(9.0, 11.5), 1)
                else:
                    _a1c_by_enc[_i] = round(rng.uniform(7.0, 8.5), 1)
            else:  # improving
                _a1c_by_enc[_i] = round(rng.uniform(9.5 - frac * 3.5, 11.5 - frac * 4.5), 1)

    doc_tmpls = cohort.get("document_templates", [])
    if doc_tmpls:
        # primary_diag_code/desc are resolved per encounter inside the loop below
        # so that only_encounter_types filtering applies to the narrative and CSV.
        _primary_diag_fallback = diag_list[0] if diag_list else {"code": "", "description": "chronic condition"}
        for doc_idx, (ed, en, etype) in enumerate(zip(enc_dates, enc_nums, enc_types), 1):
            dt_item = _wpick(doc_tmpls, "weight", rng) if len(doc_tmpls) > 1 else doc_tmpls[0]
            cc = rng.choice(dt_item.get("chief_complaints", ["Follow-up visit"]))
            # Note-signing time varies by encounter type:
            # Inpatient: signed on discharge day, a few hours after discharge
            # Outpatient/ED: signed same day, shortly after visit ends (capped at 23:30)
            _enc_end = enc_end_times[doc_idx - 1]
            if etype == "I":
                _sign_dt = _enc_end + timedelta(hours=rng.randint(1, 5))
            else:
                _sign_dt = _enc_end + timedelta(minutes=rng.randint(10, 75))
                if _sign_dt.date() != ed.date():
                    _sign_dt = ed.replace(hour=23, minute=30, second=0)
            doc_time = _ts(_sign_dt)
            note = dt_item.get("note_template", "CHIEF COMPLAINT: {chief_complaint}")
            ev = enc_vitals[doc_idx - 1]
            bp_sys = int(ev["bp_sys"])
            bp_dia = int(ev["bp_dia"])
            hr_val = ev["hr"]
            wt_val = int(ev["weight_lb"])
            age_at_enc = max(1, (ed - birth_dt).days // 365)
            ei0 = doc_idx - 1  # 0-based encounter index
            _ef_doc = _enc_facility_map[ei0]
            fac_code = _ef_doc["code"]
            fac_name = _ef_doc["name"]
            prov_code, prov_name = _fac_prov_map[fac_code]

            # Pick the primary diagnosis for this encounter's narrative and CSV.
            # Pregnancy cohort: use episode-context-aware codes (trimester / delivery / etc).
            if _is_pregnancy_cohort and ei0 in _pregnancy_enc_context:
                _pctx = _pregnancy_enc_context[ei0]
                _ptype = _pctx.get("type")
                _pdx_map = {
                    "delivery": ("O80", "Encounter for full-term uncomplicated delivery"),
                    "postpartum": ("Z39.2", "Encounter for routine postpartum follow-up"),
                    "inter_pregnancy": ("Z01.419",
                                        "Encounter for gynecological examination"
                                        " without abnormal findings"),
                }
                if _ptype == "prenatal":
                    _tri = _pctx.get("trimester", 3)
                    _zcode = ("Z34.01", "Z34.02", "Z34.03")[_tri - 1]
                    _ztail = ("first", "second", "third")[_tri - 1]
                    primary_diag_code = _zcode
                    primary_diag_desc = (
                        f"Encounter for supervision of normal first pregnancy, {_ztail} trimester"
                    )
                else:
                    primary_diag_code, primary_diag_desc = _pdx_map.get(
                        _ptype, ("Z01.419", "Encounter for gynecological examination"
                                             " without abnormal findings")
                    )
            else:
                # Prefer diagnoses whose only_encounter_types matches; fall back to unrestricted.
                _enc_primary_diag = (
                    next(
                        (d for d in primary_diags
                         if d.get("only_encounter_types") and etype in d["only_encounter_types"]),
                        None,
                    ) or next(
                        (d for d in primary_diags if not d.get("only_encounter_types")),
                        _primary_diag_fallback,
                    )
                )
                primary_diag_code = _enc_primary_diag.get("code", "")
                primary_diag_desc = _enc_primary_diag.get("description", "chronic condition")

            # BP-reactive plan with progressive dose escalation.
            # _max_fu_days starts at the actual next-encounter gap; each active clinical
            # problem can only tighten it (min), never loosen it.  follow_up_text and
            # _fu_interval are resolved after ALL condition blocks so every problem
            # participates in arbitration (intervention-sensitive + multi-condition).
            _gap = _next_enc_gaps[ei0]
            _max_fu_days = _gap if _gap else 180
            if bp_sys >= 145 or bp_dia >= 100:
                if not _has_htn_diag:
                    plan_text = (
                        "Elevated blood pressure noted. Not the primary focus of today's visit. "
                        "Recommend follow-up with primary care for further evaluation and management."
                    )
                else:
                    since_last = (ei0 - _last_ace_esc_ei) if _last_ace_esc_ei is not None else 99
                    if since_last >= 2:
                        # Ready to escalate
                        prev_dose = _ACE_DOSE_STEPS[_ace_dose_idx]
                        # Guard: next tier must actually be a higher dose (HCTZ has [12,25,25]).
                        _next_idx = _ace_dose_idx + 1
                        _primary_can_esc = (
                            _ace_dose_idx < len(_ACE_DOSE_STEPS) - 1
                            and _ACE_DOSE_STEPS[_next_idx] > prev_dose
                        )
                        if _primary_can_esc:
                            _ace_dose_idx = _next_idx
                            new_dose = _ACE_DOSE_STEPS[_ace_dose_idx]
                            plan_text = (
                                f"Blood pressure remains significantly above goal. "
                                f"Discussed adherence and home blood-pressure monitoring. "
                                f"Increasing {_HTN_DRUG} {prev_dose} mg to {new_dose} mg daily; "
                                f"follow-up BP check in {{fu_interval}}."
                            )
                            _max_fu_days = min(_max_fu_days, 42)
                            _ace_esc_events.append((ei0, new_dose))
                            _last_ace_esc_ei = ei0
                        else:
                            # Primary at effective max; escalate through second-line agent
                            _lisi_dose = _ACE_DOSE_STEPS[_ace_dose_idx]
                            if _second_dose == 0:
                                _sd1 = _second_dose_steps[0]
                                plan_text = (
                                    f"BP above goal despite {_HTN_DRUG} {_lisi_dose} mg. "
                                    f"Adding {_second_drug} {_sd1} mg daily for additional control. "
                                    f"Home BP monitoring; follow-up in {{fu_interval}}."
                                )
                                _second_dose = _sd1
                                _max_fu_days = min(_max_fu_days, 42)
                                _second_esc_events.append((ei0, ed, en, _sd1))
                                _last_ace_esc_ei = ei0
                            elif _second_dose == _second_dose_steps[0]:
                                _sd2 = _second_dose_steps[1]
                                plan_text = (
                                    f"BP still above goal on {_HTN_DRUG} {_lisi_dose} mg "
                                    f"+ {_second_drug} {_second_dose} mg. "
                                    f"Increase {_second_drug} to {_sd2} mg daily. "
                                    f"Home BP log; follow-up in {{fu_interval}}."
                                )
                                _second_dose = _sd2
                                _max_fu_days = min(_max_fu_days, 42)
                                _second_esc_events.append((ei0, ed, en, _sd2))
                                _last_ace_esc_ei = ei0
                            else:
                                plan_text = (
                                    f"BP above goal on maximal oral antihypertensive therapy "
                                    f"({_HTN_DRUG} {_lisi_dose} mg + {_second_drug} {_second_dose} mg). "
                                    f"Refer to cardiology; consider adding "
                                    f"hydrochlorothiazide 25 mg as third agent."
                                )
                    else:
                        # Still within the monitoring window after a recent change
                        plan_text = (
                            "Blood pressure being monitored following recent medication adjustment. "
                            "Continue current regimen; assess response at next visit."
                        )
                        _max_fu_days = min(_max_fu_days, 42)
            elif bp_sys >= 135 or bp_dia >= 88:
                plan_text = (
                    "Blood pressure above target. "
                    "Reinforced medication adherence and dietary sodium restriction. "
                    "Continue current regimen; re-check BP at next scheduled visit."
                )
                _max_fu_days = min(_max_fu_days, 90)
            else:
                plan_text = "Continue current medications. Follow-up in {fu_interval}."

            # A1c-reactive addendum: stateful diabetes treatment ladder.
            # Only fires when this encounter actually drew an A1c panel.
            a1c_here = _a1c_by_enc.get(ei0) if ei0 in _a1c_enc_set else None
            if a1c_here is not None and _has_dm_diag:
                if a1c_here >= 9.0:
                    if _metformin_dose < 1000:
                        plan_text += (
                            f" HbA1c {a1c_here:.1f}%: diabetes poorly controlled. "
                            f"Increasing metformin {_metformin_dose} mg to 1000 mg BID. "
                            f"Reinforce dietary changes. Repeat A1c in 3 months."
                        )
                        _metformin_dose = 1000
                        _max_fu_days = min(_max_fu_days, 90)
                        _metformin_esc_events.append((ei0, ed, en, 1000))
                    elif _dm_second_agent_event is None:
                        plan_text += (
                            f" HbA1c {a1c_here:.1f}%: poorly controlled on metformin "
                            f"{_metformin_dose} mg BID. Adding empagliflozin 10 mg daily "
                            f"for additional glycemic control and cardiovascular benefit. "
                            f"Repeat A1c in 3 months."
                        )
                        _dm_second_agent_event = (ei0, ed, en)
                        _max_fu_days = min(_max_fu_days, 90)
                    else:
                        plan_text += (
                            f" HbA1c {a1c_here:.1f}%: poorly controlled on maximal oral "
                            f"therapy. Refer to endocrinology; discuss GLP-1 agonist or insulin."
                        )
                        _max_fu_days = min(_max_fu_days, 90)
                elif a1c_here >= 7.5:
                    plan_text += (
                        f" HbA1c {a1c_here:.1f}%: above target. Reinforce diet and "
                        f"medication adherence. Repeat A1c in 3 months."
                    )
                    _max_fu_days = min(_max_fu_days, 90)
                else:
                    plan_text += (
                        f" HbA1c {a1c_here:.1f}%: at goal. Continue current diabetes regimen."
                    )

            # Resolve follow_up_text and _fu_interval from the tightest clinical constraint
            # across all active problems (intervention-sensitive + multi-condition arbitration).
            follow_up_text = _fu_text(_max_fu_days)
            _fu_interval = (
                follow_up_text
                .replace("Return in ", "")
                .replace("Return ", "")
                .rstrip(".")
                .split(";")[0]
                .strip()
            )
            plan_text = plan_text.replace("{fu_interval}", _fu_interval)

            # Scenario-specific vital overrides for ED encounters.
            # Initialized to the computed enc_vitals values; overridden for urgent scenarios
            # so the CSV captures presentation (pre-treatment) values, not post-treatment.
            _is_htn_urgency = False
            _is_af_rvr = False
            _is_hyperglycemic = False
            _glucose_admit = None
            _arr_sys = bp_sys   # arrival/presentation SBP (= post-treatment for most cases)
            _arr_dia = bp_dia
            _rvr_hr = hr_val    # arrival/presentation HR

            # Inpatient encounters use the pre-selected scenario (vitals were already forced to
            # match its BP range, so admission BP and clinical narrative are internally consistent).
            if etype == "I":
                _scen = _inpatient_scenes[ei0]
                _is_hyperglycemic = _scen.get("admission_dx") == "Hyperglycemic crisis"
                if _is_hyperglycemic:
                    _glucose_admit = rng.randint(280, 380)
                    _glucose_post = rng.randint(180, 280)
                # Collect scenario-specific medications (e.g. furosemide for ADHF, apixaban for
                # AFib) — deduplicated so each drug is only started once across all admissions.
                for _sm in _scen.get("scenario_medications", []):
                    if _sm["drug_code"] not in _scenario_med_codes:
                        _scenario_med_codes.add(_sm["drug_code"])
                        _scenario_triggered_meds.append((ei0, ed, en, _sm))
                _ip_cc = rng.choice(_scen["chief_complaints"])
                # Append chronic-disease medication decisions to the discharge plan, but
                # strip any follow-up interval language first — the scenario's discharge_plan
                # already contains an appropriate acute post-discharge follow-up, and appending
                # "follow-up BP check in 4 weeks" would create a competing instruction.
                _plan_for_addendum = re.sub(
                    r"[\s;,]*(?:Home BP (?:monitoring|log)[^.]*|"
                    r"[Ff]ollow[-\s]?up(?:\s+BP\s+check)?[^.]*)\.",
                    "",
                    plan_text,
                ).strip().rstrip(";,").strip()
                if _plan_for_addendum and not _plan_for_addendum.endswith((".", "!", "?")):
                    _plan_for_addendum += "."
                _generic_continue = _plan_for_addendum.startswith("Continue current medications")
                _discharge_addendum = (
                    "" if (_generic_continue or not _plan_for_addendum)
                    else f" {_plan_for_addendum}"
                )
                _ip_glucose_str = (
                    f", Glucose {_glucose_admit} mg/dL" if _is_hyperglycemic else ""
                )
                note = (
                    f"ADMISSION NOTE\n\n"
                    f"Chief Complaint: {_ip_cc.capitalize()}\n\n"
                    f"History of Present Illness: {given} {family} is a {age_at_enc}-year-old "
                    f"{sex_desc.lower()} with a history of {primary_diag_desc} who presents with "
                    f"{_ip_cc}.\n\n"
                    f"Vitals on Admission: BP {bp_sys}/{bp_dia} mmHg, HR {hr_val} bpm, "
                    f"Weight {wt_val} lbs{_ip_glucose_str}\n\n"
                    f"Admission Diagnosis: {_scen['admission_dx']}\n\n"
                    f"Hospital Course: {_scen['hospital_course']}\n\n"
                    f"Discharge Plan: {_scen['discharge_plan']}{_discharge_addendum}"
                )
            elif etype == "E":
                # ED encounter note — use the pre-selected scenario (chosen before the diagnoses
                # loop so anchor codes could be guaranteed there).
                _ed_scen = _ed_scenes[ei0]
                _ed_key = _ed_scen["icd_prefixes"][0]
                _ed_cc = rng.choice(_ed_scen["chief_complaints"])
                # Choose ed_course or a variant; on repeat visits use variants to avoid replay
                _ed_prior_count = _used_ed_scenario_keys.count(_ed_key)
                _ed_variants = _ed_scen.get("ed_variants", [])
                if _ed_prior_count == 0 or not _ed_variants:
                    _ed_course_text = _ed_scen.get("ed_course") or _ed_scen.get("hospital_course") or ""
                else:
                    _variant_idx = (_ed_prior_count - 1) % len(_ed_variants)
                    _ed_course_text = _ed_variants[_variant_idx]
                _used_ed_scenario_keys.append(_ed_key)
                # Disposition matches what the course text actually describes.
                # "dmit" catches both "admitted" and "Decision to admit".
                _ed_admitted = "dmit" in _ed_course_text
                if _ed_admitted:
                    _ed_disposition = (
                        "Admitted to inpatient service for continued monitoring and management."
                    )
                else:
                    _ed_disposition = (
                        f"Discharged to home in stable condition. "
                        f"Follow-up with {prov_name} within one week. "
                        f"Return to ED if symptoms worsen or new symptoms develop."
                    )
                # For urgent scenarios, arrival vitals must reflect the presentation, not the
                # post-treatment enc_vitals value.  The CSV InitialSBP/HR stores the arrival value.
                _is_htn_urgency = _ed_key.startswith(("I10", "I11", "I12", "I13"))
                _is_af_rvr = _ed_key.startswith("I48")
                _is_hyperglycemic = _ed_key.startswith(("E10", "E11", "E12", "E13"))
                if _is_hyperglycemic:
                    _glucose_admit = rng.randint(280, 380)
                    _glucose_post = rng.randint(180, 280)
                if _is_htn_urgency:
                    _arr_sys = rng.randint(188, 228)
                    _arr_dia = rng.randint(112, 135)
                    _vitals_line = (
                        f"Vitals on Arrival: BP {_arr_sys}/{_arr_dia} mmHg (pre-treatment), "
                        f"HR {hr_val} bpm, Weight {wt_val} lbs\n"
                        f"Post-treatment: BP {bp_sys}/{bp_dia} mmHg"
                    )
                elif _is_af_rvr:
                    _rvr_hr = rng.randint(110, 165)
                    _vitals_line = (
                        f"Vitals on Arrival: BP {bp_sys}/{bp_dia} mmHg, "
                        f"HR {_rvr_hr} bpm (pre-treatment), Weight {wt_val} lbs\n"
                        f"Post-treatment: HR {hr_val} bpm"
                    )
                elif _is_hyperglycemic:
                    _vitals_line = (
                        f"Vitals on Arrival: BP {bp_sys}/{bp_dia} mmHg, "
                        f"HR {hr_val} bpm, Weight {wt_val} lbs\n"
                        f"Point-of-care glucose: {_glucose_admit} mg/dL (pre-treatment)\n"
                        f"Post-treatment glucose: {_glucose_post} mg/dL"
                    )
                else:
                    _vitals_line = (
                        f"Vitals on Arrival: BP {bp_sys}/{bp_dia} mmHg, "
                        f"HR {hr_val} bpm, Weight {wt_val} lbs"
                    )
                note = (
                    f"EMERGENCY DEPARTMENT NOTE\n\n"
                    f"Chief Complaint: {_ed_cc.capitalize()}\n\n"
                    f"History of Present Illness: {given} {family} is a {age_at_enc}-year-old "
                    f"{sex_desc.lower()} with a history of {primary_diag_desc} who presents to the ED "
                    f"with {_ed_cc}. Symptoms began acutely prior to arrival.\n\n"
                    f"{_vitals_line}\n\n"
                    f"ED Assessment: {_ed_scen['admission_dx']}\n\n"
                    f"ED Course: {_ed_course_text}\n\n"
                    f"Disposition: {_ed_disposition}"
                )
            else:
                note = (note
                        .replace("{chief_complaint}", cc)
                        .replace("{patient_name}", f"{given} {family}")
                        .replace("{age}", str(age_at_enc))
                        .replace("{sex}", sex_desc.lower())
                        .replace("{diagnosis}", primary_diag_desc)
                        .replace("{compliance_statement}",
                                 rng.choice(_PHRASE_POOLS["compliance"]))
                        .replace("{bp}", f"{bp_sys}/{bp_dia}")
                        .replace("{hr}", str(hr_val))
                        .replace("{weight}", str(wt_val))
                        .replace("{plan}", plan_text))
                # Replace whatever "Return in X." the template uses — catches both
                # "Return in 3 months." and "Return in 6 months." variants.
                note = re.sub(r'Return (?:in|as) [^.\n]+\.', follow_up_text, note, count=1)
                # Vary the static denial and general-exam sentences per encounter.
                note = re.sub(r'Denies [^.]+\.', rng.choice(_denial_pool), note, count=1)
                note = note.replace("Well-appearing, no acute distress.",
                                    rng.choice(_PHRASE_POOLS["general_exam"]))
            note = note.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            type_code = dt_item.get("type_code", "PN")
            type_desc = dt_item.get("type_description", "Progress Note")

            # ---- Accumulate encounter CSV row ----
            _is_acute = etype in ("E", "I")
            _has_med_esc = (
                any(e[0] == ei0 for e in _ace_esc_events) or
                any(e[0] == ei0 for e in _second_esc_events) or
                any(e[0] == ei0 for e in _metformin_esc_events) or
                (_dm_second_agent_event is not None and _dm_second_agent_event[0] == ei0)
            )
            _has_dm_int = (
                any(e[0] == ei0 for e in _metformin_esc_events) or
                (_dm_second_agent_event is not None and _dm_second_agent_event[0] == ei0)
            )
            _has_htn_int = (
                any(e[0] == ei0 for e in _ace_esc_events) or
                any(e[0] == ei0 for e in _second_esc_events)
            )
            _disp_conflict = False
            if etype == "E":
                _note_has_admit = "dmit" in note
                _note_has_discharge_home = "Discharged to home" in note
                _disp_conflict = _note_has_admit and _note_has_discharge_home
            _vital_conflict = False
            if etype == "E":
                if _is_htn_urgency and _arr_sys < 160:
                    _vital_conflict = True
                elif _is_af_rvr and _rvr_hr < 100:
                    _vital_conflict = True
            _acute_scenario_name = ""
            if etype == "I" and ei0 in _inpatient_scenes:
                if _inpatient_scen_matched.get(ei0, False):
                    _acute_scenario_name = _inpatient_scenes[ei0].get("admission_dx", "")
            elif etype == "E":
                if _ed_scen_matched.get(ei, False):
                    _acute_scenario_name = _ed_scen.get("admission_dx", "")
            _enc_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": en,
                "EncounterType": etype,
                "EncounterStart": enc_start_times[ei0].strftime("%Y-%m-%dT%H:%M:%S"),
                "EncounterEnd": enc_end_times[ei0].strftime("%Y-%m-%dT%H:%M:%S"),
                "EncounterDurationMinutes": round(
                    (enc_end_times[ei0] - enc_start_times[ei0]).total_seconds() / 60, 1
                ),
                "FacilityCode": fac_code,
                "FacilityName": fac_name,
                "ProviderCode": prov_code,
                "ProviderName": prov_name,
                "PrimaryDiagnosisCode": primary_diag_code,
                "PrimaryDiagnosisDescription": primary_diag_desc,
                "AllEncounterDiagnosisCodes": "|".join(
                    sorted(_enc_all_dx_codes.get(ei0, []))
                ),
                "SecondaryDiagnosisCodes": "|".join(
                    d["code"] for d in _enc_secondary_diags.get(ei0, [])
                ),
                "SecondaryDiagnosisDescriptions": "|".join(
                    d.get("description", "") for d in _enc_secondary_diags.get(ei0, [])
                ),
                "AcuteScenario": _acute_scenario_name,
                "Disposition": (
                    "Inpatient Admission" if etype == "I"
                    else "Inpatient Admission" if (
                        etype == "E" and ("dmit" in note or "Admitted to inpatient" in note)
                    )
                    else "Home" if (etype == "E" and "Discharged to home" in note)
                    else "Home"
                ),
                "AdmissionDecision": (
                    "Yes" if etype == "I"
                    else "Yes" if (etype == "E" and ("dmit" in note or "Admitted to inpatient" in note))
                    else "No"
                ),
                "InitialSBP": _arr_sys,
                "InitialDBP": _arr_dia,
                "InitialHR": _rvr_hr,
                "InitialWeightLbs": wt_val,
                "A1c": _a1c_by_enc.get(ei0) if ei0 in _a1c_enc_set else "",
                "Glucose": _glucose_admit if _is_hyperglycemic else "",
                "BNP": "",
                "LinkedEncounterNumber": "",
                "ProcedureCount": 0,
                "LabCount": sum(len(_lab_enc_plans.get(ei, [])) for ei in [ei0]),
                "MedicationChangeCount": (
                    sum(1 for e in _ace_esc_events if e[0] == ei0) +
                    sum(1 for e in _second_esc_events if e[0] == ei0) +
                    sum(1 for e in _metformin_esc_events if e[0] == ei0) +
                    (1 if _dm_second_agent_event and _dm_second_agent_event[0] == ei0 else 0)
                ),
                "FollowUpDays": _gap if _gap else "",
                "NoteSignedTime": doc_time,
                "NoteTemplateVariant": etype,
                "NoteCharacterCount": len(note),
                "IsAcuteEncounter": "Yes" if _is_acute else "No",
                "HasMedicationEscalation": "Yes" if _has_med_esc else "No",
                "HasDiabetesIntervention": "Yes" if _has_dm_int else "No",
                "HasHypertensionIntervention": "Yes" if _has_htn_int else "No",
                "HasDispositionConflict": "Yes" if _disp_conflict else "No",
                "HasVitalScenarioConflict": "Yes" if _vital_conflict else "No",
                "HasTemporalConflict": "No",
            })
            if _is_hyperglycemic and _glucose_admit is not None:
                _hyperglycemic_enc_data[ei0] = _glucose_admit

            _fac_doc.setdefault(fac_code, []).append(
                f"    <Document>\n"
                f"      <EncounterNumber>{en}</EncounterNumber>\n"
                f"      <DocumentTime>{doc_time}</DocumentTime>\n"
                f"      <NoteText>{note}</NoteText>\n"
                f"      <DocumentType><Code>{type_code}</Code><Description>{type_desc}</Description></DocumentType>\n"
                f"      <Clinician><Code>{prov_code}</Code><Description>{prov_name}</Description></Clinician>\n"
                f"      <DocumentName>{type_desc}</DocumentName>\n"
                f"      <FileType>{type_desc}</FileType>\n"
                f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                f"      <EnteredOn>{doc_time}</EnteredOn>\n"
                f"      <ExternalId>Documents_{doc_idx}</ExternalId>\n"
                f"    </Document>\n"
            )

    # Post-process: link admitted ED encounters to the immediately following inpatient encounter,
    # but only when the inpatient starts within 24 hours of the ED end (same episode of care).
    _SAME_EPISODE_MAX_HOURS = 24
    for _er_idx, _er in enumerate(_enc_rows):
        if _er.get("EncounterType") == "E" and _er.get("AdmissionDecision") == "Yes":
            _next_er = _enc_rows[_er_idx + 1] if _er_idx + 1 < len(_enc_rows) else None
            if _next_er and _next_er.get("EncounterType") == "I":
                _ed_end_dt = datetime.fromisoformat(_er["EncounterEnd"])
                _ip_start_dt = datetime.fromisoformat(_next_er["EncounterStart"])
                _gap_h = (_ip_start_dt - _ed_end_dt).total_seconds() / 3600
                if 0 <= _gap_h <= _SAME_EPISODE_MAX_HOURS:
                    _er["LinkedEncounterNumber"] = _next_er["EncounterNumber"]
                    _next_er["LinkedEncounterNumber"] = _er["EncounterNumber"]

    # ---- LabOrders: use pre-selected encounter/panel pairs ----
    _lab_result_values: dict = {}  # ei -> {loinc_code: computed_value_str}
    if _lab_enc_plans:
        lab_global_idx = 1
        for ei, (ed, en) in enumerate(zip(enc_dates, enc_nums)):
            if ei not in _lab_enc_plans:
                continue
            _ef_lab = _enc_facility_map[ei]
            fac_code = _ef_lab["code"]
            fac_name = _ef_lab["name"]
            prov_code, prov_name = _fac_prov_map[fac_code]
            _ei_result_vals: dict = {}
            for lab in _lab_enc_plans[ei]:  # list of panels
                a1c_val = (
                    _a1c_by_enc.get(ei) if (
                        ei in _a1c_enc_set and
                        any(ri.get("code") == "4548-4" for ri in lab.get("result_items", []))
                    ) else None
                )
                _lab_xml_str, _lab_ri_vals = _build_lab_xml(
                    lab, en, ed, patient_id, prov_code, prov_name,
                    fac_code, fac_name, lab_global_idx, rng,
                    a1c_override=a1c_val,
                    enc_start_time=enc_start_times[ei])
                _fac_lab.setdefault(fac_code, []).append(_lab_xml_str)
                for rv in _lab_ri_vals:
                    _ei_result_vals[rv["code"]] = rv
                lab_global_idx += 1
            _lab_result_values[ei] = _ei_result_vals

    # ---- RadOrders: rate-driven, at most one per patient (last encounter) ----
    rad_tmpls = cohort.get("rad_order_templates", [])
    if rad_tmpls and rng.random() < rad_rate:
        rad = _wpick(rad_tmpls, "weight", rng)
        last_ed = enc_dates[-1]
        last_en = enc_nums[-1]
        _rad_start = enc_start_times[-1]
        _ef_rad = _enc_facility_map[n_encounters - 1]
        fac_code = _ef_rad["code"]
        fac_name = _ef_rad["name"]
        prov_code, prov_name = _fac_prov_map[fac_code]
        ets = _ts(_rad_start + timedelta(minutes=rng.randint(10, 30)))
        rad_result_ts = _ts(_rad_start + timedelta(minutes=rng.randint(45, 90)))
        eod = _ts(last_ed.replace(hour=0, minute=0, second=0))
        result_text = rng.choice(rad.get("result_texts", ["No acute findings."]))
        reason = rad.get("reason_description", "Clinical evaluation")
        _fac_rad.setdefault(fac_code, []).append(
            f"    <RadOrder>\n"
            f"      <PlacerId>RAD-{last_ed.strftime('%Y%m%d')}-{patient_id:04d}</PlacerId>\n"
            f"      <OrderItem><Code>{rad['order_code']}</Code><Description>{rad['order_description']}</Description></OrderItem>\n"
            f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
            f"      <Specimen>Radiograph</Specimen>\n"
            f"      <SpecimenCollectedTime>{ets}</SpecimenCollectedTime>\n"
            f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
            f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
            f"      <EnteredOn>{eod}</EnteredOn>\n"
            f"      <FromTime>{ets}</FromTime>\n"
            f"      <ToTime>{ets}</ToTime>\n"
            f"      <Result>\n"
            f"        <ResultTime>{rad_result_ts}</ResultTime>\n"
            f"        <ResultText>{result_text}</ResultText>\n"
            f"        <EnteredOn>{eod}</EnteredOn>\n"
            f"      </Result>\n"
            f"      <ReasonForStudy><Description>{reason}</Description></ReasonForStudy>\n"
            f"      <ExternalId>RadOrders_1</ExternalId>\n"
            f"      <EncounterNumber>{last_en}</EncounterNumber>\n"
            f"    </RadOrder>\n"
        )

    # ---- Medications: prescribed at first encounter, active throughout ----
    # Medications and vaccinations are attributed to the home facility
    fac_code = _home_fac_code
    fac_name = _home_fac_name
    prov_code = _home_prov_code
    prov_name = _home_prov_name
    _pt_diag_set = {d.get("code", "") for d in diag_list}
    # Use age at history start for min_age check — prevents assigning adult
    # chronic medications to patients who were underage when history began.
    _age_at_hist_start = max(0, age - (history_months // 12))
    med_list = [m for m in cohort.get("medications", [])
                if _age_at_hist_start >= int(m.get("min_age", 0))
                and age <= int(m.get("max_age", 999))
                and (not m.get("sex") or m.get("sex") == sex)
                and (not m.get("for_diagnosis_code")
                     or any(c.startswith(m["for_diagnosis_code"])
                            for c in _pt_diag_set))]
    # MED004 prevention: exclude conflicting RAAS class from random pool.
    # ACE inhibitor primary → drop ARBs; ARB primary → drop ACE inhibitors.
    _ACE_NAMES = frozenset({"lisinopril", "enalapril", "ramipril", "benazepril", "quinapril"})
    _ARB_NAMES = frozenset({"losartan", "valsartan", "irbesartan", "olmesartan", "candesartan"})
    _htn_lower = _HTN_DRUG.lower()
    if _htn_lower in _ACE_NAMES:
        med_list = [m for m in med_list
                    if m.get("drug_description", "").split()[0].lower() not in _ARB_NAMES]
    elif _htn_lower in _ARB_NAMES:
        med_list = [m for m in med_list
                    if m.get("drug_description", "").split()[0].lower() not in _ACE_NAMES]
    chosen_meds = []  # populated below if med_list is non-empty
    if med_list:
        # Prescribe most medications for the cohort (60–100% of the list, no duplicates)
        n_meds = max(1, round(len(med_list) * rng.uniform(0.6, 1.0)))
        n_meds = min(n_meds, len(med_list))
        chosen_meds = rng.sample(med_list, n_meds)
        seen_drug: set = set()
        med_xml_parts = []
        med_idx = 1
        first_enc_date = enc_dates[0]
        first_enc_num = enc_nums[0]
        first_eod = _ts(first_enc_date.replace(hour=0, minute=0, second=0))
        for med in chosen_meds:
            if med["drug_code"] in seen_drug:
                continue
            seen_drug.add(med["drug_code"])
            rx_start = max(birth_dt, first_enc_date - timedelta(days=rng.randint(0, 180)))
            med_xml_parts.append(
                f"    <Medication>\n"
                f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                f"      <EnteredOn>{first_eod}</EnteredOn>\n"
                f"      <FromTime>{_ts(rx_start.replace(hour=0,minute=0,second=0))}</FromTime>\n"
                f"      <ExternalId>Medications_{med_idx}</ExternalId>\n"
                f"      <EncounterNumber>{first_enc_num}</EncounterNumber>\n"
                f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
                f"      <Frequency><Code>{med.get('frequency_code','QD')}</Code>"
                f"<Description>{med.get('frequency_description','Once daily')}</Description></Frequency>\n"
                f"      <Status>{med.get('status','active')}</Status>\n"
                f"      <DrugProduct><Code>{med['drug_code']}</Code><Description>{med['drug_description']}</Description></DrugProduct>\n"
                f"      <DoseQuantity>{med.get('dose_quantity','1')}</DoseQuantity>\n"
                f"      <DoseUoM><Code>{med.get('dose_uom','mg')}</Code><Description>{med.get('dose_uom','mg')}</Description></DoseUoM>\n"
                f"      <DosageForm><Code>{med.get('dosage_form_code','TAB')}</Code><Description>{med.get('dosage_form_description','Tablet')}</Description></DosageForm>\n"
                f"      <Route><Code>{med.get('route_code','PO')}</Code><Description>{med.get('route_description','Oral')}</Description></Route>\n"
                f"    </Medication>\n"
            )
            med_idx += 1
        # For each tracked BP-escalation event, add a dose-change medication record.
        # This creates a structured medication history that matches the plan text.
        _htn_base_med = next(
            (m for m in chosen_meds if _HTN_DRUG in m.get("drug_description", "").lower()),
            None,
        )
        if _htn_base_med is None and _ace_esc_events:
            # No base med matched in cohort list — synthesise one from regimen RxNorm
            _start_dose = _ACE_DOSE_STEPS[0]
            _rc, _rd = _HTN_RXNORM.get(
                _start_dose,
                ("000000", f"{_HTN_DRUG.title()} {_start_dose} MG Oral Tablet"),
            )
            _htn_base_med = {
                "drug_code": _rc,
                "drug_description": _rd,
                "frequency_code": "QD",
                "frequency_description": "Once daily",
                "dose_uom": "mg",
                "dosage_form_code": "TAB",
                "dosage_form_description": "Tablet",
                "route_code": "PO",
                "route_description": "Oral",
            }
        if _htn_base_med and _ace_esc_events:
            for esc_ei, esc_dose in _ace_esc_events:
                esc_enc_date = enc_dates[esc_ei]
                esc_enc_num = enc_nums[esc_ei]
                esc_eod = _ts(esc_enc_date.replace(hour=0, minute=0, second=0))
                _esc_rc, _esc_rd = _HTN_RXNORM.get(
                    esc_dose,
                    (_htn_base_med["drug_code"],
                     re.sub(r'\d+ MG', f'{esc_dose} MG', _htn_base_med["drug_description"])),
                )
                med_xml_parts.append(
                    f"    <Medication>\n"
                    f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                    f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                    f"      <EnteredOn>{esc_eod}</EnteredOn>\n"
                    f"      <FromTime>{esc_eod}</FromTime>\n"
                    f"      <ExternalId>Medications_{med_idx}</ExternalId>\n"
                    f"      <EncounterNumber>{esc_enc_num}</EncounterNumber>\n"
                    f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
                    f"      <Frequency><Code>{_htn_base_med.get('frequency_code','QD')}</Code>"
                    f"<Description>{_htn_base_med.get('frequency_description','Once daily')}</Description></Frequency>\n"
                    f"      <Status>active</Status>\n"
                    f"      <DrugProduct><Code>{_esc_rc}</Code><Description>{_esc_rd}</Description></DrugProduct>\n"
                    f"      <DoseQuantity>{esc_dose}</DoseQuantity>\n"
                    f"      <DoseUoM><Code>{_htn_base_med.get('dose_uom','mg')}</Code><Description>{_htn_base_med.get('dose_uom','mg')}</Description></DoseUoM>\n"
                    f"      <DosageForm><Code>{_htn_base_med.get('dosage_form_code','TAB')}</Code><Description>{_htn_base_med.get('dosage_form_description','Tablet')}</Description></DosageForm>\n"
                    f"      <Route><Code>{_htn_base_med.get('route_code','PO')}</Code><Description>{_htn_base_med.get('route_description','Oral')}</Description></Route>\n"
                    f"    </Medication>\n"
                )
                med_idx += 1

        # Second-line add-on medication with proper dose lifecycle:
        # prior dose is discontinued when stepped up; only the final event stays active.
        for sec_idx, (sec_ei, sec_date, sec_enc, sec_dose) in enumerate(_second_esc_events):
            sec_eod = _ts(sec_date.replace(hour=0, minute=0, second=0))
            sec_code, sec_desc = _second_rxnorm.get(
                sec_dose,
                ("000000", f"{_second_drug.title()} {sec_dose} MG Oral Tablet"),
            )
            # When stepping up, write a discontinued record for the previous dose first.
            if sec_idx > 0:
                _prev_dose = _second_esc_events[sec_idx - 1][3]
                _prev_date = _second_esc_events[sec_idx - 1][1]
                _prev_enc  = _second_esc_events[sec_idx - 1][2]
                _prev_eod  = _ts(_prev_date.replace(hour=0, minute=0, second=0))
                _prev_code, _prev_desc = _second_rxnorm.get(
                    _prev_dose,
                    ("000000", f"{_second_drug.title()} {_prev_dose} MG Oral Tablet"),
                )
                med_xml_parts.append(
                    f"    <Medication>\n"
                    f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                    f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                    f"      <EnteredOn>{_prev_eod}</EnteredOn>\n"
                    f"      <FromTime>{_prev_eod}</FromTime>\n"
                    f"      <ToTime>{sec_eod}</ToTime>\n"
                    f"      <ExternalId>Medications_{med_idx}</ExternalId>\n"
                    f"      <EncounterNumber>{_prev_enc}</EncounterNumber>\n"
                    f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
                    f"      <Frequency><Code>QD</Code><Description>Once daily</Description></Frequency>\n"
                    f"      <Status>discontinued</Status>\n"
                    f"      <DrugProduct><Code>{_prev_code}</Code><Description>{_prev_desc}</Description></DrugProduct>\n"
                    f"      <DoseQuantity>{_prev_dose}</DoseQuantity>\n"
                    f"      <DoseUoM><Code>mg</Code><Description>mg</Description></DoseUoM>\n"
                    f"      <DosageForm><Code>TAB</Code><Description>Tablet</Description></DosageForm>\n"
                    f"      <Route><Code>PO</Code><Description>Oral</Description></Route>\n"
                    f"    </Medication>\n"
                )
                med_idx += 1
            med_xml_parts.append(
                f"    <Medication>\n"
                f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                f"      <EnteredOn>{sec_eod}</EnteredOn>\n"
                f"      <FromTime>{sec_eod}</FromTime>\n"
                f"      <ExternalId>Medications_{med_idx}</ExternalId>\n"
                f"      <EncounterNumber>{sec_enc}</EncounterNumber>\n"
                f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
                f"      <Frequency><Code>QD</Code><Description>Once daily</Description></Frequency>\n"
                f"      <Status>active</Status>\n"
                f"      <DrugProduct><Code>{sec_code}</Code><Description>{sec_desc}</Description></DrugProduct>\n"
                f"      <DoseQuantity>{sec_dose}</DoseQuantity>\n"
                f"      <DoseUoM><Code>mg</Code><Description>mg</Description></DoseUoM>\n"
                f"      <DosageForm><Code>TAB</Code><Description>Tablet</Description></DosageForm>\n"
                f"      <Route><Code>PO</Code><Description>Oral</Description></Route>\n"
                f"    </Medication>\n"
            )
            med_idx += 1

        # Metformin dose-escalation records (1000mg BID when step-up triggered by high A1c)
        metf_med = next(
            (m for m in chosen_meds if "metformin" in m.get("drug_description", "").lower()),
            None
        )
        if metf_med and _metformin_esc_events:
            for metf_ei, metf_date, metf_enc, metf_dose in _metformin_esc_events:
                new_desc = re.sub(r'\d+ MG', f'{metf_dose} MG',
                                  metf_med.get("drug_description", "Metformin Oral Tablet"))
                metf_eod = _ts(metf_date.replace(hour=0, minute=0, second=0))
                # RxNorm 861004 = Metformin 1000 MG Oral Tablet
                rxnorm_code = "861004" if metf_dose >= 1000 else metf_med.get("drug_code", "860975")
                med_xml_parts.append(
                    f"    <Medication>\n"
                    f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                    f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                    f"      <EnteredOn>{metf_eod}</EnteredOn>\n"
                    f"      <FromTime>{metf_eod}</FromTime>\n"
                    f"      <ExternalId>Medications_{med_idx}</ExternalId>\n"
                    f"      <EncounterNumber>{metf_enc}</EncounterNumber>\n"
                    f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
                    f"      <Frequency><Code>BID</Code><Description>Twice daily</Description></Frequency>\n"
                    f"      <Status>active</Status>\n"
                    f"      <DrugProduct><Code>{rxnorm_code}</Code><Description>{new_desc}</Description></DrugProduct>\n"
                    f"      <DoseQuantity>{metf_dose}</DoseQuantity>\n"
                    f"      <DoseUoM><Code>mg</Code><Description>mg</Description></DoseUoM>\n"
                    f"      <DosageForm><Code>TAB</Code><Description>Tablet</Description></DosageForm>\n"
                    f"      <Route><Code>PO</Code><Description>Oral</Description></Route>\n"
                    f"    </Medication>\n"
                )
                med_idx += 1

        # Empagliflozin start record (added as second DM agent after metformin maxed)
        if _dm_second_agent_event is not None:
            emp_ei, emp_date, emp_enc = _dm_second_agent_event
            emp_eod = _ts(emp_date.replace(hour=0, minute=0, second=0))
            med_xml_parts.append(
                f"    <Medication>\n"
                f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                f"      <EnteredOn>{emp_eod}</EnteredOn>\n"
                f"      <FromTime>{emp_eod}</FromTime>\n"
                f"      <ExternalId>Medications_{med_idx}</ExternalId>\n"
                f"      <EncounterNumber>{emp_enc}</EncounterNumber>\n"
                f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
                f"      <Frequency><Code>QD</Code><Description>Once daily</Description></Frequency>\n"
                f"      <Status>active</Status>\n"
                f"      <DrugProduct><Code>1656357</Code><Description>Empagliflozin 10 MG Oral Tablet</Description></DrugProduct>\n"
                f"      <DoseQuantity>10</DoseQuantity>\n"
                f"      <DoseUoM><Code>mg</Code><Description>mg</Description></DoseUoM>\n"
                f"      <DosageForm><Code>TAB</Code><Description>Tablet</Description></DosageForm>\n"
                f"      <Route><Code>PO</Code><Description>Oral</Description></Route>\n"
                f"    </Medication>\n"
            )
            med_idx += 1

        # Medications started during inpatient admissions (furosemide, apixaban, etc.)
        for sm_ei, sm_date, sm_enc, sm in _scenario_triggered_meds:
            sm_eod = _ts(sm_date.replace(hour=0, minute=0, second=0))
            med_xml_parts.append(
                f"    <Medication>\n"
                f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
                f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
                f"      <EnteredOn>{sm_eod}</EnteredOn>\n"
                f"      <FromTime>{sm_eod}</FromTime>\n"
                f"      <ExternalId>Medications_{med_idx}</ExternalId>\n"
                f"      <EncounterNumber>{sm_enc}</EncounterNumber>\n"
                f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
                f"      <Frequency><Code>{sm['frequency_code']}</Code><Description>{sm['frequency_desc']}</Description></Frequency>\n"
                f"      <Status>active</Status>\n"
                f"      <DrugProduct><Code>{sm['drug_code']}</Code><Description>{sm['drug_description']}</Description></DrugProduct>\n"
                f"      <DoseQuantity>{sm['dose_qty']}</DoseQuantity>\n"
                f"      <DoseUoM><Code>{sm['dose_uom']}</Code><Description>{sm['dose_uom']}</Description></DoseUoM>\n"
                f"      <DosageForm><Code>{sm['dosage_form_code']}</Code><Description>{sm['dosage_form_desc']}</Description></DosageForm>\n"
                f"      <Route><Code>{sm['route_code']}</Code><Description>{sm['route_desc']}</Description></Route>\n"
                f"    </Medication>\n"
            )
            med_idx += 1

        if med_xml_parts:
            _fac_med.setdefault(_home_fac_code, []).extend(med_xml_parts)

    # ---- Vaccinations: once per patient ----
    all_vaccinations = catalogs.get("vaccinations", [])
    if all_vaccinations and rng.random() < 0.6:
        vax = _wpick(all_vaccinations, "weight", rng)
        vax_date = max(birth_dt, enc_dates[0] - timedelta(days=rng.randint(14, 365)))
        vax_enc = f"ENC-{vax_date.strftime('%Y%m%d')}-{patient_id:04d}V"
        vax_eod = _ts(vax_date.replace(hour=0, minute=0, second=0))
        _fac_vax.setdefault(_home_fac_code, []).append(
            f"    <Vaccination>\n"
            f"      <EnteredBy><Code>{prov_code}</Code><Description>{prov_name}</Description></EnteredBy>\n"
            f"      <EnteredAt><Code>{fac_code}</Code><Description>{fac_name}</Description></EnteredAt>\n"
            f"      <EnteredOn>{vax_eod}</EnteredOn>\n"
            f"      <FromTime>{vax_eod}</FromTime>\n"
            f"      <ExternalId>Vaccinations_1</ExternalId>\n"
            f"      <EncounterNumber>{vax_enc}</EncounterNumber>\n"
            f"      <OrderItem><Code>{vax.get('cvx_code','140')}</Code><Description>{vax.get('description','Influenza, seasonal')}</Description></OrderItem>\n"
            f"      <OrderedBy><Code>{prov_code}</Code><Description>{prov_name}</Description></OrderedBy>\n"
            f"      <Frequency><Code>O</Code><Description>Once</Description></Frequency>\n"
            f"      <Status>V</Status>\n"
            f"      <DrugProduct><Code>{vax.get('cvx_code','140')}</Code><Description>{vax.get('description','Influenza, seasonal')}</Description></DrugProduct>\n"
            f"      <DoseQuantity>1</DoseQuantity>\n"
            f"      <DoseUoM><Code>dose</Code><Description>dose</Description></DoseUoM>\n"
            f"      <DosageForm><Code>I</Code><Description>injection</Description></DosageForm>\n"
            f"      <Route><Code>IM</Code><Description>intramuscular</Description></Route>\n"
            f"      <Indication>vaccination</Indication>\n"
            f"    </Vaccination>\n"
        )

    # Build per-facility XMLs (Patient section has __FAC_*__ placeholders)
    _patient_xml = "".join(parts)
    _home_shared_xml = "".join(_home_shared_parts)
    _seen_fcs: set = set()
    _ordered_facs: list = []
    _seen_fcs.add(_home_fac_code)
    _ordered_facs.append(fac)
    for _oi in range(n_encounters):
        _of = _enc_facility_map[_oi]
        _ofc = _of["code"]
        if _ofc not in _seen_fcs:
            _seen_fcs.add(_ofc)
            _ordered_facs.append(_of)
    _result_xmls: list = []
    for _fac_d in _ordered_facs:
        _fc = _fac_d["code"]
        _fn = _fac_d["name"]
        _fpc, _fpn = _fac_prov_map[_fc]
        _fmrn = _facility_mrn(patient_id, _fc, use_prefix=_mf_prefix)
        _fxml = "<Container>\n"
        _fxml += (_patient_xml
                  .replace("__FAC_MRN__", _fmrn)
                  .replace("__FAC_CODE__", _fc)
                  .replace("__FAC_NAME__", _fn)
                  .replace("__PROV_CODE__", _fpc)
                  .replace("__PROV_NAME__", _fpn))
        if _fc in _fac_enc:
            _fxml += "  <Encounters>\n" + "".join(_fac_enc[_fc]) + "  </Encounters>\n"
        if _fc == _home_fac_code:
            _fxml += _home_shared_xml
        if _fc in _fac_dx:
            _fxml += "  <Diagnoses>\n" + "".join(_fac_dx[_fc]) + "  </Diagnoses>\n"
        if _fc in _fac_obs:
            _fxml += "  <Observations>\n" + "".join(_fac_obs[_fc]) + "  </Observations>\n"
        if _fc in _fac_proc:
            _fxml += "  <Procedures>\n" + "".join(_fac_proc[_fc]) + "  </Procedures>\n"
        if _fc in _fac_doc:
            _fxml += "  <Documents>\n" + "".join(_fac_doc[_fc]) + "  </Documents>\n"
        if _fc in _fac_lab:
            _fxml += "  <LabOrders>\n" + "".join(_fac_lab[_fc]) + "  </LabOrders>\n"
        if _fc in _fac_rad:
            _fxml += "  <RadOrders>\n" + "".join(_fac_rad[_fc]) + "  </RadOrders>\n"
        if _fc in _fac_med:
            _fxml += "  <Medications>\n" + "".join(_fac_med[_fc]) + "  </Medications>\n"
        if _fc in _fac_vax:
            _fxml += "  <Vaccinations>\n" + "".join(_fac_vax[_fc]) + "  </Vaccinations>\n"
        _fxml += "</Container>\n"
        _result_xmls.append((_fc, inject_container_fields(_fxml, _fc)))

    # Build patient_facilities rows (one per unique facility this patient was seen at)
    _fac_rows: list = []
    for _fac_d in _ordered_facs:
        _fc = _fac_d["code"]
        _fn = _fac_d["name"]
        _fmrn2 = _facility_mrn(patient_id, _fc, use_prefix=_mf_prefix)
        _fac_enc_dates = [
            enc_dates[_oi] for _oi in range(n_encounters)
            if _enc_facility_map[_oi]["code"] == _fc
        ]
        _fac_rows.append({
            "PatientID": patient_id,
            "FacilityCode": _fc,
            "FacilityName": _fn,
            "HealthSystemCode": _fac_d.get("health_system_code", ""),
            "HealthSystemName": _fac_d.get("health_system_name", ""),
            "FacilityMRN": _fmrn2,
            "IsPrimaryFacility": "Yes" if _fc == _home_fac_code else "No",
            "FirstEncounterDate": (
                min(_fac_enc_dates).strftime("%Y-%m-%d") if _fac_enc_dates else ""
            ),
            "LastEncounterDate": (
                max(_fac_enc_dates).strftime("%Y-%m-%d") if _fac_enc_dates else ""
            ),
            "EncounterCount": len(_fac_enc_dates),
            "ProviderCode": _fac_prov_map[_fc][0],
            "ProviderName": _fac_prov_map[_fc][1],
            "XMLFileName": f"patient_{patient_id:06d}_{_fc}.xml",
        })

    # =========================================================================
    # Build CSV data structures
    # =========================================================================

    _MED_CLASS = {
        "lisinopril": "ACE inhibitor",
        "enalapril": "ACE inhibitor",
        "ramipril": "ACE inhibitor",
        "losartan": "ARB",
        "valsartan": "ARB",
        "irbesartan": "ARB",
        "amlodipine": "CCB",
        "nifedipine": "CCB",
        "hydrochlorothiazide": "Thiazide diuretic",
        "chlorthalidone": "Thiazide diuretic",
        "metformin": "Biguanide",
        "empagliflozin": "SGLT2 inhibitor",
        "furosemide": "Loop diuretic",
        "apixaban": "Anticoagulant (DOAC)",
        "rivaroxaban": "Anticoagulant (DOAC)",
        "metoprolol": "Beta-blocker",
        "carvedilol": "Beta-blocker",
        "atorvastatin": "Statin",
        "rosuvastatin": "Statin",
        "albuterol": "SABA",
        "fluticasone": "ICS",
        "montelukast": "Leukotriene modifier",
        "aspirin": "Antiplatelet",
        "clopidogrel": "Antiplatelet",
        "nitroglycerin": "Nitrate",
        "insulin": "Insulin",
        "doxycycline": "Antibiotic",
        "amoxicillin": "Antibiotic",
        "cefuroxime": "Antibiotic",
        "azithromycin": "Antibiotic",
        "ciprofloxacin": "Antibiotic",
        "tiotropium": "LAMA",
        "budesonide": "ICS",
        "prednisone": "Oral Corticosteroid",
        "prednisolone": "Oral Corticosteroid",
        "methylprednisolone": "Oral Corticosteroid",
    }

    _med_rows: list = []
    _med_evt_id = 1
    _FIRST_ENC_DATE = enc_dates[0] if enc_dates else _REFERENCE_DATE

    # Base medications from chosen_meds (started at first encounter)
    for cm in chosen_meds:
        drug_name = cm.get("drug_description", "").split()[0].lower()
        _cm_dose = cm.get("dose_value", cm.get("dose_amount", ""))
        if not _cm_dose:
            _cm_dose_m = re.search(
                r'\b(\d+(?:\.\d+)?)\s*(?:MG|MCG|UNITS?|UNT|ML)\b',
                cm.get("drug_description", ""), re.IGNORECASE
            )
            _cm_dose = _cm_dose_m.group(1) if _cm_dose_m else ""
        # Antibiotics and oral corticosteroids are acute courses, not chronic maintenance
        _drug_cls = _MED_CLASS.get(drug_name, cm.get("drug_class", "Other"))
        _is_antibiotic = _drug_cls == "Antibiotic"
        _is_oral_steroid = _drug_cls == "Oral Corticosteroid"
        _course_days = 21 if _is_antibiotic else 10 if _is_oral_steroid else 0
        _end_dt = (
            (_FIRST_ENC_DATE + timedelta(days=_course_days)).strftime("%Y-%m-%d")
            if _course_days else ""
        )
        _is_acute = _is_antibiotic or _is_oral_steroid
        _med_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": enc_nums[0] if enc_nums else "",
            "MedicationEventID": f"{patient_id:06d}-M{_med_evt_id:03d}",
            "MedicationName": cm.get("drug_description", ""),
            "GenericIngredient": drug_name,
            "DrugClass": _MED_CLASS.get(drug_name, "Other"),
            "DoseValue": _cm_dose,
            "DoseUnit": cm.get("dose_uom", "mg"),
            "Frequency": cm.get("frequency_code", ""),
            "Route": cm.get("route_code", "PO"),
            "StartDateTime": _FIRST_ENC_DATE.strftime("%Y-%m-%d"),
            "EndDateTime": _end_dt,
            "Action": "Start",
            "PreviousDoseValue": "",
            "NewDoseValue": _cm_dose,
            "IsActiveAfterEncounter": "No" if _is_acute else "Yes",
            "IsChronicMaintenance": "No" if _is_acute else "Yes",
            "IsDuplicateActiveIngredient": "No",
            "IsInvalidDoseChange": "No",
        })
        _med_evt_id += 1

    # Primary HTN drug escalation events
    _prev_primary_dose = _ACE_DOSE_STEPS[0]
    for esc_ei, esc_dose in _ace_esc_events:
        is_invalid = esc_dose <= _prev_primary_dose
        _med_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": enc_nums[esc_ei],
            "MedicationEventID": f"{patient_id:06d}-M{_med_evt_id:03d}",
            "MedicationName": f"{_HTN_DRUG.title()} {esc_dose} MG Oral Tablet",
            "GenericIngredient": _HTN_DRUG,
            "DrugClass": _MED_CLASS.get(_HTN_DRUG, "Antihypertensive"),
            "DoseValue": esc_dose,
            "DoseUnit": "mg",
            "Frequency": "QD",
            "Route": "PO",
            "StartDateTime": enc_dates[esc_ei].strftime("%Y-%m-%d"),
            "EndDateTime": "",
            "Action": "Increase",
            "PreviousDoseValue": _prev_primary_dose,
            "NewDoseValue": esc_dose,
            "IsActiveAfterEncounter": "Yes",
            "IsChronicMaintenance": "Yes",
            "IsDuplicateActiveIngredient": "No",
            "IsInvalidDoseChange": "Yes" if is_invalid else "No",
        })
        _prev_primary_dose = esc_dose
        _med_evt_id += 1

    # Second-line agent escalation events
    _prev_second_dose = 0
    for s_ei, s_date, s_enc, s_dose in _second_esc_events:
        action = "Start" if _prev_second_dose == 0 else "Increase"
        is_invalid = (_prev_second_dose > 0 and s_dose <= _prev_second_dose)
        _med_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": s_enc,
            "MedicationEventID": f"{patient_id:06d}-M{_med_evt_id:03d}",
            "MedicationName": f"{_second_drug.title()} {s_dose} MG Oral Tablet",
            "GenericIngredient": _second_drug,
            "DrugClass": _MED_CLASS.get(_second_drug, "Antihypertensive"),
            "DoseValue": s_dose,
            "DoseUnit": "mg",
            "Frequency": "QD",
            "Route": "PO",
            "StartDateTime": s_date.strftime("%Y-%m-%d"),
            "EndDateTime": "",
            "Action": action,
            "PreviousDoseValue": _prev_second_dose if _prev_second_dose > 0 else "",
            "NewDoseValue": s_dose,
            "IsActiveAfterEncounter": "Yes",
            "IsChronicMaintenance": "Yes",
            "IsDuplicateActiveIngredient": "No",
            "IsInvalidDoseChange": "Yes" if is_invalid else "No",
        })
        _prev_second_dose = s_dose
        _med_evt_id += 1

    # Metformin escalation events
    _prev_metf_dose = 500
    for m_ei, m_date, m_enc, m_dose in _metformin_esc_events:
        _med_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": m_enc,
            "MedicationEventID": f"{patient_id:06d}-M{_med_evt_id:03d}",
            "MedicationName": f"Metformin {m_dose} MG Oral Tablet",
            "GenericIngredient": "metformin",
            "DrugClass": "Biguanide",
            "DoseValue": m_dose,
            "DoseUnit": "mg",
            "Frequency": "BID",
            "Route": "PO",
            "StartDateTime": m_date.strftime("%Y-%m-%d"),
            "EndDateTime": "",
            "Action": "Increase",
            "PreviousDoseValue": _prev_metf_dose,
            "NewDoseValue": m_dose,
            "IsActiveAfterEncounter": "Yes",
            "IsChronicMaintenance": "Yes",
            "IsDuplicateActiveIngredient": "No",
            "IsInvalidDoseChange": "Yes" if m_dose <= _prev_metf_dose else "No",
        })
        _prev_metf_dose = m_dose
        _med_evt_id += 1

    # Empagliflozin start event
    if _dm_second_agent_event:
        dm2_ei, dm2_date, dm2_enc = _dm_second_agent_event
        _med_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": dm2_enc,
            "MedicationEventID": f"{patient_id:06d}-M{_med_evt_id:03d}",
            "MedicationName": "Empagliflozin 10 MG Oral Tablet",
            "GenericIngredient": "empagliflozin",
            "DrugClass": "SGLT2 inhibitor",
            "DoseValue": 10,
            "DoseUnit": "mg",
            "Frequency": "QD",
            "Route": "PO",
            "StartDateTime": dm2_date.strftime("%Y-%m-%d"),
            "EndDateTime": "",
            "Action": "Start",
            "PreviousDoseValue": "",
            "NewDoseValue": 10,
            "IsActiveAfterEncounter": "Yes",
            "IsChronicMaintenance": "Yes",
            "IsDuplicateActiveIngredient": "No",
            "IsInvalidDoseChange": "No",
        })
        _med_evt_id += 1

    # Scenario-triggered acute medications
    for sm_ei, sm_date, sm_enc, sm_dict in _scenario_triggered_meds:
        sm_name = sm_dict.get("drug_description", sm_dict.get("drug_name", ""))
        sm_generic = sm_name.split()[0].lower()
        _sm_dose = sm_dict.get("dose_value", "")
        if not _sm_dose:
            _sm_dose_m = re.search(
                r'\b(\d+(?:\.\d+)?)\s*(?:MG|MCG|UNITS?|UNT|ML)\b',
                sm_name, re.IGNORECASE
            )
            _sm_dose = _sm_dose_m.group(1) if _sm_dose_m else ""
        _med_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": sm_enc,
            "MedicationEventID": f"{patient_id:06d}-M{_med_evt_id:03d}",
            "MedicationName": sm_name,
            "GenericIngredient": sm_generic,
            "DrugClass": _MED_CLASS.get(sm_generic, "Acute"),
            "DoseValue": _sm_dose,
            "DoseUnit": sm_dict.get("dose_uom", "mg"),
            "Frequency": sm_dict.get("frequency_code", ""),
            "Route": sm_dict.get("route_code", "IV"),
            "StartDateTime": sm_date.strftime("%Y-%m-%d"),
            "EndDateTime": "",
            "Action": "Start",
            "PreviousDoseValue": "",
            "NewDoseValue": _sm_dose,
            "IsActiveAfterEncounter": "No",
            "IsChronicMaintenance": "No",
            "IsDuplicateActiveIngredient": "No",
            "IsInvalidDoseChange": "No",
        })
        _med_evt_id += 1

    # Normalize lifecycle: for chronic maintenance drugs, only the most recent
    # dose record (last in list) stays IsActiveAfterEncounter=Yes; earlier rows
    # (historical dose steps) are marked No so they don't trigger false MED002.
    _last_idx_by_drug: dict = {}
    for _mi, _mrow in enumerate(_med_rows):
        if _mrow.get("IsChronicMaintenance") == "Yes":
            _last_idx_by_drug[_mrow.get("GenericIngredient", "")] = _mi
    for _mi, _mrow in enumerate(_med_rows):
        if _mrow.get("IsChronicMaintenance") == "Yes":
            _drug_key = _mrow.get("GenericIngredient", "")
            if _mi != _last_idx_by_drug.get(_drug_key, _mi):
                _mrow["IsActiveAfterEncounter"] = "No"

    # Flag duplicate active ingredients (true duplicates after lifecycle normalization)
    _active_ingredients: dict = {}
    for mrow in _med_rows:
        if mrow.get("IsActiveAfterEncounter") == "Yes":
            ing = mrow.get("GenericIngredient", "")
            if ing in _active_ingredients:
                mrow["IsDuplicateActiveIngredient"] = "Yes"
                _active_ingredients[ing]["IsDuplicateActiveIngredient"] = "Yes"
            else:
                _active_ingredients[ing] = mrow

    # ---- Lab rows ----
    _lab_rows: list = []
    _lab_evt_id = 1
    for ei, panels in _lab_enc_plans.items():
        enc_date = enc_dates[ei]
        enc_num = enc_nums[ei]
        _ei_rv_map = _lab_result_values.get(ei, {})
        for lab in panels:
            for item in lab.get("result_items", []):
                _item_code = item.get("code", "")
                _rv_entry = _ei_rv_map.get(_item_code, {})
                _computed_val = _rv_entry.get("value", "")
                _computed_interp = _rv_entry.get("interp", "")
                _lab_rows.append({
                    "PatientID": patient_id,
                    "EncounterNumber": enc_num,
                    "LabEventID": f"{patient_id:06d}-L{_lab_evt_id:03d}",
                    "LabCode": _item_code,
                    "LabName": item.get("description", ""),
                    "ResultValue": _computed_val,
                    "Unit": item.get("units", ""),
                    "ReferenceLow": item.get("normal_min", item.get("reference_low", "")),
                    "ReferenceHigh": item.get("normal_max", item.get("reference_high", "")),
                    "ResultDateTime": (
                        enc_start_times[ei].strftime("%Y-%m-%dT%H:%M:%S")
                        if enc_start_times else enc_date.strftime("%Y-%m-%d")
                    ),
                    "AbnormalFlag": _computed_interp if _computed_interp != "N" else "",
                })
                _lab_evt_id += 1

    # Add point-of-care glucose for hyperglycemic encounters (not part of standard panels)
    for _hg_ei, _hg_glucose in _hyperglycemic_enc_data.items():
        if _hg_glucose is None:
            continue
        _lab_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": enc_nums[_hg_ei],
            "LabEventID": f"{patient_id:06d}-L{_lab_evt_id:03d}",
            "LabCode": "2339-0",
            "LabName": "Glucose",
            "ResultValue": str(_hg_glucose),
            "Unit": "mg/dL",
            "ReferenceLow": "70",
            "ReferenceHigh": "99",
            "ResultDateTime": (
                enc_start_times[_hg_ei].strftime("%Y-%m-%dT%H:%M:%S")
                if enc_start_times else enc_dates[_hg_ei].strftime("%Y-%m-%d")
            ),
            "AbnormalFlag": "H",
        })
        _lab_evt_id += 1

    # ---- Validation rows ----
    _val_rows: list = []

    # MED001: dose "increase" must result in higher dose
    for mrow in _med_rows:
        if mrow.get("IsInvalidDoseChange") == "Yes":
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": mrow.get("EncounterNumber", ""),
                "ValidationRuleID": "MED001",
                "Severity": "ERROR",
                "Category": "Medication",
                "Description": (
                    f"Dose 'increase' did not result in higher dose: {mrow['MedicationName']}"
                ),
                "Field1": "PreviousDoseValue",
                "Value1": mrow.get("PreviousDoseValue", ""),
                "Field2": "NewDoseValue",
                "Value2": mrow.get("NewDoseValue", ""),
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # MED002: duplicate active maintenance ingredient
    for mrow in _med_rows:
        if (mrow.get("IsDuplicateActiveIngredient") == "Yes"
                and mrow.get("IsChronicMaintenance") == "Yes"):
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": mrow.get("EncounterNumber", ""),
                "ValidationRuleID": "MED002",
                "Severity": "ERROR",
                "Category": "Medication",
                "Description": (
                    f"Multiple active maintenance doses of same ingredient: "
                    f"{mrow['GenericIngredient']}"
                ),
                "Field1": "MedicationName",
                "Value1": mrow.get("MedicationName", ""),
                "Field2": "IsActiveAfterEncounter",
                "Value2": "Yes",
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # ENC001: admit decision + discharge-home disposition
    for erow in _enc_rows:
        if erow.get("HasDispositionConflict") == "Yes":
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": erow.get("EncounterNumber", ""),
                "ValidationRuleID": "ENC001",
                "Severity": "ERROR",
                "Category": "Encounter",
                "Description": "Admission decision coexists with discharge-home disposition",
                "Field1": "AdmissionDecision",
                "Value1": "Yes",
                "Field2": "Disposition",
                "Value2": "Discharged",
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # ENC002: admitted ED without immediately following inpatient encounter
    for _er_idx, _er in enumerate(_enc_rows):
        if (_er.get("EncounterType") == "E"
                and _er.get("AdmissionDecision") == "Yes"
                and not _er.get("LinkedEncounterNumber")):
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": _er.get("EncounterNumber", ""),
                "ValidationRuleID": "ENC002",
                "Severity": "WARNING",
                "Category": "Encounter",
                "Description": (
                    "ED admission decision has no immediately following inpatient encounter"
                ),
                "Field1": "AdmissionDecision",
                "Value1": "Yes",
                "Field2": "LinkedEncounterNumber",
                "Value2": "",
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # ENC003: linked inpatient encounter outside 24-hour same-episode window
    # Build a lookup so we can find the linked encounter without an O(n²) search.
    _enc_by_num = {r["EncounterNumber"]: r for r in _enc_rows}
    for _er in _enc_rows:
        _linked_num = _er.get("LinkedEncounterNumber")
        if not _linked_num or _er.get("EncounterType") != "E":
            continue
        _linked = _enc_by_num.get(_linked_num)
        if not _linked or _linked.get("EncounterType") != "I":
            continue
        _ed_end = datetime.fromisoformat(_er["EncounterEnd"])
        _ip_start = datetime.fromisoformat(_linked["EncounterStart"])
        _gap_h = (_ip_start - _ed_end).total_seconds() / 3600
        if _gap_h < 0 or _gap_h > _SAME_EPISODE_MAX_HOURS:
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": _er.get("EncounterNumber", ""),
                "ValidationRuleID": "ENC003",
                "Severity": "ERROR",
                "Category": "Encounter",
                "Description": (
                    f"Linked inpatient encounter is not temporally adjacent "
                    f"(gap {_gap_h:.1f}h, max {_SAME_EPISODE_MAX_HOURS}h)"
                ),
                "Field1": "EncounterEnd",
                "Value1": _er.get("EncounterEnd", ""),
                "Field2": "LinkedInpatientStart",
                "Value2": _linked.get("EncounterStart", ""),
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # VIT001: hypertensive urgency arrival SBP must be ≥160 mmHg
    # VIT002: AF-RVR arrival HR must be ≥100 bpm
    for erow in _enc_rows:
        if erow.get("HasVitalScenarioConflict") != "Yes":
            continue
        _scen_name = erow.get("AcuteScenario", "")
        _enc_type = erow.get("EncounterType", "")
        _is_htn_scen = any(
            kw in _scen_name.lower() for kw in ("hypertensive urgency", "hypertension")
        ) or (_enc_type == "E" and erow.get("InitialSBP", 999) < 160)
        _is_af_scen = "atrial fibrillation" in _scen_name.lower() or "af with" in _scen_name.lower()
        if not _is_af_scen and (erow.get("InitialSBP", 999) < 160 or _is_htn_scen):
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": erow.get("EncounterNumber", ""),
                "ValidationRuleID": "VIT001",
                "Severity": "WARNING",
                "Category": "Vital",
                "Description": (
                    "Hypertensive urgency scenario: arrival SBP below 160 mmHg"
                ),
                "Field1": "InitialSBP",
                "Value1": str(erow.get("InitialSBP", "")),
                "Field2": "AcuteScenario",
                "Value2": _scen_name,
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })
        if _is_af_scen or erow.get("InitialHR", 999) < 100:
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": erow.get("EncounterNumber", ""),
                "ValidationRuleID": "VIT002",
                "Severity": "WARNING",
                "Category": "Vital",
                "Description": (
                    "AF with RVR scenario: arrival HR below 100 bpm"
                ),
                "Field1": "InitialHR",
                "Value1": str(erow.get("InitialHR", "")),
                "Field2": "AcuteScenario",
                "Value2": _scen_name,
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # VIT004: hyperglycemic scenario must have qualifying glucose in CSV
    for erow in _enc_rows:
        _erow_scen = erow.get("AcuteScenario", "")
        if "Hyperglycemic" in _erow_scen and not erow.get("Glucose"):
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": erow.get("EncounterNumber", ""),
                "ValidationRuleID": "VIT004",
                "Severity": "WARNING",
                "Category": "Vital",
                "Description": "Hyperglycemic crisis encounter has no glucose value in CSV",
                "Field1": "Glucose",
                "Value1": "",
                "Field2": "AcuteScenario",
                "Value2": _erow_scen,
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # DX002: acute scenario must be represented by at least one compatible encounter diagnosis.
    # Scenarios with acute_dx_map require the specific acute complication code (e.g. E11.65),
    # not just any code matching the prefix (e.g. E11.9 would be insufficient).
    _scenario_by_name = {s["admission_dx"]: s for s in _INPATIENT_SCENARIOS}
    for erow in _enc_rows:
        _scen_name = erow.get("AcuteScenario", "")
        if not _scen_name:
            continue
        _scen_def = _scenario_by_name.get(_scen_name)
        if not _scen_def:
            continue
        _enc_codes = [c for c in erow.get("AllEncounterDiagnosisCodes", "").split("|") if c]
        _adx_map = _scen_def.get("acute_dx_map", {})
        if _adx_map:
            # Check that at least one specific acute code from the map is present
            _required_codes = {v["code"] for v in _adx_map.values()}
            _dx2_fail = not any(c in _required_codes for c in _enc_codes)
            _dx2_expected = " or ".join(sorted(_required_codes))
        else:
            _scen_prefs = tuple(_scen_def["icd_prefixes"])
            _dx2_fail = not any(c.startswith(_scen_prefs) for c in _enc_codes)
            _dx2_expected = "/".join(_scen_def["icd_prefixes"])
        if _dx2_fail:
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": erow.get("EncounterNumber", ""),
                "ValidationRuleID": "DX002",
                "Severity": "WARNING",
                "Category": "Diagnosis",
                "Description": (
                    f"Acute scenario '{_scen_name}' not represented by "
                    f"required diagnosis ({_dx2_expected})"
                ),
                "Field1": "AcuteScenario",
                "Value1": _scen_name,
                "Field2": "AllEncounterDiagnosisCodes",
                "Value2": erow.get("AllEncounterDiagnosisCodes", ""),
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # OB001: delivery encounter falls outside any planned pregnancy episode window
    # OB002: two delivery dates within the same patient are < 280 days apart
    if _is_pregnancy_cohort:
        _delivery_dates = [_pep["delivery_date"] for _pep in _pregnancy_episodes]
        for _i in range(len(_delivery_dates)):
            for _j in range(_i + 1, len(_delivery_dates)):
                _gap_d = abs((_delivery_dates[_i] - _delivery_dates[_j]).days)
                if _gap_d < 280:
                    _val_rows.append({
                        "PatientID": patient_id,
                        "EncounterNumber": "",
                        "ValidationRuleID": "OB002",
                        "Severity": "ERROR",
                        "Category": "Obstetrics",
                        "Description": (
                            f"Two deliveries only {_gap_d} days apart"
                            f" (minimum biologically plausible interval is 280 days)"
                        ),
                        "Field1": "DeliveryDate1",
                        "Value1": str(_delivery_dates[_i]),
                        "Field2": "DeliveryDate2",
                        "Value2": str(_delivery_dates[_j]),
                        "AutoCorrected": "No",
                        "RecordRegenerated": "No",
                    })
        for _er in _enc_rows:
            if _er.get("AcuteScenario") == "Normal term delivery":
                _enc_date = datetime.fromisoformat(_er["EncounterStart"]).date()
                if not any(
                    abs((_enc_date - _dlv_d).days) <= 4
                    for _dlv_d in _delivery_dates
                ):
                    _val_rows.append({
                        "PatientID": patient_id,
                        "EncounterNumber": _er.get("EncounterNumber", ""),
                        "ValidationRuleID": "OB001",
                        "Severity": "ERROR",
                        "Category": "Obstetrics",
                        "Description": (
                            "Delivery encounter occurs outside any planned pregnancy episode"
                        ),
                        "Field1": "EncounterDate",
                        "Value1": str(_enc_date),
                        "Field2": "AcuteScenario",
                        "Value2": "Normal term delivery",
                        "AutoCorrected": "No",
                        "RecordRegenerated": "No",
                    })

    # LAB001: missing result value when reference range is present
    for lrow in _lab_rows:
        if not lrow.get("ResultValue") and (lrow.get("ReferenceLow") or lrow.get("ReferenceHigh")):
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": lrow.get("EncounterNumber", ""),
                "ValidationRuleID": "LAB001",
                "Severity": "WARNING",
                "Category": "Lab",
                "Description": (
                    f"Lab result value blank but reference range present: {lrow.get('LabName','')}"
                ),
                "Field1": "ResultValue",
                "Value1": "",
                "Field2": "LabCode",
                "Value2": lrow.get("LabCode", ""),
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # DEM001: implausible BMI (adult patients only — pediatric BMI requires age-specific
    # percentile tables; adult thresholds do not apply to patients under 18)
    _bmi_check = (
        round(enc_vitals[0]["weight_lb"] * 703 / (_patient_height_in ** 2), 1)
        if (_patient_height_in and enc_vitals)
        else None
    )
    if _bmi_check is not None and age >= 18 and (_bmi_check < 14 or _bmi_check > 60):
        _val_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": "",
            "ValidationRuleID": "DEM001",
            "Severity": "WARNING",
            "Category": "Demographics",
            "Description": f"Implausible BMI: {_bmi_check}",
            "Field1": "BaselineBMI",
            "Value1": str(_bmi_check),
            "Field2": "BaselineWeightLbs",
            "Value2": str(enc_vitals[0]["weight_lb"]) if enc_vitals else "",
            "AutoCorrected": "No",
            "RecordRegenerated": "No",
        })

    # GEO002: facility region far from patient region
    if fac.get("region") and geo.get("region"):
        if fac.get("region") != geo.get("region"):
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": "",
                "ValidationRuleID": "GEO002",
                "Severity": "INFO",
                "Category": "Geography",
                "Description": "Facility region differs from patient residence region",
                "Field1": "PatientRegion",
                "Value1": geo.get("region", ""),
                "Field2": "FacilityRegion",
                "Value2": fac.get("region", ""),
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # MF001: multi-facility patient has distinct facility count consistent with XML files
    if len(_ordered_facs) > 1:
        for _mf_fac in _ordered_facs[1:]:
            _mfc = _mf_fac["code"]
            _mf_enc_count = sum(
                1 for _oi in range(n_encounters)
                if _enc_facility_map[_oi]["code"] == _mfc
            )
            if _mf_enc_count == 0:
                _val_rows.append({
                    "PatientID": patient_id,
                    "EncounterNumber": "",
                    "ValidationRuleID": "MF001",
                    "Severity": "ERROR",
                    "Category": "MultiFacility",
                    "Description": (
                        f"Non-primary facility in ordered list has no encounters: {_mfc}"
                    ),
                    "Field1": "FacilityCode",
                    "Value1": _mfc,
                    "Field2": "EncounterCount",
                    "Value2": "0",
                    "AutoCorrected": "No",
                    "RecordRegenerated": "No",
                })

    # MF002: facility MRN prefix matches facility code (when prefix mode is on)
    if _mf_prefix:
        for _mf_row in _fac_rows:
            _expected_prefix = _mf_row["FacilityCode"] + "-"
            if not _mf_row["FacilityMRN"].startswith(_expected_prefix):
                _val_rows.append({
                    "PatientID": patient_id,
                    "EncounterNumber": "",
                    "ValidationRuleID": "MF002",
                    "Severity": "ERROR",
                    "Category": "MultiFacility",
                    "Description": (
                        f"Facility MRN does not start with facility code prefix: "
                        f"{_mf_row['FacilityMRN']}"
                    ),
                    "Field1": "FacilityMRN",
                    "Value1": _mf_row["FacilityMRN"],
                    "Field2": "ExpectedPrefix",
                    "Value2": _expected_prefix,
                    "AutoCorrected": "No",
                    "RecordRegenerated": "No",
                })

    # MF004: ED→inpatient continuity — both must be at the same facility
    for _ed_ei, _ip_ei in _ed_to_ip_pairs.items():
        _ed_fac = _enc_facility_map[_ed_ei]["code"]
        _ip_fac = _enc_facility_map[_ip_ei]["code"]
        if _ed_fac != _ip_fac:
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": enc_nums[_ed_ei] if _ed_ei < len(enc_nums) else "",
                "ValidationRuleID": "MF004",
                "Severity": "ERROR",
                "Category": "MultiFacility",
                "Description": (
                    "ED→inpatient pair split across facilities: "
                    f"ED at {_ed_fac}, inpatient at {_ip_fac}"
                ),
                "Field1": "EDFacilityCode",
                "Value1": _ed_fac,
                "Field2": "IPFacilityCode",
                "Value2": _ip_fac,
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # MF005: provider in each facility file must belong to that facility's pool
    for _mf_row in _fac_rows:
        _mfc2 = _mf_row["FacilityCode"]
        _mf_prov = _mf_row["ProviderCode"]
        _mf_valid_provs = {p["code"] for p in _tmpl_providers if p.get("facility_code") == _mfc2}
        if _mf_valid_provs and _mf_prov not in _mf_valid_provs:
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": "",
                "ValidationRuleID": "MF005",
                "Severity": "WARNING",
                "Category": "MultiFacility",
                "Description": (
                    f"Provider {_mf_prov} does not belong to facility {_mfc2}'s provider pool"
                ),
                "Field1": "ProviderCode",
                "Value1": _mf_prov,
                "Field2": "FacilityCode",
                "Value2": _mfc2,
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    # LAB003: abnormal flag must agree with numeric result vs reference range
    for lrow in _lab_rows:
        _lv = lrow.get("ResultValue", "")
        _laf = lrow.get("AbnormalFlag", "")
        _rlo = lrow.get("ReferenceLow", "")
        _rhi = lrow.get("ReferenceHigh", "")
        if not _lv or _laf not in ("H", "L"):
            continue
        try:
            _lv_f = float(_lv)
            _lo_f = float(_rlo) if _rlo != "" else None
            _hi_f = float(_rhi) if _rhi != "" else None
            _correct = (
                (_laf == "H" and _hi_f is not None and _lv_f > _hi_f) or
                (_laf == "L" and _lo_f is not None and _lv_f < _lo_f)
            )
            if not _correct:
                _val_rows.append({
                    "PatientID": patient_id,
                    "EncounterNumber": lrow.get("EncounterNumber", ""),
                    "ValidationRuleID": "LAB003",
                    "Severity": "ERROR",
                    "Category": "Lab",
                    "Description": (
                        f"Abnormal flag '{_laf}' inconsistent with result "
                        f"{_lv} vs range {_rlo}-{_rhi}: {lrow.get('LabName','')}"
                    ),
                    "Field1": "AbnormalFlag",
                    "Value1": _laf,
                    "Field2": "ResultValue",
                    "Value2": _lv,
                    "AutoCorrected": "No",
                    "RecordRegenerated": "No",
                })
        except (ValueError, TypeError):
            pass

    # LAB004: result value outside physiologic plausibility limits (separate from reference range).
    # These are hard biological ceilings — values above them cannot occur in living patients.
    _LAB004_LIMITS = {
        "2085-9":  (15, 100),   # HDL: 15-100 mg/dL (below 15 not physiologically viable)
        "88294-4": (0, 150),    # eGFR: 0-150 mL/min/1.73m²
        "3094-0":  (0, 60),     # BUN: 0-60 mg/dL
        "2160-0":  (0, 5.0),    # Creatinine: 0-5.0 mg/dL
        "6768-6":  (0, 600),    # Alk Phos: 0-600 U/L
        "1742-6":  (0, 400),    # ALT: 0-400 U/L
        "1920-8":  (0, 400),    # AST: 0-400 U/L
        "6690-2":  (0, 20),     # WBC: 0-20 K/uL
        "777-3":   (0, 700),    # Platelets: 0-700 K/uL
        "2093-3":  (100, 450),  # Total Cholesterol: 100-450 mg/dL
        "1975-2":  (0, 10),     # Total Bilirubin: 0-10 mg/dL
        "2571-8":  (40, 500),   # Triglycerides: 40-500 mg/dL (below 40 not realistic)
        "13457-7": (0, 300),    # LDL: 0-300 mg/dL
        "4548-4":  (3.0, 16.0), # HbA1c: 3.0-16.0 %
        "2951-2":  (110, 165),  # Sodium: 110-165 mEq/L
        "2823-3":  (2.0, 7.5),  # Potassium: 2.0-7.5 mEq/L
        "17861-6": (6.0, 12.0), # Calcium: 6.0-12.0 mg/dL
        "2339-0":  (20, 600),   # Glucose (blood): 20-600 mg/dL
    }
    for lrow in _lab_rows:
        _l4code = lrow.get("LabCode", "")
        if _l4code not in _LAB004_LIMITS:
            continue
        _l4v = lrow.get("ResultValue", "")
        if not _l4v:
            continue
        try:
            _l4f = float(_l4v)
            _l4lo, _l4hi = _LAB004_LIMITS[_l4code]
            if _l4f < _l4lo or _l4f > _l4hi:
                _val_rows.append({
                    "PatientID": patient_id,
                    "EncounterNumber": lrow.get("EncounterNumber", ""),
                    "ValidationRuleID": "LAB004",
                    "Severity": "ERROR",
                    "Category": "Lab",
                    "Description": (
                        f"Physiologically implausible: {lrow.get('LabName','')} "
                        f"= {_l4v} {lrow.get('Unit','')} "
                        f"(plausibility range {_l4lo}-{_l4hi})"
                    ),
                    "Field1": "ResultValue",
                    "Value1": _l4v,
                    "Field2": "PlausibilityRange",
                    "Value2": f"{_l4lo}-{_l4hi}",
                    "AutoCorrected": "No",
                    "RecordRegenerated": "No",
                })
        except (ValueError, TypeError):
            pass

    # MED004: simultaneous ACE inhibitor + ARB active maintenance therapy
    _active_maint_classes: set = set()
    for mrow in _med_rows:
        if mrow.get("IsChronicMaintenance") == "Yes" and mrow.get("IsActiveAfterEncounter") == "Yes":
            _active_maint_classes.add(mrow.get("DrugClass", ""))
    if "ACE inhibitor" in _active_maint_classes and "ARB" in _active_maint_classes:
        _arb_meds  = [m["MedicationName"] for m in _med_rows if m.get("DrugClass") == "ARB"  and m.get("IsChronicMaintenance") == "Yes"]
        _ace_meds  = [m["MedicationName"] for m in _med_rows if m.get("DrugClass") == "ACE inhibitor" and m.get("IsChronicMaintenance") == "Yes"]
        _val_rows.append({
            "PatientID": patient_id,
            "EncounterNumber": "",
            "ValidationRuleID": "MED004",
            "Severity": "WARNING",
            "Category": "Medication",
            "Description": "Simultaneous ACE inhibitor + ARB active maintenance therapy",
            "Field1": "ACE",
            "Value1": _ace_meds[0] if _ace_meds else "",
            "Field2": "ARB",
            "Value2": _arb_meds[0] if _arb_meds else "",
            "AutoCorrected": "No",
            "RecordRegenerated": "No",
        })

    # ---- Patient row ----
    _baseline_weight = enc_vitals[0]["weight_lb"] if enc_vitals else 0
    _bmi_val = (
        round(_baseline_weight * 703 / (_patient_height_in ** 2), 1)
        if (_patient_height_in and _baseline_weight)
        else ""
    )

    _diag_codes_all = set(d.get("code", "") for d in diag_list)

    def _has_diag_prefix(*prefixes):
        return any(c.startswith(prefixes) for c in _diag_codes_all)

    _patient_row = {
        "PatientID": patient_id,
        "MRN": mrn,
        "MedicaidID": (
            f"{ins_org_code}-{patient_id:07d}" if ins_number_type == "MA" else ""
        ),
        "FirstName": given,
        "MiddleName": middle,
        "LastName": family,
        "DOB": birth_dt.strftime("%Y-%m-%d"),
        "Age": age,
        "Sex": sex,
        "Race": race_row.get("race_description", ""),
        "Ethnicity": eth_desc,
        "Street": f"{street_num} {street_name} {street_type}",
        "City": city,
        "State": state_code,
        "ZIP": zip_code,
        "County": geo.get("county", ""),
        "PrimaryFacilityCode": fac_code,
        "PrimaryFacilityName": fac_name,
        "PrimaryProviderCode": prov_code,
        "PrimaryProviderName": prov_name,
        "HeightInches": round(_patient_height_in, 1) if _patient_height_in else "",
        "BaselineWeightLbs": _baseline_weight,
        "BaselineBMI": _bmi_val,
        "HasDiabetes": "Yes" if _has_diag_prefix("E10", "E11", "E12", "E13") else "No",
        "HasHypertension": "Yes" if _has_diag_prefix("I10", "I11", "I12", "I13") else "No",
        "HasCAD": "Yes" if _has_diag_prefix("I25", "I20", "I21", "I22") else "No",
        "HasHeartFailure": "Yes" if _has_diag_prefix("I50") else "No",
        "HasAFib": "Yes" if _has_diag_prefix("I48") else "No",
        "HasAsthmaCOPD": "Yes" if _has_diag_prefix("J45", "J44") else "No",
        "TotalEncounters": n_encounters,
        "OutpatientEncounters": sum(1 for t in enc_types if t == "O"),
        "EDEncounters": sum(1 for t in enc_types if t == "E"),
        "InpatientEncounters": sum(1 for t in enc_types if t == "I"),
        "FirstEncounterDate": enc_dates[0].strftime("%Y-%m-%d") if enc_dates else "",
        "LastEncounterDate": enc_dates[-1].strftime("%Y-%m-%d") if enc_dates else "",
        "IsMultiFacilityPatient": "Yes" if len(_ordered_facs) > 1 else "No",
        "DistinctFacilityCount": len(_ordered_facs),
        "DistinctHealthSystemCount": len(set(
            _fd.get("health_system_code", _fd["code"]) for _fd in _ordered_facs
        )),
    }

    # COH001: patient condition summary flags must agree with actual diag_list codes.
    # Runs after _patient_row (uses Has* flags) and _diag_codes_all (both now available).
    _coh001_checks = [
        ("HasDiabetes",     ("E10", "E11", "E12", "E13")),
        ("HasHypertension", ("I10", "I11", "I12", "I13")),
        ("HasCAD",          ("I25", "I20", "I21", "I22")),
        ("HasHeartFailure", ("I50",)),
        ("HasAFib",         ("I48",)),
        ("HasAsthmaCOPD",   ("J45", "J44")),
    ]
    _enc_diag_codes_flat = set()
    for _er in _enc_rows:
        for _c in (_er.get("AllEncounterDiagnosisCodes") or "").split("|"):
            _c = _c.strip()
            if _c:
                _enc_diag_codes_flat.add(_c)
    for _flag_name, _prefixes in _coh001_checks:
        _flag_val = _patient_row.get(_flag_name, "No")
        _has_in_diags = any(c.startswith(_prefixes) for c in _diag_codes_all)
        _has_in_encs  = any(c.startswith(_prefixes) for c in _enc_diag_codes_flat)
        if _flag_val == "Yes" and not _has_in_diags:
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": "",
                "ValidationRuleID": "COH001",
                "Severity": "ERROR",
                "Category": "Cohort",
                "Description": f"{_flag_name}=Yes but no matching ICD-10 code in patient diagnosis list",
                "Field1": _flag_name,
                "Value1": "Yes",
                "Field2": "DiagCodes",
                "Value2": ",".join(sorted(_diag_codes_all))[:120],
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })
        elif _flag_val == "Yes" and _has_in_diags and not _has_in_encs:
            # Condition in diag_list but code never reached any encounter (rare, few encounters)
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": "",
                "ValidationRuleID": "COH001",
                "Severity": "ERROR",
                "Category": "Cohort",
                "Description": (
                    f"{_flag_name}=Yes but condition code never appears in any encounter diagnosis"
                ),
                "Field1": _flag_name,
                "Value1": "Yes",
                "Field2": "EncounterDiagCodes",
                "Value2": "",
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })
        elif _flag_val == "No" and _has_in_encs:
            _val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": "",
                "ValidationRuleID": "COH001",
                "Severity": "ERROR",
                "Category": "Cohort",
                "Description": f"{_flag_name}=No but matching ICD-10 code present in encounter diagnoses",
                "Field1": _flag_name,
                "Value1": "No",
                "Field2": "EncounterDiagCodes",
                "Value2": ",".join(
                    c for c in sorted(_enc_diag_codes_flat) if c.startswith(_prefixes)
                )[:120],
                "AutoCorrected": "No",
                "RecordRegenerated": "No",
            })

    patient_data = {
        "patient": _patient_row,
        "encounters": _enc_rows,
        "medications": _med_rows,
        "labs": _lab_rows,
        "validations": _val_rows,
        "facilities": _fac_rows,
    }

    return _result_xmls, patient_data



# ---------------------------------------------------------------------------
# Multiprocessing worker helpers for template mode
# ---------------------------------------------------------------------------

# Per-worker-process state (populated once by _worker_init via initializer=)
_worker_tmpl = None
_worker_schema = None


def _worker_init(tmpl_json_str: str, no_validate: bool):
    """Called once per worker process to load shared state."""
    global _worker_tmpl, _worker_schema
    _worker_tmpl = json.loads(tmpl_json_str)
    _worker_schema = None if no_validate else load_schema()


def _worker_generate(args):
    """Generate one patient in a worker process; return serialisable result."""
    patient_id, xml_dir_str, delete_dir_str, resume = args
    xml_dir = Path(xml_dir_str)
    delete_dir = Path(delete_dir_str)

    if resume and any(xml_dir.glob(f"patient_{patient_id:06d}_*.xml")):
        return (patient_id, "skipped", [], None)

    xml_results, patient_data = generate_from_template(patient_id, _worker_tmpl)

    if _worker_schema is not None:
        for _fc, _xml in xml_results:
            is_valid, errors = validate_xml(_xml, _worker_schema)
            if not is_valid:
                (xml_dir / f"patient_{patient_id:06d}_{_fc}.invalid.xml").write_text(
                    _xml, encoding="utf-8"
                )
                return (patient_id, "invalid", errors, None)

    for _fc, _xml in xml_results:
        (xml_dir / f"patient_{patient_id:06d}_{_fc}.xml").write_text(_xml, encoding="utf-8")
        (delete_dir / f"patient_{patient_id:06d}_{_fc}_delete.xml").write_text(
            generate_delete_sda(_xml, _fc), encoding="utf-8"
        )
    return (patient_id, "ok", [], patient_data)


def run_template_mode(count: int, output_dir: Path, template_path: str, resume: bool,
                      no_validate: bool, concurrency: int):
    import csv
    with open(template_path) as f:
        tmpl = json.load(f)

    xml_dir = output_dir / "xml"
    delete_dir = output_dir / "Delete"
    output_dir.mkdir(parents=True, exist_ok=True)
    xml_dir.mkdir(exist_ok=True)
    delete_dir.mkdir(exist_ok=True)

    meta = tmpl.get("meta", {})
    n_workers = max(1, min(concurrency, os.cpu_count() or 1))
    print(f"Template mode: {count} patients from {template_path}")
    print(f"  Population : {meta.get('name','(unnamed)')}")
    print(f"  Cohorts    : {len(tmpl.get('cohorts', []))}")
    print(f"  Facilities : {len(tmpl.get('facilities', []))}")
    validate_note = "with XSD validation" if not no_validate else "validation disabled"
    parallel_note = f"{n_workers} workers" if n_workers > 1 else "single-threaded"
    print(f"  {validate_note}, {parallel_note}")
    print()

    # Collected results keyed by patient_id; sorted before CSV write so output
    # order is deterministic regardless of which worker finishes first.
    results_by_id: dict = {}  # patient_id -> patient_data dict (or None if invalid/skipped)
    ok = invalid_count = skipped_count = 0

    def _handle_result(patient_id, status, errors, patient_data):
        nonlocal ok, invalid_count, skipped_count
        if status == "skipped":
            skipped_count += 1
            ok += 1
        elif status == "invalid":
            print(f"  [INVALID] patient_{patient_id:06d}: {len(errors)} error(s)", flush=True)
            for e in errors[:3]:
                print(f"    {e}", flush=True)
            invalid_count += 1
        else:
            ok += 1
            results_by_id[patient_id] = patient_data
        total_done = ok + invalid_count
        if total_done % 100 == 0 or total_done <= 10:
            print(f"  [OK] {ok}/{count}", flush=True)

    _VALIDATION_FIELDNAMES = [
        "PatientID", "EncounterNumber", "ValidationRuleID", "Severity",
        "Category", "Description", "Field1", "Value1", "Field2", "Value2",
        "AutoCorrected", "RecordRegenerated",
    ]

    def _write_csv(rows: list, path: Path, fieldnames: list | None = None):
        if not rows and fieldnames is None:
            return
        _fnames = fieldnames if fieldnames is not None else list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_fnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"  CSV: {path} ({len(rows)} rows)")

    if n_workers > 1:
        # Serialize the template once; each worker deserialises it in its own
        # process so the lxml schema object never crosses a process boundary.
        tmpl_json = json.dumps(tmpl)
        worker_args = [
            (pid, str(xml_dir), str(delete_dir), resume)
            for pid in range(1, count + 1)
        ]
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_worker_init,
            initargs=(tmpl_json, no_validate),
        ) as executor:
            futures = {executor.submit(_worker_generate, a): a[0] for a in worker_args}
            for future in concurrent.futures.as_completed(futures):
                patient_id, status, errors, patient_data = future.result()
                _handle_result(patient_id, status, errors, patient_data)
    else:
        # Single-process path — identical logic, no pickle overhead
        schema = None if no_validate else load_schema()
        for patient_id in range(1, count + 1):
            if resume and any(xml_dir.glob(f"patient_{patient_id:06d}_*.xml")):
                _handle_result(patient_id, "skipped", [], None)
                continue
            xml_results, patient_data = generate_from_template(patient_id, tmpl)
            if schema:
                failed = False
                for _fc, _xml in xml_results:
                    is_valid, errors = validate_xml(_xml, schema)
                    if not is_valid:
                        (xml_dir / f"patient_{patient_id:06d}_{_fc}.invalid.xml").write_text(
                            _xml, encoding="utf-8"
                        )
                        _handle_result(patient_id, "invalid", errors, None)
                        failed = True
                        break
                if failed:
                    continue
            for _fc, _xml in xml_results:
                (xml_dir / f"patient_{patient_id:06d}_{_fc}.xml").write_text(
                    _xml, encoding="utf-8"
                )
                (delete_dir / f"patient_{patient_id:06d}_{_fc}_delete.xml").write_text(
                    generate_delete_sda(_xml, _fc), encoding="utf-8"
                )
            _handle_result(patient_id, "ok", [], patient_data)

    # Sort by patient_id so CSV rows are in deterministic order
    ordered_ids = sorted(results_by_id.keys())
    all_patients = [results_by_id[i]["patient"] for i in ordered_ids]
    all_encounters = [r for i in ordered_ids for r in results_by_id[i]["encounters"]]
    all_medications = [r for i in ordered_ids for r in results_by_id[i]["medications"]]
    all_labs = [r for i in ordered_ids for r in results_by_id[i]["labs"]]
    all_validations = [r for i in ordered_ids for r in results_by_id[i]["validations"]]
    all_facilities = [r for i in ordered_ids for r in results_by_id[i]["facilities"]]

    _FACILITY_FIELDNAMES = [
        "PatientID", "FacilityCode", "FacilityName",
        "HealthSystemCode", "HealthSystemName",
        "FacilityMRN", "IsPrimaryFacility",
        "FirstEncounterDate", "LastEncounterDate", "EncounterCount",
        "ProviderCode", "ProviderName", "XMLFileName",
    ]

    print()
    _write_csv(all_patients,    output_dir / "patients.csv")
    _write_csv(all_encounters,  output_dir / "encounters.csv")
    _write_csv(all_medications, output_dir / "medications.csv")
    _write_csv(all_labs,        output_dir / "labs.csv")
    _write_csv(
        all_facilities,
        output_dir / "patient_facilities.csv",
        fieldnames=_FACILITY_FIELDNAMES,
    )
    _write_csv(
        all_validations,
        output_dir / "generator_validation.csv",
        fieldnames=_VALIDATION_FIELDNAMES,
    )

    print(f"\nDone. {ok} written, {invalid_count} invalid.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic SDA3 XML patient files from a JSON template.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--count", type=int, default=10,
                        help="Number of patients to generate (default: 10)")
    parser.add_argument("--output", default="Population",
                        help="Output directory (default: Population/)")
    parser.add_argument("--mode", choices=["template"], default="template",
                        help="Generation mode (default: template)")
    parser.add_argument("--template",
                        help="Path to template JSON produced by design_population.py (required)")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Parallel workers (default: 10)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip patients whose output files already exist")
    parser.add_argument("--no-validate", action="store_true",
                        help="Disable XSD validation even if lxml and SDA.xsd are available")
    args = parser.parse_args()

    if not args.template:
        parser.error("--template is required")

    output_dir = Path(args.output)
    delete_dir = output_dir / "Delete"
    output_dir.mkdir(parents=True, exist_ok=True)
    delete_dir.mkdir(exist_ok=True)

    if not args.resume:
        for f in output_dir.glob("patient_*.xml"):
            f.unlink()
        for f in output_dir.glob("patient_*.invalid.xml"):
            f.unlink()
        xml_sub = output_dir / "xml"
        if xml_sub.exists():
            for f in xml_sub.glob("patient_*.xml"):
                f.unlink()
            for f in xml_sub.glob("patient_*.invalid.xml"):
                f.unlink()
        for csv_name in ("patients.csv", "encounters.csv", "medications.csv",
                         "labs.csv", "generator_validation.csv"):
            csv_path = output_dir / csv_name
            if csv_path.exists():
                csv_path.unlink()
        for f in delete_dir.glob("patient_*_delete.xml"):
            f.unlink()
        print(f"Cleared {output_dir}/ and {delete_dir}/")

    run_template_mode(args.count, output_dir, args.template, args.resume,
                      args.no_validate, args.concurrency)


if __name__ == "__main__":
    main()
