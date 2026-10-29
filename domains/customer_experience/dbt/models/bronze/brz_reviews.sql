-- Bronze: standardized reviews from raw (typed). Internal, keeps free-text comments.
select
    review_id,
    order_id,
    review_score::int                              as review_score,
    nullif(review_comment_title, '')               as review_comment_title,
    nullif(review_comment_message, '')             as review_comment_message,
    nullif(review_creation_date, '')::timestamp    as review_creation_date,
    nullif(review_answer_timestamp, '')::timestamp as review_answer_timestamp,
    _system_batch_id,
    _system_loaded_at
from {{ source('customer_experience_raw', 'reviews') }}
