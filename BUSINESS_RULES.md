# Business Rules — Fleet Maintenance Strategy Data Enrichment Validation

## 1. Purpose

`NEO.csv` and `LAO.csv` are partially-populated fleet maintenance strategy
extracts. Each row represents one **maintenance task** for one **asset**
(a piece of equipment) — e.g. "replace engine oil filter every 500 hours."
An enrichment process looks up each row against **AMT Snowflake** (the
source-of-truth asset/parts/commercial master data) and fills in the blank
columns. `FMG NEO Aug 26.csv` and `FMG LAO Aug 26.csv` are examples of the
*output* of that enrichment process for the FMG customer, and are used
in this document purely as **empirical evidence** of what a correctly
enriched row looks like (valid value domains, formats, and cross-field
relationships). They are not guaranteed to be defect-free — one rule
below (BR-06) is included specifically because it catches a real gap in
`FMG NEO Aug 26.csv` (a blank `SalesStatusCode`).

These rules are the metrics the validation agent (see `README.md`) applies
to a *newly enriched* NEO/LAO file to decide whether each row's enrichment
is trustworthy, and to compute/adjust the row's `ConfidenceScore`.

## 2. Schema reference

### 2.1 Fields common to NEO and LAO

| Field | Role | Populated pre-enrichment? |
|---|---|---|
| `BranchCode`, `SiteCode`, `FleetCode`, `CustomerCode` | Org hierarchy (from AMT) | No — 100% blank |
| `ModelCode` | Equipment model | NEO: yes · LAO: no |
| `AssetName` | Customer-facing asset code (e.g. `WC012`) | Yes |
| `SerialNumber` | AMT serial number (e.g. `0DMC00164`) | No — 100% blank |
| `RegistrationCounter` | Maintenance **strategy/task template** ID (NOT an asset ID — see BR-14) | Yes |
| `ComponentCode`, `ModifierCode` | Component + position code (e.g. `4303` / `LT`) | Partially (~15–28%) |
| `TaskTypeCode` | Always `RB` in observed data | No — 100% blank |
| `TaskCounterCode` | Task revision counter | NEO: yes · LAO: no |
| `StrategyTaskDescription` | Free-text task description | Yes |
| `FrequencyValue` | Interval (hours) between occurrences | Mostly yes |
| `LifeToDateValue` | Hours accumulated on the component to date | NEO: no · LAO: yes |
| `PrimaryPartNumberCode` + `NextPartNumberCode` (NEO) / `ActualPartNumberCode` (LAO) | Part(s) consumed by the task | No |
| `SourceOfSupplyCode` | Supplying plant/branch code | No |
| `StrategyUOMCode` | Unit of measure — always `H` (hours) in observed data | Yes |
| `StrategyUsageValue` + `NewStrategyUsageValue` (NEO) / `LastStrategyUsageValue` (LAO) | Usage at strategy due date | Mixed — see §2.2 |
| `StrategyDate` + `NewStrategyDate` (NEO) / `LastStrategyDate` (LAO) | Due date(s) for the task | Mixed — see §2.2 |
| `SalesStatusCode` | Commercial pipeline status | No |
| `PartClassificationCode` (NEO only) | Contract/pricing classification | No |
| `SalesLostReasonCode` | Reason a sale was lost | No |
| `SalesStatusCommentsNote` | Free-text sales history | No |
| `PurchaseOrderNumber` | Customer PO, once confirmed | No |
| `ConfidenceScore` | Pre-enrichment *source-completeness* prior (see §2.3) | Yes (seed value) |

### 2.2 NEO vs. LAO asymmetry — do not treat as a defect

The two extracts come from different upstream processes and populate a
different subset of the "current vs. new/last" date/usage columns before
enrichment:

- **NEO**: `StrategyDate`, `NewStrategyDate` populated; `LifeToDateValue`,
  `StrategyUsageValue`, `NewStrategyUsageValue` blank.
- **LAO**: `LifeToDateValue`, `LastStrategyUsageValue` populated;
  `StrategyUsageValue`, `StrategyDate`, `LastStrategyDate` blank.

The validation agent must apply the **per-dataset-type expectation**
(`src/schema.py`), not a single shared expectation, when deciding whether
a blank field is a defect or is normal for that file type.

### 2.3 The seed `ConfidenceScore`

The `ConfidenceScore` already present in `NEO.csv`/`LAO.csv` is **not**
the enrichment-quality score — it is a prior computed from how much
identifying information the source row carried *before* any AMT lookup
happened:

| Has `ComponentCode` | Has `FrequencyValue` | Observed seed score |
|---|---|---|
| No | No | 0.484 (NEO) / ~0.38 (LAO) |
| No | Yes | 0.532 |
| Yes | Yes | 0.592 – 0.678 |

The validation agent's job is to **replace this prior with a posterior
confidence score** that reflects whether the actual enrichment that was
performed is correct, complete, and consistent (see §5).

## 3. Business rule catalog

