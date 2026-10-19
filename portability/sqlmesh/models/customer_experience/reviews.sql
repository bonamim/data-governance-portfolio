MODEL (
  name sqlmesh_customer_experience_silver.reviews,
  kind VIEW,
  description 'OUTPUT PORT customer_experience.reviews — same contract as the dbt build.',
  audits (
    -- generated from domains/customer_experience/contracts/reviews.odcs.yaml (sync_contracts.py)
    not_null(columns := (review_id, order_id, review_score, has_comment, review_creation_date)),
    accepted_range(column := review_score, min_v := 1, max_v := 5),
    number_of_rows(threshold := 1000)
  )
);

SELECT
  review_id,
  order_id,
  review_score,
  (review_comment_message IS NOT NULL) AS has_comment,
  review_creation_date,
  review_answer_timestamp
FROM sqlmesh_customer_experience_bronze.brz_reviews
