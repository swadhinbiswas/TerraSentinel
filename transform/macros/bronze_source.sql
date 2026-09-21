{#-
  Reads the bronze lake directly, with no download or staging step.

  Glob selection has a history, and each step was forced by real data:

  1. **Two explicit globs** — `<root>/<source>/**` plus `<root>/backfill/<source>/**`.
     Correct while both layouts exist, fatal the moment one does not: right after a
     backfill there is no `<source>/` directory, so the live glob 404s and the model
     dies with rows sitting right there under `backfill/`.

  2. **One leading globstar** — `<root>/**/<source>/**`. Works over `hf://`, but
     DuckDB's *local* glob rejects it ("Cannot use multiple '**' in one path"), so
     the same SQL stopped working against a local lake.

  So the listing is now done once, outside dbt, by `tools/bronze_manifest.py`, which
  receives only globs known to match through `--vars '{"bronze_globs": {...}}'`.
  A source with no files becomes a clear compiler error naming the fix, instead of a
  filesystem error several layers down.

  When `bronze_globs` is absent (local development) the two-glob form is used, which
  is safe there because the synthetic generator always writes both layouts.

  3. **Hive partitioning is OFF.** The two layouts carry different partition keys
     (only live paths have `day=`), and DuckDB refuses to read mismatched keys in one
     call. Nothing is lost: `region_id` is a real column on every row, so parsing it
     out of the path was redundant — which also decouples these models from the
     physical layout entirely.

  4. **`union_by_name = 1`** because sources gain columns over time (a new FIRMS
     product adds a band, a new NSIDC file adds a field) and older partitions will
     not have them.

  Reading over `hf://` needs an authenticated DuckDB secret or it goes out
  anonymously against the Hub's public rate limit; `tools/configure_duckdb_secret`
  creates it before dbt runs.

  Usage:  select * from {{ bronze_parquet('firms') }}
-#}

{% macro bronze_root() %}
  {%- set configured = var('bronze_root', none) -%}
  {%- if configured -%}
    {{ return(configured) }}
  {%- endif -%}
  {{ return('hf://datasets/' ~ env_var('HF_BRONZE_REPO')) }}
{% endmacro %}

{% macro bronze_parquet(source_id) %}
  {%- set root = terrasentinel.bronze_root() -%}
  {%- set manifest = var('bronze_globs', none) -%}

  {%- if manifest is not none -%}
    {%- set globs = manifest.get(source_id, []) -%}
    {%- if globs | length == 0 -%}
      {#- Deliberately a *runtime* error, not raise_compiler_error: the compiler
          error fires at parse time, so one absent source would abort every
          `--select`, including selections that do not touch it. A bare
          self-describing relation name confines the failure to this model and its
          descendants, and the resulting "table does not exist" states the fix. -#}
      bronze_missing_for__{{ source_id }}__run_backfill_historical_then_rerun
    {%- else -%}
      read_parquet({{ globs | tojson }}, hive_partitioning = 0, union_by_name = 1)
    {%- endif -%}
  {%- else -%}
    read_parquet(
      ['{{ root }}/{{ source_id }}/**/*.parquet', '{{ root }}/backfill/{{ source_id }}/**/*.parquet'],
      hive_partitioning = 0,
      union_by_name = 1
    )
  {%- endif -%}
{% endmacro %}

{#- Source freshness budget in days, from project vars. -#}
{% macro freshness_days(source_id) %}
  {{ return(var(source_id ~ '_freshness_days', 7) | int) }}
{% endmacro %}

{% macro generate_schema_name(custom_schema_name, node) -%}
  {#- Schemas land as `staging`, `intermediate`, `gold` rather than `main_staging`. -#}
  {%- if custom_schema_name is none -%}
    {{ target.schema }}
  {%- else -%}
    {{ custom_schema_name | trim }}
  {%- endif -%}
{%- endmacro %}
