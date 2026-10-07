# EFTFit

EFTFit contains the custom Combine interfaces used to construct EFT
workspaces, run likelihood scans, retrieve identity-bound results, and consume
those results in plotting tools.

For a new checkout, start with the [setup guide](docs/how_to/setup.md) and then
use the [documentation index](docs/README.md). The maintained current recipe is
the [configured 1D workflow](docs/how_to/run_configured_1d.md).

## Current status

- Configured profiled and frozen 1D scans are implemented. They require an
  analysis-supplied workspace and fit configuration.
- Maintained 1D Condor execution, bounded recovery, identity-bearing results,
  and fail-closed result selection are implemented.
- The [1D result consumer](docs/how_to/run_configured_1d.md#structured-1d-results-and-the-plotting-api)
  validates ROOT results, preserves plotting APIs, and represents disconnected
  and open intervals explicitly. Package-configured TOP-26-006 WC ranges are
  scientific authority; historical hardcoded plotter ranges are retired.
  Thresholds 1 and 4 (legacy 1σ/2σ labels) are asymptotic references until Wilks
  validation. Coefficient display scales are presentation-only.
- Configured 2D scans and configured impacts are design-closed but not yet
  implemented.
- The self-contained portable fit package is designed but has not been
  materialized or qualified.
- TOP-26-006 numerical WC ranges are read from the qualified package config;
  this repository's documentation intentionally does not repeat them. Final
  interval precision and coverage require separate campaign qualification.

Older TOP-19-001, TOP-22-006, AAC, CRAB-first, hard-coded impacts, and postfit
material is indexed under [historical documentation](docs/historical/README.md).
It is not the recommended TOP-26-006 workflow.

The modern 1D consumer excludes producer-invalid points from all physical
likelihood calculations. Its `--failed-point-policy {mask,drop,error}` setting
controls presentation while preserving gaps and recovery provenance. Intervals
are structured; the historical lossy interval list adapter is retired. See the
[failed-point contract](docs/how_to/run_configured_1d.md#failed-evaluation-points-and-recovery).
