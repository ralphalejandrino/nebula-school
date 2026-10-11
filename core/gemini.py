"""The ONE network call site to the Gemini API .

stdlib urllib, no vendor SDK. Tests monkey-patch `_post_json`, so no test opens a
socket. The key is Nebula's own (created in Google AI Studio),
never the CRM's — the CRM's data must not share a free-tier project with anything.
"""
import json
import urllib.error
import urllib.request

from django.conf import settings

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiError(RuntimeError):
    def __init__(self, msg, status=None, quota=False):
        super().__init__(msg)
        self.status = status
        self.quota = quota


def _post_json(url, payload, key, timeout=300):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def generate_json(model, system, user, schema, temperature=0.1):
    """Call `model`, force JSON matching `schema`, return (parsed, usage)."""
    key = settings.GEMINI_API_KEY
    if not key:
        raise GeminiError("no SCHOOL_GEMINI_API_KEY in ~/.config/nebula/gemini.env")
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": temperature,
                             "responseMimeType": "application/json",
                             "responseSchema": schema},
    }
    try:
        resp = _post_json(ENDPOINT.format(model=model), payload, key)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        raise GeminiError(f"HTTP {e.code}: {body}", status=e.code,
                          quota=(e.code == 429 or "RESOURCE_EXHAUSTED" in body)) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise GeminiError(f"network: {e}") from None
    try:
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text), resp.get("usageMetadata", {})
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        reason = (resp.get("candidates") or [{}])[0].get("finishReason")
        raise GeminiError(f"unparseable reply ({type(e).__name__}, finish={reason})") from None
