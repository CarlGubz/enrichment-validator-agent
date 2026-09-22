# Fleet Data Enrichment Validation Agent

An AI agent, built in Python and hosted as an **Azure AI Foundry Agent**,
that validates the correctness of enriched fleet maintenance strategy data
and produces an auditable, evidence-based confidence score for every row.

It is designed around one concrete workflow at this dealer: a customer's
partial maintenance-task extract (`NEO.csv` / `LAO.csv`) gets enriched by
joining it against **AMT Snowflake** master data (asset identity, parts,
supply source, commercial status). This agent is the **quality gate** that
runs after that enrichment step and before the enriched data is trusted
downstream — it does not do the enrichment itself.

## 1. Why this exists

Enrichment (a fuzzy join against a large master-data warehouse) can go
wrong in ways a human reviewer would catch instantly but a batch pipeline
won't: a row silently left half-blank, a part number pattern that doesn't
look right, a sales-status/PO-number combination that makes no commercial
sense, a maintenance-task template ID borrowed from the wrong asset. This
agent encodes those checks as explicit, data-grounded business rules, and
adds an LLM reasoning layer on top for the judgment calls a fixed rule
can't make (does the enriched part number actually make sense for this
component and task description?).

The output is not just "pass/fail" — every row gets a **posterior
confidence score**, a **verdict**, and a **list of findings with
rule IDs**, so a downstream analyst or system can decide what to trust
automatically and what to route to a human.

## 2. How the rules were derived

The business rules (`BUSINESS_RULES.md`) are not generic data-quality
boilerplate — every rule in that document was reverse-engineered from the
four sample files in `test-data/`:

| File | Role |
|---|---|
| `NEO.csv` | Partially populated "New Equipment"-type extract, pre-enrichment |
| `FMG NEO Aug 26.csv` | The same fleet family, already enriched against AMT Snowflake — used as ground truth |
| `LAO.csv` | Partially populated "Legacy/Life-of-Asset"-type extract, pre-enrichment |
| `FMG LAO Aug 26.csv` | The same fleet family, already enriched — ground truth |

Concretely, the rules were built by measuring, across the ~6,700 and
~2,000 row enriched reference files:

- which columns are **always** populated once enrichment has run (→
  completeness rules),
- what the **actual value domains** are for categorical fields like
  `SalesStatusCode`, `PartClassificationCode`, `SourceOfSupplyCode` (→
  domain rules),
- what **deterministic format relationships** hold 100% of the time, e.g.
  `SerialNumber` is always `"0" + <suffix of AssetName>` (→ format rules),
- what **cross-field relationships** hold (e.g. `PurchaseOrderNumber` is
  populated if and only if `SalesStatusCode == "Sale Confirmed"`, verified
  against every row in the reference data) (→ consistency rules),
- that `RegistrationCounter` is a **shared maintenance task-template ID**
  used by hundreds of different assets, not a unique asset identifier — a
  fact that would otherwise cause a naive uniqueness rule to generate
  massive false positives (→ referential-integrity rules).

Every threshold in `BUSINESS_RULES.md` was back-tested against the
reference files. Running the rule engine over the already-enriched
`FMG NEO Aug 26.csv` and `FMG LAO Aug 26.csv` files (i.e. checking known-good
data) yields a 99.99%/100% `AUTO_APPROVED` rate, with the rule engine
correctly surfacing the one genuine data gap the sample contains (a blank
`SalesStatusCode` on one row). Running it over the un-enriched `NEO.csv`
correctly rejects 100% of rows, since almost every enrichment-target field
is still blank. This is the calibration bar the rules are held to: they
should be near-silent on correct data and loud on incorrect or incomplete
data.

## 3. Architecture

