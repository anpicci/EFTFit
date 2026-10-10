# Campaign lifecycle

The canonical frontend is `Fitter/scripts/eftfit_campaign.py`. It manages a
versioned local campaign envelope with one editable YAML. It does not submit
fits. The shipped `local` and `condor` providers support **no-execution planning
only**; dispatch and collection through either remain unqualified. Lobster and
CRAB campaign providers are not implemented. Synthetic providers
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
  name: data_obs  # explicit workspace dataset name, checked before real execution
selection:
  wcs: all_scientific  # or a nonempty list from the package
  modes: [profiled, frozen]
grid:
  points: 5           # synthetic illustration, NOT a production recommendation
  align_edges: true  # explicit; false selects bin centers
backend: condor       # optional default for new execution plans
keep_failures: false
```

Paths resolve relative to the YAML. `package.sha256` binds the scientific
configuration, not every datacard: this frontend consumes the separately hashed
workspace and the package configuration. It neither builds nor certifies that
workspace. The observed dataset name is a workspace object name; external
`file:dataset` selectors are unsupported because their bytes would require an
additional identity contract. Dataset presence and workspace scientific
qualification remain prerequisites to future real execution.

The package owns scientific/fixed-zero WC populations and scientific ranges.
For TOP-26-006 these are the accepted 26/10 populations. The frontend does not
require recipe, starts, reference, dataset or family fields in the package.
No inline population/range overrides, duplicate YAML keys, arbitrary argv,
second recipe YAML, or unknown fields are accepted. `InferenceRecipe` validates
the selected family/axis internally. Optional `fit_start_values` may contain
zero values allowed by that family. Nonzero starts are rejected because the
configured argv builder does not support them. A frozen scan rejects starts
for non-axis WCs. Unsupported Asimov truth/generation semantics block only that
new specification; observed campaigns and existing inspection remain usable.

Creation freezes the effective configuration and its digest, package/workspace
identities, and original YAML text. Later source YAML edits do not change it.
Package/workspace bytes are checked again for execution plans and dispatch;
status, restart and clone can operate when those external inputs are absent.

## Commands

Use a Python environment with the existing YAML and scientific reader
dependencies. No CMSSW bootstrap is needed for campaign state or planning.
The examples assume the EFTFit repository root as the working directory.

```bash
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns create campaign.yml
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns status CAMPAIGN_ID
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns plan CAMPAIGN_ID --backend condor --dry-run
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns plan CAMPAIGN_ID --backend local --dry-run
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns retry CAMPAIGN_ID --backend condor --dry-run
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns rescue CAMPAIGN_ID --backend condor --strategy minimizer_strategy_1 --dry-run
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns restart CAMPAIGN_ID
python Fitter/scripts/eftfit_campaign.py --root /path/to/campaigns clone CAMPAIGN_ID
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
receipts proving that execution occurred. A schema-4 EFTFit/009A direct-grid
plan freezes the original `N`, exact selected indices and maximal contiguous
intervals. Each interval has one typed expected member and one direct `combine`
argv with exactly one `--points N`, `--firstPoint i`, and `--lastPoint j`.
The local plan returns those argv arrays and distinct attempt working paths.
The Condor plan renders the same argv arrays into a worker script and an
explicit queue containing exactly the selected intervals. Planning runs no fit
and submits nothing. The scripts, submit descriptions and working directories
are projections requiring separate materialization and deployment review.
`--gridPoints`, `--split-points` and CombineTool are excluded from new plans.
Original indices and expected coordinates remain bound to the frozen campaign
even for sparse retry/rescue.

Historical schema-3 full-grid 009A manifests, attempts and final receipts
retain their CombineTool identity and byte format. Their decoder remains
available for old jobs; new campaign plans use direct `combine`. The schema-4
member list binds job-set identity and is shared by both renderers, attempt
records and member checks. A plan does not assert that a member exists or is a
valid ROOT result. Native direct-grid recovery submission and aggregation are
blocked pending execution and cross-member reference-NLL qualification. A
valid member or byte-bound final sidecar alone does not establish compatible
reference fits. Real execution and target storage remain separately unqualified.

`dispatch CAMPAIGN_ID ATTEMPT_ID` and `collect CAMPAIGN_ID ATTEMPT_ID` expose
the separate provider stages for integration tests and future qualified
providers. Both fail clearly for shipped local and Condor capability. Dispatch records
intent before calling a provider; an interrupted intent is never automatically
resubmitted. Collection is read-only at the provider boundary, followed by
scientific validation and an atomic batch of campaign receipts. There is no
implicit dispatch from retry/rescue and no dynamic provider import from YAML.

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
and binds the receipt to the exact bytes. Changed/missing evidence invalidates
only the affected status. A valid duplicate is accepted by the existing consumer
and is excluded from automatic retry. `deltaNLL=9990` is invalid, never a physical
point. Special-only rows cannot satisfy a grid point. Consumer mask/drop/error
and gap semantics are unchanged; this frontend uses mask for recovery evidence.

Canonical promotion uses one validated member/reference group per logical scan.
Further groups, including another attempt or backend, produce
`PROMOTION_UNQUALIFIED_REFERENCE` for affected recovered points. No raw-NLL
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

## Qualification boundary

Implemented: single YAML, frozen provenance, all six lifecycle operations,
full and selected original-grid direct local/Condor no-submit mapping,
schema-4 typed member/attempt validation with historical schema-3 readback,
mixed-provider identity via two synthetic doubles,
local crash/concurrency guards, ROOT-backed validation, isolated quarantine,
and read-only dry runs.

Not yet production-qualified: real campaign provider dispatch/collection,
cross-attempt/member NLL anchors, Asimov truth, keepFailures initial-fit semantics,
production storage, interval/grid precision and coverage, final scientific
workspace authority, configured 2D and impacts presets. These are separate
extensions or scientific gates; they do not disable existing lifecycle/status
operations.
