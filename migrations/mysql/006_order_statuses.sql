-- Executed through the guarded migration runner for retry safety.
ALTER TABLE orders ADD COLUMN status_before_after_sale VARCHAR(32) NULL;
UPDATE orders SET status='received' WHERE status='completed';
UPDATE orders SET status_before_after_sale='received' WHERE status_before_after_sale='completed';
UPDATE orders o SET status_before_after_sale=o.status,status='after_sale'
 WHERE o.status<>'after_sale' AND EXISTS (
 SELECT 1 FROM after_sale_requests a WHERE a.platform_code=o.platform_code AND a.order_id=o.order_id
 AND a.status IN ('pending_review','approved','processing'));
ALTER TABLE orders ADD CONSTRAINT chk_orders_status CHECK
 (status IN ('pending_payment','paid','fulfilling','shipped','received','after_sale','cancelled','closed'));
ALTER TABLE orders ADD CONSTRAINT chk_orders_after_sale_base CHECK
 ((status='after_sale' AND status_before_after_sale IS NOT NULL AND status_before_after_sale IN
 ('pending_payment','paid','fulfilling','shipped','received','cancelled','closed'))
 OR (status<>'after_sale' AND status_before_after_sale IS NULL));
