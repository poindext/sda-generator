"""
Patient Record service — generates a single SDA3 patient record using the
generate-sda3 skill as a system prompt and an OpenAI model as the generator.
"""
import re
from pathlib import Path
from typing import AsyncIterator

from app.config import BASE_DIR, OPENAI_API_KEY, POPULATIONS_DIR

_SKILL_PATH = BASE_DIR / ".claude" / "commands" / "generate-sda3.md"
_RECORDS_DIR = POPULATIONS_DIR / "single-records"


def _load_system_prompt() -> str:
    """Return the Rules section of the skill file as the system prompt."""
    text = _SKILL_PATH.read_text(encoding="utf-8")
    # Everything after the first horizontal rule is the rules block
    parts = text.split("---", 1)
    return parts[1].strip() if len(parts) > 1 else text


async def generate_record(
    scenario: str,
    filename: str | None = None,
    model: str = "gpt-4o",
) -> AsyncIterator[dict]:
    """
    Stream SDA3 XML generation for a single patient scenario.

    Yields:
        {"type": "token",  "content": str}
        {"type": "done",   "file_path": str, "xml": str}
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
    user_message = (
        "Generate realistic InterSystems HealthShare SDA3 XML sample data "
        "for the following scenario:\n\n" + scenario.strip()
    )

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    try:
        stream = await client.chat.completions.create(
            model=model,
            max_tokens=12000,
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

    # Persist to populations/single-records/
    _RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    if not filename:
        from datetime import datetime
        filename = f"patient_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
    elif not filename.endswith(".xml"):
        filename += ".xml"

    file_path = _RECORDS_DIR / filename
    file_path.write_text(xml, encoding="utf-8")

    yield {
        "type": "done",
        "file_path": str(file_path.relative_to(BASE_DIR)),
        "xml": xml,
    }
