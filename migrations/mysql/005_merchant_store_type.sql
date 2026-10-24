-- Store type is independent of merchandise category; it is not proof of authorization.
ALTER TABLE merchants ADD COLUMN store_type VARCHAR(24) NOT NULL DEFAULT 'general'
    COMMENT 'flagship, exclusive, specialty, general';
