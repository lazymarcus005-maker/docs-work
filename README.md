# Local Cowork Knowledge Workspace

A local-first AI document workspace: create projects, upload source documents,
index them locally on CPU, and work through a chat-first agent harness that uses
configured LLM APIs (OpenAI-compatible / LiteLLM Gateway), project tools, and
reusable Agent Skills to generate, revise, validate, and trace documents from
project context.

Spec: see `local-cowork-knowledge-workspace-spec.md`. Work is tracked as
GitHub issues (#1–#18), one per tracer-bullet ticket.

## Layout

```
backend/    FastAPI application (Python)
frontend/   Next.js application
docs/       ADRs and engineering docs
```

## Prerequisites

- Python 3.9+ (CPU only — no GPU required)
- Node.js 18+

## Run locally

```bash
# backend
python3 -m venv .venv
./.venv/bin/pip install -r backend/requirements.txt
./.venv/bin/uvicorn app.main:create_app --factory --reload --port 8000 --app-dir backend

# frontend
cd frontend && npm install && npm run dev
```

Open http://localhost:3000. Backend API: http://localhost:8000/api/health.

Runtime data (SQLite database, project workspaces, secrets) lives under
`./data/` by default; override with `COWORK_DATA_DIR`.

## Tests

```bash
./.venv/bin/pytest backend/tests
cd frontend && npm run build   # typechecks + builds
```

## Architecture in one line

Chat → Agent Harness (iterative LLM ⇄ tools/skills loop) → Context Manager →
text/vector/graph retrieval → knowledge + evidence → project files. The
Knowledge Graph is infrastructure; the agent is the interface.
