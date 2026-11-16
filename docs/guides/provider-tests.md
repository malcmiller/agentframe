# Real-provider tests

Tests against a real Ollama server, Foundry deployment or Anthropic model are
opt-in and never run in normal CI. They are marked `ollama` / `foundry` /
`anthropic` (and `entra` for a real
Entra token) and skipped unless enabled, so the default `uv run pytest` never
calls a provider. [AGENTS.md](../../AGENTS.md#real-provider-tests-opt-in-local-only)
lists what each test covers.

## Running them locally

Endpoints and model names come only from environment variables:

| Variable | Needed | Meaning |
| --- | --- | --- |
| `AGENTFRAME_TEST_OLLAMA=1` | Ollama | Enables the `ollama` tests. |
| `AGENTFRAME_OLLAMA_MODEL` | Ollama | Installed model tag, for example `llama3.2:3b` (never pulled). |
| `AGENTFRAME_OLLAMA_ENDPOINT` | Optional | Server URL; defaults to `OllamaModel`'s `http://localhost:11434`. |
| `AGENTFRAME_TEST_FOUNDRY=1` | Foundry | Enables the `foundry` tests. |
| `AGENTFRAME_FOUNDRY_ENDPOINT` | Foundry | OpenAI v1 base URL, `https://<resource>.openai.azure.com/openai/v1/`. |
| `AGENTFRAME_FOUNDRY_DEPLOYMENT` | Foundry | Deployment name. |
| `AGENTFRAME_TEST_ANTHROPIC=1` | Anthropic | Enables the `anthropic` tests. |
| `AGENTFRAME_ANTHROPIC_MODEL` | Anthropic | Model ID, for example `claude-haiku-4-5`. |
| `ANTHROPIC_API_KEY` | Anthropic | API key, passed explicitly to `AnthropicModel(api_key=...)`. |

Foundry signs in with the Azure CLI credential (run `az login`; no API key).
Foundry and Anthropic calls cost money, so prompts and caps are tiny; run them
sparingly. The Anthropic tests are the same shared tests as Ollama and Foundry.
An enabled provider with a missing required variable fails rather than skips.

```bash
AGENTFRAME_TEST_OLLAMA=1 AGENTFRAME_OLLAMA_MODEL=llama3.2:3b uv run pytest -m ollama
AGENTFRAME_TEST_FOUNDRY=1 AGENTFRAME_FOUNDRY_ENDPOINT=<base-url> \
  AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> uv run pytest -m foundry
AGENTFRAME_TEST_ANTHROPIC=1 AGENTFRAME_ANTHROPIC_MODEL=claude-haiku-4-5 \
  ANTHROPIC_API_KEY=<key> uv run pytest -m anthropic
```

The M10 planner tests in `test_providers.py` run the `examples/multi_agent`
planner with the real model (the specialists use stand-in models): one plans
and runs the example's post, checking only the entries used and completion,
and one runs the planner evaluation with `report.require(...)`. Planning is
nondeterministic and `llama3.2:3b` often plans badly, so for Ollama an
invalid or empty plan, a plan the example's checks refuse, or (for the
evaluation) more than half the plans erroring is an expected failure (xfail);
any other failure still fails. The same applies to the two-step planning test,
to the `examples/dynamic_agents` planner test (where a step the model gave bad
arguments, leaving the outcome incomplete, is also an expected failure), and
to the judge example test when the small judge fails correct answers;
measured rates are in the
[orchestration guide](orchestration.md#measure-the-planner).
For example:

```bash
AGENTFRAME_TEST_OLLAMA=1 AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \
    uv run pytest -m ollama -k real_planner
```

## Foundry workflow

The manually triggered `.github/workflows/foundry.yml` runs the `foundry` tests
from GitHub, for example before a release. It runs only from **Actions >
Foundry provider tests > Run workflow**, never on push or pull request, and
waits for a reviewer to approve the `foundry` environment. It signs in to Azure
with OpenID Connect, so the repository stores no secret; Foundry calls cost
money, so run it sparingly. There is no Ollama job, since the model download
is large.

The `FoundryAgentRegistry` and `FoundryA2AToolRegistry` tests need their own
variables (see [`FoundryAgentRegistry`](../api/discovery.md#foundryagentregistry),
[`FoundryA2AToolRegistry`](../api/discovery.md#foundrya2atoolregistry) and
[AGENTS.md](../../AGENTS.md#real-provider-tests-opt-in-local-only)), so the
workflow skips them. Both run locally after `az login`:

```bash
AGENTFRAME_TEST_FOUNDRY_AGENTS=1 \
    AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT=<project-endpoint> \
    AGENTFRAME_FOUNDRY_A2A_AGENT=<agent-name> \
    AGENTFRAME_TEST_FOUNDRY_A2A_TOOLS=1 \
    AGENTFRAME_FOUNDRY_A2A_CONNECTION=<connection-name> \
    uv run pytest -m foundry tests/integration/test_foundry_agents.py \
    tests/integration/test_foundry_a2a_tools.py
```

The `FoundryA2AToolRegistry` test only reads: it lists the project's A2A
connections and fetches the named connection's card, sending no message. It
has not yet run against a real project, so the **Foundry User** role it is
expected to need for reading connections is unconfirmed; record the result
here after the first run.

### One-time setup

One-time setup, by a maintainer with rights to create an app registration and
assign roles on the Foundry resource (`<resource>` and `<resource-group>` are
your Foundry resource and its resource group):

1. Create an app registration and its service principal for the workflow:

   ```bash
   APP_ID=$(az ad app create --display-name agentframe-foundry-tests \
       --query appId -o tsv)
   SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
   ```

2. Trust only this repository's `foundry` environment through a federated
   credential (no client secret). The subject must match the token's `sub`
   claim exactly. Repositories created after July 15, 2026, like this one,
   use [immutable subject claims](https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-github-actions-oidc-tokens/)
   that include the owner and repository IDs (`repo:<owner>@<owner-id>/<repo>@<repo-id>`):

   ```bash
   az ad app federated-credential create --id "$APP_ID" --parameters '{
     "name": "agentframe-foundry-environment",
     "issuer": "https://token.actions.githubusercontent.com",
     "subject": "repo:malcmiller@58534574/agentframe@1452010815:environment:foundry",
     "audiences": ["api://AzureADTokenExchange"]
   }'
   ```

   For a fork, read your prefix with
   `gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix`
   (or the IDs with `gh api repos/<owner>/<repo> --jq '.owner.id, .id'`) and
   append `:environment:foundry`. If sign-in fails with **AADSTS700213** (no
   matching federated identity), copy the `subject claim - ...` value from the
   workflow's Azure login log into the federated credential.

3. Grant inference only, on the one Foundry resource. **Cognitive Services
   OpenAI User** allows calling deployments (including the Responses API) but
   not managing them; do not use broader roles such as Contributor. If calls
   still fail with 403 after the assignment propagates (a few minutes), assign
   **Foundry User** (formerly Azure AI User) on the same scope instead:

   ```bash
   RESOURCE_ID=$(az cognitiveservices account show --name <resource> \
       --resource-group <resource-group> --query id -o tsv)
   az role assignment create --assignee-object-id "$SP_ID" \
       --assignee-principal-type ServicePrincipal \
       --role "Cognitive Services OpenAI User" --scope "$RESOURCE_ID"
   ```

4. In the GitHub repository, open **Settings > Environments**, create an
   environment named `foundry`, enable **Required reviewers** (add yourself)
   and limit **Deployment branches and tags** to `main`.
5. Add these **environment variables** (not secrets; they are identifiers) to
   `foundry`:

   | Variable | Value |
   | --- | --- |
   | `AZURE_CLIENT_ID` | `$APP_ID` from step 1. |
   | `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
   | `AZURE_SUBSCRIPTION_ID` | The subscription containing the Foundry resource. |
   | `AGENTFRAME_FOUNDRY_ENDPOINT` | OpenAI v1 base URL, `https://<resource>.openai.azure.com/openai/v1/` or `https://<resource>.services.ai.azure.com/openai/v1/`. |
   | `AGENTFRAME_FOUNDRY_DEPLOYMENT` | Deployment name. |

   For example, `gh variable set AZURE_CLIENT_ID --env foundry --body "$APP_ID"`.

Then start the workflow on `main` and approve the pending `foundry` deployment.
A missing variable makes the tests fail rather than skip.
