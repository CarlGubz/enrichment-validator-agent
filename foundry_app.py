"""Microsoft Foundry — Hosted Agent entrypoint (Responses protocol).

Thin wrapper over agent.run_validation_agent, the Foundry counterpart of a
plain CLI/HTTP wrapper. The entire core (src/, agent.py, BUSINESS_RULES.md,
prompts/) is unchanged -- deploying to Foundry only swaps the entrypoint.

This host implements the OpenAI-compatible **Responses** protocol via the
`azure-ai-agentserver-responses` SDK (POST /responses) rather than a bespoke
Invocations (POST /invocations) payload. The platform then manages
conversation history/session lifecycle and any OpenAI-compatible SDK can
call the agent directly.

run_validation_agent's request/response contract (see agent.py) doesn't map
onto a chat message naturally -- this agent validates a file and returns a
structured report, it doesn't converse -- so the contract travels as JSON
text: the caller's message text *is* the run_validation_agent request dict,
JSON-encoded, and the reply text *is* the run_validation_agent response
dict, JSON-encoded. Any OpenAI Responses-compatible client can call it, e.g.:

    client.responses.create(model="<agent>", input=json.dumps(request_contract))

With STORAGE_BACKEND=azure_blob configured (see src/storage.py,
.env.foundry.example), `input`/`reference` can be a bare
"<container>/<blob_name>" path instead of inline content, and the
validated CSV is written back automatically to the matching "-outbound"
container -- e.g. input "fmg-inbound/NEO.csv" writes results to
"fmg-outbound/<run_id>/NEO_validated.csv":

    client.responses.create(model="<agent>", input=json.dumps({
        "dataset_type": "NEO",
        "input": "fmg-inbound/NEO.csv",
        "reference": "fmg-inbound/FMG NEO Aug 26.csv",
    }))

Deploy (recommended):
    az login
    azd ai agent init      # scaffolds/updates the Foundry agent definition + RBAC
    azd deploy              # source-ZIP or container build, deploy, wire the endpoint

Deploy (container, manual):
    docker build --platform linux/amd64 -t <acr>.azurecr.io/enrichment-validation-agent:latest .
    docker push <acr>.azurecr.io/enrichment-validation-agent:latest
    # then register the image as a Hosted Agent (SDK/REST/azd), protocol=responses
"""
import asyncio
import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    TextResponse,
)

from agent import run_validation_agent

logger = logging.getLogger(__name__)

app = ResponsesAgentServerHost(
    # This agent is stateless / single-turn (one file in, one report out) -- it
    # has no use for platform-managed conversation history.
    options=ResponsesServerOptions(default_fetch_history_count=1),
)


def _extract_payload(text: str) -> dict:
    """Parse the run_validation_agent request contract out of the caller's
    message text (see agent.py) -- optionally wrapped in a {"payload": {...}}
    / {"input_data": {...}} envelope for callers that prefer a generic outer
    shape.
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(body, dict):
        return {}
    if "dataset_type" in body or "input" in body:  # already the raw contract
        return body
    for key in ("payload", "input_data", "body", "data"):
        inner = body.get(key)
        if isinstance(inner, dict):
            return inner
    return body


# GET /health -- kept alongside the SDK's built-in GET /readiness for continuity
# with existing health-probe configuration (e.g. Logic Apps / Container Apps).
async def _health(_request: Request) -> JSONResponse:
    from src.ai_reasoning import ai_configured

    return JSONResponse({"status": "ok", "ai_configured": ai_configured()})

app.add_route("/health", _health, methods=["GET"])


@app.response_handler
async def handle_create(
    request: CreateResponse,
    context: ResponseContext,
    _cancellation_signal: asyncio.Event,
):
    """Run the validation agent and return the run_validation_agent result as
    response text."""
    user_text = await context.get_input_text()
    payload = _extract_payload(user_text)

    # run_validation_agent is synchronous (file I/O + LLM calls) -- keep it off
    # the event loop.
    result = await asyncio.get_running_loop().run_in_executor(None, run_validation_agent, payload)

    return TextResponse(context, request, text=json.dumps(result))


if __name__ == "__main__":
    # Local dev:  python foundry_app.py
    app.run(port=int(os.getenv("PORT", "8088")))
