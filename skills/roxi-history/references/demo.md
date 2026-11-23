# Synthetic West Annex test

From the Roxi checkout, set up PostgreSQL, run `python -m review_app migrate`, and run `python tools/seed_history.py --output .local/history-replay.json`. Start the Roxi app and add its stdio MCP server to the agent client using `python -m review_app.mcp_server` from the same checkout. Set `EXERCISE_REVIEW_API_EXERCISES=west-annex-history` for the demo.

Give the agent the `exercise_id`, `as_of`, and `known_at.after_late` values from the receipt. Ask “Which reports disagree?” The answer should identify the conflict between Receiving Dock chat and the fleet report for the West Annex delivery van. Each original citation should include its exact message version or JSON pointer.

For “What changed?”, use `known_at.before_late` as the baseline and keep both other cutoffs. The later knowledge snapshot includes a late fleet observation at 15:03 UTC.
