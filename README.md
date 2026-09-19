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
cd frontend && npm test       # Settings interaction tests
```

## Jev routing evaluation

Configure a TypeSafe API key in Settings and choose a default chat LLM profile.
Review the sample labels in `backend/app/jev/evaluation_cases.json`; the sample
is marked `human_reviewed: false`, so its report cannot pass the acceptance gate
until a person reviews the prompts and labels and changes that flag to `true`.

Run the live comparison from the repository root:

```bash
PYTHONPATH=backend .venv/bin/python -m app.jev.evaluation
```

The command sends each sample request to TypeSafe and the configured chat LLM,
then writes a report to `data/jev-evaluation/latest.md`. The report contains
case IDs and predictions, not the request text or API credentials.

## Architecture in one line

Chat → Agent Harness (iterative LLM ⇄ tools/skills loop) → Context Manager →
text/vector/graph retrieval → knowledge + evidence → project files. The
Knowledge Graph is infrastructure; the agent is the interface.
