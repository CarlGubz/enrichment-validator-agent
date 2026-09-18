"""CLI entry point for the Fleet Data Enrichment Validation Agent.

Thin wrapper over agent.run_validation_agent -- the same function
foundry_app.py calls when this agent is deployed as a Microsoft Foundry
Hosted Agent. This file exists for local runs, CI, and quick sanity checks
against test-data/ without needing a live Foundry project.

Usage examples
--------------
Dry run (deterministic rules only, no Azure/Foundry credentials needed) --
useful for local testing and for CI:

    python main.py --input test-data/NEO.csv \\
        --reference "test-data/FMG NEO Aug 26.csv" \\
        --dataset-type NEO \\
        --output out/NEO_validated.csv \\
        --no-ai

Full run against a configured Foundry model (see .env.example):

    python main.py --input test-data/NEO.csv \\
        --reference "test-data/FMG NEO Aug 26.csv" \\
        --dataset-type NEO \\
        --output out/NEO_validated.csv \\
        --batch-size 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import run_validation_agent

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is only required when running with AI reasoning enabled


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fleet Data Enrichment Validation Agent")
    parser.add_argument("--input", required=True, help="Path to the enriched NEO/LAO CSV to validate")
    parser.add_argument(
        "--reference",
        required=False,
        help="Path to the AMT/FMG reference CSV used for referential-integrity checks (BR-26..BR-28)",
    )
    parser.add_argument("--dataset-type", required=True, choices=["NEO", "LAO"])
    parser.add_argument("--output", required=True, help="Path to write the validated output CSV")
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip the AI reasoning layer and score using deterministic rules only",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Number of rows sent to the AI reasoning layer per call",
    )
    args = parser.parse_args(argv)

    request = {
        "dataset_type": args.dataset_type,
        "input": {"path": args.input},
        "reference": {"path": args.reference} if args.reference else None,
        "use_ai": not args.no_ai,
        "batch_size": args.batch_size,
        "output_path": args.output,
    }
    result = run_validation_agent(request)

    if result["status"] != "succeeded":
        print(f"Validation failed: {result.get('error')}", file=sys.stderr)
        return 1

    print(f"Validated {result['summary']['row_count']} rows -> {result['output_csv_path']}")
    print("Verdict summary:", result["summary"]["verdict_counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
