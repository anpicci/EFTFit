# Campaign lifecycle

The canonical frontend is `Fitter/scripts/eftfit_campaign.py`. It manages a
versioned local campaign envelope with one editable YAML. The shipped `local`
provider dispatches and collects direct Combine members. The `condor` provider
dispatches the same planned members through `condor_submit` and collects bounded
Condor outcomes. Both paths have synthetic test coverage; real CMSSW and scheduler
execution require separate owner qualification. Lobster and CRAB campaign providers
exist only in tests and are not selectable in the installed CLI.

## One YAML

```yaml
schema_version: 1
package:
  path: /path/to/fit_input_package
  sha256: <sha256 of config/TOP_26_006.yml>
workspace:
  path: /path/to/workspace.root
  sha256: <sha256 of workspace.root>
dataset:
  mode: observed
  name: data_obs  # named workspace dataset; contents need separate lineage evidence
selection:
  wcs: all_scientific  # or a nonempty list from the package
  modes: [profiled, frozen]
grid:
  points: 5           # synthetic illustration, NOT a production recommendation
  align_edges: true  # explicit; false selects bin centers
  selected_indices: [1, 3]  # optional original-grid work request
execution:
  points_per_member: auto  # optional; Condor auto is 5, local auto is all
reference:
  mode: auto  # optional; split logical scans require a prepared shared snapshot
backend: condor       # optional default for new execution plans
# To dispatch locally, add to this same YAML:
# local_runtime:
#   cmsset_default: /path/to/cmsset_default.sh
#   scram_arch: <supported SCRAM_ARCH>
#   release_src: /path/to/CMSSW/src
# To dispatch through Condor on a shared filesystem, add:
# condor_runtime:
#   cmsset_default: /path/to/cmsset_default.sh
#   scram_arch: <supported SCRAM_ARCH>
#   release_src: /path/to/CMSSW/src
#   request_memory_mb: 4096  # optional; effective Condor default is 4096 MiB
keep_failures: false
```

Paths resolve relative to the YAML. At creation, `package.sha256` identifies
the package configuration bytes, not every datacard. Reference preparation,
planning, and dispatch reread that configuration and compare its parsed scientific
fields with the frozen effective spec. Comment or formatting changes retain the
original and observed hashes in technical provenance; a scientific change or
missing package input blocks new execution. The workspace keeps an exact byte
identity check. This frontend neither builds nor certifies that
workspace. The observed dataset name is a workspace object name; external
`file:dataset` selectors are unsupported because their bytes would require an
additional identity contract. Dataset presence and workspace scientific
qualification remain prerequisites to future real execution.

`execution.points_per_member` accepts a positive integer, `all`, or `auto`.
An integer is the maximum length of each member's consecutive original-index
interval on either provider. Condor `auto` resolves to 5 points per member;
local `auto` resolves to `all`. These are execution defaults, not statistical
constants, and the actual job count follows the selected spans. For example,
100 consecutive points yield 20 members at 5 points each; 103 yield 21 with a
100–102 tail. Fifty isolated even indices yield 50 members. `all` leaves each
maximal contiguous selected span intact: a sparse selection can still create
multiple members. Workers retain the global `--points`, `--alignEdges`, ranges,
and original `--firstPoint`/`--lastPoint`. The native plan records its resolved
policy and exact intervals. New YAML without `execution` freezes `auto`.
Previously recorded effective specs without `execution` keep the historical
maximal-contiguous-span behavior and their original manifest interpretation.

For a new plan with multiple members in one logical scan, `reference.mode:
auto` requires a shared reference. `reference.mode: shared_snapshot` requests
it even for a single member. Prepare each logical scan with
`prepare-reference CAMPAIGN_ID --scan-id WC:MODE` before publishing its member
plan. Preparation is an explicit operation and may run a real global fit; a
`plan --dry-run` never performs it. The reference uses the same scientific
workspace, dataset mode, POI/profile/freeze controls and parameter ranges.
For expected SM, Combine saves the generated Asimov toy and the fitted
`MultiDimFit` snapshot in the reference output. Preparation also requires a
saved global fit-result object before recording a ready reference; its presence
alone does not certify convergence. Members load that snapshot,
read the saved toy with `--toysFile`, and skip their initial fit. They do not
apply the zero-valued generation assignments again after loading the fitted
snapshot. For observed mode, members read the named stored dataset from the
saved workspace. A failed or interrupted preparation is never marked ready;
the native event history and status show its outcome or unresolved intent.
Legacy effective specs without `reference` retain independent-fit semantics.

