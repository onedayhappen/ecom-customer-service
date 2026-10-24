CREATE TABLE IF NOT EXISTS return_shipments (
 return_shipment_id VARCHAR(64) PRIMARY KEY, authorization_id VARCHAR(64) NOT NULL,
 carrier_code VARCHAR(32) NOT NULL, tracking_number VARCHAR(64) NOT NULL,
 status VARCHAR(24) NOT NULL, operation_id VARCHAR(128) NOT NULL UNIQUE,
 created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
 UNIQUE KEY uq_return_tracking(carrier_code,tracking_number),
 KEY idx_return_authorization(authorization_id),
 FOREIGN KEY(authorization_id) REFERENCES return_authorizations(authorization_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS return_shipment_events (
 event_id VARCHAR(64) PRIMARY KEY, return_shipment_id VARCHAR(64) NOT NULL,
 status VARCHAR(24) NOT NULL, description VARCHAR(512), source VARCHAR(128) NOT NULL,
 event_time DATETIME(6) NOT NULL,
 FOREIGN KEY(return_shipment_id) REFERENCES return_shipments(return_shipment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS return_shipment_items (
 return_shipment_id VARCHAR(64) NOT NULL, after_sale_id VARCHAR(64) NOT NULL,
 order_item_id VARCHAR(64) NOT NULL, quantity INT NOT NULL,
 PRIMARY KEY(return_shipment_id,order_item_id), CHECK(quantity > 0),
 FOREIGN KEY(return_shipment_id) REFERENCES return_shipments(return_shipment_id),
 FOREIGN KEY(after_sale_id,order_item_id) REFERENCES after_sale_items(after_sale_id,order_item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
ALTER TABLE after_sale_requests ADD COLUMN stage VARCHAR(32) NULL;
ALTER TABLE after_sale_requests ADD COLUMN completed_at DATETIME(6) NULL;
ALTER TABLE shipments ADD COLUMN shipment_kind VARCHAR(24) NOT NULL DEFAULT 'original';
ALTER TABLE shipments ADD COLUMN after_sale_id VARCHAR(64) NULL;
ALTER TABLE shipments ADD COLUMN receipt_confirmed_at DATETIME(6) NULL;
ALTER TABLE shipment_items ADD COLUMN actual_sku_id VARCHAR(64) NULL;
ALTER TABLE shipment_items ADD COLUMN actual_spec_snapshot JSON NULL;
ALTER TABLE shipments ADD CONSTRAINT fk_shipments_after_sale FOREIGN KEY(after_sale_id) REFERENCES after_sale_requests(after_sale_id);
ALTER TABLE shipment_items ADD CONSTRAINT fk_shipment_actual_sku FOREIGN KEY(actual_sku_id) REFERENCES product_skus(sku_id);
ALTER TABLE shipments ADD CONSTRAINT chk_shipment_kind CHECK(shipment_kind IN ('original','exchange'));
ALTER TABLE shipments ADD CONSTRAINT chk_exchange_sale CHECK(shipment_kind <> 'exchange' OR after_sale_id IS NOT NULL);
UPDATE after_sale_requests SET stage=CASE WHEN status='pending_review' THEN 'awaiting_review' WHEN status IN ('completed','cancelled','rejected','closed') THEN status ELSE NULL END WHERE stage IS NULL;
UPDATE shipment_items si JOIN shipments s ON s.shipment_id=si.shipment_id JOIN order_items i ON i.order_item_id=si.order_item_id
 SET si.actual_sku_id=i.sku_id,si.actual_spec_snapshot=i.spec_snapshot
 WHERE s.shipment_kind='original' AND si.actual_sku_id IS NULL;
