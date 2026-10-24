ALTER TABLE orders DROP CHECK chk_orders_status;
ALTER TABLE orders DROP CHECK chk_orders_after_sale_base;
UPDATE orders SET status='pending_receipt' WHERE status IN ('fulfilling','shipped');
UPDATE orders SET status_before_after_sale='pending_receipt' WHERE status_before_after_sale IN ('fulfilling','shipped');
ALTER TABLE orders ADD CONSTRAINT chk_orders_status CHECK
 (status IN ('pending_payment','paid','pending_receipt','received','after_sale','cancelled','closed'));
ALTER TABLE orders ADD CONSTRAINT chk_orders_after_sale_base CHECK
 ((status='after_sale' AND status_before_after_sale IS NOT NULL AND status_before_after_sale IN
 ('pending_payment','paid','pending_receipt','received','cancelled','closed'))
 OR (status<>'after_sale' AND status_before_after_sale IS NULL));
