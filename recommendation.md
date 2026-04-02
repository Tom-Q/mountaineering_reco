# Recommendation Prompt

This file contains the prompt template used to generate route recommendations.
It is versioned here separately from application code so that prompt iterations
are tracked independently.

## Template

(to be developed)

## Design notes

- The LLM is NOT responsible for grade filtering. That is handled deterministically in `src/grades.py`.
- The LLM's job: given a shortlist of routes that are within the user's limits, assess which are
  in good shape *right now* based on conditions reports and weather.
- Explicitly instruct the model to flag uncertainty when conditions reports are stale (> 2 weeks)
  or contradictory.
- Inject the user's history in structured form, not as a prose summary.
