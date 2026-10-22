CREATE TABLE IF NOT EXISTS user_profiles (
    user_id VARCHAR(64) NOT NULL PRIMARY KEY,
    display_name VARCHAR(80) NULL,
    avatar_url VARCHAR(512) NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_user_profiles_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_contacts (
    contact_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    contact_type VARCHAR(16) NOT NULL,
    value_ciphertext VARBINARY(512) NOT NULL,
    lookup_digest BINARY(32) NOT NULL,
    key_version SMALLINT UNSIGNED NOT NULL,
    verified_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_user_contacts_lookup (contact_type, lookup_digest),
    KEY idx_user_contacts_user (user_id),
    CONSTRAINT fk_user_contacts_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_addresses (
    address_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    recipient_name_ciphertext VARBINARY(512) NOT NULL,
    phone_ciphertext VARBINARY(512) NOT NULL,
    province_code VARCHAR(16) NOT NULL,
    city_code VARCHAR(16) NOT NULL,
    district_code VARCHAR(16) NOT NULL,
    detail_ciphertext VARBINARY(1024) NOT NULL,
    key_version SMALLINT UNSIGNED NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    deleted_at DATETIME(6) NULL,
    KEY idx_user_addresses_owner (user_id, deleted_at),
    CONSTRAINT fk_user_addresses_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_platform_accounts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    platform_code VARCHAR(16) NOT NULL,
    external_user_id VARCHAR(128) NOT NULL,
    verified_at DATETIME(6) NOT NULL,
    status VARCHAR(16) NOT NULL,
    UNIQUE KEY uq_user_platform_identity (platform_code, external_user_id),
    KEY idx_user_platform_owner (user_id),
    CONSTRAINT fk_user_platform_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_roles (
    user_id VARCHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    granted_by VARCHAR(64) NULL,
    granted_at DATETIME(6) NOT NULL,
    PRIMARY KEY (user_id, role),
    CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_user_roles_granter FOREIGN KEY (granted_by) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS product_skus (
    sku_id VARCHAR(64) NOT NULL PRIMARY KEY,
    product_id VARCHAR(64) NOT NULL,
    sku_code VARCHAR(64) NOT NULL,
    spec_values JSON NOT NULL,
    list_price DECIMAL(12,2) NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'CNY',
    status VARCHAR(16) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_product_sku_code (product_id, sku_code),
    CONSTRAINT chk_product_sku_price CHECK (list_price >= 0),
    CONSTRAINT fk_product_skus_product FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sku_inventory (
    sku_id VARCHAR(64) NOT NULL PRIMARY KEY,
    available_qty INT NOT NULL DEFAULT 0,
    reserved_qty INT NOT NULL DEFAULT 0,
    version BIGINT NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT chk_sku_inventory_qty CHECK (available_qty >= 0 AND reserved_qty >= 0),
    CONSTRAINT fk_sku_inventory_sku FOREIGN KEY (sku_id) REFERENCES product_skus(sku_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sku_price_history (
    price_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    sku_id VARCHAR(64) NOT NULL,
    price_type VARCHAR(16) NOT NULL,
    amount DECIMAL(12,2) NOT NULL,
    currency CHAR(3) NOT NULL,
    effective_from DATETIME(6) NOT NULL,
    effective_until DATETIME(6) NULL,
    source VARCHAR(64) NOT NULL,
    KEY idx_sku_price_lookup (sku_id, price_type, effective_from),
    CONSTRAINT chk_sku_price_amount CHECK (amount >= 0),
    CONSTRAINT fk_sku_price_sku FOREIGN KEY (sku_id) REFERENCES product_skus(sku_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id VARCHAR(64) NOT NULL PRIMARY KEY,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    sku_id VARCHAR(64) NULL,
    product_id VARCHAR(64) NULL,
    title_snapshot VARCHAR(255) NOT NULL,
    spec_snapshot JSON NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    discount_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    payable_amount DECIMAL(12,2) NOT NULL,
    KEY idx_order_items_order (platform_code, order_id),
    KEY idx_order_items_sku (sku_id),
    KEY idx_order_items_product (product_id),
    CONSTRAINT chk_order_items_amount CHECK (quantity > 0 AND unit_price >= 0 AND discount_amount >= 0 AND payable_amount >= 0),
    CONSTRAINT fk_order_items_order FOREIGN KEY (platform_code, order_id) REFERENCES orders(platform_code, order_id) ON DELETE RESTRICT,
    CONSTRAINT fk_order_items_sku FOREIGN KEY (sku_id) REFERENCES product_skus(sku_id) ON DELETE RESTRICT,
    CONSTRAINT fk_order_items_product FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_address_snapshots (
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    recipient_name_ciphertext VARBINARY(512) NOT NULL,
    phone_ciphertext VARBINARY(512) NOT NULL,
    province_code VARCHAR(16) NOT NULL,
    city_code VARCHAR(16) NOT NULL,
    district_code VARCHAR(16) NOT NULL,
    detail_ciphertext VARBINARY(1024) NOT NULL,
    key_version SMALLINT UNSIGNED NOT NULL,
    captured_at DATETIME(6) NOT NULL,
    PRIMARY KEY (platform_code, order_id),
    CONSTRAINT fk_order_address_order FOREIGN KEY (platform_code, order_id) REFERENCES orders(platform_code, order_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS payment_transactions (
    payment_id VARCHAR(64) NOT NULL PRIMARY KEY,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    provider_txn_id VARCHAR(128) NULL,
    amount DECIMAL(12,2) NOT NULL,
    currency CHAR(3) NOT NULL,
    status VARCHAR(24) NOT NULL,
    paid_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_payment_provider_txn (provider, provider_txn_id),
    KEY idx_payment_order_owner (platform_code, order_id, user_id),
    KEY idx_payment_user (user_id),
    CONSTRAINT chk_payment_amount CHECK (amount >= 0),
    CONSTRAINT fk_payment_order_owner FOREIGN KEY (platform_code, order_id, user_id) REFERENCES orders(platform_code, order_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_payment_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id VARCHAR(64) NOT NULL PRIMARY KEY,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    tracking_number VARCHAR(64) NULL,
    carrier VARCHAR(32) NULL,
    status VARCHAR(32) NOT NULL,
    shipped_at DATETIME(6) NULL,
    delivered_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    KEY idx_shipments_order (platform_code, order_id),
    KEY idx_shipments_tracking (carrier, tracking_number),
    CONSTRAINT fk_shipments_order FOREIGN KEY (platform_code, order_id) REFERENCES orders(platform_code, order_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shipment_items (
    shipment_id VARCHAR(64) NOT NULL,
    order_item_id VARCHAR(64) NOT NULL,
    quantity INT NOT NULL,
    PRIMARY KEY (shipment_id, order_item_id),
    KEY idx_shipment_items_order_item (order_item_id),
    CONSTRAINT chk_shipment_items_qty CHECK (quantity > 0),
    CONSTRAINT fk_shipment_items_shipment FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id) ON DELETE RESTRICT,
    CONSTRAINT fk_shipment_items_order_item FOREIGN KEY (order_item_id) REFERENCES order_items(order_item_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shipment_events (
    event_id VARCHAR(64) NOT NULL PRIMARY KEY,
    shipment_id VARCHAR(64) NOT NULL,
    event_time DATETIME(6) NOT NULL,
    status VARCHAR(32) NOT NULL,
    location VARCHAR(128) NULL,
    description VARCHAR(512) NULL,
    source_event_id VARCHAR(128) NULL,
    recorded_at DATETIME(6) NOT NULL,
    KEY idx_shipment_events_history (shipment_id, event_time),
    UNIQUE KEY uq_shipment_source_event (shipment_id, source_event_id),
    CONSTRAINT fk_shipment_events_shipment FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS promotion_rules (
    promotion_id VARCHAR(64) NOT NULL,
    version INT NOT NULL,
    merchant_id VARCHAR(64) NULL,
    platform_code VARCHAR(16) NOT NULL,
    channel VARCHAR(64) NULL,
    name VARCHAR(128) NOT NULL,
    kind VARCHAR(24) NOT NULL,
    currency CHAR(3) NOT NULL,
    minimum_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    discount_amount DECIMAL(12,2) NULL,
    discount_bps INT NULL,
    cap_amount DECIMAL(12,2) NULL,
    exclusive_group VARCHAR(64) NULL,
    priority INT NOT NULL DEFAULT 0,
    starts_at DATETIME(6) NOT NULL,
    ends_at DATETIME(6) NOT NULL,
    status VARCHAR(16) NOT NULL,
    PRIMARY KEY (promotion_id, version),
    KEY idx_promotions_scope (platform_code, status, starts_at, ends_at),
    KEY idx_promotions_merchant (merchant_id),
    CONSTRAINT fk_promotions_merchant FOREIGN KEY (merchant_id) REFERENCES merchants(merchant_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS coupon_templates (
    coupon_id VARCHAR(64) NOT NULL PRIMARY KEY,
    merchant_id VARCHAR(64) NULL,
    platform_code VARCHAR(16) NOT NULL,
    name VARCHAR(128) NOT NULL,
    kind VARCHAR(24) NOT NULL,
    currency CHAR(3) NOT NULL,
    threshold_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    discount_amount DECIMAL(12,2) NULL,
    discount_bps INT NULL,
    cap_amount DECIMAL(12,2) NULL,
    total_limit INT NULL,
    per_user_limit INT NOT NULL,
    claim_starts_at DATETIME(6) NOT NULL,
    claim_ends_at DATETIME(6) NOT NULL,
    valid_days INT NULL,
    fixed_expires_at DATETIME(6) NULL,
    status VARCHAR(16) NOT NULL,
    KEY idx_coupon_templates_window (platform_code, status, claim_starts_at, claim_ends_at),
    KEY idx_coupon_templates_merchant (merchant_id),
    CONSTRAINT chk_coupon_template_limits CHECK (threshold_amount >= 0 AND per_user_limit > 0 AND (total_limit IS NULL OR total_limit >= 0)),
    CONSTRAINT fk_coupon_templates_merchant FOREIGN KEY (merchant_id) REFERENCES merchants(merchant_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS coupon_scopes (
    coupon_id VARCHAR(64) NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id VARCHAR(64) NOT NULL,
    PRIMARY KEY (coupon_id, scope_type, scope_id),
    CONSTRAINT fk_coupon_scopes_template FOREIGN KEY (coupon_id) REFERENCES coupon_templates(coupon_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_coupons (
    user_coupon_id VARCHAR(64) NOT NULL PRIMARY KEY,
    coupon_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    claimed_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    used_at DATETIME(6) NULL,
    version BIGINT NOT NULL DEFAULT 0,
    KEY idx_user_coupons_wallet (user_id, status, expires_at),
    KEY idx_user_coupons_template (coupon_id),
    CONSTRAINT fk_user_coupons_template FOREIGN KEY (coupon_id) REFERENCES coupon_templates(coupon_id) ON DELETE RESTRICT,
    CONSTRAINT fk_user_coupons_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS coupon_redemptions (
    redemption_id VARCHAR(64) NOT NULL PRIMARY KEY,
    user_coupon_id VARCHAR(64) NOT NULL,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    discount_amount DECIMAL(12,2) NOT NULL,
    currency CHAR(3) NOT NULL,
    status VARCHAR(16) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    released_at DATETIME(6) NULL,
    UNIQUE KEY uq_coupon_redemption_request (request_id),
    KEY idx_coupon_redemption_order (platform_code, order_id),
    KEY idx_coupon_redemption_coupon (user_coupon_id),
    CONSTRAINT chk_coupon_redemption_amount CHECK (discount_amount >= 0),
    CONSTRAINT fk_coupon_redemption_wallet FOREIGN KEY (user_coupon_id) REFERENCES user_coupons(user_coupon_id) ON DELETE RESTRICT,
    CONSTRAINT fk_coupon_redemption_order FOREIGN KEY (platform_code, order_id) REFERENCES orders(platform_code, order_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS after_sale_requests (
    after_sale_id VARCHAR(64) NOT NULL PRIMARY KEY,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    kind VARCHAR(24) NOT NULL,
    status VARCHAR(24) NOT NULL,
    reason VARCHAR(255) NOT NULL,
    requested_amount DECIMAL(12,2) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    closed_at DATETIME(6) NULL,
    KEY idx_after_sale_order_owner (platform_code, order_id, user_id),
    KEY idx_after_sale_user_history (user_id, created_at),
    CONSTRAINT fk_after_sale_order_owner FOREIGN KEY (platform_code, order_id, user_id) REFERENCES orders(platform_code, order_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_after_sale_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS after_sale_items (
    after_sale_id VARCHAR(64) NOT NULL,
    order_item_id VARCHAR(64) NOT NULL,
    quantity INT NOT NULL,
    requested_amount DECIMAL(12,2) NULL,
    PRIMARY KEY (after_sale_id, order_item_id),
    KEY idx_after_sale_items_order_item (order_item_id),
    CONSTRAINT chk_after_sale_item_qty CHECK (quantity > 0),
    CONSTRAINT fk_after_sale_items_request FOREIGN KEY (after_sale_id) REFERENCES after_sale_requests(after_sale_id) ON DELETE RESTRICT,
    CONSTRAINT fk_after_sale_items_order_item FOREIGN KEY (order_item_id) REFERENCES order_items(order_item_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS refund_transactions (
    refund_id VARCHAR(64) NOT NULL PRIMARY KEY,
    after_sale_id VARCHAR(64) NOT NULL,
    payment_id VARCHAR(64) NULL,
    provider VARCHAR(32) NULL,
    provider_refund_id VARCHAR(128) NULL,
    amount DECIMAL(12,2) NOT NULL,
    currency CHAR(3) NOT NULL,
    status VARCHAR(24) NOT NULL,
    requested_at DATETIME(6) NOT NULL,
    completed_at DATETIME(6) NULL,
    UNIQUE KEY uq_refund_provider_txn (provider, provider_refund_id),
    KEY idx_refund_after_sale (after_sale_id),
    KEY idx_refund_payment (payment_id),
    CONSTRAINT chk_refund_amount CHECK (amount >= 0),
    CONSTRAINT fk_refund_after_sale FOREIGN KEY (after_sale_id) REFERENCES after_sale_requests(after_sale_id) ON DELETE RESTRICT,
    CONSTRAINT fk_refund_payment FOREIGN KEY (payment_id) REFERENCES payment_transactions(payment_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS invoice_requests (
    invoice_id VARCHAR(64) NOT NULL PRIMARY KEY,
    platform_code VARCHAR(16) NOT NULL,
    order_id VARCHAR(40) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    invoice_type VARCHAR(24) NOT NULL,
    title_ciphertext VARBINARY(1024) NOT NULL,
    tax_number_ciphertext VARBINARY(512) NULL,
    key_version SMALLINT UNSIGNED NOT NULL,
    status VARCHAR(24) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    issued_at DATETIME(6) NULL,
    UNIQUE KEY uq_invoice_request_owner (user_id, request_id),
    KEY idx_invoice_order_owner (platform_code, order_id, user_id),
    CONSTRAINT fk_invoice_order_owner FOREIGN KEY (platform_code, order_id, user_id) REFERENCES orders(platform_code, order_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_invoice_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS business_policy_versions (
    policy_id VARCHAR(64) NOT NULL,
    version INT NOT NULL,
    action VARCHAR(32) NOT NULL,
    platform_code VARCHAR(16) NOT NULL,
    channel VARCHAR(64) NULL,
    rules JSON NOT NULL,
    effective_from DATETIME(6) NOT NULL,
    effective_until DATETIME(6) NULL,
    source VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL,
    PRIMARY KEY (policy_id, version),
    KEY idx_policy_lookup (action, platform_code, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
