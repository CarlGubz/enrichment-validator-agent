"""CSV I/O helpers shared by the CLI (main.py) and the hosted-agent entrypoint
(foundry_app.py). Both need to read/write rows from either a local file path or
inline base64 content -- a Foundry Hosted Agent has no shared filesystem with
its caller, so inline content is the only way it can receive/return data.
"""

from __future__ import annotations

import base64
import csv
import io
from pathlib import Path
from typing import List, Union


def resolve_ref(ref: Union[str, dict]) -> dict:
    """A bare string is shorthand for {"path": ref}; dict refs pass through."""
    if isinstance(ref, str):
        return {"path": ref}
    return ref


def read_rows(ref: dict) -> List[dict]:
    """Reads CSV rows from a resolved ref: {"path": "..."} or
    {"content_base64": "...", "filename": "..."}."""
    if "content_base64" in ref:
        text = base64.b64decode(ref["content_base64"]).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))
    with open(ref["path"], newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def rows_to_csv_text(rows: List[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def rows_to_csv_base64(rows: List[dict]) -> str:
    return base64.b64encode(rows_to_csv_text(rows).encode("utf-8")).decode("ascii")


def write_rows(path: str, rows: List[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(rows_to_csv_text(rows))