| Producer input | EFTFit dataset mode | Dataset fitted |
| --- | --- | --- |
| Blinded standard card | `observed` | Stored `data_obs`, which is precomputed SM Asimov for that producer lineage |
| Blinded standard card | `expected`, `truth: sm` | Combine-generated SM Asimov under the declared nominal generation configuration |
| Unblinded card | `observed` | Stored real `data_obs` |
| Unblinded card | `expected`, `truth: sm` | Combine-generated SM Asimov under the declared nominal generation configuration |

`producer_label`, `observed`, and `data_obs` alone do not prove the contents of
a stored dataset. A blinded stored Asimov and a runtime-generated Asimov are
different constructions. The generation truth, minimizer start and fitted
snapshot are distinct. Expected-SM generation requires zero inside every
scientific WC range, including WCs outside the selected scan axis. Observed
scans retain their declared ranges. This shared reference is scoped to parallel
1D scans; it does not impose a global prerequisite on ordinary inference or impacts.
The partition helper accepts effective original indices, including a
producer's nontrivial effective grid cardinality, but the native campaign
adapter and physical-row reader here remain 1D. Multidimensional campaigns
and impacts execution require their own native scientific adapters.

Synthetic tests validate planning, fake reference outcomes, member ownership,
and recovery without running Combine or Condor. The first real N=100 centered
expected-SM ctG reference and five-point members require a separate authorized
production run. Its readback must check the intended dataset/common NLL
reference, unique physical points 0–99, and agreement with the accepted
monolithic curve under scientific judgment. Synthetic evidence establishes
neither empirical NLL equality, convergence nor speedup.

`local_runtime` is optional for planning and required for local dispatch. It
contains typed setup-script, architecture, and release-source bindings;
arbitrary shell commands are not accepted. The worker starts non-login Bash
with a small environment allowlist. It exports the selected `SCRAM_ARCH`,
sources the selected setup file with Bash nounset temporarily disabled, enters
the configured release `src`, calls `cmsenv`, restores nounset, sets an unlimited
stack and disables core dumps. It then resolves `combine`, checks that
`CMSSW_BASE/src` matches the configured release and that the executable is the
active release's architecture-specific binary, and executes the planner's exact
`combine` argv. Controller
`PYTHONPATH`, Conda variables, and `LD_LIBRARY_PATH` are excluded. Dispatch
records the setup file identity; the worker outcome records the resolved
`combine` path, architecture, CMSSW base and stack. Resolution and binding
checks establish setup and binary lookup; only the worker exit and output show
whether Combine ran successfully. Runtime settings do not enter the scientific
job-set identity.

`condor_runtime` uses the same typed release bindings for Condor dispatch.
Its optional positive integer `request_memory_mb` defaults to 4096 MiB and
renders one explicit `request_memory = 4096M` submit request (or the configured
value). The effective request is bound to the dispatch runtime; it changes no
scientific argv or direct-grid job-set identity. The campaign's stored
`spec_digest` still protects the entire frozen effective specification, so it
does change when a newly created campaign specifies a different memory value.
The controller, submit host and workers must share readable source, setup,
release, workspace, and attempt paths and writable attempt/result storage.
The worker uses `/usr/bin/python3` for its standard-library adapter, then a
clean non-login CMSSW Bash for the planned `combine` argv. Condor does not
transfer inputs in this provider. Controller readability alone does not prove
worker access; deployment paths, admission and resources require a separate
real preparation check. No CPU or disk request is inferred from the synthetic
examples.

