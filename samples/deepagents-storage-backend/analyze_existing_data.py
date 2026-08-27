# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "langchain-azure-ai",
#     "langchain-azure-storage[deepagents]",
#     "langchain[anthropic,openai]",
# ]
#
# [tool.uv.sources]
# langchain-azure-storage = { path = "../../libs/azure-storage", editable = true }
# ///
"""Review existing operational data and write a report to Azure Blob Storage.

The agent sees two durable filesystem routes backed by an existing Storage
account: ``/source/`` for input data and ``/reports/`` for its output. Deep
Agents' internal files remain in thread-scoped state and are not persisted.

Run from this directory (see README.md for environment setup):
    uv run --env-file .env analyze_existing_data.py
"""

import asyncio
import os
from contextlib import AsyncExitStack
from urllib.parse import quote

from _shared import build_model
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_azure_storage.deepagents import AzureBlobBackend

REPORT_NAME = "operations-review.md"

SYSTEM_PROMPT = """You are an operations analyst reviewing files in Azure Blob Storage.

Treat /source/ as read-only. Never write, edit, or delete anything under /source/.
Use only evidence from files you actually inspect. Cite each finding with its source
path. Write your final report only to /reports/operations-review.md. The report must
include an executive summary, notable findings, anomalies or missing information,
recommended actions, and a list of files reviewed.
"""


def _required_environment_variable(name: str) -> str:
    """Return a required environment variable or raise a helpful error."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Set {name} in .env before running this sample")
    return value


def _normalize_prefix(value: str) -> str:
    """Normalize a Blob prefix while rejecting parent-directory traversal."""
    parts = [part for part in value.strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise RuntimeError("Blob prefixes cannot contain '.' or '..' segments")
    return "/".join(parts) + ("/" if parts else "")


def _report_url(
    account_url: str, container_name: str, report_prefix: str
) -> str:
    """Build the URL of the report written by the agent."""
    blob_name = quote(f"{report_prefix}{REPORT_NAME}", safe="/")
    return f"{account_url.rstrip('/')}/{quote(container_name)}/{blob_name}"


def _prefixes_overlap(source_prefix: str, report_prefix: str) -> bool:
    """Return whether either normalized prefix contains the other."""
    return source_prefix.startswith(report_prefix) or report_prefix.startswith(
        source_prefix
    )


async def main() -> None:
    """Review existing source blobs and persist a grounded operations report."""
    account_url = _required_environment_variable("AZURE_STORAGE_ACCOUNT_URL")
    source_container = _required_environment_variable(
        "AZURE_STORAGE_SOURCE_CONTAINER"
    )
    report_container = os.environ.get(
        "AZURE_STORAGE_REPORT_CONTAINER", source_container
    )
    source_prefix = _normalize_prefix(
        os.environ.get("AZURE_STORAGE_SOURCE_PREFIX", "incoming")
    )
    report_prefix = _normalize_prefix(
        os.environ.get("AZURE_STORAGE_REPORT_PREFIX", "agent-reports")
    )
    if report_container == source_container and _prefixes_overlap(
        source_prefix, report_prefix
    ):
        raise RuntimeError(
            "Source and report prefixes cannot overlap in the same container. "
            "Use separate prefixes or a separate report container."
        )

    async with AsyncExitStack() as stack:
        source = await stack.enter_async_context(
            AzureBlobBackend(
                account_url=account_url,
                container_name=source_container,
                prefix=source_prefix,
            )
        )
        reports = await stack.enter_async_context(
            AzureBlobBackend(
                account_url=account_url,
                container_name=report_container,
                prefix=report_prefix,
            )
        )

        source_listing = await source.als("/")
        if source_listing.error is not None:
            raise RuntimeError(f"Could not list source blobs: {source_listing.error}")
        if not source_listing.entries:
            raise RuntimeError(
                f"No source blobs found at {source_container}/{source_prefix}"
            )

        backend = CompositeBackend(
            default=StateBackend(),
            routes={"/source/": source, "/reports/": reports},
        )
        agent = create_deep_agent(
            model=build_model(),
            backend=backend,
            system_prompt=SYSTEM_PROMPT,
            permissions=[
                FilesystemPermission(
                    operations=["write"], paths=["/source/**"], mode="deny"
                )
            ],
        )

        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Review the operational files under /source/. Identify "
                            "important patterns, exceptions, and missing information. "
                            "Write the evidence-based report requested in your "
                            "instructions to /reports/operations-review.md."
                        ),
                    }
                ]
            }
        )

        report = await reports.aread(f"/{REPORT_NAME}", limit=20)
        if report.error is not None:
            raise RuntimeError(
                "The agent finished without writing the expected report: "
                f"{report.error}"
            )

        print(result["messages"][-1].content)
        print(
            "\nReport persisted to:\n  "
            + _report_url(account_url, report_container, report_prefix)
        )


if __name__ == "__main__":
    asyncio.run(main())