```
                         ┌─────────────────────────────┐
                         │   Enriched NEO/LAO CSV       │
                         │  (output of enrichment step) │
                         └───────────────┬───────────────┘
                                         │
                                         ▼
 ┌────────────────────┐        ┌──────────────────────┐        ┌───────────────────────────┐
 │ AMT / FMG reference │──────▶ │  Deterministic Rule   │──────▶ │  Confidence Scoring        │
 │ data (or live AMT   │        │  Engine (BR-01..28)   │        │  (rules_engine findings +  │
 │ Snowflake query)     │        │  rules_engine.py      │        │   AI plausibility score)   │
 └────────────────────┘        └──────────┬───────────┘        └──────────────┬────────────┘
                                           │  findings, base_score                            │
                                           ▼                                                   │
                                ┌──────────────────────────┐                                  │
                                │ AI reasoning layer        │                                  │
                                │ (src/ai_reasoning.py +    │──────────────────────────────────┘
                                │ prompts/system_prompt.md) │
                                │ - given the deterministic │
                                │   findings as context     │
                                │ - reasons about semantic  │
                                │   plausibility             │
                                │ - returns structured JSON │
                                │   verdict per row         │
                                └──────────────────────────┘
                                           │
                                           ▼
                         ┌─────────────────────────────┐
                         │ Validated CSV: verdict,       │
                         │ posterior ConfidenceScore,    │
                         │ findings, reviewer rationale  │
                         └─────────────────────────────┘
```

`agent.py::run_validation_agent()` is the single, transport-agnostic
callable that wires the pipeline above together — give it a request dict,
get a response dict back. Two surfaces call it:

- **`main.py`** — a CLI for local runs, CI, and the calibration checks in
  §8, reading/writing local CSV files.
- **`foundry_app.py`** — an OpenAI-compatible **Responses**-protocol HTTP
  host (via `azure-ai-agentserver-responses`) used when this agent is
  deployed as a Microsoft Foundry **Hosted Agent** (see §9). Foundry has no
  shared filesystem with its caller, so this surface takes a Blob Storage
  path or inline base64 content instead of a local file path —
  `src/storage.py` and `src/io_utils.py` are what let `agent.py` accept
  any of the three.

Two layers inside `run_validation_agent`, deliberately kept separate and
independently testable:

1. **Deterministic layer** (`src/rules_engine.py`, `src/schema.py`,
   `src/reference_data.py`) — pure Python, no LLM call, implements every
   rule in `BUSINESS_RULES.md` that can be checked mechanically. Runs
   standalone via `--no-ai` for CI/local testing and does not require any
   Azure credentials.
2. **AI reasoning layer** (`src/ai_reasoning.py`, `prompts/system_prompt.md`)
   — calls the Foundry project's model gateway directly
   (`AIProjectClient(...).get_openai_client()`) with the deterministic
   findings already included as context, and adds a plausibility judgment
   plus a human-readable rationale that the rule engine cannot produce on
   its own. It is never required for the agent to run: with no model
   configured, or `use_ai=false`, the deterministic score stands on its
   own.

`src/confidence_scoring.py` combines both layers into the final,
auditable score using the rubric in `BUSINESS_RULES.md` §4.

## 4. Repository layout

```
main.py                     CLI entry point (python main.py ...) -- local/CI use
agent.py                    Transport-agnostic core: run_validation_agent(request) -> response
foundry_app.py               Microsoft Foundry Hosted Agent entrypoint (Responses protocol)
azure.yaml                  azd project file; `azd ai agent init` scaffolds/updates it
Dockerfile                   linux/amd64 image for a container-based Foundry deploy
requirements.txt            All Python dependencies -- the single file both `azd deploy` and Docker install
.env.example                 Environment variables template (local runs)
.env.foundry.example         Environment variables template (Foundry-hosted runs)
BUSINESS_RULES.md          Business rules / validation metrics (read this first)
README.md                  This file
prompts/
  system_prompt.md         The AI reasoning layer's system prompt / instructions
src/
  schema.py                 NEO vs LAO field expectations, known value domains
  reference_data.py         AMT/FMG reference-data lookup (CSV-backed; swappable for a live Snowflake query)
  rules_engine.py           Deterministic BR-01..BR-28 checks -> Finding objects
  confidence_scoring.py     Finding list -> posterior ConfidenceScore + verdict
  ai_reasoning.py            Foundry model-gateway call for the AI plausibility layer
  io_utils.py                 CSV read/write helpers (local path or inline base64)
  storage.py                  Local / Blob storage backend -- writes "<dataset_type>_final.csv" alongside the input
test-data/
  NEO.csv, LAO.csv                     Partial (pre-enrichment) extracts
  FMG NEO Aug 26.csv, FMG LAO Aug 26.csv   Enriched reference extracts (ground truth used to derive the rules)
```

