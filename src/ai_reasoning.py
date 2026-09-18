"""AI plausibility-judgment layer.

Faithful to the hybrid design used across this dealer's Foundry agents: the
deterministic rule engine (rules_engine.py) owns the numbers and the
findings; the LLM is given those findings as context and only adds the
semantic/contextual judgment a fixed rule can't make (see
prompts/system_prompt.md). It never recomputes or overrides a deterministic
finding, and it is never required for the agent to run -- with no model
configured (or `use_ai=False`), the deterministic score stands on its own
and the agent is still fully callable/testable (see confidence_scoring.py).

Uses the Foundry project's OpenAI-compatible gateway
(`AIProjectClient(...).get_openai_client()`) rather than a separate
Assistants-style persistent agent+threads loop -- this *is* already running
as the hosted agent, so it calls the model directly for its own reasoning
step instead of standing up a second nested agent.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from .confidence_scoring import compute_base_score
from .reference_data import ReferenceData
from .rules_engine import run_all_checks
from .schema import DatasetSchema

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_prompt.md"

_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "row_id": {"type": "string"},
                    "ai_plausibility_score": {"type": "number"},
                    "ai_rationale": {"type": "string"},
                    "additional_findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "severity": {"type": "string", "enum": ["CRITICAL", "MAJOR", "MINOR", "INFO"]},
                                "field": {"type": "string"},
                                "message": {"type": "string"},
                            },
                            "required": ["severity", "field", "message"],
                        },
                    },
                    "recommended_action": {
                        "type": "string",
                        "enum": ["AUTO_APPROVE", "SEND_TO_REVIEW", "REJECT"],
                    },
                    "explanation_for_reviewer": {"type": "string"},
                },
                "required": [
                    "row_id",
                    "ai_plausibility_score",
                    "ai_rationale",
                    "additional_findings",
                    "recommended_action",
                    "explanation_for_reviewer",
                ],
            },
        }
    },
    "required": ["verdicts"],
}


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def ai_configured() -> bool:
    return bool(os.environ.get("PROJECT_ENDPOINT")) and bool(os.environ.get("MODEL_DEPLOYMENT_NAME"))


def _client():
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )
    return project.get_openai_client()


def _chunks(items: List[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _precompute_findings(
    rows_by_id: Dict[str, dict], schema: DatasetSchema, reference: Optional[ReferenceData]
) -> Dict[str, dict]:
    """Runs the deterministic rule engine once per row so the model is given
    findings as context, rather than asked to call a tool to fetch them."""
    seen_keys: set = set()
    precomputed = {}
    for row_id, row in rows_by_id.items():
        findings = run_all_checks(row, schema, reference, seen_keys)
        precomputed[row_id] = {
            "row": row,
            "findings": [asdict(f) for f in findings],
            "base_score": compute_base_score(findings),
        }
    return precomputed


def _call_model(system_prompt: str, user_payload: dict, model: str, reasoning_effort: str) -> dict:
    resp = _client().responses.create(
        model=model,
        reasoning={"effort": reasoning_effort},
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "row_verdicts",
                "schema": _RESPONSE_SCHEMA,
                "strict": True,
            }
        },
    )
    return json.loads(resp.output_text)


def evaluate_batches(
    rows_by_id: Dict[str, dict],
    schema: DatasetSchema,
    reference: Optional[ReferenceData],
    batch_size: int = 20,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, dict]:
    """Evaluates every row in rows_by_id and returns {row_id: verdict_dict}.

    Never raises to the caller: on any failure it returns whatever verdicts
    were already collected (possibly none), so the caller can fall back to
    the deterministic-only score for the rows that weren't reached.
    """
    model = model or os.environ["MODEL_DEPLOYMENT_NAME"]
    reasoning_effort = reasoning_effort or os.environ.get("REASONING_EFFORT", "low")
    system_prompt = load_system_prompt()

    precomputed = _precompute_findings(rows_by_id, schema, reference)
    all_ids = list(rows_by_id.keys())

    results: Dict[str, dict] = {}
    for batch_ids in _chunks(all_ids, batch_size):
        user_payload = {
            "instruction": (
                "Evaluate each of the following rows. Each entry already includes "
                "the deterministic findings and base_score computed by the rule "
                "engine -- do not recompute or contradict them, only add what they "
                "could not catch."
            ),
            "rows": {rid: precomputed[rid] for rid in batch_ids},
        }
        try:
            parsed = _call_model(system_prompt, user_payload, model, reasoning_effort)
        except Exception as exc:  # model/network failure -- degrade gracefully
            for rid in batch_ids:
                results[rid] = {
                    "ai_plausibility_score": None,
                    "ai_rationale": f"AI evaluation unavailable: {type(exc).__name__}: {exc}",
                    "additional_findings": [],
                    "recommended_action": None,
                    "explanation_for_reviewer": "",
                }
            continue

        for verdict in parsed.get("verdicts", []):
            row_id = verdict.get("row_id")
            if row_id in batch_ids:
                results[row_id] = verdict

    return results
