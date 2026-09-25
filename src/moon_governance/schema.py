"""定义云赏月内容治理服务在共享数据库中的表结构。"""

from __future__ import annotations


SCHEMA = """
CREATE TABLE IF NOT EXISTS moon_rule_sets (
    rule_version INTEGER PRIMARY KEY CHECK(rule_version >= 2),
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS moon_submissions (
    submission_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    kind TEXT NOT NULL,
    author_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'withdrawn', 'taken_down')),
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    closed_reason TEXT,
    closed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS moon_submission_versions (
    submission_id TEXT NOT NULL REFERENCES moon_submissions(submission_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    text_body TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    media_json TEXT NOT NULL,
    media_hash TEXT NOT NULL,
    declaration_json TEXT NOT NULL,
    citations_json TEXT NOT NULL,
    visibility_scope TEXT NOT NULL,
    review_state TEXT NOT NULL CHECK(review_state IN ('pending', 'approved', 'rejected')),
    rule_version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (submission_id, version)
);
CREATE TABLE IF NOT EXISTS moon_review_tasks (
    task_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES moon_submissions(submission_id),
    version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('initial', 'appeal')),
    state TEXT NOT NULL CHECK(state IN ('open', 'decided', 'cancelled')),
    rule_version INTEGER NOT NULL,
    original_reviewer TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision TEXT CHECK(decision IN ('approve', 'reject')),
    decision_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moon_review_tasks_open
    ON moon_review_tasks(state, kind, created_at);
CREATE TABLE IF NOT EXISTS moon_collections (
    collection_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    usage_scope TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draft', 'published', 'archived')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS moon_collection_items (
    collection_id TEXT NOT NULL REFERENCES moon_collections(collection_id),
    submission_id TEXT NOT NULL REFERENCES moon_submissions(submission_id),
    version INTEGER NOT NULL,
    position INTEGER NOT NULL,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (collection_id, submission_id)
);
CREATE TABLE IF NOT EXISTS moon_collection_publications (
    collection_id TEXT NOT NULL REFERENCES moon_collections(collection_id),
    publication_version INTEGER NOT NULL,
    members_json TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (collection_id, publication_version)
);
"""


def ensure_schema(connection) -> None:
    """在共享数据库上创建治理服务所需的表（可重复执行）。"""

    connection.executescript(SCHEMA)