`main.py`, `agent.py`, and `foundry_app.py` are kept at the project root
(rather than inside `src/`) because hosting/deployment tooling — including
`azd`'s Python Hosted Agent source-ZIP deploy — expects a top-level
entry-point script; `main.py` and `agent.py` also insert the project root
onto `sys.path` at startup so the `src` package resolves regardless of the
working directory the host launches them from.

## 5. The business rules and confidence-scoring metrics

Full detail, including every rule's ID, severity, and the empirical
evidence behind it, is in **[`BUSINESS_RULES.md`](BUSINESS_RULES.md)**.
Summary:

- **Completeness (BR-01–09)** — which fields must be non-blank once
  enrichment has run, per dataset type.
- **Domain (BR-10–15)** — categorical fields must fall within the value
  sets observed in AMT reference data (soft signal — unseen values are
  flagged for review, not auto-rejected, since the sample may not be
  exhaustive).
- **Format (BR-16–20)** — pattern-level checks such as the deterministic
  `SerialNumber` ↔ `AssetName` relationship, part-number shape, PO-number
  shape, valid `YYYYMMDD` dates.
- **Cross-field consistency (BR-21–25)** — e.g. `SalesLostReasonCode` iff
  `SalesStatusCode == "Lost"`; `PurchaseOrderNumber` iff
  `SalesStatusCode == "Sale Confirmed"`.
- **Referential integrity (BR-26–28)** — the enriched row's composite key
  must be unique and must resolve against the AMT/FMG master data, and
  the enrichment must not have attached the wrong asset's data to a task
  template shared across many assets.
- **Confidence scoring (BR-29–33)** — any `CRITICAL` finding caps the
  score at ≤ 0.40; any `MAJOR` finding caps it at ≤ 0.70; the final score
  blends the deterministic base score (70% weight) with the AI
  plausibility score (30% weight); every score is stored with the
  findings that produced it.

Row-level verdicts: `AUTO_APPROVED` (≥0.85), `NEEDS_REVIEW` (0.60–0.849),
`REJECTED` (<0.60), with two or more `CRITICAL` findings forcing
`REJECTED` regardless of the numeric score.

## 6. The agent prompt

**[`prompts/system_prompt.md`](prompts/system_prompt.md)** is the system
prompt used by the AI reasoning layer (`src/ai_reasoning.py`). It
instructs the model to:

1. Treat the deterministic findings it's given as context as ground
   truth — never recompute, contradict, or re-derive a rule violation
   from memory.
2. Add only genuinely new findings the deterministic layer could not
   catch (primarily semantic/contextual plausibility).
3. Return a strict JSON verdict per row (`ai_plausibility_score`,
   `ai_rationale`, `additional_findings`, `recommended_action`,
   `explanation_for_reviewer`).
4. Never invent or "fix" a field value — the agent is evaluative only.
5. Write reviewer-facing explanations for a fleet/parts domain analyst,
   not a data scientist.

Edit this file to tune the agent's behavior — it's read fresh on every
call, so no redeploy or re-registration step is needed for a local run;
for a deployed Hosted Agent, ship the updated file as part of the next
`azd deploy`.

## 7. Setup (local)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
```

`requirements.txt` intentionally installs everything needed for every
surface (CLI, AI reasoning layer, the Foundry Hosted Agent host, and the
Blob storage backend) from one file — see the note at the top of that
file for why it isn't split. None of those packages are *imported* by
`main.py --no-ai` at runtime (the Azure-dependent modules only import
their SDKs lazily, inside the functions that use them), so a `--no-ai`
dry run needs no credentials even though the packages are installed.

To run the AI reasoning layer locally (outside of a Foundry-hosted
deployment), copy `.env.example` to `.env` and fill in:
- `PROJECT_ENDPOINT` — your Foundry project's endpoint URL.
- `MODEL_DEPLOYMENT_NAME` — the deployed model's name (must match a model
  actually deployed in that Foundry project, not a generic model-family
  name).

Then authenticate with `az login` so `DefaultAzureCredential` can pick up
your credentials.

## 8. Running the validator

**Dry run (deterministic rules only — no Azure credentials required).**
This is the fastest way to sanity-check the rules against real data:

```bash
python main.py \
  --input "test-data/NEO.csv" \
  --reference "test-data/FMG NEO Aug 26.csv" \
  --dataset-type NEO \
  --output out/NEO_final.csv \
  --no-ai
