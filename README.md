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
- Configured 2D scans and configured impacts are design-closed but not yet
  implemented.
- The self-contained portable fit package is designed but has not been
  materialized or qualified.
- Final TOP-26-006 numerical WC ranges still require correction and freeze;
  this repository's documentation intentionally does not repeat them.

Older TOP-19-001, TOP-22-006, AAC, CRAB-first, hard-coded impacts, and postfit
material is indexed under [historical documentation](docs/historical/README.md).
It is not the recommended TOP-26-006 workflow.
