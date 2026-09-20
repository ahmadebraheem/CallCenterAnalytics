select _dataset_id, `OUTCOME_ID`, count() as rows
from {{ ref('stg_interaction_outcome_fact') }}
group by _dataset_id, `OUTCOME_ID` having count() > 1
