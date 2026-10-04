"""普惠支持模块在基础服务数据库上扩展的业务表。"""

from __future__ import annotations


SUPPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS support_policies (
    policy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','frozen','closed')),
    config_json TEXT NOT NULL,
    total_budget INTEGER NOT NULL CHECK(total_budget >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_at TEXT
);
CREATE TABLE IF NOT EXISTS support_applicants (
    applicant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS support_affiliations (
    applicant_id TEXT NOT NULL REFERENCES support_applicants(applicant_id),
    related_key TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (applicant_id, related_key)
);
CREATE TABLE IF NOT EXISTS support_providers (
    provider_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS support_reviewer_conflicts (
    reviewer_id TEXT NOT NULL,
    provider_id TEXT NOT NULL REFERENCES support_providers(provider_id),
    reason TEXT NOT NULL,
    declared_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (reviewer_id, provider_id)
);
CREATE TABLE IF NOT EXISTS support_applications (
    application_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES support_policies(policy_id),
    applicant_id TEXT NOT NULL REFERENCES support_applicants(applicant_id),
    provider_id TEXT NOT NULL REFERENCES support_providers(provider_id),
    category TEXT NOT NULL,
    region_id TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    requested_amount INTEGER NOT NULL CHECK(requested_amount > 0),
    status TEXT NOT NULL,
    exit_obligations_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(policy_id, applicant_id)
);
CREATE TABLE IF NOT EXISTS support_materials (
    application_id TEXT NOT NULL REFERENCES support_applications(application_id),
    material_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (application_id, material_key)
);
CREATE TABLE IF NOT EXISTS support_reviews (
    application_id TEXT NOT NULL REFERENCES support_applications(application_id),
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (application_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS support_rankings (
    policy_id TEXT NOT NULL REFERENCES support_policies(policy_id),
    application_id TEXT NOT NULL REFERENCES support_applications(application_id),
    run_id TEXT NOT NULL,
    score INTEGER NOT NULL,
    position INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, application_id)
);
CREATE TABLE IF NOT EXISTS support_reservations (
    application_id TEXT PRIMARY KEY REFERENCES support_applications(application_id),
    policy_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    source TEXT NOT NULL CHECK(source IN ('ranking','promotion','special')),
    status TEXT NOT NULL CHECK(status IN ('active','converted','released')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS support_commitments (
    application_id TEXT PRIMARY KEY REFERENCES support_applications(application_id),
    policy_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    released_back INTEGER NOT NULL DEFAULT 0 CHECK(released_back >= 0),
    status TEXT NOT NULL CHECK(status IN ('active','completed','exited')),
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS support_installments (
    application_id TEXT NOT NULL REFERENCES support_applications(application_id),
    installment_no INTEGER NOT NULL,
    milestone_key TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    status TEXT NOT NULL CHECK(status IN ('pending','released','cancelled')),
    released_at TEXT,
    PRIMARY KEY (application_id, installment_no)
);
CREATE TABLE IF NOT EXISTS support_milestones (
    application_id TEXT NOT NULL REFERENCES support_applications(application_id),
    milestone_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reported','verified','rejected')),
    reported_by TEXT NOT NULL,
    verified_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (application_id, milestone_key)
);
CREATE TABLE IF NOT EXISTS support_outcomes (
    application_id TEXT NOT NULL REFERENCES support_applications(application_id),
    metric_key TEXT NOT NULL,
    value REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reported','verified','rejected')),
    reported_by TEXT NOT NULL,
    verified_by TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (application_id, metric_key)
);
CREATE TABLE IF NOT EXISTS support_decisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS support_budget_ledger (
    entry_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    application_id TEXT,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    available_after INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS support_special_approvals (
    approval_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES support_policies(policy_id),
    application_id TEXT NOT NULL REFERENCES support_applications(application_id),
    amount INTEGER NOT NULL CHECK(amount > 0),
    justification TEXT NOT NULL,
    fairness_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','cosigned','rejected')),
    proposed_by TEXT NOT NULL,
    cosigned_by TEXT,
    created_at TEXT NOT NULL,
    cosigned_at TEXT
);
"""
