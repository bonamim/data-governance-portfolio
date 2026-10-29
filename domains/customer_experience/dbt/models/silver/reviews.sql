{{ config(access='public') }}
-- OUTPUT PORT: customer_experience.reviews, tagged `output_port` in its ODCS
-- contract. A deliberate publish from Silver (tag synced to dbt from the contract).
-- Free-text comments are not published (PII), and consumers get a has_comment
-- flag plus the score and timing. _system_loaded_at is the platform's, passed
-- through, and _internal_row_hash this product's own (ADR-0028): a hash of the
-- published values only, so it is never a fingerprint of the comment.
select
    review_id,
    order_id,
    review_score,
    (review_comment_message is not null) as has_comment,
    review_creation_date,
    review_answer_timestamp,
    _system_loaded_at,
    md5(concat_ws('|',
        review_id, order_id, review_score, review_comment_message is not null,
        review_creation_date, review_answer_timestamp
    )) as _internal_row_hash
from {{ ref('brz_reviews') }}
