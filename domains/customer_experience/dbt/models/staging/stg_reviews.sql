-- Typed reviews (engineering layer, internal). Keeps the free-text comments so the
-- domain can use them internally; the output port does not publish them.
with source as (
    select * from {{ source('customer_experience_raw', 'reviews') }}
),

typed as (
    select
        review_id,
        order_id,
        review_score::int                               as review_score,
        nullif(review_comment_title, '')                as review_comment_title,
        nullif(review_comment_message, '')              as review_comment_message,
        nullif(review_creation_date, '')::timestamp     as review_creation_date,
        nullif(review_answer_timestamp, '')::timestamp  as review_answer_timestamp
    from source
)

select * from typed