Each rule has an ID, a severity, and a scope (which dataset types it
applies to). Severities:

- **CRITICAL** — the enriched value is almost certainly wrong or the
  record cannot be trusted for commercial/parts use. Large confidence
  penalty; row should be routed to human review before use.
- **MAJOR** — a required field is missing/malformed post-enrichment, or
  a strong cross-field inconsistency exists. Moderate confidence penalty.
- **MINOR** — a soft/statistical expectation is violated (e.g. a value
  outside the previously-observed domain, which may simply mean the
  observed domain was incomplete). Small confidence penalty, agent
  should reason about it rather than auto-fail.
- **INFO** — observation logged for audit trail; no penalty.

### A. Completeness rules (post-enrichment mandatory fields)

| ID | Rule | Severity |
|---|---|---|
| BR-01 | `BranchCode`, `SiteCode`, `FleetCode`, `CustomerCode` must be non-blank after enrichment. | CRITICAL |
| BR-02 | `SerialNumber` must be non-blank after enrichment. | CRITICAL |
| BR-03 | `ComponentCode` and `ModifierCode` must be non-blank after enrichment (even if blank in the source row). | MAJOR |
| BR-04 | `TaskTypeCode` must be non-blank after enrichment. | MAJOR |
| BR-05 | `PrimaryPartNumberCode` and the dataset's secondary part field (`NextPartNumberCode`/`ActualPartNumberCode`) must be non-blank after enrichment. | MAJOR |
| BR-06 | `SalesStatusCode` must be non-blank after enrichment. (Empirically, ~0.01% of reference rows violate this — a genuine upstream defect; the rule exists to catch exactly this class of gap.) | MAJOR |
| BR-07 | `SourceOfSupplyCode` must be non-blank after enrichment. | MAJOR |
| BR-08 | `PartClassificationCode` must be non-blank after enrichment (NEO only). | MAJOR |
| BR-09 | `StrategyUsageValue` (and dataset-specific counterpart) must be non-blank after enrichment. | MINOR |

### B. Domain / enumeration rules

| ID | Rule | Severity |
|---|---|---|
| BR-10 | `SalesStatusCode` ∈ {`Unknown`, `Confident`, `Unlikely`, `Sale Confirmed`, `Lost`}. Unseen values → review, not auto-fail. | MAJOR / MINOR if unseen |
| BR-11 | `PartClassificationCode` ∈ {`503 PEX CONTRACT`, `000 CAT NEW`, `000 CAT REMAN`, `486 FLEXI`, `CUSTOMER REBUILD`, `445 CUSTOMER OWNED`} (NEO only). Unseen values → review. | MAJOR / MINOR if unseen |
| BR-12 | `StrategyUOMCode` expected to be `H`. Any other value is unusual given the observed data and should be flagged for review (not hard-failed, since other UOMs may legitimately exist in the full AMT domain). | MINOR |
| BR-13 | `TaskTypeCode` expected to be `RB`. Other values flagged for review. | MINOR |
| BR-14 | `ModifierCode` should match the observed positional-code pattern: `00`, or 1–2 letters optionally followed by a 2-digit number (e.g. `RI`, `LT`, `L01`, `R02`, `MN`). | MINOR |
| BR-15 | `SourceOfSupplyCode` should be one of the observed plant codes (`000`, `501`, `503`, `483`, `485`, `666`, `445`, or a recognizable branch code such as `BUC`/`AKS`). Unseen values → review. | MINOR |

### C. Format / pattern rules

| ID | Rule | Severity |
|---|---|---|
| BR-16 | `SerialNumber` must equal `"0" + <suffix>` where `<suffix>` is the token following `" - "` in `AssetName` (e.g. `AssetName="WC012 - DMC00164"` ⇒ `SerialNumber="0DMC00164"`). This pattern held for **100%** of the 6,758-row NEO reference sample. | CRITICAL |
| BR-17 | Part number fields (`PrimaryPartNumberCode`, `NextPartNumberCode`/`ActualPartNumberCode`) should be alphanumeric, 5–14 characters, containing at least one digit (CAT-style part numbers, e.g. `3531688X`, `1U3182X`, `10R9966`, and side/position-suffixed variants like `3377940XR`, `3510988XAHS`, `3288492XFSRAHS`). Real CAT catalog numbers are irregular enough that this is a *soft* sanity check — the authoritative check is the exact match against the reference/master data (BR-27). | MINOR |
| BR-18 | `PurchaseOrderNumber`, when populated, must be a 10-digit numeric string starting with `45` (SAP PO number convention observed in 100% of the 77 populated reference rows). | MAJOR |
| BR-19 | `StrategyDate`, `NewStrategyDate`/`LastStrategyDate` must be 8-digit `YYYYMMDD` strings that parse to a valid calendar date. | MAJOR |
| BR-20 | `RegistrationCounter`, `ComponentCode`, `TaskCounterCode` must be non-empty alphanumeric codes (no embedded whitespace). | MINOR |

