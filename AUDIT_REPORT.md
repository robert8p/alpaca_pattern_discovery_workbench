# v1.1.0 release audit report

## Scope

The audit was initiated after v1.0.7 failed on an unescaped percent sign inside parameterised SQL. The defect was treated as a test-system failure, not an isolated line defect.

## Defects found and corrected

1. Literal `%` characters in generated discovery labels were parsed as invalid Psycopg placeholders.
2. SQL tests checked fragments without emulating the database driver's placeholder grammar.
3. Numeric bucket boundaries were not frozen explicitly enough for exact replay.
4. Null predictor values could fall into a terminal category.
5. Sealed evaluation did not persist and enforce all discovery sampling metadata.
6. Discovery and sealed statistics did not apply all cost-adjusted metrics identically.
7. Requested horizons were not checked against the feature set's generated outcomes.
8. Rule families could be run against sessions for which their time buckets were not calibrated.
9. Some date predicates inhibited partition pruning.
10. Dashboard deletion could remove upstream assets without a clear dependency decision.

## Release-gate coverage

### Generated SQL

The local preflight generates and validates:

- 6 rule families
- 2 directions
- 4 holding horizons
- 2 sampling modes
- grouped discovery/validation SQL
- exact validation/sealed SQL
- feature-build SQL
- universe-build SQL

Total generated-query checks: **194**.

Definition hash in the assembled release:

```text
0290db620e4e1983c4cb407a35ea78f5b0c49c2adc7d68bf2875523d7161ceef
```

### Static and unit results in the assembly environment

```text
38 passed
1 PostgreSQL integration test skipped because no local PostgreSQL server/driver was available
Python compilation passed
JavaScript syntax passed
158 literal SQL statements passed placeholder-grammar inspection
194 generated queries passed binding validation
Raw rd_ write-protection scan passed
Secret scan passed
```

### Database-backed CI included in the repository

`.github/workflows/ci.yml` starts PostgreSQL 16 and runs the skipped integration test with real Psycopg. The test:

1. Creates synthetic `rd_assets` and `rd_bars`.
2. Installs the workbench schema.
3. Builds a liquidity-ranked universe.
4. Generates a complete feature set.
5. Asks PostgreSQL to plan all 194 generated production queries.
6. Runs discovery.
7. Verifies frozen sampling/rule metadata.
8. Runs sealed evaluation.

A commit should not be deployed unless this GitHub Action is green.

## Deployment preflight

The deployed app also performs PostgreSQL planning against the actual Supabase schema. New discovery and sealed-test jobs are rejected when preflight fails.

## Residual limitations

- Assembly-environment tests cannot reproduce the performance characteristics of the user's Supabase compute tier.
- Statistical discovery still carries multiple-testing and survivorship risks.
- The current candidate metrics are event-level, not a portfolio-capital simulation.
- Application-level raw-table protection is stronger with a dedicated database role.
