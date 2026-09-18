"""Agent entrypoint.

run_validation_agent(request) is the single callable used by every surface
(CLI via main.py, Foundry Hosted Agent via foundry_app.py). It is
transport-agnostic: give it a dict, get a dict back.

Request contract (dataset_type and input are required; everything else is
optional):
{
  "dataset_type": "NEO" | "LAO",
  "input":     "fmg-inbound/NEO.csv" | "path/to/enriched.csv" | {"path": "..."} | {"content_base64": "...", "filename": "NEO.csv"},
  "reference": "fmg-inbound/FMG NEO Aug 26.csv" | {"path": "..."} | {"content_base64": "...", "filename": "..."},   # optional -- enables BR-26..BR-28
  "use_ai": true,                      # set false to score with deterministic rules only
  "batch_size": 20,                    # rows per AI reasoning call
  "output_name": "NEO_validated.csv",  # optional -- output file/blob name (default: "<dataset_type>_validated.csv")
  "output_path": "out/validated.csv",  # optional -- write to this EXACT local path instead of via the storage backend
  "return_inline": false               # also include the validated CSV as base64 in the response
}

`input`/`reference` accept a bare "<container>/<blob_name>" string (e.g.
"fmg-inbound/NEO.csv") when STORAGE_BACKEND=azure_blob is configured (see
src/storage.py), a local file path otherwise, or inline
{"content_base64": ..., "filename": ...} for a transport with no shared
filesystem or blob access. The output is written back through the same
storage backend: with Blob storage, "fmg-inbound/..." input automatically
writes to "fmg-outbound/<run_id>/<output_name>" -- the container's
"inbound"/"outbound" swap is automatic, nothing about the target container
needs to be specified. `output_path` bypasses that and writes to an exact
local path instead (used by main.py's CLI).

Response contract:
{
  "status": "succeeded" | "failed",
  "run_id": "...",
  "dataset_type": "...",
  "summary": {"row_count": N, "verdict_counts": {"AUTO_APPROVED": .., "NEEDS_REVIEW": .., "REJECTED": ..}},
  "output_csv_path": "...",            # local path, or "<container>/<blob>" for Blob storage
  "output_csv_base64": "...",          # present only when return_inline=true
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
from src.storage import get_storage


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
        storage = get_storage()

        input_local_path = storage.fetch_input(resolve_ref(request["input"]), track=True)
        rows = read_rows({"path": input_local_path})

        reference = None
        if request.get("reference"):
            # track=False: the reference file's container must never override
            # the main input's container, which is what determines the
            # output container (fmg-inbound -> fmg-outbound).
            reference_local_path = storage.fetch_input(resolve_ref(request["reference"]), track=False)
            reference = ReferenceData.from_rows(read_rows({"path": reference_local_path}), schema)

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
            # Exact local path override -- bypasses the storage backend
            # entirely (used by main.py's CLI).
            write_rows(output_path, output_rows)
            response["output_csv_path"] = output_path
        else:
            output_name = request.get("output_name") or f"{dataset_type}_validated.csv"
            response["output_csv_path"] = storage.write_rows(output_rows, output_name, run_id)

        if request.get("return_inline"):
            response["output_csv_base64"] = rows_to_csv_base64(output_rows)

        return response

    except Exception as exc:  # surface a clean error to the caller
        return {
            "status": "failed",
            "run_id": run_id,
            "dataset_type": dataset_type,
            "error": f"{type(exc).__name__}: {exc}",
        }
