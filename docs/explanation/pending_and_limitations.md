# Design-closed features and remaining limitations

This page separates accepted future semantics from runnable source. None of
the design-closed sections below is an implemented current workflow.

## Configured 2D scans

Status: **Design closed, not implemented**.

The accepted configured modes are:

| Mode | Two scientific axes | Other 24 scientific WCs | Technical 10 |
|---|---|---|---|
| profiled | scanned | profiled | initialized to zero and fixed |
| frozen | scanned | initialized to zero and fixed | initialized to zero and fixed |

Both modes use the complete scientific range map from the fit config. The
current `batch2DScanEFT` implementation is legacy: it does not accept a fit
config and selects a legacy range table. Do not treat it as the configured
TOP-26-006 consumer.

## Configured impacts

Status: **Design closed, implementation pending**.

The default `impacts_frozen` contract uses one scientific target per
fit-producing job set: the target is active and reported, the other 25
scientific WCs are fixed to zero, and all 10 technical WCs are fixed to zero.

The optional `impacts_profiled` contract uses the complete scientific-WC
membership in one production. All scientific WCs remain active/profiled as
appropriate, the 10 technical WCs remain fixed to zero, and downstream target
selection does not redefine the production population. Deterministic
serialization order is a technical requirement, not a separate scientific
property of that population.

For both modes:

- the complete scientific range map comes from the fit config;
- technical WCs are never targets or impact parameters;
- non-WC impact parameters are the exact nonconstant RooRealVar nuisance
  population owned by the final workspace's `ModelConfig`, excluding both WC
  populations and other non-nuisance categories;
- shared semantic fields agree across stages wherever applicable, including
  workspace identity, scientific POI/state definition, dataset definition,
  range authority, nuisance-selector authority, and other cross-stage analysis
  semantics;
- stage-specific algorithm controls intentionally differ: `ImpactInitialFit`
  is the fit-producing initial-fit stage, `ImpactNuisance` is the
  fit-producing nuisance-impact stage, and `ImpactCollect` is an
  aggregation/result-production stage with no fit lifecycle of its own; and
- execution should generalize the maintained 1D `job_set_id`, `attempt_id`,
  expected-member, recovery, publication, and resolver lifecycle.

The current `ImpactInitialFit`, `ImpactNuisance`, and `ImpactCollect` methods
are legacy implementations with hard-coded lists, ranges, paths, and nuisance
selection. They are not the configured TOP-26-006 workflow. The historical
test-only nonzero WC assignment is unintended and must not be reproduced.

## Portable fit package

Status: **Design closed, not materialized**.

The accepted design is one self-contained package supporting both:

1. use of an included frozen workspace; and
2. rebuild from an included final combined card, its referenced payload
   closure, and the exact scaling-data payload.

The package also contains a package-local frozen fit config and one canonical
manifest. A recipient does not need a `topeft` checkout. Exact compatible
CMSSW, ROOT, CombinedLimit, CombineHarvester, and EFTFit software remains an
explicit dependency; the design is not an offline container distribution.

No package has yet been built, frozen, or recipient-qualified. No download or
materialized package is implied by this design.

## Current limitations

The configured 2D and impacts consumers are not yet runnable, the portable fit
package has not been materialized or qualified, and exact-model statistical
validation remains future work. See the
[contributor roadmap](../developers/roadmap.md) for status and dependency order.

Until those items are closed:

- old range numbers are not final authority;
- configured 2D and impacts must not be advertised as runnable;
- the portable package must not be advertised as available; and
- unit/source evidence must not be described as exact-final statistical
  qualification.

Current postfit material is also historical. No accepted TOP-26-006 postfit
recipe has been established by this documentation migration.
