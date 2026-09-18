"""Dataset schema definitions for the NEO and LAO fleet maintenance extracts.

These definitions were derived empirically from test-data/NEO.csv,
test-data/LAO.csv, test-data/FMG NEO Aug 26.csv and test-data/FMG LAO Aug 26.csv.
See BUSINESS_RULES.md for the rationale behind each field's expectations.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DatasetSchema:
    """Per-dataset-type field expectations."""

    name: str
    # Secondary part-number / usage / date field names differ between NEO and LAO.
    secondary_part_field: str
    secondary_usage_field: str
    secondary_date_field: str
    has_part_classification: bool
    has_review_status: bool
    # Fields that must be non-blank once enrichment has run.
    required_post_enrichment: tuple
    # Fields that are legitimately blank pre-enrichment for this dataset type
    # (used only to distinguish "not yet enriched" from "enrichment produced a gap").
    expected_blank_pre_enrichment: tuple
    composite_key_fields: tuple


NEO_SCHEMA = DatasetSchema(
    name="NEO",
    secondary_part_field="NextPartNumberCode",
    secondary_usage_field="NewStrategyUsageValue",
    secondary_date_field="NewStrategyDate",
    has_part_classification=True,
    has_review_status=True,
    required_post_enrichment=(
        "BranchCode",
        "SiteCode",
        "FleetCode",
        "CustomerCode",
        "SerialNumber",
        "ComponentCode",
        "ModifierCode",
        "TaskTypeCode",
        "PrimaryPartNumberCode",
        "NextPartNumberCode",
        "SourceOfSupplyCode",
        "SalesStatusCode",
        "PartClassificationCode",
        "StrategyUsageValue",
    ),
    expected_blank_pre_enrichment=(
        "BranchCode",
        "SiteCode",
        "FleetCode",
        "CustomerCode",
        "SerialNumber",
        "TaskTypeCode",
        "LifeToDateValue",
        "PrimaryPartNumberCode",
        "NextPartNumberCode",
        "SourceOfSupplyCode",
        "StrategyUsageValue",
        "NewStrategyUsageValue",
        "SalesStatusCode",
        "PartClassificationCode",
        "SalesLostReasonCode",
        "SalesStatusCommentsNote",
        "PurchaseOrderNumber",
    ),
    composite_key_fields=(
        "AssetName",
        "RegistrationCounter",
        "ComponentCode",
        "ModifierCode",
        "TaskCounterCode",
    ),
)

LAO_SCHEMA = DatasetSchema(
    name="LAO",
    secondary_part_field="ActualPartNumberCode",
    secondary_usage_field="LastStrategyUsageValue",
    secondary_date_field="LastStrategyDate",
    has_part_classification=False,
    has_review_status=False,
    required_post_enrichment=(
        "BranchCode",
        "SiteCode",
        "FleetCode",
        "CustomerCode",
        "ModelCode",
        "SerialNumber",
        "ComponentCode",
        "ModifierCode",
        "TaskTypeCode",
        "TaskCounterCode",
        "PrimaryPartNumberCode",
        "ActualPartNumberCode",
        "SourceOfSupplyCode",
        "SalesStatusCode",
        "StrategyUsageValue",
    ),
    expected_blank_pre_enrichment=(
        "BranchCode",
        "SiteCode",
        "FleetCode",
        "CustomerCode",
        "ModelCode",
        "SerialNumber",
        "TaskTypeCode",
        "TaskCounterCode",
        "PrimaryPartNumberCode",
        "ActualPartNumberCode",
        "SourceOfSupplyCode",
        "StrategyUsageValue",
        "StrategyDate",
        "LastStrategyDate",
        "SalesStatusCode",
        "SalesLostReasonCode",
        "SalesStatusCommentsNote",
        "PurchaseOrderNumber",
    ),
    composite_key_fields=(
        "AssetName",
        "RegistrationCounter",
        "ComponentCode",
        "ModifierCode",
        "TaskCounterCode",
    ),
)

SCHEMAS = {"NEO": NEO_SCHEMA, "LAO": LAO_SCHEMA}


def get_schema(dataset_type: str) -> DatasetSchema:
    try:
        return SCHEMAS[dataset_type.upper()]
    except KeyError as exc:
        raise ValueError(
            f"Unknown dataset_type {dataset_type!r}; expected one of {list(SCHEMAS)}"
        ) from exc


# --- Known value domains, observed from the enriched FMG reference extracts. ---
# Values outside these sets are NOT hard failures (the full AMT domain may be
# larger than what appears in the two sample extracts) -- see BR-10/11/12/13/15.

KNOWN_SALES_STATUS_CODES = {
    "Unknown",
    "Confident",
    "Unlikely",
    "Sale Confirmed",
    "Lost",
}

KNOWN_PART_CLASSIFICATION_CODES = {
    "503 PEX CONTRACT",
    "000 CAT NEW",
    "000 CAT REMAN",
    "486 FLEXI",
    "CUSTOMER REBUILD",
    "445 CUSTOMER OWNED",
}

KNOWN_SOURCE_OF_SUPPLY_CODES = {
    "000",
    "501",
    "503",
    "483",
    "485",
    "666",
    "445",
    "BUC",
    "AKS",
}

EXPECTED_STRATEGY_UOM = "H"
EXPECTED_TASK_TYPE_CODE = "RB"

# (PartClassificationCode, SourceOfSupplyCode) combinations observed in the
# 6,758-row NEO reference extract. There is NOT a strict numeric-prefix
# correspondence between the two fields (e.g. "486 FLEXI" legitimately pairs
# with SourceOfSupplyCode "483" or "485", never "486") -- see BR-23. An
# unobserved combination is a soft signal, not proof of a bad join.
KNOWN_CLASSIFICATION_SUPPLY_PAIRS = {
    ("503 PEX CONTRACT", "503"),
    ("503 PEX CONTRACT", "501"),
    ("503 PEX CONTRACT", "666"),
    ("000 CAT NEW", "000"),
    ("000 CAT REMAN", "000"),
    ("000 CAT REMAN", "0 -"),
    ("486 FLEXI", "483"),
    ("486 FLEXI", "485"),
    ("CUSTOMER REBUILD", "503"),
    ("CUSTOMER REBUILD", "485"),
    ("CUSTOMER REBUILD", "501"),
    ("445 CUSTOMER OWNED", "483"),
}