For runtime Asimov generation, use `dataset: {mode: expected, truth: sm}`.
The frozen effective spec records `nuisance_generation: nominal`; the plan
emits `-t -1` and the package-authoritative 26 scientific plus 10 fixed-zero
WC assignments through `--setParameters` for an independent single-member fit
or shared reference fit. In the shared case, members read the saved Asimov toy
and fitted snapshot without reapplying those zero assignments. A different
generation truth is unsupported; nonzero fit starts are separately unsupported
by the configured argv builder. The nominal
expected path does not inject a workspace dataset name. Either fit-side mode
may accompany blinded or unblinded producer cards. `observed` emits
`--dataset <name>` and no toy flag; omitted `name` defaults to `data_obs` in
the frozen spec. The name does not claim real events. In
the accepted 016B workspace, `data_obs` has separately proven prebuilt SM
Asimov source lineage. Optional `dataset.producer_label: blinded | unblinded`
records the YAML author's card provenance label in the frozen spec without
changing the fit-side mode or certifying that label.

At `CombinedLimit@6a5a27ec49d677e93833aec13d76cd8bea3e0050`,
frequentist nuisance conditioning and `--bypassFrequentistFit` remain outside
this frontend. Shared parallel scans use the source-supported `MultiDimFit`
snapshot and saved-toy route described above. The first real run must verify
the dataset, global-observable state and NLL convention empirically. No
generated Asimov result is claimed equivalent to a prebuilt card observation.

`grid.selected_indices` is an optional nonempty unique list of original
indices from `0` through `points - 1`. The effective spec sorts it; local and
Condor plans use exactly those indices while retaining original `points`,
coordinates, and the complete campaign scan domain. `retry` and `rescue`
derive eligible work automatically from attempts and receipts within that
frozen request. A partial request does not change the physical grid.

The package owns scientific/fixed-zero WC populations and scientific ranges.
For TOP-26-006 these are the accepted 26/10 populations. The frontend does not
require recipe, starts, reference, dataset or family fields in the package.
No inline population/range overrides, duplicate YAML keys, arbitrary argv,
second recipe YAML, or unknown fields are accepted. `InferenceRecipe` validates
the selected family/axis internally. Optional `fit_start_values` may contain
zero values allowed by that family. Nonzero starts are rejected because the
configured argv builder does not support them. A frozen scan rejects starts
for non-axis WCs. Unsupported generation or conditioning semantics block only
that new specification; existing campaign inspection remains usable.

Creation freezes the effective configuration and its digest, package/workspace
identities, and original YAML text. Later source YAML edits do not change it.
Package scientific fields and workspace bytes are checked again for execution
plans and dispatch; status, restart and clone can operate when those external
inputs are absent.

## Commands

Use a Python environment with the existing YAML and scientific reader
dependencies. No CMSSW bootstrap is needed for campaign state or planning.
The examples assume the EFTFit repository root as the working directory.

```bash
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns create campaign.yml
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns status CAMPAIGN_ID
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns plan CAMPAIGN_ID --backend condor --dry-run
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns plan CAMPAIGN_ID --backend condor
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns plan CAMPAIGN_ID --backend local --dry-run
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns retry CAMPAIGN_ID --backend condor --dry-run
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns rescue CAMPAIGN_ID --backend condor --strategy minimizer_strategy_1 --dry-run
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns restart CAMPAIGN_ID
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns clone CAMPAIGN_ID
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns plan CAMPAIGN_ID --backend local
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns dispatch CAMPAIGN_ID ATTEMPT_ID
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns collect CAMPAIGN_ID ATTEMPT_ID
```

`create` allocates a random campaign ID and one generation without submission.
`restart` retains the campaign/spec and all history, retires the active
generation and creates a fresh generation with planned points. No historical
results are reused. `--expected-revision N` is an optional compare-and-swap
guard against a stale restart decision. `clone` creates an independent ID and
physical namespace with an identical spec digest and a parent-ID reference;
no execution/results are shared. There is no destructive restart or cleanup.

