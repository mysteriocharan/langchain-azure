# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "deepagents>=0.7.1,<0.8",
#     "fastapi>=0.141,<1",
#     "langchain-azure-ai",
#     "langchain-azure-storage[deepagents]",
#     "langchain[anthropic,openai]",
#     "uvicorn[standard]>=0.44,<1",
# ]
#
# [tool.uv.sources]
# langchain-azure-storage = { path = "../../libs/azure-storage", editable = true }
# ///
"""Serve the observable mortgage packet Deep Agents demonstration."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from base64 import b64decode
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemPermission
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_azure_storage.deepagents import AzureBlobBackend

WEB_DIR = Path(__file__).with_name("mortgage-demo")
SHARED_STYLES = Path(__file__).with_name("web-demo") / "styles.css"
FINAL_DECISION = "04-underwriting-decision.md"
STAGE_AGENTS = (
    "intake-split-agent",
    "classification-agent",
    "extraction-agent",
    "underwriting-agent",
)
STAGE_OUTPUTS = (
    "01-packet-index.json",
    "02-classification.json",
    "03-extracted-facts.json",
    FINAL_DECISION,
)
DEFAULT_TIMEOUT_SECONDS = 55
DEFAULT_CREDENTIAL_PROCESS_TIMEOUT_SECONDS = 60
RUN_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

app = FastAPI(title="Mortgage Packet processing")


def _required_environment_variable(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Set {name} in .env before starting the mortgage demo")
    return value


def _normalize_prefix(value: str) -> str:
    parts = [part for part in value.strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise RuntimeError("Blob prefixes cannot contain '.' or '..' segments")
    return "/".join(parts) + ("/" if parts else "")


def _storage_settings() -> tuple[str, str, str, str, str]:
    return (
        _required_environment_variable("AZURE_STORAGE_ACCOUNT_URL"),
        os.environ.get(
            "AZURE_STORAGE_MORTGAGE_SOURCE_CONTAINER", "mortgage-packets"
        ),
        _normalize_prefix(
            os.environ.get(
                "AZURE_STORAGE_MORTGAGE_SOURCE_PREFIX", "MORT-2026-0042"
            )
        ),
        os.environ.get(
            "AZURE_STORAGE_MORTGAGE_OUTPUT_CONTAINER", "mortgage-decisions"
        ),
        _normalize_prefix(
            os.environ.get("AZURE_STORAGE_MORTGAGE_OUTPUT_PREFIX", "demo-runs")
        ),
    )


def _timeout_seconds() -> int:
    value = int(os.environ.get("MORTGAGE_DEMO_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    if not 20 <= value <= 60:
        raise RuntimeError("MORTGAGE_DEMO_TIMEOUT_SECONDS must be between 20 and 60")
    return value


def _credential_process_timeout_seconds() -> int:
    value = int(
        os.environ.get(
            "AZURE_CREDENTIAL_PROCESS_TIMEOUT_SECONDS",
            DEFAULT_CREDENTIAL_PROCESS_TIMEOUT_SECONDS,
        )
    )
    if not 15 <= value <= 120:
        raise RuntimeError(
            "AZURE_CREDENTIAL_PROCESS_TIMEOUT_SECONDS must be between 15 and 120"
        )
    return value


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (_content_text(item) for item in content)))
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        nested = content.get("content")
        if isinstance(nested, (str, list, dict)):
            return _content_text(nested)
    return ""


def _build_model(credential: DefaultAzureCredential) -> Any:
    model_name = _required_environment_variable("MODEL_NAME")
    endpoint_variables = (
        "AZURE_AI_PROJECT_ENDPOINT",
        "AZURE_AI_OPENAI_ENDPOINT",
        "AZURE_OPENAI_ENDPOINT",
    )
    if any(os.environ.get(name) for name in endpoint_variables):
        from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel

        return AzureAIOpenAIApiChatModel(
            credential=credential,
            model=model_name,
            disable_streaming=True,
            reasoning_effort="minimal",
            verbosity="low",
            max_retries=2,
            timeout=_timeout_seconds(),
        )
    return model_name


def _blob_url(account_url: str, container: str, blob_name: str) -> str:
    return (
        f"{account_url.rstrip('/')}/{quote(container)}/"
        f"{quote(blob_name, safe='/')}"
    )


def _read_text(result: Any) -> str:
    file_data = result.file_data
    if file_data is None:
        raise RuntimeError("Blob read returned no file data")
    content = file_data["content"]
    encoding = file_data.get("encoding", "utf-8")
    if encoding == "utf-8":
        return content
    if encoding == "base64":
        return b64decode(content).decode("utf-8")
    raise RuntimeError(f"Unsupported Blob content encoding: {encoding}")


def _excerpt_lines(content: str, offset: int, count: int = 5) -> list[dict[str, Any]]:
    lines = []
    for index, text in enumerate(content.splitlines(), offset + 1):
        if text.strip():
            lines.append({"line": index, "text": text[:180]})
        if len(lines) == count:
            break
    return lines


def _tool_event(agent_name: str, call: dict[str, Any]) -> dict[str, Any] | None:
    args = call.get("args") or {}
    tool_name = str(call.get("name", "tool"))
    call_id = str(call.get("id") or "")

    if tool_name == "task":
        target = args.get("subagent_type")
        description = args.get("description")
        if not target or not description:
            return None
        return {
            "kind": "delegation",
            "callId": call_id,
            "agent": agent_name,
            "tool": tool_name,
            "targetAgent": str(target),
            "summary": str(description).splitlines()[0],
        }

    path = args.get("file_path") or args.get("path")
    if tool_name in {"ls", "glob", "read_file", "write_file", "edit_file"}:
        if path is None and tool_name != "glob":
            return None
        return {
            "kind": "filesystem",
            "callId": call_id,
            "agent": agent_name,
            "tool": tool_name,
            "path": str(path or args.get("pattern", "/")),
        }

    return {
        "kind": "tool",
        "callId": call_id,
        "agent": agent_name,
        "tool": tool_name,
        "summary": f"Called {tool_name}",
    }


def _create_subagents(
    credential: DefaultAzureCredential, backend: CompositeBackend
) -> dict[str, Any]:
    prompts = {
        "intake-split-agent": (
                "Read /source/packet-manifest.json. Compare documents with the expected "
                "document list. Write only /output/01-packet-index.json with packet_id, "
                "document entries, page ranges, and missing_documents. Be concise."
        ),
        "classification-agent": (
                "Read /source/packet-manifest.json. "
                "Write only /output/02-classification.json with packet_id and one entry "
                "per source file containing file, document_type, and confidence."
        ),
        "extraction-agent": (
                "In your first response, make four read_file tool calls in parallel for "
                "/source/loan-application.json, /source/income-verification.txt, "
                "/source/bank-assets.csv, and /source/property-appraisal.md. Then write "
                "only /output/03-extracted-facts.json. "
                "Include packet_id, declared and verified income, monthly debt, requested "
                "loan, purchase price, down payment, latest liquid assets, appraised value, "
                "and a source_path for every fact. Do not infer values."
        ),
        "underwriting-agent": (
                "In your first response, make three read_file tool calls in parallel for "
                "/output/03-extracted-facts.json, /output/01-packet-index.json, and "
                "/source/underwriting-policy.md. Calculate LTV using the lower of purchase "
                "price or appraised value and DTI using verified monthly income. Verify "
                "assets and missing documents. Write only /output/04-underwriting-decision.md "
                "with concise Decision, Calculations, Conditions, and Evidence sections. "
                "Cite /source/ paths."
        ),
    }
    model = _build_model(credential)
    return {
        name: create_deep_agent(
            model=model,
            backend=backend,
            system_prompt=prompt,
            permissions=[
                FilesystemPermission(
                    operations=["write"], paths=["/source/**"], mode="deny"
                )
            ],
        )
        for name, prompt in prompts.items()
    }


async def _emit_transfer(
    pending: dict[str, Any],
    source: AzureBlobBackend,
    output: AzureBlobBackend,
    emit: Callable[..., Awaitable[None]],
) -> None:
    tool = pending["tool"]
    if tool not in {"read_file", "write_file", "edit_file"}:
        return

    path = pending["path"]
    args = pending["args"]
    if path.startswith("/source/"):
        backend = source
        backend_path = "/" + path.removeprefix("/source/")
        direction = "read"
    elif path.startswith("/output/"):
        backend = output
        backend_path = "/" + path.removeprefix("/output/")
        direction = "read" if tool == "read_file" else "write"
    else:
        return

    offset = max(0, int(args.get("offset", 0) or 0))
    requested_limit = max(1, int(args.get("limit", 5) or 5))
    result = await backend.aread(backend_path, offset=offset, limit=min(requested_limit, 8))
    if result.error is not None:
        return
    await emit(
        "data.transfer",
        direction=direction,
        agent=pending["agent"],
        path=path,
        tool=tool,
        lines=_excerpt_lines(_read_text(result), offset),
    )


async def _stream_agent(
    agent: Any,
    prompt: str,
    source: AzureBlobBackend,
    output: AzureBlobBackend,
    emit: Callable[..., Awaitable[None]],
    root_agent_name: str = "orchestrator",
) -> tuple[str, list[str]]:
    accumulated: dict[str, tuple[str, AIMessageChunk]] = {}
    emitted_calls: set[str] = set()
    pending_calls: dict[str, dict[str, Any]] = {}
    completed_handoffs: list[str] = []
    final_state: dict[str, Any] = {}

    async for namespace, stream_mode, data in agent.astream(
        {"messages": [{"role": "user", "content": prompt}]},
        stream_mode=["messages", "values"],
        subgraphs=True,
    ):
        if stream_mode == "values":
            if not namespace:
                final_state = data
            continue

        message, metadata = data
        agent_name = str(metadata.get("lc_agent_name") or root_agent_name)
        if isinstance(message, ToolMessage):
            call_id = str(message.tool_call_id or "")
            pending = pending_calls.get(call_id)
            await emit(
                "tool.result",
                callId=call_id,
                agent=agent_name,
                tool=message.name or "tool",
                status=message.status or "success",
            )
            if pending and (message.status or "success") == "success":
                if pending["tool"] == "task":
                    completed_handoffs.append(pending["targetAgent"])
                    await emit(
                        "handoff.completed",
                        agent=pending["targetAgent"],
                        handoff=len(completed_handoffs),
                    )
                await _emit_transfer(pending, source, output, emit)
            continue

        tool_calls: list[dict[str, Any]] = []
        if isinstance(message, AIMessageChunk) and message.id:
            previous = accumulated.get(message.id)
            combined = previous[1] + message if previous else message
            accumulated[message.id] = (agent_name, combined)
            tool_calls = combined.tool_calls
        elif isinstance(message, AIMessage):
            tool_calls = message.tool_calls

        for index, call in enumerate(tool_calls):
            event = _tool_event(agent_name, call)
            if event is None:
                continue
            call_key = event["callId"] or f"{id(message)}:{index}:{event['tool']}"
            args = dict(call.get("args") or {})
            pending_calls[call_key] = {**event, "args": args}
            if call_key in emitted_calls:
                continue
            emitted_calls.add(call_key)
            await emit("tool.call", **event)

    messages = final_state.get("messages", [])
    response = _content_text(messages[-1].content) if messages else ""
    response = response or "Packet review completed."
    return response, completed_handoffs


async def _recover_missing_decision(
    output: AzureBlobBackend,
    response: str,
    emit: Callable[..., Awaitable[None]],
) -> None:
    path = f"/{FINAL_DECISION}"
    if (await output.aread(path, limit=1)).error is None:
        return

    content = response.strip()
    required_sections = ("decision", "calculations", "conditions", "evidence")
    if not content or any(section not in content.lower() for section in required_sections):
        raise RuntimeError(
            "Underwriting returned without writing the final output or providing a "
            "complete decision response"
        )

    result = await output.awrite(path, content)
    if result.error is not None:
        raise RuntimeError(f"Could not persist recovered underwriting output: {result.error}")

    await emit(
        "output.recovered",
        agent="orchestrator",
        sourceAgent="underwriting-agent",
        path=f"/output/{FINAL_DECISION}",
    )
    await emit(
        "data.transfer",
        direction="write",
        agent="orchestrator",
        path=f"/output/{FINAL_DECISION}",
        tool="persist_returned_response",
        lines=_excerpt_lines(content, 0),
    )


async def _orchestrate_subagents(
    agents: dict[str, Any],
    prompt: str,
    source: AzureBlobBackend,
    output: AzureBlobBackend,
    emit: Callable[..., Awaitable[None]],
) -> tuple[str, list[str]]:
    completed: list[str] = []
    completion_lock = asyncio.Lock()

    async def run_stage(agent_name: str) -> str:
        output_name = STAGE_OUTPUTS[STAGE_AGENTS.index(agent_name)]
        call_id = f"stage-{agent_name}-{secrets.token_hex(3)}"
        await emit(
            "tool.call",
            kind="delegation",
            callId=call_id,
            agent="orchestrator",
            tool="task",
            targetAgent=agent_name,
            summary=f"Process MORT-2026-0042 with {agent_name}",
        )
        response, _ = await _stream_agent(
            agents[agent_name],
            (
                f"{prompt}\nExecute only your configured stage. You must call write_file "
                f"with file_path /output/{output_name} before returning."
            ),
            source,
            output,
            emit,
            root_agent_name=agent_name,
        )
        if agent_name == "underwriting-agent":
            await _recover_missing_decision(output, response, emit)
        await emit(
            "tool.result",
            callId=call_id,
            agent="orchestrator",
            tool="task",
            status="success",
        )
        async with completion_lock:
            completed.append(agent_name)
            handoff = len(completed)
        await emit("handoff.completed", agent=agent_name, handoff=handoff)
        return response

    await asyncio.gather(
        *(run_stage(agent_name) for agent_name in STAGE_AGENTS[:3])
    )
    final_response = await run_stage("underwriting-agent")
    return final_response, completed


async def _run_demo(
    prompt: str, emit: Callable[..., Awaitable[None]]
) -> None:
    account_url, source_container, source_prefix, output_container, output_base = (
        _storage_settings()
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id += f"-{secrets.token_hex(2)}"
    run_prefix = f"{output_base}{run_id}/"
    timeout_seconds = _timeout_seconds()

    await emit(
        "run.started",
        runId=run_id,
        model=_required_environment_variable("MODEL_NAME"),
        account=account_url,
        sourceContainer=source_container,
        sourcePrefix=source_prefix,
        outputContainer=output_container,
        outputPrefix=run_prefix,
        timeoutSeconds=timeout_seconds,
    )

    credential_process_timeout = _credential_process_timeout_seconds()
    model_credential = DefaultAzureCredential(
        process_timeout=credential_process_timeout
    )
    blob_credential = AsyncDefaultAzureCredential(
        process_timeout=credential_process_timeout
    )
    async with AsyncExitStack() as stack:
        stack.callback(model_credential.close)
        stack.push_async_callback(blob_credential.close)
        source = await stack.enter_async_context(
            AzureBlobBackend(
                account_url=account_url,
                container_name=source_container,
                prefix=source_prefix,
                credential=blob_credential,
            )
        )
        output = await stack.enter_async_context(
            AzureBlobBackend(
                account_url=account_url,
                container_name=output_container,
                prefix=run_prefix,
                credential=blob_credential,
            )
        )

        listing = await source.als("/")
        if listing.error is not None:
            raise RuntimeError(f"Could not list mortgage packet: {listing.error}")
        files = [
            {
                **entry,
                "virtualPath": f"/source{entry['path']}",
                "blobName": f"{source_prefix}{entry['path'].lstrip('/')}",
                "url": _blob_url(
                    account_url,
                    source_container,
                    f"{source_prefix}{entry['path'].lstrip('/')}",
                ),
            }
            for entry in listing.entries
            if not entry.get("is_dir")
        ]
        if not files:
            raise RuntimeError(
                f"No packet files found at {source_container}/{source_prefix}"
            )
        await emit("blob.inventory", files=files)

        backend = CompositeBackend(
            default=StateBackend(),
            routes={"/source/": source, "/output/": output},
        )
        agents = _create_subagents(model_credential, backend)
        await emit("agent.ready", agents=["orchestrator", *STAGE_AGENTS])

        try:
            async with asyncio.timeout(timeout_seconds):
                final_response, handoffs = await _orchestrate_subagents(
                    agents, prompt, source, output, emit
                )
        except TimeoutError as exc:
            raise RuntimeError(
                f"Demo exceeded its {timeout_seconds}-second run budget"
            ) from exc

        missing_handoffs = [agent_name for agent_name in STAGE_AGENTS if agent_name not in handoffs]
        if missing_handoffs:
            raise RuntimeError(
                "The orchestrator did not complete all required handoffs: "
                + ", ".join(missing_handoffs)
            )

        artifacts = []
        for file_name in STAGE_OUTPUTS:
            result = await output.aread(f"/{file_name}", limit=200)
            if result.error is not None:
                raise RuntimeError(f"Expected output {file_name} was not written")
            blob_name = f"{run_prefix}{file_name}"
            artifact = {
                "name": file_name,
                "virtualPath": f"/output/{file_name}",
                "blobName": blob_name,
                "runId": run_id,
                "url": _blob_url(account_url, output_container, blob_name),
                "content": _read_text(result),
            }
            artifacts.append(artifact)
            await emit("blob.verified", artifact=artifact)

        await emit(
            "run.completed",
            response=final_response,
            handoffs=len(handoffs),
            decision=next(
                artifact for artifact in artifacts if artifact["name"] == FINAL_DECISION
            ),
        )


async def _preview_blob(
    container: str, prefix: str, file_path: str, virtual_root: str
) -> dict[str, str]:
    path = PurePosixPath("/" + file_path.strip("/"))
    if not file_path or any(part in {".", ".."} for part in path.parts):
        raise HTTPException(status_code=400, detail="Invalid Blob path")

    account_url = _required_environment_variable("AZURE_STORAGE_ACCOUNT_URL")
    credential = AsyncDefaultAzureCredential(
        process_timeout=_credential_process_timeout_seconds()
    )
    async with AsyncExitStack() as stack:
        stack.push_async_callback(credential.close)
        backend = await stack.enter_async_context(
            AzureBlobBackend(
                account_url=account_url,
                container_name=container,
                prefix=prefix,
                credential=credential,
            )
        )
        result = await backend.aread(str(path), limit=300)
    if result.error is not None:
        raise HTTPException(status_code=404, detail=result.error)
    return {"path": f"/{virtual_root}{path}", "content": _read_text(result)}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/shared/styles.css")
async def shared_styles() -> FileResponse:
    return FileResponse(SHARED_STYLES, media_type="text/css")


@app.get("/api/config")
async def config() -> dict[str, Any]:
    account_url, source_container, source_prefix, output_container, output_prefix = (
        _storage_settings()
    )
    return {
        "model": _required_environment_variable("MODEL_NAME"),
        "account": account_url,
        "source": f"{source_container}/{source_prefix}",
        "outputs": f"{output_container}/{output_prefix}",
        "timeoutSeconds": _timeout_seconds(),
        "credential": "DefaultAzureCredential (server-side)",
        "subagentAccess": "Shared CompositeBackend routes; credentials are not copied",
        "readerRole": f"Storage Blob Data Reader on {source_container}",
        "writerRole": f"Storage Blob Data Contributor on {output_container}",
    }


@app.get("/api/source/{file_path:path}")
async def source_preview(file_path: str) -> dict[str, str]:
    _, source_container, source_prefix, _, _ = _storage_settings()
    return await _preview_blob(source_container, source_prefix, file_path, "source")


@app.get("/api/output/{run_id}/{file_path:path}")
async def output_preview(run_id: str, file_path: str) -> dict[str, str]:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    _, _, _, output_container, output_base = _storage_settings()
    return await _preview_blob(
        output_container, f"{output_base}{run_id}/", file_path, "output"
    )


@app.websocket("/ws/run")
async def run_socket(websocket: WebSocket) -> None:
    await websocket.accept()

    async def emit(event_type: str, **payload: Any) -> None:
        await websocket.send_json(
            {
                "type": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
        )

    try:
        request = await websocket.receive_json()
        prompt = str(request.get("prompt", "")).strip()
        if not prompt:
            raise RuntimeError("Enter a mortgage packet review request")
        await _run_demo(prompt, emit)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001 - errors are surfaced in the demo UI
        await emit("run.failed", error=str(exc))
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


app.mount("/assets", StaticFiles(directory=WEB_DIR, check_dir=False), name="assets")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("mortgage_demo:app", host="127.0.0.1", port=8001, reload=False)
