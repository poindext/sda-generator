"""
Patient Record router — single-patient SDA3 XML generation.
"""
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

router = APIRouter()


class CohortRef(BaseModel):
    template_file: str = ""
    cohort_id: str = ""


class RecordRequest(BaseModel):
    scenario: str
    filename: str = ""
    model: str = "gpt-4.1"
    cohort_refs: list[CohortRef] = []
    # legacy single-cohort fields kept for backwards compat
    template_file: str = ""
    cohort_id: str = ""


class DeleteRequest(BaseModel):
    package: str


@router.post("/patient-record/delete")
async def delete_patient_record(body: DeleteRequest):
    import shutil
    from app.config import POPULATIONS_DIR

    pkg_dir = (POPULATIONS_DIR / "single-records" / body.package).resolve()
    records_resolved = (POPULATIONS_DIR / "single-records").resolve()
    if not str(pkg_dir).startswith(str(records_resolved)):
        raise HTTPException(403)
    if not pkg_dir.exists():
        raise HTTPException(404, "Package not found")
    shutil.rmtree(pkg_dir)
    return {"deleted": body.package}


@router.get("/patient-record/download-zip")
async def download_zip(path: str):
    from app.config import BASE_DIR, POPULATIONS_DIR
    full = (BASE_DIR / path).resolve()
    pop_resolved = POPULATIONS_DIR.resolve()
    if not str(full).startswith(str(pop_resolved)):
        raise HTTPException(403)
    if not full.exists():
        raise HTTPException(404)
    return FileResponse(full, media_type="application/zip", filename=full.name)