`plan` selects points without dispatched or collected execution. `retry` requires
a collected terminal missing, invalid, or incomplete scientific result. `rescue`
requires a collected numerical grid failure (`deltaNLL=9990`) and its explicit
strategy. Accepted points, promotion-only quarantine, and dispatch intents
awaiting collection are excluded. A plan-only attempt is not recovery evidence.
When none qualify, the command returns `no_eligible_points` with point-specific
reasons and creates no attempt. `--scan-id WC:mode`
can bound the logical scan; there is no user point-index retry selector. Retry
preserves the numerical contract. Rescue requires the explicit
`minimizer_strategy_1` strategy, changing only
`--cminDefaultMinimizerStrategy=0` to `=1`, and records the exact difference.
This strategy has a validated argv contract and synthetic tests, **not a
qualification of scientific recovery or convergence**. Unknown strategies fail
locally. No grid size is selected by default.

`--backend` takes precedence over the frozen optional default. Changing backend
creates a new attempt in the same campaign/generation without changing the spec
digest or point identities. Missing/unsupported providers produce a local error
with no fallback. Status does not load providers. A direct-grid job-set identity
is the same across local and Condor for the same campaign, generation, scan,
selected original indices, scientific argv and workspace SHA. The frontend records the
requested/actual provider, capabilities in the plan, runtime provenance (including
maintained source hashes and whether the execution environment is unresolved) and
execution links; provider names are not part of campaign or point identity.

Without `--dry-run`, planning records an immutable planned attempt. It does not
dispatch it. Plans contain argv arrays and predicted 009A links; these are not
receipts proving that execution occurred. A new schema-5 EFTFit/009A direct-grid
plan freezes the original `N`, exact selected indices, resolved points-per-member
policy and partition intervals. Each interval has one typed expected member and one direct `combine`
argv with exactly one `--points N`, `--firstPoint i`, and `--lastPoint j`.
The local plan returns those argv arrays and distinct attempt working paths.
Each reserved attempt has its own execution root, so retries cannot replace
earlier members even when the scientific job-set ID is unchanged.
The Condor plan renders the same argv arrays into a worker script and an
explicit queue containing exactly the selected intervals. Planning runs no fit
and submits nothing. The scripts, submit descriptions and working directories
are projections. Condor dispatch materializes a bound submit file, worker
entrypoint and immutable 009A manifest/attempt record before submission.
`--gridPoints`, `--split-points` and CombineTool are excluded from new plans.
Original indices and expected coordinates remain bound to the frozen campaign
even for sparse retry/rescue.

Historical schema-3 full-grid 009A manifests, attempts and final receipts
retain their CombineTool identity and byte format. Their decoder remains
available for old jobs; new campaign plans use direct `combine`. Schema-4 and
schema-5 member lists bind job-set identity and are shared by both renderers, attempt
records and member checks. A plan does not assert that a member exists or is a
valid ROOT result. Cross-member reference-NLL qualification and aggregation
remain separate. A
valid member or byte-bound final sidecar alone does not establish compatible
reference fits. Real CMSSW execution and target storage remain separately unqualified.