### D. Cross-field consistency rules

| ID | Rule | Severity |
|---|---|---|
| BR-21 | If `SalesStatusCode == "Lost"`, then `SalesLostReasonCode` must be non-blank. If `SalesStatusCode != "Lost"`, `SalesLostReasonCode` should be blank. | MAJOR |
| BR-22 | `PurchaseOrderNumber` should be populated **if and only if** `SalesStatusCode == "Sale Confirmed"`. (Observed: 76/76 "Sale Confirmed" rows had a PO; only 1/6,681 non-confirmed row had one — treat the non-confirmed+PO case as MINOR since rare legitimate exceptions exist.) | MAJOR (missing PO on Confirmed) / MINOR (PO present on non-Confirmed) |
| BR-23 | `PartClassificationCode` and `SourceOfSupplyCode` should co-occur as one of the combinations already observed in the AMT reference data. **This is not a strict numeric-prefix match** — e.g. `503 PEX CONTRACT` legitimately pairs with `SourceOfSupplyCode` `503`, `501`, *or* `666`, and `486 FLEXI` pairs with `483` or `485` (never literally `486`). Only flag combinations that were never seen in the reference sample, and treat it as a soft signal since the sample may not be exhaustive. | MINOR |
| BR-24 | `PrimaryPartNumberCode` and the secondary part field usually match (~97–98% of reference rows). When they differ, that alone is not an error (it represents a part-number supersession, logged as INFO); if the secondary field *also* fails the BR-17 sanity pattern, log an additional MINOR finding prompting a master-data check. | INFO / MINOR |
| BR-25 | `ModelCode` in the enriched row should be a superset/extension of the source `ModelCode` token when the source file supplied one (e.g. source `785D` vs. enriched `785DWC` is acceptable; an enriched `ModelCode` that shares no common prefix with the source token is suspect). | MAJOR |

### E. Referential integrity rules (require the AMT/FMG reference lookup)

| ID | Rule | Severity |
|---|---|---|
| BR-26 | The composite key `AssetName + RegistrationCounter + ComponentCode + ModifierCode + TaskCounterCode` must be unique within the enriched output (it is unique in 100% of the 6,758-row reference). A duplicate indicates the enrichment join fanned out incorrectly. | CRITICAL |
| BR-27 | Every enriched row's composite key (or, when an exact composite match is unavailable, `AssetName + RegistrationCounter`) should resolve to at least one record in the AMT/FMG reference/master data. Rows that cannot be matched at all should be flagged for manual review rather than silently enriched with a best guess. | CRITICAL |
| BR-28 | When multiple reference rows share the same `RegistrationCounter` (it is a shared task-template ID, not an asset ID — 203 distinct templates observed across 6,758 rows), the enrichment must have selected the reference row whose `AssetName`/`SerialNumber` actually matches the source asset, not merely any row with the same `RegistrationCounter`. | CRITICAL |

### F. Confidence-score rules

| ID | Rule |
|---|---|
| BR-29 | The posterior `ConfidenceScore` must be recomputed from the outcome of BR-01…BR-28, not copied from the pre-enrichment seed value. |
| BR-30 | Any CRITICAL finding caps the row's confidence score at ≤ 0.40 regardless of how many other checks pass. |
| BR-31 | Any MAJOR finding caps the row's confidence score at ≤ 0.70. |
| BR-32 | A row with zero findings may still be capped below 1.0 if the AI reviewer judges the semantic content (e.g. `StrategyTaskDescription` vs. `ComponentCode`/`ModifierCode`) to be implausible — deterministic rules alone cannot catch every error. |
| BR-33 | The final confidence score and the list of findings that produced it must both be persisted per row (auditability) — never emit a bare number. |

## 4. Scoring rubric (used by `src/confidence_scoring.py`)

Final score = deterministic base score (rule engine) blended with an AI
plausibility adjustment:

```
base_score = 1.0
for finding in findings:
    base_score -= PENALTY[finding.severity]   # CRITICAL 0.35, MAJOR 0.15, MINOR 0.05
base_score = clamp(base_score, 0.0, 1.0)
base_score = min(base_score, CAP[worst_severity_present])   # BR-30 / BR-31

final_score = clamp(
    0.7 * base_score + 0.3 * ai_plausibility_score,
    0.0, 1.0
)
```

`ai_plausibility_score` (0.0–1.0) is produced by the LLM reasoning step
described in `prompts/system_prompt.md`, which looks at fields the
deterministic engine cannot fully evaluate (free text vs. codes,
task-description semantics, whether the *combination* of values makes
mechanical/commercial sense).

## 5. Row-level verdicts

| Final score | Verdict |
|---|---|
| ≥ 0.85 | `AUTO_APPROVED` |
| 0.60 – 0.849 | `NEEDS_REVIEW` |
| < 0.60 | `REJECTED` |

CRITICAL findings always force at least `NEEDS_REVIEW` regardless of
score; two or more CRITICAL findings force `REJECTED`.
