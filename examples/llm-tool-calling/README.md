# LLM tool-calling demos

Three runnable examples showing how to drive `agent-kanban` from any
function-calling LLM. The kanban server stays plain REST; the agent
client defines tools and dispatches their results back over HTTP.

| File | Stack | When to use |
|---|---|---|
| [`openai_sdk_demo.py`](openai_sdk_demo.py) | OpenAI Python SDK + any OAI-compatible endpoint (Ollama, vLLM, llama.cpp server, OpenAI itself) | most flexible — same code works against many backends |
| [`ollama_demo.py`](ollama_demo.py) | Ollama native `/api/chat` with `tools=` | shortest path if you only target Ollama |
| [`curl_examples.sh`](curl_examples.sh) | plain `curl` | for shell-driven agents, smoke-tests, manual exploration |

## Prerequisites

```bash
# Server must be running
.venv/bin/python -m kanban_ui

# Have at least one project (creates 'default' on first launch)
curl http://localhost:7777/api/projects
```

For Python demos:

```bash
.venv/bin/pip install openai httpx     # or: pip install ollama for ollama_demo.py
```

## Running

```bash
# Generic OAI-compatible (against Ollama on default port)
OPENAI_BASE_URL=http://localhost:11434/v1 \
OPENAI_API_KEY=ollama \
MODEL=hermes3:8b \
.venv/bin/python examples/llm-tool-calling/openai_sdk_demo.py

# Direct Ollama
MODEL=hermes3:8b .venv/bin/python examples/llm-tool-calling/ollama_demo.py

# Just curl
bash examples/llm-tool-calling/curl_examples.sh
```

The Python demos run a 1-turn tool-call loop:
1. User: "Create a task X in project Y"
2. LLM picks `kanban_create` tool, args `{title: "X", project_id: "Y"}`.
3. Demo dispatches HTTP POST to `/api/tasks`.
4. LLM gets the response, writes a confirmation.

Open `http://localhost:7777/p/<project>` in browser to see the new card.
