# Deep Agents Azure Blob Storage Backend Samples

Runnable examples for using
[`AzureBlobBackend`](../../libs/azure-storage/langchain_azure_storage/deepagents/backend.py)
as a [Deep Agents](https://github.com/langchain-ai/deepagents) filesystem backend, so an
agent's workspace lives in Azure Blob Storage instead of process memory.

## Resources these samples create

The self-contained demos create a blob container named **`agent-workspace`** in your
storage account, or reuse it if it already exists. The existing-data sample instead uses
containers and prefixes you configure. The samples do not request deletions. Files the
agents write are left in place so you can inspect them afterwards.

Each sample writes under its own prefix, so they don't interfere with each other:

| Sample | Prefix in `agent-workspace` |
| --- | --- |
| `basic_agent.py` | `session-001/` |
| `resume_workspace.py` | `research-session/` |
| `composite_with_memories.py` | `composite-demo/memories/`, `composite-demo/workspace/` |
| `analyze_existing_data.py` | Configurable source and report containers/prefixes |

To clean up everything the samples created:

```bash
az storage container delete --name agent-workspace --account-name <your-account> --auth-mode login
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed
- Python 3.11+ (required by the `deepagents` extra)
- An Azure Storage account. The self-contained demos require permission to create a
  container. The existing-data sample uses containers that already exist; see its
  least-privilege role guidance below.
- A chat model to drive the agents (see [Configuring the model](#configuring-the-model))

To run without an Azure Storage account, see
[Running against the Azurite emulator](#running-against-the-azurite-emulator).

## Setup

No Storage account, subscription, tenant, resource group, or credential is committed to
this repository. Start with the blank template (`.env` is gitignored):

```bash
cp mortgage-demo.env.example .env
```

Choose either an existing account or explicitly provision a new one before starting the
agents. Storage provisioning is intentionally kept outside the agent runtime so the
model and its tools never receive Azure resource-management permissions.

### Option 1: Use an existing Storage account

Set the Blob endpoint for any account in a subscription you can access:

```env
AZURE_STORAGE_ACCOUNT_URL=
```

Credentials come from
[`DefaultAzureCredential`](https://learn.microsoft.com/azure/developer/python/sdk/authentication/credential-chains?tabs=dac#defaultazurecredential-overview),
so signing in with the Azure CLI is enough:

```bash
az login
```

`DefaultAzureCredential` also picks up managed identity, workload identity, and
environment-variable credentials. To use a different credential type, pass `credential=`
to `AzureBlobBackend` directly.

For the mortgage demo, create private `mortgage-packets` and `mortgage-decisions`
containers, upload the files from `mortgage-demo-data` beneath `MORT-2026-0042/`, and
grant the runtime identity **Storage Blob Data Reader** on the source container and
**Storage Blob Data Contributor** on the output container.

### Option 2: Provision a new Storage account

The following opt-in setup creates a secure general-purpose v2 account in a subscription,
resource group, and region you choose. Leave `STORAGE_ACCOUNT_NAME` empty to generate a
globally unique name. It uses Microsoft Entra authentication and does not print or save
account keys or connection strings.

```bash
SUBSCRIPTION_ID=""
RESOURCE_GROUP=""
LOCATION=""
STORAGE_ACCOUNT_NAME=""

: "${SUBSCRIPTION_ID:?Set SUBSCRIPTION_ID}"
: "${RESOURCE_GROUP:?Set RESOURCE_GROUP}"
: "${LOCATION:?Set LOCATION}"

az login
az account set --subscription "$SUBSCRIPTION_ID"

if [ -z "$STORAGE_ACCOUNT_NAME" ]; then
  STORAGE_ACCOUNT_NAME="$(python -c "import secrets; print('lcmortgage' + secrets.token_hex(6))")"
fi

az storage account create \
  --name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --kind StorageV2 \
  --sku Standard_LRS \
  --allow-blob-public-access false \
  --https-only true \
  --min-tls-version TLS1_2

STORAGE_ID="$(az storage account show \
  --name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)"
PRINCIPAL_ID="$(az ad signed-in-user show --query id --output tsv)"

az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type User \
  --role "Storage Blob Data Contributor" \
  --scope "$STORAGE_ID"

az storage container create \
  --name mortgage-packets \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --auth-mode login \
  --public-access off
az storage container create \
  --name mortgage-decisions \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --auth-mode login \
  --public-access off
az storage blob upload-batch \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --auth-mode login \
  --source mortgage-demo-data \
  --destination mortgage-packets \
  --destination-path MORT-2026-0042

printf 'AZURE_STORAGE_ACCOUNT_URL=https://%s.blob.core.windows.net\n' \
  "$STORAGE_ACCOUNT_NAME"
```

Copy the printed `AZURE_STORAGE_ACCOUNT_URL` into your local `.env`. Azure role
assignments can take a few minutes to propagate; if a container command initially returns
an authorization error, retry it after propagation completes. For production or a
deployed managed identity, replace the setup identity's account-level role with the
container-scoped roles described in the mortgage demo section below.

### Storage environment variables

| Variable | Description |
| --- | --- |
| `AZURE_STORAGE_ACCOUNT_URL` | Blob endpoint of your storage account, authenticated with `DefaultAzureCredential`. |
| `AZURE_STORAGE_CONNECTION_STRING` | Alternative: authenticate with a connection string. Takes precedence when both are set. |

### Configuring the model

`MODEL_NAME` selects the chat model. To run the samples entirely on Azure, set it to your
model deployment name and set one Azure AI endpoint variable — the samples then build the
model with
[`langchain-azure-ai`](../../libs/azure-ai#microsoft-foundry-models), authenticated with
`DefaultAzureCredential`:

```env
MODEL_NAME=gpt-5.5
AZURE_AI_PROJECT_ENDPOINT=https://<your-resource>.services.ai.azure.com/api/projects/<your-project>
```

`AZURE_AI_OPENAI_ENDPOINT` and `AZURE_OPENAI_ENDPOINT` work too; see
[`AzureAIOpenAIApiChatModel`](../../libs/azure-ai/langchain_azure_ai/chat_models/openai.py)
for how each is resolved.

With no Azure AI endpoint set, `MODEL_NAME` is passed through to
[`init_chat_model`](https://docs.langchain.com/oss/python/langchain/models) as a
`provider:model` identifier, so any supported provider works:

```env
MODEL_NAME=anthropic:claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-...your-key-here...
```

## Running the samples

Each sample uses [PEP 723 inline script metadata](https://peps.python.org/pep-0723/), so
uv installs the dependencies automatically — no separate install step. The scripts pin
`langchain-azure-storage` to this repository checkout via `[tool.uv.sources]`, so they
run against your local code; if you copy a sample elsewhere, delete that block to use the
released package instead.

### Basic agent ([basic_agent.py](basic_agent.py))

A minimal Deep Agent whose workspace persists in Azure Blob Storage. After the run it
lists the workspace and prints the blob URL each file landed at.

```bash
cd samples/deepagents-storage-backend
uv run --env-file .env basic_agent.py
```

### Review existing data and write a report ([analyze_existing_data.py](analyze_existing_data.py))

This operations-review example connects a Deep Agent to data already in Azure Blob
Storage. It maps the source prefix to `/source/`, maps a report prefix in the same
Storage account to `/reports/`, and keeps the agent's internal bookkeeping in memory.
The agent inspects text-readable operational files such as Markdown, text, CSV, and JSON,
then writes an evidence-based `operations-review.md` back to Blob Storage.

`AzureBlobBackend` provides a virtual filesystem route; it does not mount Blob Storage as
an operating-system drive. This cloud-data sample requires account-URL authentication;
the connection-string and Azurite configuration described later applies to the other
samples. Configure the existing account and data location in `.env`:

```env
AZURE_STORAGE_ACCOUNT_URL=https://<your-account>.blob.core.windows.net
AZURE_STORAGE_SOURCE_CONTAINER=<existing-source-container>
AZURE_STORAGE_SOURCE_PREFIX=incoming/
AZURE_STORAGE_REPORT_CONTAINER=<existing-report-container>
AZURE_STORAGE_REPORT_PREFIX=agent-reports/
MODEL_NAME=<your-model>
AZURE_AI_PROJECT_ENDPOINT=https://<your-resource>.services.ai.azure.com/api/projects/<your-project>
```

`AZURE_STORAGE_REPORT_CONTAINER` defaults to the source container when omitted. The
source prefix defaults to `incoming/`, and the report prefix defaults to
`agent-reports/`. Source and report prefixes cannot overlap when they share a container.
Each run replaces `operations-review.md` in the report prefix.

For least privilege, use separate source and report containers in the same account. Grant
the agent identity **Storage Blob Data Reader** on the source container and **Storage Blob
Data Contributor** on the report container. The sample also applies a Deep Agents
filesystem permission that denies writes, edits, and deletes under `/source/**`. If both
routes use one container, the identity needs contributor access there; Azure RBAC cannot
enforce different permissions between prefixes in one container, so separate containers
provide the stronger boundary. Enable Blob versioning or soft delete before pointing an
agent at important data.

Run the review:

```bash
cd samples/deepagents-storage-backend
uv run --env-file .env analyze_existing_data.py
```

The script fails early when the configured source location contains no blobs and verifies
that the report exists before printing its Blob URL.

### Live web demonstration ([web_demo.py](web_demo.py))

The web demonstration turns the existing-data workflow into an observable multi-agent
review. A coordinator delegates to a metrics analyst, an incident analyst, and a report
writer. The page shows each delegation and filesystem tool call as it happens, highlights
source blobs when an agent reads them, and verifies every report after it is written.
It displays observable actions and tool results, not private model reasoning.

The browser never receives Azure credentials. The FastAPI server authenticates with
`DefaultAzureCredential`, and source previews are served only from the configured source
prefix. Every run writes to an isolated prefix beneath
`<AZURE_STORAGE_REPORT_PREFIX>/web-demo/<run-id>/`, leaving previous demonstrations in
place for comparison.

Use the same `.env` configuration as `analyze_existing_data.py`, then start the site:

```bash
cd samples/deepagents-storage-backend
uv --system-certs run --env-file .env web_demo.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), select **Run review**, and watch the
source inventory, agent activity, and durable Blob artifacts update in real time. The
`--system-certs` option lets uv use the operating system certificate store, which is
commonly required on managed corporate networks.

### Mortgage Packet processing ([mortgage_demo.py](mortgage_demo.py))

This demonstration processes the synthetic packet in
[`mortgage-demo-data`](mortgage-demo-data) through four specialists: intake,
classification, extraction, and underwriting. A coordinator runs the three independent
analysis stages concurrently, then delegates the final policy check to underwriting.
Scheduling is deterministic, so model latency is reserved for the four specialist Deep
Agents rather than spent deciding a known dependency graph.
Each specialist writes one durable stage artifact, and the server verifies all four
Blobs before reporting success. A 55-second agent timeout and low GPT-5 reasoning effort
keep the complete review within a one-minute demonstration budget.

The browser visualizes evidence from successful filesystem tools rather than model
reasoning. Every transfer includes its direction, agent, virtual path, tool, and exact
numbered excerpt. Source and verified output rows open server-mediated Blob previews.

Configure the two authorization boundaries in `.env` (the values shown are defaults):

```env
AZURE_STORAGE_MORTGAGE_SOURCE_CONTAINER=mortgage-packets
AZURE_STORAGE_MORTGAGE_SOURCE_PREFIX=MORT-2026-0042
AZURE_STORAGE_MORTGAGE_OUTPUT_CONTAINER=mortgage-decisions
AZURE_STORAGE_MORTGAGE_OUTPUT_PREFIX=demo-runs
MORTGAGE_DEMO_TIMEOUT_SECONDS=55
```

The server creates one asynchronous `DefaultAzureCredential` per run and injects that
credential into both `AzureBlobBackend` instances. `CompositeBackend` exposes those
instances as routes, and Deep Agents gives the resulting filesystem tools to every
subagent. Credential values are not copied into prompts, agent state, WebSocket events,
or browser code.

```python
credential = AsyncDefaultAzureCredential()
source = AzureBlobBackend(
    account_url=account_url,
    container_name="mortgage-packets",
    prefix="MORT-2026-0042/",
    credential=credential,
)
output = AzureBlobBackend(
    account_url=account_url,
    container_name="mortgage-decisions",
    prefix=f"demo-runs/{run_id}/",
    credential=credential,
)
backend = CompositeBackend(
    default=StateBackend(),
    routes={"/source/": source, "/output/": output},
)
```

For a deployed managed identity, grant only container-scoped data-plane roles:

```bash
STORAGE_ID=$(az storage account show --name <account> --query id --output tsv)
PRINCIPAL_ID=<managed-identity-object-id>

az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope "$STORAGE_ID/blobServices/default/containers/mortgage-packets"

az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "$STORAGE_ID/blobServices/default/containers/mortgage-decisions"
```

Azure RBAC assignments are additive. A broader Contributor or Owner assignment inherited
from the account, resource group, or subscription cannot be reduced by a Reader role on
the source container. Use a dedicated managed identity without broader data-plane roles.
The demo also denies writes to `/source/**` through `FilesystemPermission`, providing an
agent-level guardrail in addition to Azure authorization.

Upload the six files under `MORT-2026-0042/`, then start the site:

```bash
cd samples/deepagents-storage-backend
uv --system-certs run --env-file .env mortgage_demo.py
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001) and select **Process packet**.

### Resuming a workspace ([resume_workspace.py](resume_workspace.py))

The demo only a durable backend can run: one agent writes research notes and is torn
down completely; a brand-new backend and agent then attach to the same prefix and
summarize what they find. State survives because it lives in Blob Storage, not in
process memory.

```bash
cd samples/deepagents-storage-backend
uv run --env-file .env resume_workspace.py
```

### Composite backend with memory and subagents ([composite_with_memories.py](composite_with_memories.py))

Routes part of the agent's filesystem to Azure Blob Storage with
[`CompositeBackend`](https://docs.langchain.com/oss/python/deepagents/backends#compositebackend-router):
`/memories/` holds an `AGENTS.md` that survives every run, `/workspace/` holds the shared
working files, and everything else stays thread-scoped in `StateBackend`.

That last part matters: Deep Agents writes its own bookkeeping into the backend (offloaded
large tool results under `/large_tool_results/`, conversation history under
`/conversation_history/`), and with a bare backend those land in your container next to the
agent's real output. Routing only the prefixes you care about keeps the container clean.

A coder and a tester subagent share the durable `/workspace/`, so the coder's files are
immediately visible to the tester. The run is streamed so the output attributes each file
operation to the agent that performed it, then prints what each route persisted.

```bash
cd samples/deepagents-storage-backend
uv run --env-file .env composite_with_memories.py
```

## Browsing the results

In the [Azure portal](https://portal.azure.com), open your storage account and go to
**Data storage > Containers > agent-workspace**. The prefixes in
[Resources these samples create](#resources-these-samples-create) appear as folders; open
one to read the files an agent wrote.

[Azure Storage Explorer](https://azure.microsoft.com/products/storage/storage-explorer)
works too, and is the easiest option when running against Azurite: connect to the **Local
storage emulator** with its default settings and browse `agent-workspace` under
**devstoreaccount1 > Blob Containers**.

## Running against the Azurite emulator

To run the samples with no Azure Storage account, use the
[Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) emulator.
You still need a chat model.

Start it with [Docker](https://docs.docker.com/get-docker/):

```bash
docker run -d --name azurite -p 10000:10000 \
  mcr.microsoft.com/azure-storage/azurite \
  azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck
```

(`--skipApiVersionCheck` keeps the emulator working when the `azure-storage-blob` client
library is newer than the Azurite image.)

Then point the samples at it with Azurite's well-known development connection string,
replacing `AZURE_STORAGE_ACCOUNT_URL` in your `.env`:

```env
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;
```

That account name and key are Azurite's
[published defaults](https://learn.microsoft.com/azure/storage/common/storage-use-azurite#well-known-storage-account-and-key),
identical for every Azurite install — not a secret. The shorthand
`UseDevelopmentStorage=true` is a .NET convention that the Python SDK does not accept, so
the full string is required here.

To tear the emulator down:

```bash
docker rm -f azurite
```
