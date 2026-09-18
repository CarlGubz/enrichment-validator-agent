"""Agent entrypoint.

run_validation_agent(request) is the single callable used by every surface
(CLI via main.py, Foundry Hosted Agent via foundry_app.py). It is
transport-agnostic: give it a dict, get a dict back.

Request contract (dataset_type and input are required; everything else is
optional):
{
  "dataset_type": "NEO" | "LAO",
  "input":     "path/to/enriched.csv" | {"path": "..."} | {"content_base64": "...", "filename": "NEO.csv"},
  "reference": "path/to/FMG.csv"       | {"path": "..."} | {"content_base64": "...", "filename": "..."},   # optional -- enables BR-26..BR-28
  "use_ai": true,                      # set false to score with deterministic rules only
  "batch_size": 20,                    # rows per AI reasoning call
  "output_path": "out/validated.csv",  # optional -- write the validated CSV here (local/CLI use)
  "return_inline": false               # include the validated CSV as base64 in the response
}

Response contract:
{
  "status": "succeeded" | "failed",
  "run_id": "...",
  "dataset_type": "...",
  "summary": {"row_count": N, "verdict_counts": {"AUTO_APPROVED": .., "NEEDS_REVIEW": .., "REJECTED": ..}},
  "output_csv_path": "...",            # present when output_path was given
  "output_csv_base64": "...",          # present when return_inline=true, or when no output_path was given
  "error": "..."                       # only on failure
}

See BUSINESS_RULES.md for what "validated" means (BR-01..BR-33) and
prompts/system_prompt.md for the AI reasoning layer's instructions.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.confidence_scoring import score_row
from src.io_utils import read_rows, resolve_ref, rows_to_csv_base64, write_rows
from src.reference_data import ReferenceData
from src.rules_engine import Finding, run_all_checks
from src.schema import get_schema


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _row_id(row: dict, index: int) -> str:
    return f"{index}:{row.get('AssetName', '')}:{row.get('RegistrationCounter', '')}:{row.get('TaskCounterCode', '')}"


def run_validation_agent(request: dict) -> dict:
    run_id = request.get("run_id") or _default_run_id()
    dataset_type = request.get("dataset_type")

    try:
        if not dataset_type:
            raise ValueError("request must include 'dataset_type': 'NEO' or 'LAO'")
        if not request.get("input"):
            raise ValueError("request must include 'input' (the enriched CSV to validate)")

        schema = get_schema(dataset_type)
        rows = read_rows(resolve_ref(request["input"]))

        reference = None
        if request.get("reference"):
            reference = ReferenceData.from_rows(
                read_rows(resolve_ref(request["reference"])), schema
            )

        rows_by_id = {_row_id(r, i): r for i, r in enumerate(rows)}

        ai_results: dict = {}
        if request.get("use_ai", True):
            from src.ai_reasoning import ai_configured, evaluate_batches

            if ai_configured():
                ai_results = evaluate_batches(
                    rows_by_id, schema, reference, batch_size=request.get("batch_size", 20)
                )

        seen_keys: set = set()
        output_rows = []
        verdict_counts: dict = {}
        for row_id, row in rows_by_id.items():
            findings = run_all_checks(row, schema, reference, seen_keys)
            ai = ai_results.get(row_id)
            ai_score = ai.get("ai_plausibility_score") if ai else None
            ai_rationale = ai.get("ai_rationale") if ai else None

            if ai:
                for extra in ai.get("additional_findings", []):
                    findings.append(Finding(
                        rule_id="AI-REVIEW",
                        severity=extra.get("severity", "MINOR"),
                        field=extra.get("field", ""),
                        message=extra.get("message", ""),
                    ))

            result = score_row(findings, ai_plausibility_score=ai_score, ai_rationale=ai_rationale)

            output_row = dict(row)
            output_row["RowId"] = row_id
            output_row["ValidationVerdict"] = result.verdict
            output_row["PosteriorConfidenceScore"] = result.final_score
            output_row["FindingCount"] = len(result.findings)
            output_row["Findings"] = json.dumps([asdict(f) for f in result.findings])
            output_row["AIRationale"] = ai_rationale or ""
            output_rows.append(output_row)
            verdict_counts[result.verdict] = verdict_counts.get(result.verdict, 0) + 1

        response = {
            "status": "succeeded",
            "run_id": run_id,
            "dataset_type": dataset_type,
            "summary": {"row_count": len(output_rows), "verdict_counts": verdict_counts},
        }

        output_path = request.get("output_path")
        if output_path:
            write_rows(output_path, output_rows)
            response["output_csv_path"] = output_path
        if request.get("return_inline") or not output_path:
            response["output_csv_base64"] = rows_to_csv_base64(output_rows)

        return response

    except Exception as exc:  # surface a clean error to the caller
        return {
            "status": "failed",
            "run_id": run_id,
            "dataset_type": dataset_type,
            "error": f"{type(exc).__name__}: {exc}",
        }
