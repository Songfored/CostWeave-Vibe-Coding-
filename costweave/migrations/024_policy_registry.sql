CREATE TABLE IF NOT EXISTS registered_policies (
    registry_policy_id TEXT PRIMARY KEY,
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    signer_key_id TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL,
    signature_verified INTEGER NOT NULL CHECK (signature_verified = 1),
    initial_state TEXT NOT NULL CHECK (initial_state = 'draft'),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL CHECK (may_change_actual_route = 0),
    runtime_activation_executed INTEGER NOT NULL CHECK (runtime_activation_executed = 0),
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (policy_name, policy_version)
);

CREATE TABLE IF NOT EXISTS policy_promotion_evidence (
    evidence_id TEXT PRIMARY KEY,
    registry_policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    data_origin TEXT NOT NULL,
    independent_evaluation INTEGER NOT NULL,
    case_count INTEGER NOT NULL CHECK (case_count >= 0),
    cohort_hash TEXT NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    may_change_actual_route INTEGER NOT NULL CHECK (may_change_actual_route = 0),
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (registry_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS policy_promotion_gate_reports (
    gate_report_id TEXT PRIMARY KEY,
    registry_policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('evaluation','shadow','canary','activation')),
    eligible INTEGER NOT NULL,
    decision_code TEXT NOT NULL,
    registry_transition_allowed INTEGER NOT NULL,
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL CHECK (may_change_actual_route = 0),
    runtime_activation_executed INTEGER NOT NULL CHECK (runtime_activation_executed = 0),
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (registry_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (evidence_id) REFERENCES policy_promotion_evidence(evidence_id)
        ON DELETE RESTRICT,
    UNIQUE (registry_policy_id, evidence_id, stage)
);

CREATE TABLE IF NOT EXISTS policy_state_transitions (
    transition_id TEXT PRIMARY KEY,
    registry_policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    from_state TEXT NOT NULL CHECK (from_state IN ('draft','evaluated','shadow','canary','active','frozen','retired')),
    to_state TEXT NOT NULL CHECK (to_state IN ('draft','evaluated','shadow','canary','active','frozen','retired')),
    from_revision INTEGER NOT NULL CHECK (from_revision >= 0),
    to_revision INTEGER NOT NULL CHECK (to_revision = from_revision + 1),
    gate_report_id TEXT,
    signature_verified INTEGER NOT NULL CHECK (signature_verified = 1),
    approval_id TEXT,
    reason_code TEXT NOT NULL,
    registry_pointer_changed INTEGER NOT NULL,
    runtime_route_changed INTEGER NOT NULL CHECK (runtime_route_changed = 0),
    execution_side_effect_count INTEGER NOT NULL CHECK (execution_side_effect_count = 0),
    transition_json TEXT NOT NULL,
    transition_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (registry_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (gate_report_id) REFERENCES policy_promotion_gate_reports(gate_report_id)
        ON DELETE RESTRICT,
    UNIQUE (registry_policy_id, to_revision)
);

CREATE TABLE IF NOT EXISTS registry_policy_cards (
    policy_card_id TEXT PRIMARY KEY,
    registry_policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    current_state TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision >= 0),
    signature_verified INTEGER NOT NULL,
    latest_gate_report_id TEXT,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    runtime_route_changed INTEGER NOT NULL CHECK (runtime_route_changed = 0),
    card_json TEXT NOT NULL,
    card_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (registry_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (latest_gate_report_id) REFERENCES policy_promotion_gate_reports(gate_report_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS policy_registry_rollbacks (
    rollback_id TEXT PRIMARY KEY,
    frozen_policy_id TEXT NOT NULL,
    freeze_transition_id TEXT NOT NULL UNIQUE,
    rollback_policy_id TEXT NOT NULL,
    rollback_policy_state TEXT NOT NULL,
    current_policy_frozen INTEGER NOT NULL CHECK (current_policy_frozen = 1),
    registry_pointer_changed INTEGER NOT NULL CHECK (registry_pointer_changed = 1),
    runtime_route_changed INTEGER NOT NULL CHECK (runtime_route_changed = 0),
    runtime_rollback_executed INTEGER NOT NULL CHECK (runtime_rollback_executed = 0),
    execution_side_effect_count INTEGER NOT NULL CHECK (execution_side_effect_count = 0),
    rollback_json TEXT NOT NULL,
    rollback_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (frozen_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (rollback_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (freeze_transition_id) REFERENCES policy_state_transitions(transition_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS policy_registry_pointer_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    registry_policy_id TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('activation','rollback','freeze','retirement')),
    runtime_route_changed INTEGER NOT NULL CHECK (runtime_route_changed = 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (registry_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_registered_policy_version
    ON registered_policies(policy_name, policy_version);
CREATE INDEX IF NOT EXISTS idx_policy_evidence_level
    ON policy_promotion_evidence(evidence_level, data_origin, independent_evaluation, case_count);
CREATE INDEX IF NOT EXISTS idx_policy_gate_stage
    ON policy_promotion_gate_reports(stage, eligible, created_at);
CREATE INDEX IF NOT EXISTS idx_policy_transition_state
    ON policy_state_transitions(registry_policy_id, to_revision, to_state);

CREATE TRIGGER IF NOT EXISTS trg_registered_policy_immutable
BEFORE UPDATE ON registered_policies BEGIN
    SELECT RAISE(ABORT, 'registered policies are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_registered_policy_delete_immutable
BEFORE DELETE ON registered_policies BEGIN
    SELECT RAISE(ABORT, 'registered policies cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_evidence_immutable
BEFORE UPDATE ON policy_promotion_evidence BEGIN
    SELECT RAISE(ABORT, 'policy promotion evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_evidence_delete_immutable
BEFORE DELETE ON policy_promotion_evidence BEGIN
    SELECT RAISE(ABORT, 'policy promotion evidence cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_gate_immutable
BEFORE UPDATE ON policy_promotion_gate_reports BEGIN
    SELECT RAISE(ABORT, 'policy promotion gate reports are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_gate_delete_immutable
BEFORE DELETE ON policy_promotion_gate_reports BEGIN
    SELECT RAISE(ABORT, 'policy promotion gate reports cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_transition_immutable
BEFORE UPDATE ON policy_state_transitions BEGIN
    SELECT RAISE(ABORT, 'policy state transitions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_transition_delete_immutable
BEFORE DELETE ON policy_state_transitions BEGIN
    SELECT RAISE(ABORT, 'policy state transitions cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_registry_policy_card_immutable
BEFORE UPDATE ON registry_policy_cards BEGIN
    SELECT RAISE(ABORT, 'registry policy cards are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_registry_policy_card_delete_immutable
BEFORE DELETE ON registry_policy_cards BEGIN
    SELECT RAISE(ABORT, 'registry policy cards cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_registry_rollback_immutable
BEFORE UPDATE ON policy_registry_rollbacks BEGIN
    SELECT RAISE(ABORT, 'policy registry rollbacks are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_registry_rollback_delete_immutable
BEFORE DELETE ON policy_registry_rollbacks BEGIN
    SELECT RAISE(ABORT, 'policy registry rollbacks cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_registry_pointer_immutable
BEFORE UPDATE ON policy_registry_pointer_events BEGIN
    SELECT RAISE(ABORT, 'policy registry pointer events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_policy_registry_pointer_delete_immutable
BEFORE DELETE ON policy_registry_pointer_events BEGIN
    SELECT RAISE(ABORT, 'policy registry pointer events cannot be deleted');
END;
