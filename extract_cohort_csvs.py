"""
extract_cohort_csvs.py
----------------------
Reads SDA3 XML files from a cohort shard directory and writes CSV files in the
same schema as generate_population.py output, so clinicians can compare quality
across the two datasets side-by-side.

Usage:
    python3 extract_cohort_csvs.py --input cohort/shard-00000 --output cohort_csv

Output CSVs (same schema as population_1000/):
    patients.csv
    encounters.csv
    medications.csv
    labs.csv
    patient_facilities.csv
    generator_validation.csv
"""

import argparse
import csv
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REFERENCE_DATE = datetime(2026, 8, 25)

_VITAL_LOINC = {
    "8480-6": "sbp",
    "8462-4": "dbp",
    "8867-4": "hr",
    "9279-1": "rr",
    "8310-5": "temp_c",
    "39156-5": "bmi",
    "29463-7": "weight_kg",
    "3141-9": "weight_kg",
    "8302-2": "height_cm",
}

_DRUG_CLASS_MAP = {
    "metformin": "Biguanide",
    "insulin": "Insulin",
    "glargine": "Insulin",
    "lispro": "Insulin",
    "aspart": "Insulin",
    "detemir": "Insulin",
    "glipizide": "Sulfonylurea",
    "glyburide": "Sulfonylurea",
    "glimepiride": "Sulfonylurea",
    "sitagliptin": "DPP-4 inhibitor",
    "saxagliptin": "DPP-4 inhibitor",
    "alogliptin": "DPP-4 inhibitor",
    "linagliptin": "DPP-4 inhibitor",
    "empagliflozin": "SGLT-2 inhibitor",
    "dapagliflozin": "SGLT-2 inhibitor",
    "canagliflozin": "SGLT-2 inhibitor",
    "semaglutide": "GLP-1 agonist",
    "liraglutide": "GLP-1 agonist",
    "dulaglutide": "GLP-1 agonist",
    "exenatide": "GLP-1 agonist",
    "pioglitazone": "Thiazolidinedione",
    "rosiglitazone": "Thiazolidinedione",
    "lisinopril": "ACE inhibitor",
    "enalapril": "ACE inhibitor",
    "ramipril": "ACE inhibitor",
    "benazepril": "ACE inhibitor",
    "losartan": "ARB",
    "valsartan": "ARB",
    "irbesartan": "ARB",
    "olmesartan": "ARB",
    "amlodipine": "CCB",
    "nifedipine": "CCB",
    "diltiazem": "CCB",
    "verapamil": "CCB",
    "hydrochlorothiazide": "Thiazide diuretic",
    "chlorthalidone": "Thiazide diuretic",
    "furosemide": "Loop diuretic",
    "spironolactone": "Potassium-sparing diuretic",
    "atorvastatin": "Statin",
    "rosuvastatin": "Statin",
    "simvastatin": "Statin",
    "pravastatin": "Statin",
    "ezetimibe": "Cholesterol absorption inhibitor",
    "aspirin": "Antiplatelet",
    "clopidogrel": "Antiplatelet",
    "warfarin": "Anticoagulant",
    "apixaban": "Anticoagulant",
    "rivaroxaban": "Anticoagulant",
    "metoprolol": "Beta blocker",
    "atenolol": "Beta blocker",
    "carvedilol": "Beta blocker",
    "bisoprolol": "Beta blocker",
    "levothyroxine": "Thyroid hormone",
    "albuterol": "Short-acting bronchodilator",
    "fluticasone": "Inhaled corticosteroid",
    "montelukast": "Leukotriene modifier",
    "prednisone": "Corticosteroid",
    "omeprazole": "PPI",
    "pantoprazole": "PPI",
    "esomeprazole": "PPI",
}

_VALIDATION_FIELDNAMES = [
    "PatientID", "EncounterNumber", "ValidationRuleID", "Severity",
    "Category", "Description", "Field1", "Value1", "Field2", "Value2",
    "AutoCorrected", "RecordRegenerated",
]

