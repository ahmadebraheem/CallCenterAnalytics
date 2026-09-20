select _dataset_id, `IRF_ID`, count() as rows
from {{ ref('stg_interaction_resource_fact') }}
group by _dataset_id, `IRF_ID` having count() > 1