@router.get("/patient-record/list")
async def list_patient_records():
    from datetime import datetime
    from app.config import BASE_DIR, POPULATIONS_DIR

    records_dir = POPULATIONS_DIR / "single-records"
    records_dir.mkdir(parents=True, exist_ok=True)

    results = []

    # Package directories (new format — one dir per generation)
    for pkg in sorted(records_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not pkg.is_dir():
            continue
        xml_files = sorted(pkg.glob("*.xml"))
        zip_files = list(pkg.glob("*.zip"))
        zip_path = str(zip_files[0].relative_to(BASE_DIR)) if zip_files else ""
        desc_file = pkg / "description.txt"
        scenario_file = pkg / "scenario.txt"
        description = ""
        if desc_file.exists():
            description = desc_file.read_text(encoding="utf-8").strip()[:140]
        elif scenario_file.exists():
            first_line = scenario_file.read_text(encoding="utf-8").split("\n")[0].strip()
            description = first_line[:140]
        results.append({
            "name": pkg.name,
            "type": "package",
            "file_count": len(xml_files),
            "files": [f.name for f in xml_files],
            "created": datetime.fromtimestamp(pkg.stat().st_mtime).isoformat(),
            "zip_path": zip_path,
            "description": description,
        })

    # Legacy single XML files (fallback / pre-split format)
    for f in sorted(records_dir.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True):
        results.append({
            "name": f.name,
            "type": "single",
            "file_count": 1,
            "files": [f.name],
            "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            "zip_path": "",
        })

    return {"records": results}


@router.get("/patient-record/cohort-templates")
async def list_cohort_templates():
    from app.services import patient_record_svc
    return {"options": patient_record_svc.list_cohort_options()}


@router.get("/patient-record/next-scenario")
async def get_next_scenario(package: str):
    """Return the scenario_next.txt for a package (used by the UI regenerate button)."""
    from app.config import BASE_DIR, POPULATIONS_DIR
    path = (POPULATIONS_DIR / "single-records" / package / "scenario_next.txt").resolve()
    records_resolved = (POPULATIONS_DIR / "single-records").resolve()
    if not str(path).startswith(str(records_resolved)):
        raise HTTPException(403)
    if not path.exists():
        raise HTTPException(404, "No refined scenario available for this package")
    return {"scenario": path.read_text(encoding="utf-8")}


@router.get("/patient-record/summary")
async def get_patient_summary(package: str):
    """Parse all facility XMLs in a package and return a structured clinical summary."""
    from app.config import POPULATIONS_DIR
    from xml.etree import ElementTree as ET

    pkg_dir = (POPULATIONS_DIR / "single-records" / package).resolve()
    records_resolved = (POPULATIONS_DIR / "single-records").resolve()
    if not str(pkg_dir).startswith(str(records_resolved)):
        raise HTTPException(403)
    if not pkg_dir.exists():
        raise HTTPException(404, "Package not found")

    # Only parse ADD files (not DELETE files)
    xml_files = [f for f in sorted(pkg_dir.glob("*.xml"))
                 if "_DELETE" not in f.name]
    if not xml_files:
        raise HTTPException(404, "No XML files found")

    def txt(el, path, default=""):
        node = el.find(path)
        return node.text.strip() if node is not None and node.text else default

    def fmt_dt(s):
        """Trim ISO datetime to YYYY-MM-DD for display."""
        return s[:10] if s else ""

    patient = {}
    facilities = {}
    encounters, diagnoses, medications, labs, vitals, procedures, allergies, problems = (
        [], [], [], [], [], [], [], []
    )
    seen = {"enc": set(), "dx": set(), "med": set(), "lab": set(),
            "proc": set(), "allergy": set(), "prob": set()}

    for xml_file in xml_files:
        try:
            tree = ET.parse(xml_file)
        except ET.ParseError:
            continue
        root = tree.getroot()

        # Patient demographics (take from first file)
        if not patient:
            p = root.find("Patient")
            if p is not None:
                patient = {
                    "family": txt(p, "Name/FamilyName"),
                    "given":  txt(p, "Name/GivenName"),
                    "dob":    fmt_dt(txt(p, "BirthTime")),
                    "gender": txt(p, "Gender/Description") or txt(p, "Gender/Code"),
                    "race":   txt(p, "Race/Description"),
                }

        # Facilities
        facility_code = txt(root, "SendingFacility")
        if facility_code:
            # Try to get description from any EnteredAt
            fac_desc = ""
            for ea in root.iter("EnteredAt"):
                if txt(ea, "Code") == facility_code:
                    fac_desc = txt(ea, "Description")
                    break
            facilities[facility_code] = fac_desc or facility_code

        # Encounters
        for enc in root.findall(".//Encounter"):
            enc_num = txt(enc, "EncounterNumber")
            if enc_num in seen["enc"]:
                continue
            seen["enc"].add(enc_num)
            encounters.append({
                "date":     fmt_dt(txt(enc, "FromTime")),
                "type":     txt(enc, "EncounterType"),
                "facility": txt(enc, "HealthCareFacility/Code") or txt(enc, "EnteredAt/Code"),
                "provider": txt(enc, "AdmittingClinician/Description") or txt(enc, "EnteredBy/Description"),
                "enc_num":  enc_num,
            })

        # Diagnoses
        for dx in root.findall(".//Diagnosis"):
            code = txt(dx, "Diagnosis/Code")
            desc = txt(dx, "Diagnosis/Description")
            key = code or desc
            if not key or key in seen["dx"]:
                continue
            seen["dx"].add(key)
            diagnoses.append({
                "code":    code,
                "desc":    desc,
                "onset":   fmt_dt(txt(dx, "OnsetTime") or txt(dx, "EnteredOn")),
                "status":  txt(dx, "Status/Description") or txt(dx, "Status/Code") or "active",
            })

        # Problems
        for prob in root.findall(".//Problem"):
            code = txt(prob, "Problem/Code")
            desc = txt(prob, "Problem/Description")
            key = code or desc
            if not key or key in seen["prob"]:
                continue
            seen["prob"].add(key)
            problems.append({
                "code":   code,
                "desc":   desc,
                "onset":  fmt_dt(txt(prob, "FromTime") or txt(prob, "EnteredOn")),
                "status": txt(prob, "Status/Description") or txt(prob, "Status/Code") or "active",
            })

        # Medications
        for med in root.findall(".//Medication"):
            drug = txt(med, "DrugProduct/Description")
            from_t = fmt_dt(txt(med, "FromTime"))
            key = f"{drug}|{from_t}"
            if not drug or key in seen["med"]:
                continue
            seen["med"].add(key)
            status = txt(med, "Status") or "active"
            if status.lower() in ("d", "discontinued", "completed", "c"):
                continue
            medications.append({
                "drug":   drug,
                "dose":   f"{txt(med, 'Dose/Amount')} {txt(med, 'Dose/Unit')}".strip(),
                "route":  txt(med, "Route/Code"),
                "freq":   txt(med, "Frequency/Code") or txt(med, "Frequency/Description"),
                "from":   from_t,
            })

        # Allergies
        for al in root.findall(".//Allergy"):
            allergen = txt(al, "Allergy/Description") or txt(al, "Allergy/Code")
            if not allergen or allergen in seen["allergy"]:
                continue
            seen["allergy"].add(allergen)
            allergies.append({
                "allergen": allergen,
                "reaction": txt(al, "Reaction/Description"),
                "category": txt(al, "AllergyCategory/Description"),
            })

        # Observations / vitals — group by encounter date
        vital_by_enc = {}
        for obs in root.findall(".//Observation"):
            code = txt(obs, "ObservationCode/Code")
            value = txt(obs, "ObservationValue")
            if not value:
                continue
            enc_num = txt(obs, "EncounterNumber")
            date = fmt_dt(txt(obs, "ObservationTime") or txt(obs, "EnteredOn"))
            key = enc_num or date
            if key not in vital_by_enc:
                vital_by_enc[key] = {"date": date, "enc": enc_num, "items": {}}
            vital_by_enc[key]["items"][code] = value
        for v in vital_by_enc.values():
            items = v["items"]
            entry = {"date": v["date"], "enc": v["enc"]}
            bp_s = items.get("8480-6")
            bp_d = items.get("8462-4")
            if bp_s and bp_d:
                entry["bp"] = f"{bp_s}/{bp_d}"
            if "8867-4" in items:
                entry["hr"] = items["8867-4"]
            if "8302-2" in items:
                entry["ht"] = items["8302-2"]
            if "29463-7" in items:
                entry["wt"] = items["29463-7"]
            if "39156-5" in items:
                entry["bmi"] = items["39156-5"]
            if "8310-5" in items:
                entry["temp"] = items["8310-5"]
            vitals.append(entry)

        # Lab orders
        for lo in root.findall(".//LabOrder"):
            date = fmt_dt(txt(lo, "FromTime") or txt(lo, "EnteredOn"))
            for item in lo.findall(".//LabResultItem"):
                test = txt(item, "TestItemCode/Description") or txt(item, "TestItemCode/Code")
                val  = txt(item, "ResultValue")
                units = txt(item, "ResultValueUnits")
                key = f"{date}|{test}"
                if not test or key in seen["lab"]:
                    continue
                seen["lab"].add(key)
                labs.append({
                    "date":  date,
                    "test":  test,
                    "value": f"{val} {units}".strip() if units else val,
                    "flag":  txt(item, "ResultInterpretation"),
                })

        # Procedures
        for proc in root.findall(".//Procedure"):
            code = txt(proc, "Procedure/Code")
            desc = txt(proc, "Procedure/Description")
            date = fmt_dt(txt(proc, "ProcedureTime") or txt(proc, "FromTime"))
            key = f"{date}|{code or desc}"
            if not (code or desc) or key in seen["proc"]:
                continue
            seen["proc"].add(key)
            procedures.append({
                "date":     date,
                "code":     code,
                "desc":     desc,
                "provider": txt(proc, "Clinician/Description"),
            })

    # Sort collections
    encounters.sort(key=lambda x: x["date"], reverse=True)
    labs.sort(key=lambda x: x["date"], reverse=True)
    vitals.sort(key=lambda x: x["date"], reverse=True)
    procedures.sort(key=lambda x: x["date"], reverse=True)

    description = ""
    desc_file = pkg_dir / "description.txt"
    if desc_file.exists():
        description = desc_file.read_text(encoding="utf-8").strip()

    return {
        "package":    package,
        "description": description,
        "patient":    patient,
        "facilities": [{"code": k, "name": v} for k, v in facilities.items()],
        "encounters": encounters,
        "diagnoses":  diagnoses,
        "problems":   problems,
        "medications": medications,
        "allergies":  allergies,
        "vitals":     vitals,
        "labs":       labs,
        "procedures": procedures,
    }


@router.post("/patient-record/generate")
async def generate_patient_record(body: RecordRequest):
    from app.services import patient_record_svc

    async def _stream():
        # Normalise: merge legacy single-cohort fields into cohort_refs list
        refs = [{"template_file": r.template_file, "cohort_id": r.cohort_id}
                for r in body.cohort_refs]
        if not refs and body.template_file:
            refs = [{"template_file": body.template_file, "cohort_id": body.cohort_id}]
        async for event in patient_record_svc.generate_record(
            scenario=body.scenario,
            filename=body.filename or None,
            model=body.model,
            cohort_refs=refs,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