```

**Full run, including the AI reasoning layer:**

```bash
python main.py \
  --input "test-data/FMG NEO Aug 26.csv" \
  --reference "test-data/FMG NEO Aug 26.csv" \
  --dataset-type NEO \
  --output out/NEO_final.csv \
  --batch-size 20
```

CLI arguments:

| Flag | Meaning |
|---|---|
| `--input` | The enriched CSV to validate (post-enrichment). |
| `--reference` | The AMT/FMG reference/master-data CSV used for BR-26..BR-28 (omit to skip referential-integrity checks). |
| `--dataset-type` | `NEO` or `LAO` — selects the correct field expectations from `src/schema.py`. |
| `--output` | Where to write the validated CSV. |
| `--no-ai` | Skip the AI reasoning layer; score using deterministic rules only. |
| `--batch-size` | Rows per AI reasoning call (default 20). |

### Output columns added to every row

| Column | Meaning |
|---|---|
| `RowId` | Stable identifier used to correlate rows with the AI reasoning layer's response. |
| `ValidationVerdict` | `AUTO_APPROVED` / `NEEDS_REVIEW` / `REJECTED`. |
| `PosteriorConfidenceScore` | The recomputed 0.0–1.0 confidence score (replaces the pre-enrichment seed score). |
| `FindingCount` | Number of rule/AI findings raised for this row. |
| `Findings` | JSON array of `{rule_id, severity, field, message}`. |
| `AIRationale` | The AI reasoning layer's plausibility rationale (blank in `--no-ai` mode). |

### What to expect against the sample data

Running the deterministic layer against the already-enriched reference
files as a self-check (validating them against themselves) yields:

- `FMG NEO Aug 26.csv`: 6,757 / 6,758 `AUTO_APPROVED`, 1 `NEEDS_REVIEW`
  (the genuine blank-`SalesStatusCode` row).
- `FMG LAO Aug 26.csv`: 1,971 / 1,971 `AUTO_APPROVED`.

Running it against the un-enriched `NEO.csv` (which is still 100% blank
on every enrichment target field) correctly yields 9,296 / 9,296
`REJECTED` — proving the agent won't rubber-stamp data that hasn't
actually been enriched yet.

## 9. Deploying to Microsoft Foundry (Hosted Agents)

The same core (`agent.py`, `src/`, `prompts/`, `BUSINESS_RULES.md`) deploys
to **Foundry Agent Service → Hosted Agents** ("bring your own code").
Nothing in `agent.py` or `src/` changes for this target — only the
entrypoint.

Added for this target:

| File | Purpose |
|---|---|
| `foundry_app.py` | Responses-protocol HTTP host wrapping `run_validation_agent`. |
| `Dockerfile` | linux/amd64 image (Foundry requires x86_64), Python 3.13, runs `python foundry_app.py`. |
| `azure.yaml` | azd project file; `azd ai agent init` scaffolds/updates it with the Foundry agent definition. |
| `.env.foundry.example` | Env vars for a local container run against the Foundry gateway. |
| `.dockerignore` / `.agentignore` | Keep the build context / source-ZIP deploy package clean. |

**Protocol.** `foundry_app.py` hosts the OpenAI-compatible **Responses**
protocol (`POST /responses`) via the `azure-ai-agentserver-responses` SDK,
not a bespoke Invocations protocol — so any OpenAI Responses-compatible
client can call it, and the platform manages session lifecycle for you.
This agent is single-turn (one file in, one report out), so it doesn't use
conversation history: the caller's message text *is* the
`run_validation_agent` request contract, JSON-encoded, and the reply text
*is* the response contract, JSON-encoded —

```python
resp = client.responses.create(model="<agent-name>", input=json.dumps({
    "dataset_type": "NEO",
    "input": {"content_base64": "...", "filename": "NEO.csv"},
    "reference": {"content_base64": "...", "filename": "FMG NEO Aug 26.csv"},
    "return_inline": True,
}))
result = json.loads(resp.output_text)
```

Note the request uses `content_base64` here rather than a local path —
a Hosted Agent has no shared filesystem with its caller, which is exactly
what `src/io_utils.py` and `agent.py`'s `return_inline` option exist for.

`GET /readiness` (SDK built-in) and `GET /health` (reports whether the AI
reasoning layer is configured) are both available for availability checks.

**Blob storage input/output (same directory, final filename).** Set
`STORAGE_BACKEND=azure_blob` (see `.env.foundry.example`) and `input`/
`reference` can instead be a bare `"<container>/<blob_name>"` path. The
validated CSV is written back **automatically** into the *same blob
directory* the input came from, as `"<dataset_type>_final.csv"` — nothing
about the destination needs to be named explicitly:

```python
resp = client.responses.create(model="<agent-name>", input=json.dumps({
    "dataset_type": "NEO",
    "input": "fmg-outbound/20260917-235351-976c79/NEO_enriched.csv",
    "reference": "fmg-inbound/FMG NEO Aug 26.csv",
}))
result = json.loads(resp.output_text)
print(result["output_csv_path"])  # "fmg-outbound/20260917-235351-976c79/NEO_final.csv"
```

This is implemented in `src/storage.py` (`AzureBlobStorage`, mirroring
this dealer's other Foundry agents' `core/storage.py`):

- `fetch_input()` accepts a full blob URL (with or without a SAS token) or
  a bare `container/blob` path, downloads it to a local temp file, and —
  only for the *main* `input` (not `reference`) — records its container
  and blob "directory" (everything between the container and the
  filename).
- `write_rows()` uploads the validated CSV into that same recorded
  directory, under the requested `output_name` (default
  `"<dataset_type>_final.csv"`, e.g. `NEO_final.csv` / `LAO_final.csv`) —
  there is no separate run-id subfolder for the output. The **container**
  it writes to is derived separately (`_derive_output_container`):
  - If the input container's name contains `"inbound"` (case-insensitive),
    that's replaced with `"outbound"` — e.g. `FMG-Inbound` → `FMG-outbound`.
  - Otherwise, if the input container is known but doesn't match that
    pattern — e.g. this agent was handed a file that already lives in
    `fmg-outbound`, written there by an upstream enrichment/matching step
    — results are written back into that **same** container. (An earlier
    version of this fell back to `OUTPUT_CONTAINER` here instead, which
    caused a "the specified container does not exist" error whenever that
    fallback container hadn't actually been created — don't reintroduce
    that fallback for this branch.)
  - Only when the input container is unknown at all (the input arrived as
    `content_base64`, which names no container) does it fall back to
    `OUTPUT_CONTAINER` (default `"outputs"`) — make sure that container
    actually exists in your storage account if you rely on this path.
- Authentication: set `AZURE_STORAGE_CONNECTION_STRING`, or
  `AZURE_STORAGE_ACCOUNT_URL` with Entra ID (`DefaultAzureCredential` —
  the Hosted Agent's managed identity in Azure, `az login` locally).
- `azure-storage-blob` (this backend's only extra dependency) is already
  in `requirements.txt` alongside everything else — see the note at the
  top of that file for why it's one file, not split by surface.
- With `STORAGE_BACKEND` unset (or `"local"`, the default), the exact same
  request shape works against local files/dirs instead — useful for
  testing the inbound/outbound naming convention without a real storage
  account by pointing `LOCAL_OUTPUT_DIR` at a scratch folder.

**Accepting the upstream enrichment/matching step's handoff payload
directly.** If whatever runs before this agent (a matching/enrichment
step, a Logic App) already emits a summary like this —

```json
{
  "CorrelationId": "123",
  "Container": "fmg-outbound",
  "CsvRows": 9296,
  "SnowflakeRows": 11514,
  "MatchedRows": 2406,
  "ExceptionRows": 6890,
  "EnrichedFile": "20260917-235351-976c79/NEO_enriched.csv",
  "ExceptionFile": "20260917-235351-976c79/NEO_exceptions.csv"
}
```

— you can pass it to this agent as-is, with no translation step. `agent.py`'s
`_normalize_request()` derives `input` from `Container` + `EnrichedFile`
(only when `input` isn't already given explicitly), infers `dataset_type`
from `"NEO"`/`"LAO"` appearing in `EnrichedFile`'s name, and echoes
`CorrelationId` back in the response for tracking. `EnrichedFile` is the
priority artifact — it's what gets fetched and validated; `ExceptionFile`
and the row-count fields are carried through untouched for traceability
but are not themselves read or validated by this agent.

**Deploy (recommended — azd):**
```bash
az login
azd ai agent init     # scaffolds/updates the Foundry agent definition + RBAC
azd deploy             # source-ZIP or container build, deploy, wire the endpoint
```
Python hosted agents default to **source-ZIP** deployment (no Docker
needed); the `Dockerfile` is there if you prefer a container image.

`azd ai agent init` is what actually populates `azure.yaml`'s `services:`
block with the Foundry project connection (`host: azure.ai.project`) and
this agent's hosted-agent definition (`host: azure.ai.agent`,
`entryPoint: foundry_app.py`, `protocols: [{protocol: responses}]`) — the
committed `azure.yaml` intentionally ships without a hardcoded project
endpoint/resource so it isn't tied to one Foundry project.

**Deploy (container, manual):**
```bash
docker build --platform linux/amd64 -t <acr>.azurecr.io/enrichment-validation-agent:latest .
docker push <acr>.azurecr.io/enrichment-validation-agent:latest
# register the image as a Hosted Agent via azd / Python SDK / REST
```

> Two version-sensitive notes, carried over from this dealer's other
> Foundry agents: confirm the exact **Responses endpoint path** and the
> **azd agent host** behavior against the current Foundry quickstart (the
> SDK and tooling are still evolving), and note the Foundry RBAC roles
> were recently renamed (Foundry User/Owner/Project Manager, formerly
> Azure AI …).

**Troubleshooting: `ModuleNotFoundError: No module named 'azure.ai.agentserver'`
after deploy.** This means the environment `foundry_app.py` actually ran in
never installed `azure-ai-agentserver-responses` — almost always because a
dependency file with a non-standard name (e.g. a separate
`requirements-foundry.txt`) was relied on. Azure's remote build (Oryx),
used by both `azd deploy`'s source-ZIP path and a Docker build, only
auto-installs a file literally named `requirements.txt` at the project
root; anything else is silently ignored, no error. That's why this
project keeps a single `requirements.txt` with everything in it (see the
note at the top of that file) instead of splitting a "base" file from a
"Foundry" file. If you hit this again after editing dependencies, check
that whatever new package you added landed in `requirements.txt` itself,
not a second file.

## 10. Extending to a live AMT Snowflake connection

`src/reference_data.py`'s `ReferenceData.load_csv`/`from_rows` are
intentionally the only places that know about CSV files. To point the
referential-integrity checks (BR-26–28) at live AMT Snowflake data instead
of a snapshot:

1. Add a `ReferenceData.from_snowflake(connection, dataset_type)`
   classmethod that runs a query returning the same columns used by
   `composite_key_fields` in `src/schema.py`, plus every field the rule
   engine reads.
2. Populate `by_composite_key`, `by_registration_counter`, and
   `by_asset_name` exactly as `from_rows` does.
3. Swap the constructor call in `agent.py::run_validation_agent`. Nothing
   in `rules_engine.py`, `confidence_scoring.py`, or `ai_reasoning.py`
   needs to change.

## 11. Testing

There is no separate test suite in this initial version; validation is
done by running the CLI in `--no-ai` mode against the four files in
`test-data/` and checking the verdict summary matches §8's "What to
expect" numbers (this is the calibration check performed while building
`BUSINESS_RULES.md`, and it should be re-run any time a rule's regex or
domain set is changed).
