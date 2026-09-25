"""在基础层数据库上扩展云赏月内容治理所需的表结构。"""

from __future__ import annotations

GOVERNANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    kind TEXT NOT NULL CHECK(kind IN ('poetry_chain', 'hometown_intro', 'blessing')),
    author_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected', 'withdrawn', 'compliance_removed')),
    current_version_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_versions (
    version_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    text_body TEXT NOT NULL,
    media_json TEXT NOT NULL,
    declaration_json TEXT NOT NULL,
    citations_json TEXT NOT NULL,
    visibility_scope TEXT NOT NULL CHECK(visibility_scope IN ('public', 'event', 'internal')),
    review_status TEXT NOT NULL CHECK(review_status IN ('pending', 'approved', 'rejected')),
    decision_json TEXT,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(submission_id, version_no)
);
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    version_id TEXT NOT NULL REFERENCES submission_versions(version_id),
    kind TEXT NOT NULL CHECK(kind IN ('initial', 'reevaluation', 'appeal')),
    rule_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'leased', 'decided', 'cancelled')),
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    version_id TEXT NOT NULL REFERENCES submission_versions(version_id),
    task_id TEXT NOT NULL REFERENCES review_tasks(task_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'decided')),
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS topics (
    topic_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    audience_scope TEXT NOT NULL CHECK(audience_scope IN ('public', 'event', 'internal')),
    status TEXT NOT NULL CHECK(status IN ('draft', 'published', 'archived')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    snapshot_json TEXT
);
CREATE TABLE IF NOT EXISTS topic_members (
    topic_id TEXT NOT NULL REFERENCES topics(topic_id),
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    version_id TEXT NOT NULL REFERENCES submission_versions(version_id),
    position INTEGER NOT NULL CHECK(position >= 0),
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (topic_id, submission_id)
);
CREATE TABLE IF NOT EXISTS rule_registry (
    rule_version INTEGER PRIMARY KEY,
    note TEXT NOT NULL,
    activated_by TEXT NOT NULL,
    activated_at TEXT NOT NULL
);
"""

INITIAL_RULE_NOTE = "初始审核规则"


def ensure_governance_schema(connection, now: str) -> None:
    """建表并登记初始审核规则版本。"""

    connection.executescript(GOVERNANCE_SCHEMA)
    row = connection.execute("SELECT MAX(rule_version) AS version FROM rule_registry").fetchone()
    if row["version"] is None:
        connection.execute(
            "INSERT INTO rule_registry(rule_version,note,activated_by,activated_at) VALUES(1,?,?,?)",
            (INITIAL_RULE_NOTE, "system", now),
        )
