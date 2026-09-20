select _dataset_id, `LOB`, count() as rows
from {{ ref('stg_dim_lob') }}
group by _dataset_id, `LOB` having count() > 1