`dispatch CAMPAIGN_ID ATTEMPT_ID` and `collect CAMPAIGN_ID ATTEMPT_ID` are
separate stages. Local dispatch records intent before launching a worker;
interruption or missing completion evidence leaves an unknown intent that is
never automatically resubmitted. Each worker has a private directory,
stdout/stderr, start and outcome records, and a typed 009A manifest/attempt
record. Successful worker output is linked exclusively to the exact expected
member name. A non-collision link error is recorded as `publication_failed`
with its error and process exit code; collection does not accept that member.
Nonzero exit, missing output and invalid ROOT are classified
separately. Collection is read-only at the provider boundary; the campaign
then commits one atomic batch of scientific receipts. Duplicate dispatch or
collection is rejected. Condor dispatch records an intent before calling
`condor_submit -terse`, binds the returned cluster and contiguous process range
to the exact planned member set, and records the raw response. A nonzero,
missing, malformed or lost response does not create a successful binding; a
possible submission requires manual investigation before any new attempt.
If `condor_submit` could not be started, an append-only `submission_not_started`
event records that no job was created by that invocation. `status` shows the
attempt as planned with a proven non-start, and the same public `dispatch`
command may be run again for that attempt after the launch problem is fixed.
It reuses and checks the original materialized member/submit evidence before
submitting. An intent with a possible submission and no binding remains blocked
against resubmission; missing output alone does not establish a non-start.
Condor collection queries only bound scheduler identities. Idle, running,
held, suspended, transferring, evicted or unknown outcomes leave the campaign
intent uncollected. Terminal scheduler failures produce invalid member receipts.
For each exact job, collection checks the active queue and then scheduler
history. A successful empty response means no record in that source, as does
JSON `[]`; only non-empty malformed JSON is a format error. Absence from both
sources leaves the scheduler outcome unknown.
Terminal success requires identity-bound worker evidence and still passes the
ordinary ROOT and coordinate validator. Missing ROOT becomes a missing receipt;
collisions and publication failures remain invalid. A terminal worker exit alone
does not qualify the fit. No automatic submission, retry or background query occurs.
There is no implicit dispatch from retry/rescue or dynamic provider import
from YAML.

All mutating commands support `--dry-run`: no IDs are reserved, directories
created, lock files written, or state changed. A zero-filled attempt ID in a
dry-run plan is explicitly a preview namespace, not an executable reservation.
`status` is always read-only. JSON goes to stdout on success; local errors go
to stderr with exit status 2. Unsupported dependent operations can error during
dry-run without modifying anything.

## State and evidence

The campaign owns only immutable spec/generation/point relations, planned
attempts, dispatch intents and scientific validation receipts. 009A owns
execution manifests, attempts and members. Campaign receipts reference these
files by path and digest instead of copying their ledgers. Grid point indices
and expected coordinates are persisted together; filenames and approximate
coordinates never define logical identity. ROOT coordinates are checked against
the identity-owned expected point with a tolerance bounded by one quarter of
grid spacing and float32 output precision.

`status` reports `planned`, `running`, `missing`, `invalid`, `accepted`, or
`unresolved`. `running` means dispatch intent awaiting collection, not a live
scheduler claim. Missing scheduler evidence after interruption needs explicit
inspection. Member presence is insufficient: collection validates 009A
job/attempt/member ownership, reopens ROOT through `scan_1d`, checks coordinates,
and binds the receipt to the exact bytes. Later missing or changed evidence is
shown as an `evidence_issue` on an accepted historical point; it calls for
focused diagnosis and does not silently turn that point into a fit failure or
force a retry. Duplicate physical rows for one original coordinate are invalid.
`deltaNLL=9990` is invalid, never a physical
point. Special-only rows cannot satisfy a grid point. Consumer mask/drop/error
and gap semantics are unchanged; this frontend uses mask for recovery evidence.

Canonical promotion for new shared scans uses the same scientific dataset,
selection and reference artifact across members and eligible retries; different
member and attempt identifiers do not change that group. Frozen legacy
campaigns retain their original member-bound grouping. A scientifically
unqualified different group produces `PROMOTION_UNQUALIFIED_REFERENCE` for
affected recovered points. No raw-NLL
minimum across attempts, offset guessing or automatic replacement is performed.
A separate scan may establish its own group independently. An accepted point
means a valid grid evaluation under this bounded consumer contract, not proof of
initial-fit convergence, a global minimum, or publication-quality intervals.

**`--keepFailures` remains off and unsupported.** Its initial-fit/special-entry
failure contract is open: `quantileExpected=-1` alone does not certify a successful
initial fit. The grid `9990` rule does not resolve that separate issue. Enabling
`keep_failures` rejects only that specification.

## Persistence guarantees and limits

