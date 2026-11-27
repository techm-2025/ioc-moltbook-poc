#!/bin/bash
# Creates the CFN databases on first Postgres start.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE cfn_mgmt'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cfn_mgmt')\gexec

    SELECT 'CREATE DATABASE cfn_cp'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cfn_cp')\gexec
EOSQL
