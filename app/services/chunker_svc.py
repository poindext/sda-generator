"""
Chunker service — splits a generated population's XML files into
zip archives of CHUNK_SIZE patients each.

Chunks contain XML patient files only (no CSVs).
Chunking is by patient (not raw file count) so all facility files
for a patient stay together and multi-facility patients don't inflate
the chunk count.
Chunk zips are written to <population_dir>/chunks/.
"""
import re
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import CHUNK_SIZE

# Max parallel workers for chunk writing and file deletion.
# Bound at 4 — beyond that, Windows I/O scheduler sees diminishing returns
# and memory pressure from concurrent file reads grows.
_MAX_WORKERS = 4


def _write_one_chunk(chunk_path: Path, pid_batch: list[int],
                     patient_files: dict) -> Path:
    """Write a single zip chunk. Runs in a thread pool worker."""
    with zipfile.ZipFile(chunk_path, "w", zipfile.ZIP_STORED) as zf:
        for pid in pid_batch:
            for xml_file in patient_files[pid]:
                zf.write(xml_file, xml_file.name)
    return chunk_path


def build_chunks(population_dir: Path, chunk_size: int = CHUNK_SIZE) -> list[Path]:
    """
    Build zip chunks for the given population directory.
    Returns list of created chunk zip paths.
    Existing chunks are removed first so rebuilds are clean.
    Chunks are sized by patient count — all XML files belonging to the
    same patient ID are kept in the same zip.
    """
    chunks_dir = population_dir / "chunks"
    if chunks_dir.exists():
        for old in chunks_dir.glob("*.zip"):
            old.unlink()
    chunks_dir.mkdir(exist_ok=True)

    xml_dir = population_dir / "xml"
    search_dir = xml_dir if xml_dir.is_dir() else population_dir

    # Group files by patient ID (patient_000001_FACILITY.xml → id 1)
    patient_files: dict[int, list[Path]] = defaultdict(list)
    _pat = re.compile(r"^patient_(\d+)_")
    for f in sorted(search_dir.iterdir()):
        if f.suffix != ".xml":
            continue
        m = _pat.match(f.name)
        if m:
            patient_files[int(m.group(1))].append(f)

    patient_ids = sorted(patient_files)
    batches = list(_batched(patient_ids, chunk_size))
    n_batches = len(batches)
    _pfx = population_dir.name

    # Build all chunks in parallel — each worker writes one zip independently.
    workers = min(_MAX_WORKERS, n_batches)
    chunk_paths: list[Path] = [
        chunks_dir / f"{_pfx}_chunk_{i:03d}_of_{n_batches:03d}.zip"
        for i in range(1, n_batches + 1)
    ]
    futures_map: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, batch in zip(chunk_paths, batches):
            fut = pool.submit(_write_one_chunk, path, batch, patient_files)
            futures_map[fut] = path
        for fut in as_completed(futures_map):
            fut.result()  # re-raise any exception from the worker

    # Delete source XML files in parallel now that they're safely in the zips.
    all_xml: list[Path] = [f for files in patient_files.values() for f in files]
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        list(pool.map(lambda f: f.unlink(missing_ok=True), all_xml))

    if search_dir != population_dir and not any(search_dir.iterdir()):
        search_dir.rmdir()

    return chunk_paths


def list_chunks(population_dir: Path) -> list[dict]:
    """Return metadata for all chunks in a population directory."""
    chunks_dir = population_dir / "chunks"
    if not chunks_dir.exists():
        return []
    result = []
    for p in sorted(chunks_dir.glob("*.zip")):
        result.append({
            "name": p.name,
            "size_bytes": p.stat().st_size,
            "size_mb": round(p.stat().st_size / 1_048_576, 1),
        })
    return result


def _batched(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]
