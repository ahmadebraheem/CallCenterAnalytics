select _dataset_id, `AGENT_ID`, count() as rows
from {{ ref('stg_dim_agent') }}
group by _dataset_id, `AGENT_ID` having count() > 1
