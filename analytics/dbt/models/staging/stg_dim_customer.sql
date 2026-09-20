-- Only publish complete, reconciled dataset attempts.
select *
from {{ source('gim_raw', 'dim_customer') }}
where _load_id in (select load_id from {{ source('gim_raw', '_dataset_loads') }})
