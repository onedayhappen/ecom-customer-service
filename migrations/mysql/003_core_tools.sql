CREATE TABLE IF NOT EXISTS business_operation_receipts (
    operation_id VARCHAR(128) NOT NULL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL,
    arguments_digest CHAR(64) NOT NULL,
    result JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    KEY idx_business_receipts_user (user_id, created_at),
    CONSTRAINT fk_business_receipts_user FOREIGN KEY (user_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS promotion_scopes (
    promotion_id VARCHAR(64) NOT NULL,
    version INT NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id VARCHAR(64) NOT NULL,
    PRIMARY KEY (promotion_id, version, scope_type, scope_id),
    CONSTRAINT fk_promotion_scope_version FOREIGN KEY (promotion_id, version) REFERENCES promotion_rules(promotion_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS coupon_claim_rules (
    user_coupon_id VARCHAR(64) NOT NULL PRIMARY KEY,
    rules_snapshot JSON NOT NULL,
    captured_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_coupon_claim_rules_instance FOREIGN KEY (user_coupon_id) REFERENCES user_coupons(user_coupon_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS inventory_reservations (
    reservation_id VARCHAR(64) NOT NULL PRIMARY KEY,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    order_item_id VARCHAR(64) NOT NULL,
    sku_id VARCHAR(64) NOT NULL,
    quantity INT NOT NULL,
    status VARCHAR(16) NOT NULL,
    version BIGINT NOT NULL DEFAULT 0,
    request_id VARCHAR(128) NOT NULL UNIQUE,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    KEY idx_inventory_reservations_order (platform_code, order_id),
    CONSTRAINT chk_inventory_reservation_quantity CHECK (quantity > 0),
    CONSTRAINT fk_inventory_reservation_order FOREIGN KEY (platform_code, order_id) REFERENCES orders(platform_code, order_id),
    CONSTRAINT fk_inventory_reservation_item FOREIGN KEY (order_item_id) REFERENCES order_items(order_item_id),
    CONSTRAINT fk_inventory_reservation_sku FOREIGN KEY (sku_id) REFERENCES product_skus(sku_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_events (
    event_id VARCHAR(64) NOT NULL PRIMARY KEY,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    operation_id VARCHAR(128) NOT NULL UNIQUE,
    kind VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    KEY idx_order_events_order (platform_code, order_id),
    CONSTRAINT fk_order_event_order FOREIGN KEY (platform_code, order_id) REFERENCES orders(platform_code, order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS after_sale_events (
    event_id VARCHAR(64) NOT NULL PRIMARY KEY,
    after_sale_id VARCHAR(64) NOT NULL,
    operation_id VARCHAR(128) NOT NULL UNIQUE,
    kind VARCHAR(32) NOT NULL,
    source VARCHAR(24) NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    KEY idx_after_sale_events_request (after_sale_id, created_at),
    CONSTRAINT fk_sale_event_request FOREIGN KEY (after_sale_id) REFERENCES after_sale_requests(after_sale_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_address_changes (
    change_id VARCHAR(64) NOT NULL PRIMARY KEY,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    operation_id VARCHAR(128) NOT NULL UNIQUE,
    previous_ciphertext MEDIUMBLOB NOT NULL,
    replacement_ciphertext MEDIUMBLOB NOT NULL,
    key_version SMALLINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_address_change_order FOREIGN KEY (platform_code, order_id) REFERENCES orders(platform_code, order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
