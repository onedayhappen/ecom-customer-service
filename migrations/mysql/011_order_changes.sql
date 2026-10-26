CREATE TABLE IF NOT EXISTS order_edit_details (
 platform_code VARCHAR(16) NOT NULL, order_id VARCHAR(40) NOT NULL,
 details_ciphertext MEDIUMBLOB NOT NULL, key_version SMALLINT UNSIGNED NOT NULL,
 updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY(platform_code,order_id),
 FOREIGN KEY(platform_code,order_id) REFERENCES orders(platform_code,order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
