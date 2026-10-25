#!/bin/sh
# The platform connects as an ordinary role that owns its database: never a superuser, never BYPASSRLS, so row-level
# security isolates organizations. The api refuses to start as a role that would bypass it.
set -eu
psql -v ON_ERROR_STOP=1 --username postgres <<SQL
CREATE ROLE harnessgateway LOGIN PASSWORD '${HG_DB_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE DATABASE harnessgateway OWNER harnessgateway;
SQL
