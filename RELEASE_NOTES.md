# Release notes

## 1.0.1 — Nullable universe-regex fix

- Fixes universe-build failure `could not determine data type of parameter $18` when **Include symbol regex** is left blank.
- Explicitly casts both optional regex parameters to PostgreSQL `text`.
- Existing failed universe jobs can be retried after deployment; no database migration is required.

## 1.0.0

Initial release of the Alpaca Pattern Discovery Workbench.

- Button-driven data-quality reports
- Liquidity-ranked and frozen research universes
- Date-chunked, resumable intraday feature generation
- Interpretable discovery families
- Discovery and validation metrics stored separately
- Explicit candidate shortlisting and rejection
- Separate sealed-evaluation jobs
- Family/direction/horizon discovery checkpoints
- Raw `rd_` tables protected by application-level read-only policy
- Render Blueprint with Python 3.12.7 pinned
