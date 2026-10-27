{{ config(access='public') }}
-- OUTPUT PORT: customer_experience.reviews, tagged `output_port` in its ODCS
-- contract. A deliberate publish from Silver (tag synced to dbt from the contract).
-- Free-text comments are not published (PII), and consumers get a has_comment
-- flag plus the score and timing.
select
    review_id,
    order_id,
    review_score,
    (review_comment_message is not null) as has_comment,
    review_creation_date,
    review_answer_timestamp
from {{ ref('brz_reviews') }}
