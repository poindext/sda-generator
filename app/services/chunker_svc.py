"""
Chunker service — splits a generated population's XML files into
zip archives of CHUNK_SIZE records each.

Chunks contain XML patient files only (no CSVs).
Chunk zips are written to <population_dir>/chunks/.
"""
import zipfile
from pathlib import Path

from app.config import CHUNK_SIZE


def build_chunks(population_dir: Path, chunk_size: int = CHUNK_SIZE) -> list[Path]:
    """
    Build zip chunks for the given population directory.
    Returns list of created chunk zip paths.
    Existing chunks are removed first so rebuilds are clean.
    """
    chunks_dir = population_dir / "chunks"
    if chunks_dir.exists():
        for old in chunks_dir.glob("chunk_*.zip"):
            old.unlink()
    chunks_dir.mkdir(exist_ok=True)

    xml_files = sorted(
        f for f in population_dir.iterdir()
        if f.suffix == ".xml" and f.parent == population_dir
    )

    chunk_paths: list[Path] = []
    batches = list(_batched(xml_files, chunk_size))
    for i, batch in enumerate(batches, 1):
        chunk_path = chunks_dir / f"chunk_{i:03d}_of_{len(batches):03d}.zip"
        with zipfile.ZipFile(chunk_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for xml_file in batch:
                zf.write(xml_file, xml_file.name)
        chunk_paths.append(chunk_path)

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
