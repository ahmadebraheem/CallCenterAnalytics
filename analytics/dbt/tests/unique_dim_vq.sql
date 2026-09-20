select _dataset_id, `VQ_ID`, count() as rows
from {{ ref('stg_dim_vq') }}
group by _dataset_id, `VQ_ID` having count() > 1
