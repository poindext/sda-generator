"""
Populations router — list, detail, download, delete.
Download is gated: if QA was requested, the population must be approved first.
"""
import asyncio
import csv
import io
import json
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.config import POPULATIONS_DIR, TEMPLATES_DIR
from app.services import job_store, fixer_svc
from app.services.chunker_svc import list_chunks

router = APIRouter()


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------

@router.get("/populations")
async def list_populations():
    pops = []
    for d in sorted(POPULATIONS_DIR.iterdir(),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        meta = _read_meta(d) or _infer_meta(d)
        if not meta:
            continue
        pops.append(_pop_summary(d, meta))
    return {"populations": pops}


# --------------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------------

@router.get("/populations/{pop_id}")
async def get_population(pop_id: str):
    d = _find(pop_id)
    meta = _read_meta(d) or _infer_meta(d)
    if not meta:
        raise HTTPException(404, "Population has no recognisable metadata")
    detail = _pop_summary(d, meta)

    # Patient count from CSV
    patients_csv = d / f"{pop_id}_patients.csv"
    if patients_csv.exists():
        with open(patients_csv, newline="", encoding="utf-8") as f:
            detail["patient_count"] = sum(1 for _ in csv.reader(f)) - 1
    else:
        detail["patient_count"] = 0

    # QA issues
    qa_file = d / "qa_issues.json"
    if qa_file.exists():
        qa_data = json.loads(qa_file.read_text(encoding="utf-8"))
        detail["qa_issues"] = qa_data.get("issues", [])
        detail["qa_summary"] = qa_data.get("summary", {})
    else:
        detail["qa_issues"] = []
        detail["qa_summary"] = {}

    # Validation warnings
    val_csv = d / f"{pop_id}_generator_validation.csv"
    if val_csv.exists():
        with open(val_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        detail["validation_warnings"] = len(rows)
    else:
        detail["validation_warnings"] = 0

    # Chunks
    detail["chunks"] = list_chunks(d)

    return detail


# --------------------------------------------------------------------------
# Population stats
# --------------------------------------------------------------------------

@router.get("/populations/{pop_id}/stats")
async def get_population_stats(pop_id: str):
    d = _find(pop_id)
    return await asyncio.to_thread(_compute_stats, d)


def _compute_stats(pop_dir: Path) -> dict:
    stats: dict = {}
    _pfx = pop_dir.name

    # ── patients.csv ──────────────────────────────────────────
    pat_csv = pop_dir / f"{_pfx}_patients.csv"
    if pat_csv.exists():
        sex_counts: dict = {}
        age_buckets = {"0–17": 0, "18–44": 0, "45–64": 0, "65–74": 0, "75+": 0}
        race_counts: dict = {}
        county_counts: dict = {}
        condition_totals = {
            "Diabetes": 0, "Hypertension": 0, "CAD": 0,
            "Heart Failure": 0, "AFib": 0, "Asthma/COPD": 0,
        }
        cond_fields = {
            "Diabetes": "HasDiabetes", "Hypertension": "HasHypertension",
            "CAD": "HasCAD", "Heart Failure": "HasHeartFailure",
            "AFib": "HasAFib", "Asthma/COPD": "HasAsthmaCOPD",
        }
        multi_fac = 0
        total_patients = 0

        with open(pat_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total_patients += 1
                sex_counts[row.get("Sex", "?")] = \
                    sex_counts.get(row.get("Sex", "?"), 0) + 1
                try:
                    age = int(float(row.get("Age", 0)))
                    if age < 18:
                        age_buckets["0–17"] += 1
                    elif age < 45:
                        age_buckets["18–44"] += 1
                    elif age < 65:
                        age_buckets["45–64"] += 1
                    elif age < 75:
                        age_buckets["65–74"] += 1
                    else:
                        age_buckets["75+"] += 1
                except ValueError:
                    pass
                race = row.get("Race", "Unknown") or "Unknown"
                race_counts[race] = race_counts.get(race, 0) + 1
                county = row.get("County", "Unknown") or "Unknown"
                county_counts[county] = county_counts.get(county, 0) + 1
                for label, field in cond_fields.items():
                    if row.get(field, "").strip().lower() in ("1", "true", "yes"):
                        condition_totals[label] += 1
                if row.get("IsMultiFacilityPatient", "").strip().lower() \
                        in ("1", "true", "yes"):
                    multi_fac += 1

        top_counties = sorted(county_counts.items(),
                              key=lambda x: x[1], reverse=True)[:10]
        stats["patients"] = {
            "total": total_patients,
            "sex": sex_counts,
            "age_buckets": age_buckets,
            "race": dict(sorted(race_counts.items(),
                                key=lambda x: x[1], reverse=True)),
            "conditions": condition_totals,
            "multi_facility_pct": round(multi_fac / total_patients * 100, 1)
                                  if total_patients else 0,
            "top_counties": [{"county": c, "count": n}
                             for c, n in top_counties],
        }

    # ── encounters.csv ────────────────────────────────────────
    enc_csv = pop_dir / f"{_pfx}_encounters.csv"
    if enc_csv.exists():
        enc_types: dict = {}
        dx_counts: dict = {}
        total_enc = 0
        acute_count = 0
        dates = []

        with open(enc_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total_enc += 1
                t = row.get("EncounterType", "?")
                enc_types[t] = enc_types.get(t, 0) + 1
                dx = row.get("PrimaryDiagnosisDescription", "").strip()
                if dx:
                    dx_counts[dx] = dx_counts.get(dx, 0) + 1
                if row.get("IsAcuteEncounter", "").strip().lower() \
                        in ("1", "true", "yes"):
                    acute_count += 1
                d = row.get("EncounterStart", "")
                if d:
                    dates.append(d[:10])

        top_dx = sorted(dx_counts.items(),
                        key=lambda x: x[1], reverse=True)[:10]
        enc_type_labels = {"O": "Outpatient", "E": "Emergency", "I": "Inpatient"}
        stats["encounters"] = {
            "total": total_enc,
            "by_type": {enc_type_labels.get(k, k): v
                        for k, v in enc_types.items()},
            "acute_pct": round(acute_count / total_enc * 100, 1)
                         if total_enc else 0,
            "avg_per_patient": round(
                total_enc / stats["patients"]["total"], 1)
                if stats.get("patients", {}).get("total") else 0,
            "date_range": {
                "earliest": min(dates) if dates else None,
                "latest":   max(dates) if dates else None,
            },
            "top_diagnoses": [{"dx": d, "count": n} for d, n in top_dx],
        }

    # ── medications.csv ───────────────────────────────────────
    med_csv = pop_dir / f"{_pfx}_medications.csv"
    if med_csv.exists():
        med_counts: dict = {}
        drug_class_counts: dict = {}
        total_meds = 0
        with open(med_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total_meds += 1
                name = row.get("MedicationName", "").strip()
                if name:
                    med_counts[name] = med_counts.get(name, 0) + 1
                cls = row.get("DrugClass", "").strip()
                if cls:
                    drug_class_counts[cls] = \
                        drug_class_counts.get(cls, 0) + 1
        stats["medications"] = {
            "total_events": total_meds,
            "top_medications": [{"name": n, "count": c}
                                for n, c in sorted(med_counts.items(),
                                    key=lambda x: x[1],
                                    reverse=True)[:10]],
            "top_drug_classes": [{"class": c, "count": n}
                                 for c, n in sorted(
                                     drug_class_counts.items(),
                                     key=lambda x: x[1],
                                     reverse=True)[:8]],
        }

    # ── labs.csv ──────────────────────────────────────────────
    lab_csv = pop_dir / f"{_pfx}_labs.csv"
    if lab_csv.exists():
        lab_counts: dict = {}
        abnormal = 0
        total_labs = 0
        with open(lab_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total_labs += 1
                name = row.get("LabName", "").strip()
                if name:
                    lab_counts[name] = lab_counts.get(name, 0) + 1
                if row.get("AbnormalFlag", "").strip():
                    abnormal += 1
        stats["labs"] = {
            "total_results": total_labs,
            "abnormal_pct": round(abnormal / total_labs * 100, 1)
                            if total_labs else 0,
            "top_labs": [{"name": n, "count": c}
                         for n, c in sorted(lab_counts.items(),
                             key=lambda x: x[1],
                             reverse=True)[:10]],
        }

    return stats


# --------------------------------------------------------------------------
# Download chunk
# --------------------------------------------------------------------------

@router.get("/populations/{pop_id}/chunks/{chunk_name}")
async def download_chunk(pop_id: str, chunk_name: str):
    d = _find(pop_id)
    meta = _read_meta(d)

    if not _is_downloadable(meta):
        raise HTTPException(
            403,
            "Population is not yet downloadable — QA review is required and has not been approved.",
        )

    chunk_path = d / "chunks" / chunk_name
    if not chunk_path.exists() or chunk_path.suffix != ".zip":
        raise HTTPException(404, "Chunk not found")

    return FileResponse(
        path=str(chunk_path),
        filename=chunk_name,
        media_type="application/zip",
    )


# --------------------------------------------------------------------------
# CSV downloads
# --------------------------------------------------------------------------

_CSV_BASES = [
    "patients", "encounters", "diagnoses", "medications", "labs",
    "observations", "procedures", "allergies", "vaccinations",
    "illness_histories", "social_histories", "family_histories",
    "radiology_orders", "documents", "patient_facilities",
    "generator_validation",
]


def _csv_files(pop_id: str) -> list[str]:
    return [f"{pop_id}_{b}.csv" for b in _CSV_BASES]


@router.get("/populations/{pop_id}/csvs")
async def list_csvs(pop_id: str):
    d = _find(pop_id)
    files = []
    for name in _csv_files(pop_id):
        p = d / name
        if p.exists():
            files.append({"name": name, "size_kb": round(p.stat().st_size / 1024, 1)})
    return {"files": files}


@router.get("/populations/{pop_id}/csvs/{filename}")
async def download_csv(pop_id: str, filename: str):
    d = _find(pop_id)
    if filename not in _csv_files(pop_id):
        raise HTTPException(400, f"Invalid CSV filename: {filename}")
    p = d / filename
    if not p.exists():
        raise HTTPException(404, f"{filename} not found")
    return FileResponse(
        path=str(p),
        filename=filename,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/populations/{pop_id}/csvs.zip")
async def download_csvs_zip(pop_id: str):
    d = _find(pop_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in _csv_files(pop_id):
            p = d / name
            if p.exists():
                zf.write(p, arcname=name)
    buf.seek(0)
    zip_name = f"{pop_id}_csvs.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


# --------------------------------------------------------------------------
# Auto-fix
# --------------------------------------------------------------------------

@router.post("/populations/{pop_id}/fix")
async def fix_population(pop_id: str):
    """Start a fix-regenerate-QA loop for a population in needs_review state."""
    d = _find(pop_id)
    meta = _read_meta(d)
    if not meta:
        raise HTTPException(404, "Population metadata not found")
    if meta.get("qa_status") != "needs_review":
        raise HTTPException(
            400,
            f"Population is not in 'needs_review' state (current: {meta.get('qa_status')})",
        )

    template_id = meta.get("template_id", "")
    template_path = TEMPLATES_DIR / f"{template_id}.template.json"
    if not template_path.exists():
        raise HTTPException(404, f"Template not found: {template_id}")

    try:
        tmpl = json.loads(template_path.read_text(encoding="utf-8"))
        history_months = int(tmpl.get("meta", {}).get("history_months", 24))
    except Exception:
        history_months = 24

    job = job_store.create_job("fix", {
        "pop_id": pop_id,
        "template_id": template_id,
        "output_dir": str(d),
    })

    await fixer_svc.start_fix(
        job_id=job.id,
        template_path=str(template_path),
        output_dir=str(d),
        count=meta.get("count", 100),
        history_months=history_months,
    )

    return {"job_id": job.id}


# --------------------------------------------------------------------------
# Re-run QA only (no regeneration)
# --------------------------------------------------------------------------

@router.post("/populations/{pop_id}/reqa")
async def reqa_population(pop_id: str):
    """Re-run QA on an existing population without regenerating it."""
    from app.services import generator_svc
    d = _find(pop_id)
    meta = _read_meta(d) or _infer_meta(d)
    if not meta:
        raise HTTPException(404, "Population metadata not found")

    template_id = meta.get("template_id", "")
    template_path = TEMPLATES_DIR / f"{template_id}.template.json" if template_id else None

    job = job_store.create_job("reqa", {"pop_id": pop_id, "output_dir": str(d)})

    async def _run():
        from app.services import qa_svc, chunker_svc
        job_store.update_status(job.id, "running")
        job_store.append_progress(job.id, "── Re-running QA (no regeneration) ─────────────")
        approved = await asyncio.to_thread(
            qa_svc.run_qa_sync, job.id,
            str(template_path) if template_path and template_path.exists() else "",
            str(d),
        )
        m = _read_meta(d) or meta.copy()
        if approved:
            m["qa_status"] = "approved"
            if not m.get("chunks_built"):
                job_store.append_progress(job.id, "Building download chunks…")
                await asyncio.to_thread(chunker_svc.build_chunks, d)
                m["chunks_built"] = True
            if (d / "_meta.json").exists() or meta is not m:
                generator_svc._write_meta(d, m)
            job_store.update_status(job.id, "completed", result={"qa_status": "approved"})
        else:
            m["qa_status"] = "needs_review"
            if (d / "_meta.json").exists():
                generator_svc._write_meta(d, m)
            job_store.update_status(job.id, "completed", result={"qa_status": "needs_review"})

    asyncio.create_task(_run())
    return {"job_id": job.id}


# --------------------------------------------------------------------------
# Override QA — force-approve and unlock download without a clean QA pass
# --------------------------------------------------------------------------

@router.post("/populations/{pop_id}/override-qa")
async def override_qa(pop_id: str):
    """Mark population as QA-overridden and build chunks so download is unlocked."""
    from app.services import chunker_svc, generator_svc
    d = _find(pop_id)
    meta = _read_meta(d) or _infer_meta(d)
    if not meta:
        raise HTTPException(404, "Population metadata not found")
    if meta.get("qa_status") != "needs_review":
        raise HTTPException(
            400,
            f"Only 'needs_review' populations can be overridden (current: {meta.get('qa_status')})",
        )
    meta["qa_status"] = "overridden"
    if not meta.get("chunks_built"):
        await asyncio.to_thread(chunker_svc.build_chunks, d)
        meta["chunks_built"] = True
    generator_svc._write_meta(d, meta)
    return {"qa_status": "overridden", "downloadable": True}


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------

@router.delete("/populations/{pop_id}")
async def delete_population(pop_id: str):
    d = _find(pop_id)
    job_store.delete_jobs_for_population(str(d))
    shutil.rmtree(d)
    return {"deleted": True, "id": pop_id}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _find(pop_id: str) -> Path:
    d = POPULATIONS_DIR / pop_id
    if not d.is_dir():
        raise HTTPException(404, f"Population not found: {pop_id}")
    return d


def _read_meta(pop_dir: Path) -> dict | None:
    p = pop_dir / "_meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _infer_meta(pop_dir: Path) -> dict | None:
    """Build minimal metadata for CLI-generated populations that have no _meta.json."""
    pop_id = pop_dir.name
    patients_csv = pop_dir / f"{pop_id}_patients.csv"
    if not patients_csv.exists():
        return None  # not a population directory

    try:
        with open(patients_csv, newline="", encoding="utf-8") as f:
            count = sum(1 for _ in f) - 1  # subtract header row
    except Exception:
        count = 0

    qa_file = pop_dir / "qa_issues.json"
    if qa_file.exists():
        try:
            issues = json.loads(qa_file.read_text(encoding="utf-8")).get("issues", [])
            qa_status = "approved" if not issues else "needs_review"
        except Exception:
            qa_status = "needs_review"
        run_qa = True
    else:
        qa_status = "skipped"
        run_qa = False

    chunks_built = (pop_dir / "chunks").is_dir() and any(
        (pop_dir / "chunks").iterdir()
    )

    return {
        "population_name": pop_dir.name,
        "template_id": "",
        "count": count,
        "run_qa": run_qa,
        "qa_status": qa_status,
        "chunks_built": chunks_built,
        "job_id": "",
    }


def _pop_summary(d: Path, meta: dict) -> dict:
    return {
        "id": d.name,
        "population_name": meta.get("population_name", d.name),
        "template_id": meta.get("template_id", ""),
        "count_requested": meta.get("count", 0),
        "run_qa": meta.get("run_qa", False),
        "qa_status": meta.get("qa_status", "unknown"),
        "chunks_built": meta.get("chunks_built", False),
        "downloadable": _is_downloadable(meta),
        "job_id": meta.get("job_id", ""),
        "modified": d.stat().st_mtime,
    }


def _is_downloadable(meta: dict) -> bool:
    return meta.get("chunks_built", False)
