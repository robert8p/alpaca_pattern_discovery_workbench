# Alpaca Pattern Discovery Workbench 2.1.1 — Release Audit

## Scope

Version 2.1.1 fixes the Candidate Analysis Export download transport. The ZIP payload, discovery engine, feature definitions, database schema, completed discovery results and sealed-test methodology are unchanged.

## Root cause

Version 2.1.0 used an asynchronous browser sequence: `fetch()` → `response.blob()` → `URL.createObjectURL()` → synthetic `a.click()`. Some browsers can block the final synthetic download because the original user activation is no longer considered active after the awaited network operation. This produces the observed symptom: the control can appear to do nothing even when the export endpoint itself is healthy.

## Correction

The Candidate controls are now a native HTML GET form targeting `/api/candidates/export`.

- `discovery_run_id` is a named GET field.
- `status_filter` is a named GET field.
- **Download analysis export** is a normal submit button.
- The server returns the ZIP using `Content-Disposition: attachment`.
- There is no `fetch()`, Blob, object URL, or synthetic click in the download path.

This preserves the browser's original user gesture and delegates file download to the browser's native attachment handling.

## Regression controls

The release suite verifies that:

- the Candidate export control is a native GET form;
- both Candidate filters are submitted to the export endpoint;
- production JavaScript contains no `downloadCandidateExport`, `response.blob()` or `URL.createObjectURL` download path;
- the actual FastAPI export handler returns HTTP 200, `application/zip`, `Content-Disposition: attachment` and a readable ZIP package;
- the ZIP contains the candidate leaderboard, frozen rule definitions, discovery-run metadata, discovery tasks, feature-set metadata, universe metadata, included symbols, summary and analysis prompt;
- the export endpoint is read-only;
- application, worker and migration version markers agree.

## Release gate

Executed against the exact source tree packaged for release:

- **53 automated tests passed**
- **1 PostgreSQL integration test skipped** because this assembly environment has no PostgreSQL server
- Python compilation passed
- JavaScript syntax validation passed
- 220 literal SQL statements inspected
- 173 statically countable SQL bindings checked
- 5 `ON CONFLICT ... DO UPDATE` statements checked for explicit conflict targets
- 196 generated discovery-query combinations validated
- Render Blueprint / Python 3.12.7 pin validated
- Raw `rd_` write-protection scan passed
- Candidate export server-response regression passed
- Candidate export browser-transport regression passed

The skipped PostgreSQL test is not material to this browser transport change; the repository retains the PostgreSQL CI workflow for database-backed release validation.

## Migration / rerun impact

None.

- No schema migration is required.
- No feature set needs rebuilding.
- No discovery scan needs rerunning.
- Existing candidates are exported immediately after the web service is upgraded.
