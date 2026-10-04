"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_regions (
    region_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tier TEXT NOT NULL,
    priority REAL NOT NULL CHECK(priority >= 0 AND priority <= 1),
    population INTEGER NOT NULL CHECK(population >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_providers (
    provider_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_policies (
    version TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    frozen_by TEXT NOT NULL,
    frozen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_rounds (
    round_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL REFERENCES incl_policies(version),
    budget_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS incl_applicants (
    applicant_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL UNIQUE REFERENCES organizations(organization_id),
    region_id TEXT NOT NULL REFERENCES incl_regions(region_id),
    affiliation_group TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    beneficiary_population INTEGER NOT NULL CHECK(beneficiary_population >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_applications (
    application_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES incl_rounds(round_id),
    applicant_id TEXT NOT NULL REFERENCES incl_applicants(applicant_id),
    category TEXT NOT NULL,
    provider_id TEXT NOT NULL REFERENCES incl_providers(provider_id),
    requested_amount INTEGER NOT NULL CHECK(requested_amount > 0),
    status TEXT NOT NULL,
    via_special INTEGER NOT NULL DEFAULT 0 CHECK(via_special IN (0, 1)),
    submitted_at TEXT NOT NULL,
    screened_at TEXT,
    screening_result TEXT,
    screening_reason TEXT,
    screening_detail TEXT,
    reserved_amount INTEGER,
    reserved_at TEXT,
    expires_at TEXT,
    waitlist_rank INTEGER,
    current_run_id TEXT,
    commitment_id TEXT,
    committed_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(round_id, applicant_id, category)
);
CREATE INDEX IF NOT EXISTS idx_incl_app_round_cat ON incl_applications(round_id, category, status);
CREATE TABLE IF NOT EXISTS incl_materials (
    application_id TEXT NOT NULL REFERENCES incl_applications(application_id),
    material_key TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY(application_id, material_key)
);
CREATE TABLE IF NOT EXISTS incl_milestones (
    application_id TEXT NOT NULL REFERENCES incl_applications(application_id),
    code TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    prerequisite INTEGER NOT NULL CHECK(prerequisite IN (0, 1)),
    seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'reported', 'verified', 'failed')),
    evidence_json TEXT,
    reported_by TEXT,
    reported_at TEXT,
    verified_by TEXT,
    verified_at TEXT,
    PRIMARY KEY(application_id, code)
);
CREATE TABLE IF NOT EXISTS incl_reviewer_scores (
    application_id TEXT NOT NULL REFERENCES incl_applications(application_id),
    reviewer_id TEXT NOT NULL,
    score REAL NOT NULL CHECK(score >= 0 AND score <= 100),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(application_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS incl_reviewer_links (
    reviewer_id TEXT NOT NULL,
    provider_id TEXT NOT NULL REFERENCES incl_providers(provider_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(reviewer_id, provider_id)
);
CREATE TABLE IF NOT EXISTS incl_ranking_runs (
    run_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES incl_rounds(round_id),
    policy_version TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_ranking_items (
    run_id TEXT NOT NULL REFERENCES incl_ranking_runs(run_id),
    application_id TEXT NOT NULL REFERENCES incl_applications(application_id),
    category TEXT NOT NULL,
    rank_position INTEGER NOT NULL,
    total_score REAL NOT NULL,
    factors_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    PRIMARY KEY(run_id, application_id)
);
CREATE TABLE IF NOT EXISTS incl_commitments (
    commitment_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES incl_applications(application_id),
    round_id TEXT NOT NULL,
    category TEXT NOT NULL,
    total_amount INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_installments (
    commitment_id TEXT NOT NULL REFERENCES incl_commitments(commitment_id),
    seq INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    trigger_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scheduled', 'paid', 'released')),
    paid_at TEXT,
    PRIMARY KEY(commitment_id, seq)
);
CREATE TABLE IF NOT EXISTS incl_outcomes (
    application_id TEXT NOT NULL REFERENCES incl_applications(application_id),
    metric_code TEXT NOT NULL,
    value REAL NOT NULL,
    effective INTEGER NOT NULL DEFAULT 0 CHECK(effective IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN ('reported', 'verified', 'rejected')),
    reported_by TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    verified_by TEXT,
    verified_at TEXT,
    PRIMARY KEY(application_id, metric_code)
);
CREATE TABLE IF NOT EXISTS incl_exits (
    exit_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES incl_applications(application_id),
    kind TEXT NOT NULL,
    responsibility_amount INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_specials (
    special_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    application_id TEXT NOT NULL REFERENCES incl_applications(application_id),
    amount INTEGER NOT NULL CHECK(amount > 0),
    reason TEXT NOT NULL,
    initiator_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
    countersigner_id TEXT,
    countersigned_at TEXT,
    fairness_before_json TEXT,
    fairness_after_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incl_fund_ledger (
    ledger_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    category TEXT NOT NULL,
    application_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incl_ledger_pool ON incl_fund_ledger(round_id, category);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
