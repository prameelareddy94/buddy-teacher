"""Local model via Ollama: ONE call returning JSON {hint, answer, confident, pages}."""
import json

import httpx

from buddy.config import get_settings

LOCAL_SCHEMA = {
    "type": "object",
    "properties": {
        "hint": {"type": "string"},
        "answer": {"type": "string"},
        "confident": {"type": "boolean"},
        "pages": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["hint", "answer", "confident", "pages"],
}

LOCAL_FORMAT = """Reply with JSON only:
{"hint": "<one short hint>", "answer": "<the answer>", "confident": <true if the \
passages clearly contain the answer, else false>, "pages": [<page numbers from the \
cites you used>]}"""


async def ask_local(system: str, prompt: str) -> tuple[dict, dict]:
    """Returns (parsed JSON, usage). Raises on connection/parse errors."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.ollama_timeout) as client:
        r = await client.post(f"{s.ollama_url}/api/chat", json={
            "model": s.ollama_model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "stream": False,
            "think": False,          # Qwen 3: skip the reasoning trace, we want speed
            "format": LOCAL_SCHEMA,  # constrained decoding to this JSON schema
            "keep_alive": "30m",
            "options": {"temperature": 0.2, "num_ctx": 6144},
        })
        r.raise_for_status()
        body = r.json()
    data = json.loads(body["message"]["content"])
    usage = {"input_tokens": body.get("prompt_eval_count", 0),
             "output_tokens": body.get("eval_count", 0)}
    return data, usage


async def is_up() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"{get_settings().ollama_url}/api/tags")).status_code == 200
    except httpx.HTTPError:
        return False
