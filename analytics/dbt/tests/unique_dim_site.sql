select _dataset_id, `SITE`, count() as rows
from {{ ref('stg_dim_site') }}
group by _dataset_id, `SITE` having count() > 1
