CREATE TABLE IF NOT EXISTS address_labels (
 address_id BIGINT UNSIGNED PRIMARY KEY, label VARCHAR(100) NOT NULL,
 province_name VARCHAR(64), city_name VARCHAR(64), district_name VARCHAR(64),
 FOREIGN KEY(address_id) REFERENCES user_addresses(address_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sku_fulfillment (
 sku_id VARCHAR(64) PRIMARY KEY, warehouse_id VARCHAR(64) NOT NULL,
 weight_grams INT NOT NULL, source VARCHAR(128) NOT NULL, updated_at DATETIME(6) NOT NULL,
 CHECK(weight_grams > 0), FOREIGN KEY(sku_id) REFERENCES product_skus(sku_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS delivery_rules (
 rule_id VARCHAR(64) NOT NULL, version INT NOT NULL, platform_code VARCHAR(16) NOT NULL,
 merchant_id VARCHAR(64) NOT NULL, warehouse_id VARCHAR(64) NOT NULL,
 province_code VARCHAR(16) NOT NULL, city_code VARCHAR(16) NOT NULL DEFAULT '*',
 district_code VARCHAR(16) NOT NULL DEFAULT '*', currency CHAR(3) NOT NULL,
 base_grams INT NOT NULL, base_fee DECIMAL(12,2) NOT NULL,
 step_grams INT NOT NULL, step_fee DECIMAL(12,2) NOT NULL,
 min_days INT NOT NULL, max_days INT NOT NULL, source VARCHAR(128) NOT NULL,
 starts_at DATETIME(6) NOT NULL, ends_at DATETIME(6), status VARCHAR(16) NOT NULL,
 PRIMARY KEY(rule_id,version), CHECK(base_grams > 0 AND step_grams > 0 AND base_fee >= 0 AND step_fee >= 0 AND min_days >= 0 AND max_days >= min_days),
 FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS restock_plans (
 plan_id VARCHAR(64) PRIMARY KEY, sku_id VARCHAR(64) NOT NULL, warehouse_id VARCHAR(64) NOT NULL,
 expected_received_at DATETIME(6), expected_available_at DATETIME(6), quantity INT,
 status VARCHAR(24) NOT NULL, source VARCHAR(128) NOT NULL, updated_at DATETIME(6) NOT NULL,
 FOREIGN KEY(sku_id) REFERENCES product_skus(sku_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS membership_levels (
 platform_code VARCHAR(16) NOT NULL, level_code VARCHAR(32) NOT NULL, version INT NOT NULL,
 name VARCHAR(64) NOT NULL, benefits JSON NOT NULL, source VARCHAR(128) NOT NULL,
 PRIMARY KEY(platform_code,level_code,version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS memberships (
 user_id VARCHAR(64) NOT NULL, platform_code VARCHAR(16) NOT NULL, level_code VARCHAR(32) NOT NULL,
 level_version INT NOT NULL, starts_at DATETIME(6) NOT NULL, expires_at DATETIME(6) NOT NULL,
 status VARCHAR(16) NOT NULL, updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY(user_id,platform_code), FOREIGN KEY(user_id) REFERENCES users(user_id),
 FOREIGN KEY(platform_code,level_code,level_version) REFERENCES membership_levels(platform_code,level_code,version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS points_ledger (
 entry_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, platform_code VARCHAR(16) NOT NULL,
 delta BIGINT NOT NULL, kind VARCHAR(32) NOT NULL, source VARCHAR(128) NOT NULL,
 reverses_entry_id VARCHAR(64), expires_at DATETIME(6), created_at DATETIME(6) NOT NULL,
 UNIQUE KEY uq_points_source(user_id,platform_code,source),
 FOREIGN KEY(user_id) REFERENCES users(user_id), FOREIGN KEY(reverses_entry_id) REFERENCES points_ledger(entry_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS return_authorizations (
 authorization_id VARCHAR(64) PRIMARY KEY, after_sale_id VARCHAR(64) NOT NULL UNIQUE,
 address_ciphertext BLOB NOT NULL, key_version SMALLINT UNSIGNED NOT NULL,
 carriers JSON NOT NULL, requirements JSON NOT NULL, deadline DATETIME(6) NOT NULL,
 status VARCHAR(24) NOT NULL, source VARCHAR(128) NOT NULL, updated_at DATETIME(6) NOT NULL,
 FOREIGN KEY(after_sale_id) REFERENCES after_sale_requests(after_sale_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS return_shipments (
 return_shipment_id VARCHAR(64) PRIMARY KEY, authorization_id VARCHAR(64) NOT NULL UNIQUE,
 carrier_code VARCHAR(32) NOT NULL, tracking_number VARCHAR(64) NOT NULL,
 status VARCHAR(24) NOT NULL, operation_id VARCHAR(128) NOT NULL UNIQUE,
 created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
 UNIQUE KEY uq_return_tracking(carrier_code,tracking_number),
 FOREIGN KEY(authorization_id) REFERENCES return_authorizations(authorization_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS return_shipment_events (
 event_id VARCHAR(64) PRIMARY KEY, return_shipment_id VARCHAR(64) NOT NULL,
 status VARCHAR(24) NOT NULL, description VARCHAR(512), source VARCHAR(128) NOT NULL,
 event_time DATETIME(6) NOT NULL, FOREIGN KEY(return_shipment_id) REFERENCES return_shipments(return_shipment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS service_work_orders (
 work_order_id VARCHAR(64) PRIMARY KEY, after_sale_id VARCHAR(64) NOT NULL UNIQUE,
 kind VARCHAR(32) NOT NULL, status VARCHAR(24) NOT NULL, details JSON NOT NULL,
 provider_reference VARCHAR(128), source VARCHAR(128) NOT NULL,
 updated_at DATETIME(6) NOT NULL, FOREIGN KEY(after_sale_id) REFERENCES after_sale_requests(after_sale_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS order_item_benefits (
 order_item_id VARCHAR(64) PRIMARY KEY, price_protection_until DATETIME(6),
 price_basis VARCHAR(32), eligible BOOLEAN NOT NULL, source VARCHAR(128) NOT NULL,
 FOREIGN KEY(order_item_id) REFERENCES order_items(order_item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS service_inventory_reservations (
 work_order_id VARCHAR(64) PRIMARY KEY, sku_id VARCHAR(64) NOT NULL,
 quantity INT NOT NULL, status VARCHAR(16) NOT NULL, updated_at DATETIME(6) NOT NULL,
 CHECK(quantity > 0), FOREIGN KEY(work_order_id) REFERENCES service_work_orders(work_order_id),
 FOREIGN KEY(sku_id) REFERENCES product_skus(sku_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS price_protection_prices (
 price_id VARCHAR(64) PRIMARY KEY, sku_id VARCHAR(64) NOT NULL, platform_code VARCHAR(16) NOT NULL,
 currency CHAR(3) NOT NULL, amount DECIMAL(12,2) NOT NULL, price_basis VARCHAR(32) NOT NULL,
 starts_at DATETIME(6) NOT NULL, ends_at DATETIME(6) NOT NULL, source VARCHAR(128) NOT NULL,
 CHECK(amount >= 0), FOREIGN KEY(sku_id) REFERENCES product_skus(sku_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS issued_invoices (
 issued_invoice_id VARCHAR(64) PRIMARY KEY, invoice_id VARCHAR(64) NOT NULL,
 invoice_number VARCHAR(128) NOT NULL UNIQUE, status VARCHAR(24) NOT NULL,
 document_key VARCHAR(255), mime_type VARCHAR(64), sha256 CHAR(64),
 provider VARCHAR(64) NOT NULL, issued_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
 FOREIGN KEY(invoice_id) REFERENCES invoice_requests(invoice_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS invoice_corrections (
 correction_id VARCHAR(64) PRIMARY KEY, issued_invoice_id VARCHAR(64) NOT NULL,
 invoice_id VARCHAR(64) NOT NULL, status VARCHAR(24) NOT NULL,
 operation_id VARCHAR(128) NOT NULL UNIQUE, updated_at DATETIME(6) NOT NULL,
 FOREIGN KEY(issued_invoice_id) REFERENCES issued_invoices(issued_invoice_id),
 FOREIGN KEY(invoice_id) REFERENCES invoice_requests(invoice_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS checkout_quotes (
 quote_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, platform_code VARCHAR(16) NOT NULL,
 order_id VARCHAR(40) NOT NULL, snapshot JSON NOT NULL, snapshot_digest CHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL, created_at DATETIME(6) NOT NULL, expires_at DATETIME(6) NOT NULL,
 UNIQUE KEY uq_quote_order(platform_code,order_id), FOREIGN KEY(user_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS order_expirations (
 platform_code VARCHAR(16) NOT NULL, order_id VARCHAR(40) NOT NULL,
 expires_at DATETIME(6) NOT NULL, status VARCHAR(16) NOT NULL,
 PRIMARY KEY(platform_code,order_id), KEY idx_order_expiry(status,expires_at), FOREIGN KEY(platform_code,order_id) REFERENCES orders(platform_code,order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS commerce_provider_requests (
 request_id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
 platform_code VARCHAR(16) NOT NULL, order_id VARCHAR(40) NOT NULL,
 kind VARCHAR(32) NOT NULL, target_id VARCHAR(64) NOT NULL,
 payload_ciphertext MEDIUMBLOB NOT NULL, key_version SMALLINT UNSIGNED NOT NULL,
 status VARCHAR(24) NOT NULL, provider_reference VARCHAR(128), result JSON,
 attempts INT NOT NULL DEFAULT 0, lease_until DATETIME(6), lease_token VARCHAR(64),
 created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
 KEY idx_provider_pending(status,updated_at),
 FOREIGN KEY(platform_code,order_id,user_id) REFERENCES orders(platform_code,order_id,user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
