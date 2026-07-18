"""
LLM-as-judge scorer: rates each bot reply 1–5 against the case's
`reply_rubric` using Gemini with a forced JSON response schema at
temperature 0.

The judge is REPORT-ONLY — it never gates CI (judge models drift, and a
gate that drifts trains people to ignore it). Scores land in the eval
report next to the deterministic pass/fail.
"""

import asyncio
import json

from google import genai
from google.genai import types

from config import settings

JUDGE_MODEL = "gemini-2.5-flash"

_JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "score": {"type": "INTEGER", "description": "1 (bad) to 5 (excellent)"},
        "rationale": {"type": "STRING"},
    },
    "required": ["score", "rationale"],
}

_JUDGE_PROMPT = """\
You are grading a household-assistant Discord bot's reply.

User message:
{user_message}

Bot reply (action JSON already stripped; judge only the visible text):
{reply}

Rubric — the reply should satisfy this:
{rubric}

General expectations regardless of rubric: concise (confirmations are one
sentence), warm but not sycophantic, no invented facts, no markdown headers
for short replies.

Score 1-5: 5 = fully satisfies rubric and expectations; 3 = acceptable but
notably imperfect; 1 = wrong, misleading, or ignores the request.
"""


class Judge:
    def __init__(self, model: str = JUDGE_MODEL):
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = model

    async def score(self, user_message: str, reply: str, rubric: str) -> dict:
        prompt = _JUDGE_PROMPT.format(user_message=user_message, reply=reply, rubric=rubric)
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=_JUDGE_SCHEMA,
                ),
            ),
        )
        try:
            return json.loads(response.text or "{}")
        except json.JSONDecodeError:
            return {"score": None, "rationale": f"unparseable judge output: {response.text!r}"}
