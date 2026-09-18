"""SQLite storage: connection handling and schema.

The spec recommends DuckDB but explicitly allows SQLite where appropriate
(§16). SQLite gives us a zero-dependency, battle-tested local store with
FTS5 for text search; all access goes through this module and repository
helpers so the engine stays replaceable (NFR-004).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  instruction TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  stored_name TEXT NOT NULL,
  media_type TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'NEW',
  parser_version TEXT NOT NULL DEFAULT '',
  extraction_version TEXT NOT NULL DEFAULT '',
  embedding_version TEXT NOT NULL DEFAULT '',
  last_processed_at TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);

CREATE TABLE IF NOT EXISTS document_versions (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  content_hash TEXT NOT NULL,
  stored_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_versions ON document_versions(document_id);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL,
  section_path TEXT NOT NULL DEFAULT '[]',
  text TEXT NOT NULL,
  page INTEGER,
  sequence INTEGER NOT NULL DEFAULT 0,
  source_element_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, section_path, chunk_id UNINDEXED
);

CREATE TABLE IF NOT EXISTS embeddings (
  chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);

CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  type TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, type, canonical_name)
);
CREATE INDEX IF NOT EXISTS idx_entities_project ON entities(project_id);

CREATE TABLE IF NOT EXISTS entity_aliases (
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  norm_alias TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(entity_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_aliases_norm ON entity_aliases(project_id, norm_alias);

CREATE TABLE IF NOT EXISTS relations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  source_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,
  target_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  confidence REAL NOT NULL DEFAULT 1.0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, source_entity_id, relation_type, target_entity_id)
);
CREATE INDEX IF NOT EXISTS idx_relations_project ON relations(project_id);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_entity_id);

CREATE TABLE IF NOT EXISTS relation_evidence (
  id TEXT PRIMARY KEY,
  relation_id TEXT NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
  document_id TEXT NOT NULL,
  chunk_id TEXT NOT NULL,
  page INTEGER,
  text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_relation_evidence ON relation_evidence(relation_id);

CREATE TABLE IF NOT EXISTS processing_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  document_id TEXT,
  type TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'PENDING',
  payload TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON processing_jobs(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_document ON processing_jobs(document_id);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT 'New chat',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  project_id TEXT NOT NULL,
  user_message_id TEXT,
  harness_type TEXT NOT NULL DEFAULT 'native',
  llm_profile_id TEXT,
  selected_skill TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  iteration_count INTEGER NOT NULL DEFAULT 0,
  tool_call_count INTEGER NOT NULL DEFAULT 0,
  skill_call_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON agent_runs(session_id);

CREATE TABLE IF NOT EXISTS llm_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  provider_type TEXT NOT NULL DEFAULT 'openai-compatible',
  base_url TEXT NOT NULL,
  model TEXT NOT NULL,
  api_key_ref TEXT,
  timeout_seconds INTEGER NOT NULL DEFAULT 120,
  max_output_tokens INTEGER,
  context_window_override INTEGER,
  tool_calling_mode TEXT NOT NULL DEFAULT 'auto',
  streaming_enabled INTEGER NOT NULL DEFAULT 1,
  custom_headers TEXT NOT NULL DEFAULT '{}',
  retry_count INTEGER NOT NULL DEFAULT 2,
  tls_verify INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0,
  enabled_by_default INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  source_dir TEXT NOT NULL,
  manifest TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS project_skills (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  skill_id TEXT NOT NULL REFERENCES skills(id),
  enabled INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (project_id, skill_id)
);

CREATE TABLE IF NOT EXISTS skill_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  session_id TEXT,
  skill_id TEXT NOT NULL,
  skill_version TEXT NOT NULL DEFAULT '',
  run_id TEXT,
  instruction TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'RUNNING',
  result TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_runs_project ON skill_runs(project_id);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  file_name TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'markdown',
  skill_id TEXT,
  skill_version TEXT,
  validation_status TEXT NOT NULL DEFAULT 'unknown',
  current_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, file_name)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id);

CREATE TABLE IF NOT EXISTS artifact_versions (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  source_context TEXT NOT NULL DEFAULT '[]',
  validation TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(artifact_id, version)
);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'fact',
  source TEXT NOT NULL DEFAULT 'user',
  status TEXT NOT NULL DEFAULT 'active',
  source_refs TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id, status);

CREATE TABLE IF NOT EXISTS conflicts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflicts_project ON conflicts(project_id);

CREATE TABLE IF NOT EXISTS review_items (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  suggestion TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_project ON review_items(project_id, status);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  project_id TEXT,
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
