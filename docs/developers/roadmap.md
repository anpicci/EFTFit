# EFTFit contributor roadmap

This page is for EFTFit contributors and developers. It records implementation
status and dependency order in repository-facing terms. It is not a fit
configuration, scientific authority, production authorization, or substitute
for qualification evidence. User workflows and current contracts remain in
the [documentation index](../README.md).

## Status

### Implemented now

- Configured 1D profiled and frozen scan consumers read the analysis-owned fit
  configuration.
- Maintained 1D Condor execution, bounded recovery, identity-bound retrieval,
  and canonical result selection are implemented.

### Design agreed but implementation pending

- Configured 2D scan consumers should apply the same analysis-owned population
  and range authority while preserving their two-axis semantics.
- Configured impact consumers should preserve shared cross-stage analysis
  semantics while using stage-appropriate fit and aggregation controls.

The accepted semantics and current limitations are summarized in
[design-closed features and remaining limitations](../explanation/pending_and_limitations.md).

### Blocked by an external analysis/configuration dependency

- The authoritative analysis fit configuration update is pending.
- Exact-final model inputs are pending for final package and model
  qualification.
- The likelihood nuisance and correlation model is pending where required for
  policy-closing statistical validation.

These external inputs block final qualification; they do not require
contributors to postpone implementation of the configured 2D or impact
consumers. Those consumers can be developed and validated against the accepted
configuration contract before an exact-final workspace is available.

### Future validation work

- Freeze exact package inputs, materialize the portable fit package, and
  qualify rebuild and included-workspace use once exact-final artifacts are
  available.
- Validate exact-model inference, including minimization and global-minimum
  studies plus asymptotic or coverage studies as required by the analysis.

## Dependency order

1. Finalize and freeze the authoritative analysis fit configuration.
2. Implement and validate the configured 2D and impact consumers against that
   contract.
3. Once exact-final statistical inputs and model artifacts are available,
   freeze the exact package inputs and materialize and qualify the portable fit
   package.
4. Perform exact-model statistical inference validation.

The exact-final workspace is required for final package/model qualification and
policy-closing inference, not for writing the configured 2D or impact
consumers.

Roadmap updates should cite implementation or qualification evidence and link
to the maintained contract that owns the behavior. Do not duplicate WC names,
numerical ranges, or other analysis configuration values here.
