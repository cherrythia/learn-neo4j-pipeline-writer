#!/bin/bash
# Runs once on first MySQL start (docker-entrypoint-initdb.d). Passwords come from the
# environment (see docker-compose.yml / .env); the script fails if they are missing.
set -euo pipefail

mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE DATABASE IF NOT EXISTS governance;
CREATE DATABASE IF NOT EXISTS demo_compliance;
-- Scratch database for integration tests, so they never touch real governance data.
CREATE DATABASE IF NOT EXISTS governance_test;

-- writer: owns the governance store. reader: read-only on the demo source data.
CREATE USER IF NOT EXISTS 'writer'@'%' IDENTIFIED BY '${MYSQL_WRITER_PASSWORD:?MYSQL_WRITER_PASSWORD is required}';
CREATE USER IF NOT EXISTS 'reader'@'%' IDENTIFIED BY '${MYSQL_READER_PASSWORD:?MYSQL_READER_PASSWORD is required}';
GRANT ALL PRIVILEGES ON governance.* TO 'writer'@'%';
GRANT ALL PRIVILEGES ON governance_test.* TO 'writer'@'%';
GRANT SELECT ON demo_compliance.* TO 'reader'@'%';
FLUSH PRIVILEGES;
SQL
