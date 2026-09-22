-- One-time Unity Catalog provisioning for TerraSentinel.
--
-- Run ONCE per workspace as an admin (SQL editor / Databricks SQL), before the
-- first `databricks bundle deploy`. Every statement is idempotent, so re-running
-- is safe. The gold TABLES are deliberately not created here: they come from
-- the generated DDL/MERGE (databricks/sql/gold_ddl.sql), executed idempotently
-- by the merge_gold task on every transform run.
--
-- Why a hand-run file instead of bundle-declared UC resources (ADR-2 in
-- databricks/README.md): the catalog needs admin rights the deployer may not
-- have, development mode prefixes resource names (a dev_ schema would not match
-- the paths the jobs write to), and pre-existing objects make declarative
-- creates brittle across targets. SQL with IF NOT EXISTS is honest about all
-- three.

CREATE CATALOG IF NOT EXISTS terrasentinel
  COMMENT 'TerraSentinel climate & satellite anomaly monitoring';

CREATE SCHEMA IF NOT EXISTS terrasentinel.gold
  COMMENT 'gold marts published from DuckDB by the transform job';

-- Shared volume: run_dbt writes the DuckDB gold file here, merge_gold reads it,
-- ML artifacts hand off through it within one job run.
CREATE VOLUME IF NOT EXISTS terrasentinel.gold.pipeline
  COMMENT 'pipeline storage: DuckDB gold file, ML artifacts';

-- Least privilege for the bundle's run-as principal (adjust the group):
--   GRANT USAGE ON CATALOG terrasentinel TO `terrasentinel-runners`;
--   GRANT USAGE, CREATE TABLE, MODIFY ON SCHEMA terrasentinel.gold TO `terrasentinel-runners`;
--   GRANT READ_VOLUME, WRITE_VOLUME ON VOLUME terrasentinel.gold.pipeline TO `terrasentinel-runners`;
-- Readers (dashboard / Databricks SQL):
--   GRANT SELECT ON SCHEMA terrasentinel.gold TO `terrasentinel-readers`;
