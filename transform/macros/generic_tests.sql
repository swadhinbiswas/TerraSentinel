{% test column_between(model, column_name, min_value, max_value) %}
{#- Range check as a generic test, so every model can assert physical bounds
    without pulling in dbt_utils. -#}
select *
from {{ model }}
where {{ column_name }} is not null
  and ({{ column_name }} < {{ min_value }} or {{ column_name }} > {{ max_value }})
{% endtest %}

{% test not_older_than(model, column_name, source_id, days) %}
{#- Freshness gate. Implemented as a plain test rather than `source freshness`
    because the lake is files on the Hub, not a table with a load timestamp —
    there is no `loaded_at_field` to point at. Returns a row (and therefore
    fails) when the newest observation is staler than the budget.

    The budget is overridable per source via a project var, so an operator can
    loosen it during a known upstream outage without editing the test. -#}

{%- set tolerance = var(source_id ~ '_freshness_days', days) -%}

select max({{ column_name }}) as latest_observation
from {{ model }}
having max({{ column_name }}) < current_date - interval '{{ tolerance }} days'

{% endtest %}

{% test unique_combination_of_columns(model, combination_of_columns) %}
{#- Composite key check. -#}
select
    {{ combination_of_columns | join(', ') }},
    count(*) as row_count
from {{ model }}
group by {{ combination_of_columns | join(', ') }}
having count(*) > 1
{% endtest %}

{% test not_empty(model) %}
{#- A mart that materialises zero rows is usually a silent upstream breakage
    rather than a genuinely quiet period. -#}
select 1
from {{ model }}
having count(*) = 0
{% endtest %}
