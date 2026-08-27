# /// script
# requires-python = ">=3.11"
# dependencies = [
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
"""Serve a live web demonstration of Deep Agents using Azure Blob Storage."""

from __future__ import annotations

import os
import secrets
from base64 import b64decode
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from deepagents import SubAgent, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemPermission
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_azure_storage.deepagents import AzureBlobBackend

WEB_DIR = Path(__file__).with_name("web-demo")
FINAL_REPORT = "operations-review.md"

app = FastAPI(title="Blob Agent Observatory")


def _required_environment_variable(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Set {name} in .env before starting the web demo")
    return value


def _normalize_prefix(value: str) -> str:
    parts = [part for part in value.strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise RuntimeError("Blob prefixes cannot contain '.' or '..' segments")
    return "/".join(parts) + ("/" if parts else "")


def _source_settings() -> tuple[str, str, str]:
    return (
        _required_environment_variable("AZURE_STORAGE_ACCOUNT_URL"),
        _required_environment_variable("AZURE_STORAGE_SOURCE_CONTAINER"),
        _normalize_prefix(os.environ.get("AZURE_STORAGE_SOURCE_PREFIX", "incoming")),
    )


def _report_settings() -> tuple[str, str]:
    source_container = _required_environment_variable(
        "AZURE_STORAGE_SOURCE_CONTAINER"
    )
    return (
        os.environ.get("AZURE_STORAGE_REPORT_CONTAINER", source_container),
        _normalize_prefix(
            os.environ.get("AZURE_STORAGE_REPORT_PREFIX", "agent-reports")
        ),
    )


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
            max_retries=3,
            timeout=180,
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


def _tool_event(agent_name: str, call: dict[str, Any]) -> dict[str, Any] | None:
    args = call.get("args") or {}
    tool_name = str(call.get("name", "tool"))
    call_id = str(call.get("id") or "")

    if tool_name == "task":
        subagent = args.get("subagent_type")
        description = args.get("description")
        if not subagent or not description:
            return None
        return {
            "kind": "delegation",
            "callId": call_id,
            "agent": agent_name,
            "tool": tool_name,
            "targetAgent": str(subagent),
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


def _create_agent(
    credential: DefaultAzureCredential, backend: CompositeBackend
) -> Any:
    subagents = [
        SubAgent(
            name="metrics-analyst",
            description="Analyzes fulfillment metrics against documented thresholds.",
            system_prompt=(
                "Read /source/review-context.md and "
                "/source/fulfillment-metrics.csv. Calculate threshold exceptions "
                "without inventing causes. Write a concise evidence file to "
                "/reports/metrics-analysis.md and cite the source paths."
            ),
        ),
        SubAgent(
            name="incident-analyst",
            description="Reviews operational incidents and identifies unresolved risk.",
            system_prompt=(
                "Read /source/incidents.json and /source/review-context.md. Identify "
                "open or material incidents without inferring unsupported causes. "
                "Write findings to /reports/incident-analysis.md and cite source paths."
            ),
        ),
        SubAgent(
            name="report-writer",
            description="Combines specialist findings into the final operations report.",
            system_prompt=(
                "Read /reports/metrics-analysis.md and "
                "/reports/incident-analysis.md, then inspect source files when needed. "
                "Write /reports/operations-review.md with an executive summary, "
                "notable findings, anomalies or missing information, recommended "
                "actions, and files reviewed. Every factual finding must cite a "
                "/source/ path."
            ),
        ),
    ]
    coordinator_prompt = (
        "You coordinate a visible operations review. Delegate the metrics review to "
        "metrics-analyst, then the incident review to incident-analyst, then ask "
        "report-writer to produce /reports/operations-review.md. Do not perform the "
        "specialists' file work yourself. Return a short completion message."
    )
    return create_deep_agent(
        model=_build_model(credential),
        backend=backend,
        subagents=subagents,
        system_prompt=coordinator_prompt,
        permissions=[
            FilesystemPermission(
                operations=["write"], paths=["/source/**"], mode="deny"
            )
        ],
    )


async def _stream_agent(
    agent: Any,
    prompt: str,
    emit: Callable[..., Awaitable[None]],
) -> str:
    accumulated: dict[str, tuple[str, AIMessageChunk]] = {}
    emitted_calls: set[str] = set()
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
        agent_name = str(metadata.get("lc_agent_name") or "coordinator")
        if isinstance(message, ToolMessage):
            await emit(
                "tool.result",
                agent=agent_name,
                tool=message.name or "tool",
                status=message.status or "success",
            )
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
            if call_key in emitted_calls:
                continue
            emitted_calls.add(call_key)
            await emit("tool.call", **event)

    messages = final_state.get("messages", [])
    return str(messages[-1].content) if messages else "Review completed."


async def _run_demo(
    prompt: str, emit: Callable[..., Awaitable[None]]
) -> None:
    account_url, source_container, source_prefix = _source_settings()
    report_container, report_base_prefix = _report_settings()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id += f"-{secrets.token_hex(2)}"
    run_prefix = f"{report_base_prefix}web-demo/{run_id}/"

    await emit(
        "run.started",
        runId=run_id,
        model=_required_environment_variable("MODEL_NAME"),
        account=account_url,
        sourceContainer=source_container,
        sourcePrefix=source_prefix,
        reportContainer=report_container,
        reportPrefix=run_prefix,
    )

    model_credential = DefaultAzureCredential(process_timeout=60)
    blob_credential = AsyncDefaultAzureCredential(process_timeout=60)
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
        reports = await stack.enter_async_context(
            AzureBlobBackend(
                account_url=account_url,
                container_name=report_container,
                prefix=run_prefix,
                credential=blob_credential,
            )
        )

        listing = await source.als("/")
        if listing.error is not None:
            raise RuntimeError(f"Could not list source blobs: {listing.error}")
        if not listing.entries:
            raise RuntimeError(
                f"No source blobs found at {source_container}/{source_prefix}"
            )

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
        await emit("blob.inventory", files=files)

        backend = CompositeBackend(
            default=StateBackend(),
            routes={"/source/": source, "/reports/": reports},
        )
        agent = _create_agent(model_credential, backend)
        await emit(
            "agent.ready",
            agents=[
                "coordinator",
                "metrics-analyst",
                "incident-analyst",
                "report-writer",
            ],
        )
        final_response = await _stream_agent(agent, prompt, emit)

        artifacts = []
        for file_name in (
            "metrics-analysis.md",
            "incident-analysis.md",
            FINAL_REPORT,
        ):
            result = await reports.aread(f"/{file_name}")
            if result.error is not None:
                if file_name == FINAL_REPORT:
                    raise RuntimeError(
                        "The agents finished without writing the final report: "
                        f"{result.error}"
                    )
                continue
            blob_name = f"{run_prefix}{file_name}"
            artifact = {
                "name": file_name,
                "virtualPath": f"/reports/{file_name}",
                "blobName": blob_name,
                "url": _blob_url(account_url, report_container, blob_name),
                "content": _read_text(result),
            }
            artifacts.append(artifact)
            await emit("blob.verified", artifact=artifact)

        await emit(
            "run.completed",
            response=final_response,
            report=next(
                artifact for artifact in artifacts if artifact["name"] == FINAL_REPORT
            ),
        )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/config")
async def config() -> dict[str, str]:
    account_url, source_container, source_prefix = _source_settings()
    report_container, report_prefix = _report_settings()
    return {
        "model": _required_environment_variable("MODEL_NAME"),
        "account": account_url,
        "source": f"{source_container}/{source_prefix}",
        "reports": f"{report_container}/{report_prefix}web-demo/",
    }


@app.get("/api/source/{file_path:path}")
async def source_preview(file_path: str) -> dict[str, str]:
    path = PurePosixPath("/" + file_path.strip("/"))
    if not file_path or any(part in {".", ".."} for part in path.parts):
        raise HTTPException(status_code=400, detail="Invalid source path")

    account_url, source_container, source_prefix = _source_settings()
    credential = AsyncDefaultAzureCredential(process_timeout=60)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(credential.close)
        source = await stack.enter_async_context(
            AzureBlobBackend(
                account_url=account_url,
                container_name=source_container,
                prefix=source_prefix,
                credential=credential,
            )
        )
        result = await source.aread(str(path), limit=300)
    if result.error is not None:
        raise HTTPException(status_code=404, detail=result.error)
    return {"path": f"/source{path}", "content": _read_text(result)}


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
            raise RuntimeError("Enter an operations review request")
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

    uvicorn.run("web_demo:app", host="127.0.0.1", port=8000, reload=False)