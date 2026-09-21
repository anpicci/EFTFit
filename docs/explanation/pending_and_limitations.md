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

The optional `impacts_profiled` contract uses one ordered all-26-scientific-WC
production. All scientific WCs remain active/profiled as appropriate, the 10
technical WCs remain fixed to zero, and downstream target selection does not
redefine the production population.

For both modes:

- the complete scientific range map comes from the fit config;
- technical WCs are never targets or impact parameters;
- non-WC impact parameters are the exact nonconstant RooRealVar nuisance
  population owned by the final workspace's `ModelConfig`, excluding both WC
  populations and other non-nuisance categories;
- initial fit, nuisance fits, and collection bind the same target/POI state,
  selector, dataset mode, range authority, and material fit options; and
- execution should generalize the maintained 1D `job_set_id`, `attempt_id`,
  expected-member, recovery, publication, and resolver lifecycle.

The current `ImpactInitialFit`, `ImpactNuisance`, and `ImpactCollect` methods
are legacy implementations with hard-coded lists, ranges, paths, and nuisance
selection. They are not the configured TOP-26-006 workflow. The historical
`ctW=1` assignment is an unintended test artifact and must not be reproduced.

## Portable fit package

Status: **Design closed, not materialized**.

The accepted design is one self-contained package supporting both:

1. use of an included frozen workspace; and
2. rebuild from an included final combined card, its referenced payload
   closure, and the exact scaling-data payload.

The package also contains a package-local frozen fit config and one canonical
manifest. A recipient does not need a `topeft` checkout. Exact compatible
CMSSW, ROOT, CombinedLimit, and EFTFit software remains an explicit dependency;
the design is not an offline container distribution.

No package has yet been built, frozen, or recipient-qualified. No download or
materialized package is implied by this design.

## Limitations before exact-final TOP-26-006 inference

The remaining ordered work is:

1. explicitly unlock `topeft`, correct the final numerical WC ranges, and
   freeze the authoritative fit config;
2. qualify the exact package-local fit-config bytes and final workspace/model
   population;
3. implement the configured 2D and impacts consumers without changing their
   accepted semantics;
4. materialize and recipient-qualify the portable fit package; and
5. perform exact-final statistical validation.

Until those gates pass:

- old range numbers are not final authority;
- configured 2D and impacts must not be advertised as runnable;
- the portable package must not be advertised as available; and
- unit/source evidence must not be described as exact-final statistical
  qualification.

Current postfit material is also historical. No accepted TOP-26-006 postfit
recipe has been established by this documentation migration.
