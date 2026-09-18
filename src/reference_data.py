"""Loads an enriched reference extract (a stand-in for a live AMT Snowflake
lookup) and exposes fast composite-key / partial-key lookups used by the
referential-integrity rules (BR-26..BR-28).

In production, `ReferenceData` would be backed by a live query against the
AMT Snowflake warehouse rather than a CSV snapshot. The CSV-backed
implementation here is deliberately swappable: replace `load_csv` with a
Snowflake query that returns the same column set and everything downstream
(rules_engine, confidence_scoring, foundry_agent) keeps working unchanged.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .schema import DatasetSchema


@dataclass
class ReferenceData:
    schema: DatasetSchema
    rows: List[dict] = field(default_factory=list)
    by_composite_key: Dict[tuple, dict] = field(default_factory=dict)
    by_registration_counter: Dict[str, List[dict]] = field(default_factory=lambda: defaultdict(list))
    by_asset_name: Dict[str, List[dict]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def load_csv(cls, path: str, schema: DatasetSchema) -> "ReferenceData":
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        return cls.from_rows(rows, schema)

    @classmethod
    def from_rows(cls, rows: List[dict], schema: DatasetSchema) -> "ReferenceData":
        ref = cls(schema=schema, rows=rows)
        for row in rows:
            key = tuple(row.get(f, "") for f in schema.composite_key_fields)
            ref.by_composite_key[key] = row
            ref.by_registration_counter[row.get("RegistrationCounter", "")].append(row)
            ref.by_asset_name[row.get("AssetName", "")].append(row)
        return ref

    def composite_key_of(self, row: dict) -> tuple:
        return tuple(row.get(f, "") for f in self.schema.composite_key_fields)

    def exact_match(self, row: dict) -> Optional[dict]:
        """BR-27: exact composite-key match against the master/reference data."""
        return self.by_composite_key.get(self.composite_key_of(row))

    def candidates_for_registration_counter(self, row: dict) -> List[dict]:
        """All reference rows sharing this row's RegistrationCounter.

        RegistrationCounter identifies a shared maintenance *task template*,
        not a unique asset (BR-28) -- a single value can legitimately map to
        hundreds of different assets. This is used to detect the case where
        enrichment picked an arbitrary row sharing the template instead of
        the one actually matching the source asset.
        """
        return list(self.by_registration_counter.get(row.get("RegistrationCounter", ""), []))

    def candidates_for_asset_name(self, row: dict) -> List[dict]:
        return list(self.by_asset_name.get(row.get("AssetName", ""), []))

    def is_duplicate_composite_key(self, row: dict, seen_keys: set) -> bool:
        """BR-26: composite key must be unique within the enriched output being validated."""
        return self.composite_key_of(row) in seen_keys
