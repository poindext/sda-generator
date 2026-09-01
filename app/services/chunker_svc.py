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
from pathlib import Path

from app.config import CHUNK_SIZE


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
        for old in chunks_dir.glob("chunk_*.zip"):
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

    chunk_paths: list[Path] = []
    for i, pid_batch in enumerate(batches, 1):
        chunk_path = chunks_dir / f"chunk_{i:03d}_of_{len(batches):03d}.zip"
        with zipfile.ZipFile(chunk_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for pid in pid_batch:
                for xml_file in patient_files[pid]:
                    zf.write(xml_file, xml_file.name)
        chunk_paths.append(chunk_path)

    # Remove source XML files now that they're safely in the zips
    for xml_files in patient_files.values():
        for f in xml_files:
            f.unlink(missing_ok=True)
    if search_dir != population_dir and not any(search_dir.iterdir()):
        search_dir.rmdir()

    return chunk_paths


def list_chunks(population_dir: Path) -> list[dict]:
    """Return metadata for all chunks in a population directory."""
    chunks_dir = population_dir / "chunks"
    if not chunks_dir.exists():
        return []
    result = []
    for p in sorted(chunks_dir.glob("chunk_*.zip")):
        result.append({
            "name": p.name,
            "size_bytes": p.stat().st_size,
            "size_mb": round(p.stat().st_size / 1_048_576, 1),
        })
    return result


def _batched(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]
