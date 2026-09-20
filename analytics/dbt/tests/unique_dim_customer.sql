select _dataset_id, `CUSTOMER_ID`, count() as rows
from {{ ref('stg_dim_customer') }}
group by _dataset_id, `CUSTOMER_ID` having count() > 1
