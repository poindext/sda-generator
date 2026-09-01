"""
Wizard service — manages the LLM-driven interview sessions.

Each session holds a chat history.  Sessions are in-memory; they are short-lived
(a single browser session of the wizard flow).  Uses the same system prompt and
READY_SIGNAL as the CLI population_wizard.py.
"""
import sys
import uuid
from pathlib import Path
from typing import AsyncIterator

# Pull the system prompt and synthesis prompt directly from the CLI wizard
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
from population_wizard import _SYSTEM_PROMPT, _SYNTHESIS_PROMPT, READY_SIGNAL  # noqa: E402

from app.config import OPENAI_API_KEY, DEFAULT_MODEL

_sessions: dict[str, dict] = {}   # session_id -> {history, ready, document}


def create_session(selected_cohorts: list[str]) -> str:
    session_id = f"wiz_{uuid.uuid4().hex[:12]}"
    cohort_ctx = ""
    if selected_cohorts:
        cohort_ctx = (
            "\n\nNote: The user has pre-selected the following approved cohort types "
            "from the catalog. Include these in the population and ask for details about "
            "prevalence, age ranges, and any sub-population specifics:\n- "
            + "\n- ".join(selected_cohorts)
        )
    _sessions[session_id] = {
        "history": [{"role": "system", "content": _SYSTEM_PROMPT + cohort_ctx}],
        "ready": False,
        "document": None,
    }
    return session_id


async def opening_message(session_id: str) -> AsyncIterator[str]:
    """Stream the wizard's opening question."""
    sess = _get(session_id)
    sess["history"].append({
        "role": "user",
        "content": "Hello — I need to design a synthetic patient population.",
    })
    async for chunk in _stream_chat(sess["history"]):
        yield chunk


async def send_message(session_id: str, user_text: str) -> AsyncIterator[dict]:
    """
    Add user message, stream assistant reply.
    Yields dicts: {"type": "token", "content": str}
                  {"type": "done",  "ready": bool}
    """
    sess = _get(session_id)
    sess["history"].append({"role": "user", "content": user_text})

    full_reply = ""
    async for chunk in _stream_chat(sess["history"]):
        full_reply += chunk
        yield {"type": "token", "content": chunk}

    sess["history"].append({"role": "assistant", "content": full_reply})

    ready = READY_SIGNAL in full_reply
    if ready:
        sess["ready"] = True
    yield {"type": "done", "ready": ready}


async def synthesize_document(session_id: str) -> AsyncIterator[dict]:
    """
    Trigger document synthesis.  Streams token-by-token, then saves the final
    document text on the session.
    Yields dicts: {"type": "token", "content": str}
                  {"type": "done",  "document": str}
    """
    sess = _get(session_id)
    sess["history"].append({"role": "user", "content": _SYNTHESIS_PROMPT})

    document = ""
    async for chunk in _stream_chat(sess["history"], max_tokens=2048, temperature=0.2):
        document += chunk
        yield {"type": "token", "content": chunk}

    sess["document"] = document
    sess["history"].append({"role": "assistant", "content": document})
    yield {"type": "done", "document": document}


def get_document(session_id: str) -> str | None:
    return _sessions.get(session_id, {}).get("document")


def is_ready(session_id: str) -> bool:
    return _sessions.get(session_id, {}).get("ready", False)


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _get(session_id: str) -> dict:
    if session_id not in _sessions:
        raise KeyError(f"Wizard session not found: {session_id}")
    return _sessions[session_id]


async def _stream_chat(
    messages: list,
    max_tokens: int = 1024,
    temperature: float = 0.3,
) -> AsyncIterator[str]:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("openai package not installed")

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    stream = await client.chat.completions.create(
        model=DEFAULT_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=messages,
        stream=True,
    )
    async for event in stream:
        delta = event.choices[0].delta.content
        if delta:
            yield delta
