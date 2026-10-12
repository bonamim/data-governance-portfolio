-- Output port: customer_experience.reviews — the published Reviews data product.
-- Free-text comments are NOT published (they can carry PII); consumers get only a
-- has_comment flag plus the score and timing. Consumed by Sales (sales_performance).
{{ config(access='public') }}

select
    review_id,
    order_id,
    review_score,
    (review_comment_message is not null) as has_comment,
    review_creation_date,
    review_answer_timestamp
from {{ ref('stg_reviews') }}
