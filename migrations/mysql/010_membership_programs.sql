CREATE TABLE IF NOT EXISTS membership_programs (
 program_id VARCHAR(64) PRIMARY KEY, platform_code VARCHAR(16) NOT NULL,
 merchant_id VARCHAR(64), program_type VARCHAR(16) NOT NULL, name VARCHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL, registration_default BOOLEAN NOT NULL DEFAULT FALSE,
 rule_version INT NOT NULL, evaluation_rules JSON NOT NULL,
 CHECK (program_type IN ('growth','paid','store')),
 FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS membership_subscriptions (
 subscription_id VARCHAR(64) PRIMARY KEY, membership_id VARCHAR(64) NOT NULL,
 plan_code VARCHAR(32) NOT NULL, starts_at DATETIME(6) NOT NULL, ends_at DATETIME(6) NOT NULL,
 status VARCHAR(16) NOT NULL, activation_mode VARCHAR(24) NOT NULL,
 payment_ref VARCHAR(128), plan_snapshot JSON NOT NULL, request_id VARCHAR(128) NOT NULL UNIQUE,
 created_at DATETIME(6) NOT NULL,
 CHECK (ends_at > starts_at), CHECK (plan_code IN ('monthly','annual')),
 CHECK (status IN ('active','cancelled','suspended')),
 FOREIGN KEY(membership_id) REFERENCES memberships(membership_id),
 KEY idx_member_subscription_window(membership_id,starts_at,ends_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS membership_growth_ledger (
 entry_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, program_id VARCHAR(64) NOT NULL,
 delta BIGINT NOT NULL, event_type VARCHAR(32) NOT NULL, event_key VARCHAR(128) NOT NULL,
 original_entry_id VARCHAR(64), order_reference VARCHAR(128), refund_reference VARCHAR(128),
 effective_at DATETIME(6) NOT NULL, expires_at DATETIME(6) NOT NULL,
 UNIQUE KEY uq_growth_event(program_id,event_key),
 FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(program_id) REFERENCES membership_programs(program_id),
 FOREIGN KEY(original_entry_id) REFERENCES membership_growth_ledger(entry_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS membership_events (
 event_id VARCHAR(64) PRIMARY KEY, membership_id VARCHAR(64) NOT NULL,
 event_type VARCHAR(32) NOT NULL, from_level VARCHAR(32), to_level VARCHAR(32),
 effective_at DATETIME(6) NOT NULL, ends_at DATETIME(6), reason VARCHAR(255) NOT NULL,
 rule_version INT NOT NULL, request_id VARCHAR(128) NOT NULL UNIQUE,
 FOREIGN KEY(membership_id) REFERENCES memberships(membership_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS membership_benefits (
 benefit_id VARCHAR(64) NOT NULL, version INT NOT NULL,
 program_id VARCHAR(64) NOT NULL, level_code VARCHAR(32) NOT NULL, level_version INT NOT NULL,
 benefit_family VARCHAR(64) NOT NULL, period_type VARCHAR(16) NOT NULL, quota INT NOT NULL,
 membership_check VARCHAR(24) NOT NULL, rules JSON NOT NULL, status VARCHAR(16) NOT NULL,
 effective_from DATETIME(6) NOT NULL, effective_until DATETIME(6),
 PRIMARY KEY(benefit_id,version), CHECK(quota > 0),
 FOREIGN KEY(program_id,level_code,level_version) REFERENCES membership_levels(program_id,level_code,version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS benefit_entitlements (
 entitlement_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, program_id VARCHAR(64) NOT NULL,
 benefit_family VARCHAR(64) NOT NULL, period_key VARCHAR(32) NOT NULL,
 quota INT NOT NULL, claimed_count INT NOT NULL DEFAULT 0,
 starts_at DATETIME(6) NOT NULL, expires_at DATETIME(6) NOT NULL, status VARCHAR(16) NOT NULL,
 benefit_id VARCHAR(64) NOT NULL, benefit_version INT NOT NULL, evidence_snapshot JSON NOT NULL,
 UNIQUE KEY uq_member_period(user_id,program_id,benefit_family,period_key),
 CHECK(claimed_count >= 0 AND claimed_count <= quota),
 FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(program_id) REFERENCES membership_programs(program_id),
 FOREIGN KEY(benefit_id,benefit_version) REFERENCES membership_benefits(benefit_id,version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
