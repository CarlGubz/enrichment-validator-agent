"""Deterministic business-rule engine.

Implements the rule catalog in BUSINESS_RULES.md (section 3, BR-01..BR-28).
This module produces `Finding` objects only -- it never computes a final
confidence score itself. Score computation lives in confidence_scoring.py
so that the deterministic and AI-adjusted layers stay independently
testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from .reference_data import ReferenceData
from .schema import (
    DatasetSchema,
    EXPECTED_STRATEGY_UOM,
    EXPECTED_TASK_TYPE_CODE,
    KNOWN_CLASSIFICATION_SUPPLY_PAIRS,
    KNOWN_PART_CLASSIFICATION_CODES,
    KNOWN_SALES_STATUS_CODES,
    KNOWN_SOURCE_OF_SUPPLY_CODES,
)

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_MAJOR = "MAJOR"
SEVERITY_MINOR = "MINOR"
SEVERITY_INFO = "INFO"

_PART_NUMBER_RE = re.compile(r"^(?=.*\d)[A-Z0-9]{5,14}$")
_PO_NUMBER_RE = re.compile(r"^45\d{8}$")
_MODIFIER_CODE_RE = re.compile(r"^(00|[A-Z]{1,2}\d{0,2})$")
_DATE_RE = re.compile(r"^\d{8}$")


@dataclass
class Finding:
    rule_id: str
    severity: str
    field: str
    message: str


def _blank(value: Optional[str]) -> bool:
    return value is None or not str(value).strip()


def _require_non_blank(row: dict, field: str, rule_id: str, severity: str) -> List[Finding]:
    if _blank(row.get(field)):
        return [Finding(rule_id, severity, field, f"{field} is blank after enrichment.")]
    return []


def _valid_date(value: str) -> bool:
    if not _DATE_RE.match(value or ""):
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
        return True
    except ValueError:
        return False


def check_completeness(row: dict, schema: DatasetSchema) -> List[Finding]:
    """BR-01..BR-09."""
    findings: List[Finding] = []

    for f in ("BranchCode", "SiteCode", "FleetCode", "CustomerCode"):
        findings += _require_non_blank(row, f, "BR-01", SEVERITY_CRITICAL)

    findings += _require_non_blank(row, "SerialNumber", "BR-02", SEVERITY_CRITICAL)

    for f in ("ComponentCode", "ModifierCode"):
        findings += _require_non_blank(row, f, "BR-03", SEVERITY_MAJOR)

    findings += _require_non_blank(row, "TaskTypeCode", "BR-04", SEVERITY_MAJOR)

    findings += _require_non_blank(row, "PrimaryPartNumberCode", "BR-05", SEVERITY_MAJOR)
    findings += _require_non_blank(row, schema.secondary_part_field, "BR-05", SEVERITY_MAJOR)

    findings += _require_non_blank(row, "SalesStatusCode", "BR-06", SEVERITY_MAJOR)
    findings += _require_non_blank(row, "SourceOfSupplyCode", "BR-07", SEVERITY_MAJOR)

    if schema.has_part_classification:
        findings += _require_non_blank(row, "PartClassificationCode", "BR-08", SEVERITY_MAJOR)

    findings += _require_non_blank(row, "StrategyUsageValue", "BR-09", SEVERITY_MINOR)

    return findings


def check_domains(row: dict, schema: DatasetSchema) -> List[Finding]:
    """BR-10..BR-15."""
    findings: List[Finding] = []

    sales_status = row.get("SalesStatusCode", "")
    if sales_status and sales_status not in KNOWN_SALES_STATUS_CODES:
        findings.append(Finding(
            "BR-10", SEVERITY_MINOR, "SalesStatusCode",
            f"SalesStatusCode {sales_status!r} is outside the previously observed domain "
            f"{sorted(KNOWN_SALES_STATUS_CODES)}; verify against AMT before trusting.",
        ))

    if schema.has_part_classification:
        pcc = row.get("PartClassificationCode", "")
        if pcc and pcc not in KNOWN_PART_CLASSIFICATION_CODES:
            findings.append(Finding(
                "BR-11", SEVERITY_MINOR, "PartClassificationCode",
                f"PartClassificationCode {pcc!r} is outside the previously observed domain.",
            ))

    uom = row.get("StrategyUOMCode", "")
    if uom and uom != EXPECTED_STRATEGY_UOM:
        findings.append(Finding(
            "BR-12", SEVERITY_MINOR, "StrategyUOMCode",
            f"StrategyUOMCode {uom!r} is not the expected {EXPECTED_STRATEGY_UOM!r}.",
        ))

    task_type = row.get("TaskTypeCode", "")
    if task_type and task_type != EXPECTED_TASK_TYPE_CODE:
        findings.append(Finding(
            "BR-13", SEVERITY_MINOR, "TaskTypeCode",
            f"TaskTypeCode {task_type!r} is not the expected {EXPECTED_TASK_TYPE_CODE!r}.",
        ))

    modifier = row.get("ModifierCode", "")
    if modifier and not _MODIFIER_CODE_RE.match(modifier):
        findings.append(Finding(
            "BR-14", SEVERITY_MINOR, "ModifierCode",
            f"ModifierCode {modifier!r} does not match the observed positional-code pattern.",
        ))

    sos = row.get("SourceOfSupplyCode", "")
    if sos and sos not in KNOWN_SOURCE_OF_SUPPLY_CODES:
        findings.append(Finding(
            "BR-15", SEVERITY_MINOR, "SourceOfSupplyCode",
            f"SourceOfSupplyCode {sos!r} is outside the previously observed domain.",
        ))

    return findings


def check_formats(row: dict, schema: DatasetSchema) -> List[Finding]:
    """BR-16..BR-20."""
    findings: List[Finding] = []

    asset_name = row.get("AssetName", "")
    serial = row.get("SerialNumber", "")
    if serial and " - " in asset_name:
        suffix = asset_name.split(" - ")[-1].split(" ")[0]
        if serial != f"0{suffix}":
            findings.append(Finding(
                "BR-16", SEVERITY_CRITICAL, "SerialNumber",
                f"SerialNumber {serial!r} does not match expected '0'+suffix of "
                f"AssetName {asset_name!r} (expected '0{suffix}').",
            ))

    for f in ("PrimaryPartNumberCode", schema.secondary_part_field):
        val = row.get(f, "")
        if val and not _PART_NUMBER_RE.match(val):
            findings.append(Finding(
                "BR-17", SEVERITY_MINOR, f,
                f"{f} {val!r} does not match the expected part-number pattern. "
                "CAT catalog part numbers can be irregular -- treat as a prompt to "
                "verify against the reference/master data (BR-27), not a hard failure.",
            ))

    po = row.get("PurchaseOrderNumber", "")
    if po and not _PO_NUMBER_RE.match(po):
        findings.append(Finding(
            "BR-18", SEVERITY_MAJOR, "PurchaseOrderNumber",
            f"PurchaseOrderNumber {po!r} does not match the expected 10-digit '45xxxxxxxx' pattern.",
        ))

    for f in ("StrategyDate", schema.secondary_date_field):
        val = row.get(f, "")
        if val and not _valid_date(val):
            findings.append(Finding(
                "BR-19", SEVERITY_MAJOR, f,
                f"{f} {val!r} is not a valid YYYYMMDD date.",
            ))

    for f in ("RegistrationCounter", "ComponentCode", "TaskCounterCode"):
        val = row.get(f, "")
        if val and val != val.strip():
            findings.append(Finding(
                "BR-20", SEVERITY_MINOR, f,
                f"{f} {val!r} contains leading/trailing whitespace.",
            ))

    return findings


def check_cross_field_consistency(row: dict, schema: DatasetSchema) -> List[Finding]:
    """BR-21..BR-25."""
    findings: List[Finding] = []

    sales_status = row.get("SalesStatusCode", "")
    lost_reason = row.get("SalesLostReasonCode", "")
    if sales_status == "Lost" and _blank(lost_reason):
        findings.append(Finding(
            "BR-21", SEVERITY_MAJOR, "SalesLostReasonCode",
            "SalesStatusCode is 'Lost' but SalesLostReasonCode is blank.",
        ))
    elif sales_status and sales_status != "Lost" and not _blank(lost_reason):
        findings.append(Finding(
            "BR-21", SEVERITY_MAJOR, "SalesLostReasonCode",
            f"SalesLostReasonCode is populated ({lost_reason!r}) but SalesStatusCode is "
            f"{sales_status!r}, not 'Lost'.",
        ))

    po = row.get("PurchaseOrderNumber", "")
    if sales_status == "Sale Confirmed" and _blank(po):
        findings.append(Finding(
            "BR-22", SEVERITY_MAJOR, "PurchaseOrderNumber",
            "SalesStatusCode is 'Sale Confirmed' but PurchaseOrderNumber is blank.",
        ))
    elif sales_status and sales_status != "Sale Confirmed" and not _blank(po):
        findings.append(Finding(
            "BR-22", SEVERITY_MINOR, "PurchaseOrderNumber",
            f"PurchaseOrderNumber is populated but SalesStatusCode is {sales_status!r}, "
            "not 'Sale Confirmed' (rare but not impossible).",
        ))

    if schema.has_part_classification:
        pcc = row.get("PartClassificationCode", "")
        sos = row.get("SourceOfSupplyCode", "")
        if pcc and sos and (pcc, sos) not in KNOWN_CLASSIFICATION_SUPPLY_PAIRS:
            findings.append(Finding(
                "BR-23", SEVERITY_MINOR, "PartClassificationCode",
                f"The combination of PartClassificationCode {pcc!r} and "
                f"SourceOfSupplyCode {sos!r} was not seen in the reference data; "
                "worth a second look, though the reference sample may not be exhaustive.",
            ))

    primary = row.get("PrimaryPartNumberCode", "")
    secondary = row.get(schema.secondary_part_field, "")
    if primary and secondary and primary != secondary:
        if not _PART_NUMBER_RE.match(secondary):
            findings.append(Finding(
                "BR-24", SEVERITY_MINOR, schema.secondary_part_field,
                f"{schema.secondary_part_field} {secondary!r} differs from PrimaryPartNumberCode "
                f"and does not itself look like a valid part number.",
            ))
        else:
            findings.append(Finding(
                "BR-24", SEVERITY_INFO, schema.secondary_part_field,
                f"Part number supersession: {primary!r} -> {secondary!r}.",
            ))

    return findings


def check_referential_integrity(
    row: dict, schema: DatasetSchema, reference: Optional[ReferenceData], seen_keys: set
) -> List[Finding]:
    """BR-26..BR-28. Requires a ReferenceData instance (skipped if not supplied)."""
    findings: List[Finding] = []
    if reference is None:
        return findings

    key = reference.composite_key_of(row)
    if key in seen_keys:
        findings.append(Finding(
            "BR-26", SEVERITY_CRITICAL, "AssetName",
            f"Composite key {key} is duplicated within the enriched output; "
            "the enrichment join likely fanned out incorrectly.",
        ))
    seen_keys.add(key)

    match = reference.exact_match(row)
    if match is None:
        candidates = reference.candidates_for_registration_counter(row)
        if not candidates:
            findings.append(Finding(
                "BR-27", SEVERITY_CRITICAL, "AssetName",
                "No reference/master-data record found for this row's composite key or "
                "RegistrationCounter; enrichment cannot be verified.",
            ))
        else:
            asset_candidates = [c for c in candidates if c.get("AssetName") == row.get("AssetName")]
            if not asset_candidates:
                findings.append(Finding(
                    "BR-28", SEVERITY_CRITICAL, "AssetName",
                    f"RegistrationCounter {row.get('RegistrationCounter')!r} exists in the "
                    f"reference data for {len(candidates)} other asset(s), but none matches "
                    f"this row's AssetName {row.get('AssetName')!r} -- possible mismatched join.",
                ))
            else:
                findings.append(Finding(
                    "BR-27", SEVERITY_MAJOR, "AssetName",
                    "Row's exact task key was not found, but the same asset and "
                    "RegistrationCounter exist under a different Component/Modifier/TaskCounter "
                    "combination; verify the enriched component/modifier values.",
                ))

    return findings


def run_all_checks(
    row: dict,
    schema: DatasetSchema,
    reference: Optional[ReferenceData] = None,
    seen_keys: Optional[set] = None,
) -> List[Finding]:
    """Runs the full BR-01..BR-28 deterministic rule catalog against one row."""
    if seen_keys is None:
        seen_keys = set()
    findings: List[Finding] = []
    findings += check_completeness(row, schema)
    findings += check_domains(row, schema)
    findings += check_formats(row, schema)
    findings += check_cross_field_consistency(row, schema)
    findings += check_referential_integrity(row, schema, reference, seen_keys)
    return findings
