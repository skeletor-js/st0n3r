# Providers & models

st0n3r is provider-agnostic. Any model is named by a single string:

```
<provider>/<model-id>
```

Examples: `anthropic/claude-sonnet-5`, `openai/gpt-5.2`, `ollama/llama3.3`, `codex/gpt-5-codex`. The part before the first slash picks the provider entry; everything after it is passed to that provider verbatim, so model ids that themselves contain slashes work: `openrouter/deepseek/deepseek-chat`.

## Built-in providers

| Provider | Kind | API key env var | Endpoint |
|---|---|---|---|
| `anthropic` | anthropic | `ANTHROPIC_API_KEY` | api.anthropic.com |
| `openai` | openai_compat | `OPENAI_API_KEY` | api.openai.com |
| `openrouter` | openai_compat | `OPENROUTER_API_KEY` | openrouter.ai/api/v1 |
| `together` | openai_compat | `TOGETHER_API_KEY` | api.together.xyz/v1 |
| `groq` | openai_compat | `GROQ_API_KEY` | api.groq.com/openai/v1 |
| `ollama` | openai_compat | none | localhost:11434/v1 |
| `codex` | codex_cli | none (uses `codex login`) | local `codex` binary |

Keys are read from the environment only — they are never stored in `stoner.yaml`. `stoner providers` shows every entry and whether its key is set. The `anthropic` kind needs the `anthropic` extra installed; every `openai_compat` entry needs the `openai` extra (`pip install 'st0n3r[all]'` covers both).

## Model roles

`stoner.yaml` assigns a model to each job:

```yaml
models:
  writer: anthropic/claude-sonnet-5              # drafts chapters (agentic, uses tools)
  reviewer: anthropic/claude-sonnet-5            # critic passes and revisions
  archivist: anthropic/claude-haiku-4-5-20251001 # fact extraction (cheap + frequent)
```

- **writer** drives the drafting agent in `stoner write`. It's the only role that runs the full tool-calling loop.
- **reviewer** runs `stoner review` passes and `stoner revise` rewrites.
- **archivist** extracts facts after every draft. It runs often and its job is mechanical, so a small fast model is the right default.

Overrides, narrowest first:

1. `stoner write N --model openai/gpt-5.2` overrides **only the drafting stage** of that run. If the slop gate triggers an auto-revision, that revision uses the configured *reviewer* model, and the archive stage uses the configured *archivist* — a one-off experiment with a different drafting model doesn't silently reroute your whole pipeline.
2. `stoner review --model` and `stoner revise --model` override the reviewer for that run; `stoner archive --model` overrides the archivist.
3. The `STONER_MODEL` environment variable overrides the **writer** role (useful in scripts).
4. Otherwise, `stoner.yaml` wins.

`max_tokens` (default 8192) and `temperature` (default: provider default) in `stoner.yaml` apply to all roles.

## Custom OpenAI-compatible endpoints

Any endpoint that speaks the `chat/completions` wire format — vLLM, LM Studio, a corporate proxy, a hosted service not in the table — can be added under `providers:` in `stoner.yaml`:

```yaml
providers:
  mycorp:
    kind: openai_compat
    base_url: https://llm.internal.example.com/v1
    api_key_env: MYCORP_API_KEY     # name of the env var, not the key itself
models:
  writer: mycorp/big-writer-model
```

Fields: `kind` (`anthropic`, `openai_compat`, or `codex_cli`), `base_url` (openai_compat only), `api_key_env`, and `extra` (provider-specific settings). If `api_key_env` is empty and a `base_url` is set, no key is required — the local-server case. A custom entry with the same name as a built-in replaces it, so you can, say, repoint `openai` at a proxy.

### Ollama and other local servers

`ollama` works out of the box if Ollama is running on the default port:

```bash
ollama pull llama3.3
stoner write 1 --model ollama/llama3.3
```

For a non-default port or another local server, add a custom entry with the right `base_url`. Local models are free and private, but expect weaker instruction-following: small models are more likely to fumble the tool-calling loop or the strict-JSON review formats.

## Codex CLI

The `codex` provider shells out to OpenAI's Codex CLI instead of calling an API — useful if your OpenAI access is a ChatGPT subscription rather than an API key.

Setup:

1. Install the Codex CLI so that `codex` is on your PATH (see OpenAI's Codex documentation for the installer for your platform).
2. Authenticate once: `codex login`.
3. Use it: `stoner write 1 --model codex/gpt-5-codex`.

How the adapter works: each model call becomes one `codex exec "<prompt>"` invocation. The adapter feature-detects the CLI's capabilities from `codex exec --help` — if `--json` is available it parses the JSONL event stream for the final agent message; otherwise it captures plain stdout. Long drafts can be slow; raise the per-call timeout via `extra`:

```yaml
providers:
  codex:
    kind: codex_cli
    extra:
      timeout: 1200   # seconds, default 600
```

**Limitations.** The codex adapter is text-only: no native tool-calling. The engine detects this and falls back to a fenced-JSON tool protocol — the tool catalog is appended to the system prompt, and the model replies with ` ```tool_call ` blocks that the harness parses and executes. This works, but it's more fragile than native tools; expect the occasional malformed call (the harness reports parse failures back to the model and carries on). Token usage is also not reported for codex runs, so review reports show zero usage.

## Errors you'll actually see

Provider failures surface as one-line, actionable messages, not tracebacks:

```
$ stoner write 1
No Anthropic API key found. Set ANTHROPIC_API_KEY in your environment, or add
`api_key_env:` under this provider in stoner.yaml.
```

Rate limits, bad keys, and unreachable endpoints get similar treatment. Set `STONER_DEBUG=1` if you need the full stack trace.

See also: [Getting started](getting-started.md) for key setup, [FAQ](faq.md) for cost-control and privacy questions.