_FACILITY_FIELDNAMES = [
    "PatientID", "FacilityCode", "FacilityName",
    "HealthSystemCode", "HealthSystemName",
    "FacilityMRN", "IsPrimaryFacility",
    "FirstEncounterDate", "LastEncounterDate", "EncounterCount",
    "ProviderCode", "ProviderName", "XMLFileName",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt(text: str) -> datetime | None:
    """Parse ISO 8601 or HL7 datetime string; return None if unparseable."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _dt_str(text: str) -> str:
    """Return ISO datetime string without Z suffix, or the original if unparseable."""
    d = _dt(text)
    return d.strftime("%Y-%m-%dT%H:%M:%S") if d else (text or "")


def _date_str(text: str) -> str:
    """Return YYYY-MM-DD date string, or '' if unparseable."""
    d = _dt(text)
    return d.strftime("%Y-%m-%d") if d else ""


def _parse_obs_value(raw: str):
    """
    Split a flat ObservationValue like '94 mmHg' or '36.5 Cel' into (numeric, unit).
    Returns (numeric_str, unit_str).  numeric_str is '' if no number found.
    """
    if not raw:
        return "", ""
    m = re.match(r'^([+-]?\d+\.?\d*)\s*(.*)', raw.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return "", raw.strip()


def _parse_ref_range(raw: str):
    """
    Parse a ResultNormalRange string like '70-99', '<30', '>40', '4.0-5.6'.
    Returns (low_str, high_str).
    """
    if not raw:
        return "", ""
    raw = raw.strip()
    # 'X-Y' range (handle negative numbers and floats)
    m = re.match(r'^([+-]?\d+\.?\d*)\s*-\s*([+-]?\d+\.?\d*)$', raw)
    if m:
        return m.group(1), m.group(2)
    # '<X'
    m = re.match(r'^<\s*([+-]?\d+\.?\d*)$', raw)
    if m:
        return "", m.group(1)
    # '>X'
    m = re.match(r'^>\s*([+-]?\d+\.?\d*)$', raw)
    if m:
        return m.group(1), ""
    return "", ""


def _infer_abnormal(value_str: str, low_str: str, high_str: str) -> str:
    """Return 'H', 'L', 'N', or '' based on value vs reference range."""
    try:
        v = float(value_str)
    except (ValueError, TypeError):
        return ""
    try:
        lo = float(low_str) if low_str else None
    except (ValueError, TypeError):
        lo = None
    try:
        hi = float(high_str) if high_str else None
    except (ValueError, TypeError):
        hi = None
    if hi is not None and v > hi:
        return "H"
    if lo is not None and v < lo:
        return "L"
    if lo is not None or hi is not None:
        return "N"
    return ""


def _drug_class(name: str) -> str:
    lower = name.lower()
    for key, cls in _DRUG_CLASS_MAP.items():
        if key in lower:
            return cls
    return ""


def _generic_ingredient(name: str) -> str:
    """Best-effort generic name from brand/description — lowercase first token."""
    m = re.match(r'^([A-Za-z]+)', name.strip())
    return m.group(1).lower() if m else name.lower().split()[0] if name.split() else ""


def _find(el, path, default=""):
    node = el.find(path)
    return (node.text or "").strip() if node is not None and node.text else default


# ---------------------------------------------------------------------------
# Per-file parser
# ---------------------------------------------------------------------------

def parse_file(xml_path: Path):
    """
    Parse one SDA3 XML file and return dicts ready for CSV writing.
    Returns (patient_row, enc_rows, med_rows, lab_rows, fac_row, val_rows)
    or None on parse error.
    """
    patient_id = int(xml_path.stem)

    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return None

    pt = root.find("Patient")
    if pt is None:
        return None

    # ------------------------------------------------------------------ #
    # Patient demographics
    # ------------------------------------------------------------------ #
    given  = _find(pt, "Name/GivenName")
    middle = _find(pt, "Name/MiddleName")
    family = _find(pt, "Name/FamilyName")
    sex    = _find(pt, "Gender/Code")
    race   = _find(pt, "Race/Description") or _find(pt, "Race/Code")
    ethnicity = _find(pt, "EthnicGroup/Description") or _find(pt, "EthnicGroup/Code")
    dob_raw = _find(pt, "BirthTime")
    dob_dt  = _dt(dob_raw)
    dob_str = dob_dt.strftime("%Y-%m-%d") if dob_dt else ""
    age = (
        (_REFERENCE_DATE - dob_dt).days // 365
        if dob_dt else ""
    )

    # Address
    addr = pt.find("Addresses/Address")
    street  = _find(addr, "Street") if addr is not None else ""
    city    = (_find(addr, "City/Description") or _find(addr, "City/Code")) if addr is not None else ""
    state   = _find(addr, "State/Code") if addr is not None else ""
    zip_    = _find(addr, "Zip/Code") if addr is not None else ""
    county  = (_find(addr, "County/Description") or _find(addr, "County/Code")) if addr is not None else ""

    # MRN and facility from PatientNumbers
    mrn = ""
    fac_code = _find(pt, "EnteredAt/Code")
    fac_name = _find(pt, "EnteredAt/Description")
    medicaid_id = ""
    for pn in pt.findall("PatientNumbers/PatientNumber"):
        num_type = _find(pn, "NumberType")
        num_val  = _find(pn, "Number")
        if num_type == "MRN" and not mrn:
            mrn = num_val
            fac_code = fac_code or _find(pn, "Organization/Code")
            fac_name = fac_name or _find(pn, "Organization/Description")
        elif num_type in ("MA", "MedicaidID", "Medicaid") and not medicaid_id:
            medicaid_id = num_val

    # Insurance type → MedicaidID detection from MemberEnrollments
    for me in root.findall(".//MemberEnrollment"):
        plan_type = _find(me, "HealthFund/PlanType")
        fund_code = _find(me, "HealthFund/HealthFund/Code")
        if plan_type.lower() in ("medicaid", "ma") or fund_code.upper() in ("MAID", "MA", "MEDICAID"):
            enroll_num = _find(me, "MemberEnrollmentNumber")
            if not medicaid_id and enroll_num:
                medicaid_id = enroll_num

    # ------------------------------------------------------------------ #
    # Encounters
    # ------------------------------------------------------------------ #
    enc_els = root.findall("Encounters/Encounter")

    enc_rows = []
    enc_vitals = {}       # {enc_num: {sbp, dbp, hr, rr, bmi, weight_kg, height_cm}}
    enc_a1c    = {}       # {enc_num: float}
    enc_glucose = {}      # {enc_num: float}
    enc_prov   = {}       # {enc_num: (code, name)}
    enc_dates  = []

    for enc in enc_els:
        en    = _find(enc, "EncounterNumber")
        etype = _find(enc, "EncounterType")
        from_raw = _find(enc, "FromTime")
        to_raw   = _find(enc, "ToTime")
        from_dt  = _dt(from_raw)
        to_dt    = _dt(to_raw)
        if from_dt:
            enc_dates.append(from_dt)
        fac_enc_code = _find(enc, "HealthCareFacility/Code") or fac_code
        fac_enc_name = _find(enc, "HealthCareFacility/Description") or fac_name
        prov_code_e  = _find(enc, "AdmittingClinician/Code") or _find(enc, "EnteredBy/Code")
        prov_name_e  = _find(enc, "AdmittingClinician/Description") or _find(enc, "EnteredBy/Description")
        enc_prov[en] = (prov_code_e, prov_name_e)

        duration = ""
        if from_dt and to_dt:
            duration = round((to_dt - from_dt).total_seconds() / 60, 1)

        is_acute = "Yes" if etype in ("I", "E") else "No"
        disposition = "Inpatient Admission" if etype == "I" else "Home"
        admit_dec = "Yes" if etype == "I" else "No"

        enc_rows.append({
            "PatientID":                  patient_id,
            "EncounterNumber":             en,
            "EncounterType":               etype,
            "EncounterStart":              _dt_str(from_raw),
            "EncounterEnd":                _dt_str(to_raw),
            "EncounterDurationMinutes":    duration,
            "FacilityCode":                fac_enc_code,
            "FacilityName":                fac_enc_name,
            "ProviderCode":                prov_code_e,
            "ProviderName":                prov_name_e,
            # Filled after processing Diagnoses / Observations / Labs / Meds below
            "PrimaryDiagnosisCode":        "",
            "PrimaryDiagnosisDescription": "",
            "AllEncounterDiagnosisCodes":  "",
            "SecondaryDiagnosisCodes":     "",
            "SecondaryDiagnosisDescriptions": "",
            "AcuteScenario":               "",
            "Disposition":                 disposition,
            "AdmissionDecision":           admit_dec,
            "InitialSBP":                  "",
            "InitialDBP":                  "",
            "InitialHR":                   "",
            "InitialWeightLbs":            "",
            "A1c":                         "",
            "Glucose":                     "",
            "BNP":                         "",
            "LinkedEncounterNumber":       "",
            "ProcedureCount":              0,
            "LabCount":                    0,
            "MedicationChangeCount":       0,
            "FollowUpDays":                "",
            "NoteSignedTime":              "",
            "NoteTemplateVariant":         etype,
            "NoteCharacterCount":          0,
            "IsAcuteEncounter":            is_acute,
            "HasMedicationEscalation":     "No",
            "HasDiabetesIntervention":     "No",
            "HasHypertensionIntervention": "No",
            "HasDispositionConflict":      "No",
            "HasVitalScenarioConflict":    "No",
            "HasTemporalConflict":         "No",
        })

    enc_row_index = {r["EncounterNumber"]: r for r in enc_rows}

    # ------------------------------------------------------------------ #
    # Observations — build per-encounter vital map
    # ------------------------------------------------------------------ #
    for obs in root.findall("Observations/Observation"):
        en   = _find(obs, "EncounterNumber")
        code = _find(obs, "ObservationCode/Code")
        raw  = _find(obs, "ObservationValue")
        key  = _VITAL_LOINC.get(code)
        if key and en:
            val_str, _ = _parse_obs_value(raw)
            if val_str:
                enc_vitals.setdefault(en, {})[key] = val_str

    # Back-fill vitals into encounter rows
    for er in enc_rows:
        en = er["EncounterNumber"]
        vit = enc_vitals.get(en, {})
        er["InitialSBP"] = vit.get("sbp", "")
        er["InitialDBP"] = vit.get("dbp", "")
        er["InitialHR"]  = vit.get("hr", "")
        # Weight: convert kg → lbs if available
        wt_kg = vit.get("weight_kg", "")
        if wt_kg:
            try:
                er["InitialWeightLbs"] = round(float(wt_kg) * 2.20462, 1)
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # Diagnoses — group by encounter
    # ------------------------------------------------------------------ #
    all_dx_codes = set()   # patient-level for HasXxx flags
    enc_dx = {}            # {enc_num: [(code, desc)]}
    for dx in root.findall("Diagnoses/Diagnosis"):
        en   = _find(dx, "EncounterNumber")
        code = _find(dx, "Diagnosis/Code")
        desc = _find(dx, "Diagnosis/Description")
        if code:
            all_dx_codes.add(code)
        if en and code:
            enc_dx.setdefault(en, []).append((code, desc))

    # Also collect from Problems (SNOMED — map to HasXxx)
    for prob in root.findall("Problems/Problem"):
        code = _find(prob, "Problem/Code")
        if code:
            all_dx_codes.add(code)

    # Back-fill diagnosis fields into encounter rows
    for er in enc_rows:
        en = er["EncounterNumber"]
        dxs = enc_dx.get(en, [])
        if dxs:
            er["PrimaryDiagnosisCode"]        = dxs[0][0]
            er["PrimaryDiagnosisDescription"] = dxs[0][1]
            all_codes = "|".join(d[0] for d in dxs)
            er["AllEncounterDiagnosisCodes"]  = all_codes
            if len(dxs) > 1:
                er["SecondaryDiagnosisCodes"]        = "|".join(d[0] for d in dxs[1:])
                er["SecondaryDiagnosisDescriptions"] = "|".join(d[1] for d in dxs[1:])

    def _has_prefix(*prefixes):
        return any(c.startswith(prefixes) for c in all_dx_codes)

    # SNOMED code for T2DM: 44054006; HTN: 38341003; CAD: 53741008
    def _has_snomed(*codes):
        return any(c in all_dx_codes for c in codes)

    # ------------------------------------------------------------------ #
    # Lab orders
    # ------------------------------------------------------------------ #
    med_seq = [0]
    lab_rows = []
    enc_lab_count = {}   # {enc_num: count}
    lab_seq = [0]

    for lo in root.findall("LabOrders/LabOrder"):
        en = _find(lo, "EncounterNumber")
        ordered_by_code = _find(lo, "OrderedBy/Code")
        result = lo.find("Result")
        if result is None:
            continue
        result_time = _find(result, "ResultTime")
        for item in result.findall("ResultItems/LabResultItem"):
            lab_code = _find(item, "TestItemCode/Code")
            lab_name = _find(item, "TestItemCode/Description")
            val_raw  = _find(item, "ResultValue")
            units    = _find(item, "ResultValueUnits")
            ref_raw  = _find(item, "ResultNormalRange")
            interp   = _find(item, "ResultInterpretation")

            lo_ref, hi_ref = _parse_ref_range(ref_raw)
            abnormal = interp if interp else _infer_abnormal(val_raw, lo_ref, hi_ref)

            lab_seq[0] += 1
            lab_rows.append({
                "PatientID":     patient_id,
                "EncounterNumber": en,
                "LabEventID":    f"{patient_id:06d}-L{lab_seq[0]:03d}",
                "LabCode":       lab_code,
                "LabName":       lab_name,
                "ResultValue":   val_raw,
                "Unit":          units,
                "ReferenceLow":  lo_ref,
                "ReferenceHigh": hi_ref,
                "ResultDateTime": _dt_str(result_time),
                "AbnormalFlag":  abnormal,
            })

            # Capture A1c and glucose for encounter row
            if lab_code == "4548-4" and val_raw and en:
                try:
                    enc_a1c[en] = max(enc_a1c.get(en, 0.0), float(val_raw))
                except ValueError:
                    pass
            if lab_code in ("2339-0", "2345-7", "1558-6") and val_raw and en:
                try:
                    enc_glucose[en] = float(val_raw)
                except ValueError:
                    pass

        if en:
            enc_lab_count[en] = enc_lab_count.get(en, 0) + 1

    # Back-fill A1c / glucose / lab count
    for er in enc_rows:
        en = er["EncounterNumber"]
        if en in enc_a1c:
            er["A1c"] = enc_a1c[en]
        if en in enc_glucose:
            er["Glucose"] = enc_glucose[en]
        er["LabCount"] = enc_lab_count.get(en, 0)

    # ------------------------------------------------------------------ #
    # Medications
    # ------------------------------------------------------------------ #
    med_rows = []
    enc_med_count = {}

    for med in root.findall("Medications/Medication"):
        en       = _find(med, "EncounterNumber")
        drug     = _find(med, "DrugProduct/Description")
        dose_qty = _find(med, "DoseQuantity")
        dose_uom = _find(med, "DoseUoM/Code") or _find(med, "DoseUoM/Description")
        freq     = _find(med, "Frequency/Code") or _find(med, "Frequency/Description")
        route    = _find(med, "Route/Code") or _find(med, "Route/Description")
        start    = _find(med, "FromTime")
        end_t    = _find(med, "ToTime")

        med_seq[0] += 1
        med_rows.append({
            "PatientID":               patient_id,
            "EncounterNumber":          en,
            "MedicationEventID":        f"{patient_id:06d}-M{med_seq[0]:03d}",
            "MedicationName":           drug,
            "GenericIngredient":        _generic_ingredient(drug),
            "DrugClass":                _drug_class(drug),
            "DoseValue":                dose_qty,
            "DoseUnit":                 dose_uom,
            "Frequency":                freq,
            "Route":                    route,
            "StartDateTime":            _date_str(start),
            "EndDateTime":              _date_str(end_t),
            "Action":                   "Add",
            "PreviousDoseValue":        "",
            "NewDoseValue":             "",
            "IsActiveAfterEncounter":   "Yes",
            "IsChronicMaintenance":     "No",
            "IsDuplicateActiveIngredient": "No",
            "IsInvalidDoseChange":      "No",
        })

        if en:
            enc_med_count[en] = enc_med_count.get(en, 0) + 1

    # Back-fill medication counts
    for er in enc_rows:
        er["MedicationChangeCount"] = enc_med_count.get(er["EncounterNumber"], 0)

    # ------------------------------------------------------------------ #
    # Procedures
    # ------------------------------------------------------------------ #
    enc_proc_count = {}
    for proc in root.findall("Procedures/Procedure"):
        en = _find(proc, "EncounterNumber")
        if en:
            enc_proc_count[en] = enc_proc_count.get(en, 0) + 1
    for er in enc_rows:
        er["ProcedureCount"] = enc_proc_count.get(er["EncounterNumber"], 0)

    # ------------------------------------------------------------------ #
    # BMI / height / weight for patient row (first encounter values)
    # ------------------------------------------------------------------ #
    # Use first encounter's vitals for baseline
    first_enc_num = enc_rows[0]["EncounterNumber"] if enc_rows else ""
    first_vit     = enc_vitals.get(first_enc_num, {})
    bmi_val       = first_vit.get("bmi", "")
    weight_kg     = first_vit.get("weight_kg", "")
    height_cm     = first_vit.get("height_cm", "")

    # Convert weight to lbs
    try:
        baseline_weight_lbs = round(float(weight_kg) * 2.20462, 1)
    except (ValueError, TypeError):
        baseline_weight_lbs = ""

    # Height in inches
    try:
        height_in = round(float(height_cm) / 2.54, 1)
    except (ValueError, TypeError):
        height_in = ""

    # BMI as float
    try:
        bmi_float = round(float(bmi_val), 1)
    except (ValueError, TypeError):
        bmi_float = ""

    # ------------------------------------------------------------------ #
    # Provider for patient row = first encounter's provider
    # ------------------------------------------------------------------ #
    first_prov = enc_prov.get(first_enc_num, ("", ""))

    # ------------------------------------------------------------------ #
    # Encounter summary stats
    # ------------------------------------------------------------------ #
    n_enc = len(enc_rows)
    n_out = sum(1 for e in enc_rows if e["EncounterType"] == "O")
    n_ed  = sum(1 for e in enc_rows if e["EncounterType"] == "E")
    n_ip  = sum(1 for e in enc_rows if e["EncounterType"] == "I")
    first_enc_date = _date_str(enc_rows[0]["EncounterStart"])  if enc_rows else ""
    last_enc_date  = _date_str(enc_rows[-1]["EncounterStart"]) if enc_rows else ""

    # ------------------------------------------------------------------ #
    # Patient row
    # ------------------------------------------------------------------ #
    patient_row = {
        "PatientID":               patient_id,
        "MRN":                     mrn,
        "MedicaidID":              medicaid_id,
        "FirstName":               given,
        "MiddleName":              middle,
        "LastName":                family,
        "DOB":                     dob_str,
        "Age":                     age,
        "Sex":                     sex,
        "Race":                    race,
        "Ethnicity":               ethnicity,
        "Street":                  street,
        "City":                    city,
        "State":                   state,
        "ZIP":                     zip_,
        "County":                  county,
        "PrimaryFacilityCode":     fac_code,
        "PrimaryFacilityName":     fac_name,
        "PrimaryProviderCode":     first_prov[0],
        "PrimaryProviderName":     first_prov[1],
        "HeightInches":            height_in,
        "BaselineWeightLbs":       baseline_weight_lbs,
        "BaselineBMI":             bmi_float,
        "HasDiabetes":             "Yes" if (_has_prefix("E10", "E11", "E12", "E13")
                                              or _has_snomed("44054006", "73211009", "9859006")) else "No",
        "HasHypertension":         "Yes" if (_has_prefix("I10", "I11", "I12", "I13")
                                              or _has_snomed("38341003", "59621000")) else "No",
        "HasCAD":                  "Yes" if (_has_prefix("I25", "I20", "I21", "I22")
                                              or _has_snomed("53741008", "414545008")) else "No",
        "HasHeartFailure":         "Yes" if (_has_prefix("I50")
                                              or _has_snomed("84114007", "10335000")) else "No",
        "HasAFib":                 "Yes" if (_has_prefix("I48")
                                              or _has_snomed("49436004")) else "No",
        "HasAsthmaCOPD":           "Yes" if (_has_prefix("J45", "J44")
                                              or _has_snomed("195967001", "13645005")) else "No",
        "TotalEncounters":         n_enc,
        "OutpatientEncounters":    n_out,
        "EDEncounters":            n_ed,
        "InpatientEncounters":     n_ip,
        "FirstEncounterDate":      first_enc_date,
        "LastEncounterDate":       last_enc_date,
        "IsMultiFacilityPatient":  "No",   # single-facility cohort
        "DistinctFacilityCount":   1,
        "DistinctHealthSystemCount": 1,
    }

    # ------------------------------------------------------------------ #
    # Patient-facilities row
    # ------------------------------------------------------------------ #
    fac_row = {
        "PatientID":          patient_id,
        "FacilityCode":       fac_code,
        "FacilityName":       fac_name,
        "HealthSystemCode":   "",
        "HealthSystemName":   "",
        "FacilityMRN":        mrn,
        "IsPrimaryFacility":  "Yes",
        "FirstEncounterDate": first_enc_date,
        "LastEncounterDate":  last_enc_date,
        "EncounterCount":     n_enc,
        "ProviderCode":       first_prov[0],
        "ProviderName":       first_prov[1],
        "XMLFileName":        xml_path.name,
    }

    # ------------------------------------------------------------------ #
    # Validation rows
    # ------------------------------------------------------------------ #
    val_rows = []

    # COH001: no encounters
    if n_enc == 0:
        val_rows.append({
            "PatientID": patient_id, "EncounterNumber": "",
            "ValidationRuleID": "COH001", "Severity": "ERROR",
            "Category": "Encounter",
            "Description": "Patient has no encounters",
            "Field1": "TotalEncounters", "Value1": "0",
            "Field2": "", "Value2": "",
            "AutoCorrected": "No", "RecordRegenerated": "No",
        })

    # COH002: impossible BP (SBP < DBP) per encounter
    for er in enc_rows:
        sbp, dbp = er.get("InitialSBP", ""), er.get("InitialDBP", "")
        if sbp and dbp:
            try:
                if float(sbp) < float(dbp):
                    val_rows.append({
                        "PatientID": patient_id,
                        "EncounterNumber": er["EncounterNumber"],
                        "ValidationRuleID": "COH002", "Severity": "ERROR",
                        "Category": "Vitals",
                        "Description": "Systolic BP is less than diastolic BP",
                        "Field1": "InitialSBP", "Value1": sbp,
                        "Field2": "InitialDBP", "Value2": dbp,
                        "AutoCorrected": "No", "RecordRegenerated": "No",
                    })
            except ValueError:
                pass

    # COH003: A1c out of plausible range (2.0–20.0)
    for lr in lab_rows:
        if lr["LabCode"] == "4548-4":
            try:
                v = float(lr["ResultValue"])
                if v < 2.0 or v > 20.0:
                    val_rows.append({
                        "PatientID": patient_id,
                        "EncounterNumber": lr["EncounterNumber"],
                        "ValidationRuleID": "COH003", "Severity": "ERROR",
                        "Category": "Lab",
                        "Description": f"A1c value out of plausible range: {v}%",
                        "Field1": "ResultValue", "Value1": str(v),
                        "Field2": "LabCode", "Value2": "4548-4",
                        "AutoCorrected": "No", "RecordRegenerated": "No",
                    })
            except ValueError:
                pass

    # COH004: encounter end before start
    for er in enc_rows:
        start_dt = _dt(er.get("EncounterStart", ""))
        end_dt   = _dt(er.get("EncounterEnd", ""))
        if start_dt and end_dt and end_dt < start_dt:
            val_rows.append({
                "PatientID": patient_id,
                "EncounterNumber": er["EncounterNumber"],
                "ValidationRuleID": "COH004", "Severity": "ERROR",
                "Category": "Encounter",
                "Description": "Encounter end time is before start time",
                "Field1": "EncounterStart", "Value1": er["EncounterStart"],
                "Field2": "EncounterEnd",   "Value2": er["EncounterEnd"],
                "AutoCorrected": "No", "RecordRegenerated": "No",
            })

    # COH005: lab result date outside encounter window
    for lr in lab_rows:
        en = lr["EncounterNumber"]
        er = enc_row_index.get(en)
        if er and lr["ResultDateTime"]:
            res_dt   = _dt(lr["ResultDateTime"])
            start_dt = _dt(er.get("EncounterStart", ""))
            if res_dt and start_dt and res_dt.date() < start_dt.date():
                val_rows.append({
                    "PatientID": patient_id,
                    "EncounterNumber": en,
                    "ValidationRuleID": "COH005", "Severity": "WARNING",
                    "Category": "Lab",
                    "Description": "Lab result date is before encounter start date",
                    "Field1": "ResultDateTime", "Value1": lr["ResultDateTime"],
                    "Field2": "EncounterStart", "Value2": er["EncounterStart"],
                    "AutoCorrected": "No", "RecordRegenerated": "No",
                })

    return patient_row, enc_rows, med_rows, lab_rows, fac_row, val_rows


# ---------------------------------------------------------------------------
# Worker wrapper (top-level so it's picklable for multiprocessing)
# ---------------------------------------------------------------------------

def _worker(path_str: str):
    return parse_file(Path(path_str))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _write_csv(rows: list, path: Path, fieldnames: list | None = None):
    if not rows and fieldnames is None:
        return
    fnames = fieldnames if fieldnames is not None else list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {path} ({len(rows)} rows)")


def main():
    parser = argparse.ArgumentParser(description="Extract CSVs from a cohort SDA3 XML shard")
    parser.add_argument("--input",   required=True, help="Directory containing XML files")
    parser.add_argument("--output",  required=True, help="Output directory for CSV files")
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers (default: 8)")
    parser.add_argument("--limit",   type=int, default=0,
                        help="Process only first N files (0 = all, for testing)")
    args = parser.parse_args()

    input_dir  = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    xml_files = sorted(input_dir.glob("*.xml"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0)
    if args.limit:
        xml_files = xml_files[:args.limit]

    print(f"Extracting CSVs from {len(xml_files)} files in {input_dir}")
    print(f"Output: {output_dir}  |  Workers: {args.workers}")

    all_patients   = []
    all_encounters = []
    all_medications = []
    all_labs       = []
    all_facilities = []
    all_validations = []
    errors = 0

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_worker, str(p)): p for p in xml_files}
        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()
            if result is None:
                errors += 1
            else:
                patient_row, enc_rows, med_rows, lab_rows, fac_row, val_rows = result
                all_patients.append(patient_row)
                all_encounters.extend(enc_rows)
                all_medications.extend(med_rows)
                all_labs.extend(lab_rows)
                all_facilities.append(fac_row)
                all_validations.extend(val_rows)
            if done % 500 == 0:
                print(f"  processed {done}/{len(xml_files)} ...", flush=True)

    # Sort everything by PatientID for deterministic output
    all_patients.sort(key=lambda r: r["PatientID"])
    all_encounters.sort(key=lambda r: (r["PatientID"], r["EncounterStart"]))
    all_medications.sort(key=lambda r: (r["PatientID"], r["MedicationEventID"]))
    all_labs.sort(key=lambda r: (r["PatientID"], r["LabEventID"]))
    all_facilities.sort(key=lambda r: r["PatientID"])
    all_validations.sort(key=lambda r: (r["PatientID"], r["ValidationRuleID"]))

    print()
    _write_csv(all_patients,    output_dir / "patients.csv")
    _write_csv(all_encounters,  output_dir / "encounters.csv")
    _write_csv(all_medications, output_dir / "medications.csv")
    _write_csv(all_labs,        output_dir / "labs.csv")
    _write_csv(all_facilities,  output_dir / "patient_facilities.csv",
               fieldnames=_FACILITY_FIELDNAMES)
    _write_csv(all_validations, output_dir / "generator_validation.csv",
               fieldnames=_VALIDATION_FIELDNAMES)

    print(f"\nDone. {len(all_patients)} patients, {errors} parse errors.")
    if all_validations:
        from collections import Counter
        by_rule = Counter(v["ValidationRuleID"] for v in all_validations)
        print("Validation findings:")
        for rule, cnt in sorted(by_rule.items()):
            print(f"  {rule}: {cnt}")


if __name__ == "__main__":
    main()
