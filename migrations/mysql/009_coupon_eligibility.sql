ALTER TABLE users ADD COLUMN newcomer_coupon_eligible BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE coupon_templates ADD COLUMN coupon_type VARCHAR(16) NOT NULL DEFAULT 'general';
ALTER TABLE coupon_templates ADD CONSTRAINT chk_coupon_business_type CHECK (coupon_type IN ('general','newcomer','shop','platform','category'));
ALTER TABLE promotion_rules ADD COLUMN allows_coupon BOOLEAN NOT NULL DEFAULT FALSE;