Schema version 1 uses an immutable `campaign.json` plus numbered immutable events
and hash-bearing commit markers. The last committed generation event is the sole
active-generation authority; there is no second mutable pointer or status cache.
Each event links to its predecessor digest. Writers take `flock` on the immutable
campaign identity file, publish files with the maintained exclusive hard-link
writer, fsync directories, and reopen committed history. Concurrent stale
compare-and-swap restarts fail. No event, attempt or result is overwritten.

A crash before a commit marker leaves an exposed incomplete transaction. Status
retains the previous committed generation and reports the incomplete record;
further mutations fail closed. A crash during creation leaves an identifiable
incomplete namespace. Recovery/removal of interrupted transactions requires a
separately reviewed procedure; status never silently promotes or deletes them.
After ambiguous dispatch, do not retry until execution state is established.

Tests exercise local POSIX storage and two processes. **They do not qualify
multi-host NFS/Lustre locking, hard-link exclusivity, rename/cache visibility,
directory-fsync durability, or recovery after host/storage failure.** Qualify
those capabilities and deployment permissions on the target storage before
multi-host execution. No shared-filesystem production claim follows from local
tests or merely storing source files on NFS.

## Legacy practice delta

Compared with local `py3Combine@e312d533e897c6b5cad4cc54a4b3c5f9f5d54827`;
this is a source comparison, not a claim of full legacy parity. The relation
to the historical public `203dec4870f0075793c5255e53f0724463a5f54c`
reference is unqualified. Legacy choices are a comparison baseline, not the
scientific authority for the new 1D campaign.

| Source locus | Legacy executable behavior | Current 1D campaign | Class and reason | Qualification |
| --- | --- | --- | --- | --- |
| `Fitter/scripts/EFTFitter.py:440-484` at `py3Combine` | `combineTool.py` grid with conditional Condor splitting | Direct `combine` uses original-index ranges and 009A member names | Intentional difference: immutable member ownership | Source compared; no fit run |
| `Fitter/scripts/EFTFitter.py:455-457` at `py3Combine` | Zero starts for scan/tracked WCs unless overridden | Package population sets all scientific and fixed WCs to zero | Intentional difference: explicit SM generation state | Nominal command plan only |
| `Fitter/scripts/EFTFitter.py:1503,1592` at `py3Combine` | Generated impacts scripts append `-t -1` on their blinded path | Fit-side `expected` adds `-t -1` independently of producer label | Intentional difference: separate dataset layers | Legacy impacts path only; no 1D parity claim |
| `Fitter/python/CombineHelper.py:764` and `Fitter/scripts/combine_helper.py:675` at `py3Combine` | Saturated GoF helper adds `--toysFrequentist`; bypass is only a commented example | 1D frequentist conditioning is unsupported | Legacy only: different family and unverified input data | Requires separate qualification |
| `Fitter/scripts/EFTFitter.py:440-484` at `py3Combine` | No active snapshot flag in inspected legacy grid path | Native shared scans load a prepared `MultiDimFit` snapshot | Synthetic contract only; empirical snapshot/NLL readback remains | Inspected legacy path only |
| `Fitter/scripts/EFTFitter.py:484` at `py3Combine` | CombineTool output names | Job digest and `POINTS.i.j` member ownership | Intentional difference: receipt and collision control | Source compared |

The snapshot row describes the native shared-scan implementation; its empirical
dataset and NLL qualification remains a separate real-run check.

## Qualification boundary

Implemented: single YAML, frozen provenance, explicit reference preparation,
full and selected original-grid direct local/Condor no-submit mapping,
schema-5 typed member/attempt validation with schema-4 and historical schema-3 readback,
mixed-provider identity via two synthetic doubles,
local crash/concurrency guards, ROOT-backed validation, isolated quarantine,
and read-only dry runs.

Not yet production-qualified: real local CMSSW campaign dispatch/collection,
cross-attempt/member NLL anchors, runtime Asimov truth, keepFailures initial-fit semantics,
production storage, interval/grid precision and coverage, final scientific
workspace authority, configured 2D and impacts presets. These are separate
extensions or scientific gates; they do not disable existing lifecycle/status
operations.
