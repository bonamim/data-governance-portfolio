MODEL (
  name sqlmesh_customer_experience_bronze.brz_reviews,
  kind VIEW,
  description 'Bronze: typed reviews from raw (internal).'
);

SELECT
  review_id,
  order_id,
  review_score::INT AS review_score,
  NULLIF(review_comment_title, '') AS review_comment_title,
  NULLIF(review_comment_message, '') AS review_comment_message,
  NULLIF(review_creation_date, '')::TIMESTAMP AS review_creation_date,
  NULLIF(review_answer_timestamp, '')::TIMESTAMP AS review_answer_timestamp
FROM customer_experience_raw.reviews
