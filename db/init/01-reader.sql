CREATE ROLE review_reader LOGIN PASSWORD 'review-local-reader';
GRANT CONNECT ON DATABASE exercise_review TO review_reader;
