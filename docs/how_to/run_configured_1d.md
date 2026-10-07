# Run configured 1D scans

Status: **Implemented now**, with **configured input required**.

This recipe covers the maintained configured 1D path. It does not authorize or
describe configured 2D scans, configured impacts, or production-scale
submission policy.

## 1. Prepare inputs

Activate the CMSSW environment and work from `Fitter/test`:

```bash
cd "$CMSSW_BASE/src"
cmsenv
cd EFTFit/Fitter/test
ulimit -s unlimited
unset PYTHONPATH
unset PERL5LIB
```

Place or reference the qualified workspace and analysis-owned fit configuration.
The fit configuration must have this schema:

```yaml
analysis: <analysis_name>
wc_population:
  scientific: <scientific WC members in deterministic serialization order>
  fixed_zero: <fixed-zero WC members in deterministic serialization order>
wc_ranges: <mapping with exactly one entry per scientific WC>
```

Do not copy WC lists or numerical ranges from documentation. The supplied fit
configuration is their authority. Membership and scientific/fixed-zero
disposition carry the scientific meaning; list order provides deterministic
technical serialization and a pure reordering is not a scientific-model
change. For TOP-26-006, use the package-local `config/TOP_26_006.yml` for WC
populations and numerical ranges. Historical hardcoded plotter ranges are
retired from scientific use. Do not import analysis-processor range tables as
fit or plot authority.

## 2. Submit a configured scan

Load the maintained fitter:

```bash
python3 -i ../scripts/EFTFitter.py
```

From the interactive session, submit a profiled scan with:

```python
fitter.batch1DScanEFT(
    basename=".top_26_006.profiled",
    batch="condor",
    freeze=False,
    scan_wcs=[],
    workspace="workspace.root",
    fit_config="fit_config.yml",
)
```

An empty `scan_wcs` selects all scientific WCs from the fit configuration. A
nonempty list selects an invocation-local subset and must remain a subset of
that configured population. Use `freeze=True` for the frozen-other-WCs mode.

Choose observed-data or Asimov arguments deliberately for the analysis. Such
arguments are fit semantics and therefore contribute to the logical job-set
identity.

## 3. Recover unresolved members

Recovery intentionally retries only members unresolved against the recorded
expected-member set. It keeps the same `job_set_id` and creates a new
`attempt_id`:

```python
fitter.batchRecover1DScansEFT(
    basename=".top_26_006.profiled",
    scan_wcs=["<wc>"],
    job_set_ids={"<wc>": "<full_job_set_id>"},
)
```

Recovery does not infer live scheduler state. Verify scheduler state according
to the separately approved production procedure before deciding to recover.

## 4. Retrieve canonical results

Retrieve exact recorded members and publish the canonical aggregate with:

```python
fitter.batchRetrieve1DScansEFT(
    basename=".top_26_006.profiled",
    batch="condor",
    scan_wcs=["<wc>"],
    job_set_ids={"<wc>": "<full_job_set_id>"},
)
```

Retrieval fails before aggregation when an expected identity-owned member is
missing or empty, when expected members resolve to duplicate file identities,
or when a conflicting member appears inside the recorded job set's owned
result-name pattern. Wrong job-set identity or inconsistent provenance also
fails. Unrelated files and artifacts in the directory do not participate in
result selection and do not fail retrieval merely by existing. The result is
stored below `Fitter/fit_files` with its full `job_set_id` and adjacent
provenance sidecar.

## 5. Consume results

Maintained 1D plotting readers validate canonical aggregates through the
repository resolver. Resolution without an explicit identity succeeds only
when exactly one valid result exists for the requested scan name. If several
valid job sets coexist, use a plotting surface that accepts `job_set_ids` and
provide the exact scan-name-to-identity mapping.

See [current contracts](../reference/current_contracts.md) for the precise
profiled/frozen, range-authority, provenance, and ambiguity rules.

### Structured 1D results and the plotting API

The maintained core is `EFTFit.Fitter.scan_1d`. It uses uproot to read a local
ROOT `limit` tree, and has no PyROOT or fit-execution dependency. Its rendering
module `scan_1d_plot` uses Matplotlib. The historical `EFTPlot` entry points
delegate to this core and renderer:

```python
from EFTPlotter import EFTPlot

plotter = EFTPlot(
    fit_package_root="/path/to/qualified_fit_input_package",
    display_config={
        "title": "Analysis display title supplied by the caller",
        "output_directory": "/path/to/existing/plot_directory",
        "formats": ["png", "pdf"],
        # Optional: labels, scales, ranges (physical coordinates), luminosity,
        # collision_energy, reference. No analysis numbers are supplied by default.
    },
    result_options={"dir_path": "../fit_files"},
)
scan = plotter.read_scan_result([".top_26_006.profiled.ctG"], "ctG")
reference_interval = scan.intervals(1.0)
summaries = plotter.getIntervalFits(
    basename_lst=[".top_26_006.profiled"], params=["ctG"], structured=True,
)
plotter.OverlayLLPlot1DEFT(
    name1_lst=[".top_26_006.profiled.ctG"],
    name2_lst=[".top_26_006.frozen.ctG"], wc="ctG",
)
```

`fit_config=<package-local config path>` is an alternative to
`fit_package_root`. Supplying conflicting constructor `wc_ranges` fails.
The config loader owns the schema; the current schema has no independent
reference/start/impact-value fields. The result consumer does not invent them
or use display metadata as their authority.

`EFTPlot()` remains callable but emits a warning, records
`metadata_authority="legacy_non_authoritative"`, and labels its figures as
legacy/non-authoritative. It supplies no default WC population or ranges.
Pass explicit WCs for bounded legacy inspection or use package metadata.
No luminosity, collision energy, analysis title, or publication reference is
inferred from a filename or an old analysis.

Input semantics:

- Missing files/tree/required branches, empty results, and nonfinite coordinates,
  likelihoods, or tracked parameters raise errors. Malformed entries are never
  silently discarded. A result cannot mix toys.
- `quantileExpected == -1` identifies special fit entries in the supported
  MultiDimFit **grid** contract. Other nonnegative quantiles identify grid rows.
  Other negative quantiles are rejected. Without this branch, rows are explicitly
  unclassified and treated as grid with a warning; no fit minimum is inferred.
  These inputs must be grid outputs; the reader does not certify the fit algorithm,
  convergence, or result-member completeness from branch names alone.
- `raw_entries`, `grid_entries`, and `special_entries` preserve the input rows,
  tracked parameters, and file/tree/entry locators. `points` deduplicates every
  coordinate using its minimum raw deltaNLL. `grid_points` retains the sampled
  grid minima independently. Duplicate coordinates and multiplicities are recorded.
- `curve_points` uses canonical likelihoods at grid coordinates, including a
  coincident special entry. Off-grid special entries contribute to normalization
  but do not add artificial interval-resolution knots. Normalized values are
  `2 * (raw_deltaNLL - minimum_raw_deltaNLL_of_all_supplied_entries)`.
- `sampled_grid_minima` and `reported_fit_minima` are distinct. The latter are
  reported special-entry fit estimates, not certification of a continuous global
  minimum. All tied canonical minima are retained. ROOT entry order is irrelevant.
- A basename family with multiple files requires `common_reference=True` after
  verifying a shared raw likelihood reference. This grant can be supplied through
  `result_options` or a method keyword. The reader does not independently re-zero
  shards and combine them. Prefer one complete canonical merged result per family.
- Explicit `postfix` (or overlay `pf1`/`pf2`) selects an exact local filename for
  inspection, without canonical sidecar certification. It cannot be combined
  with `job_set_ids`. Otherwise the existing canonical resolver remains mandatory.

### Interval meaning and compatibility limits

`extract_intervals(sorted_unique_x, normalized_twice_delta_nll, threshold)`
returns every connected sublevel component of the piecewise-linear grid. A
boundary has a value, kind, and sampled bracket. `interpolated` crossings must
be bracketed; `sampled_threshold` marks an exact grid-level crossing. A
`scan_boundary` has `value=None`, meaning the crossing is unresolved beyond the
observed domain, **not** a physical infinite limit. Single-point input reports
`insufficient_resolution`; a grid entirely above the threshold reports
`no_sublevel_component`. A grid entirely below it returns an interval open on
both sides. Exact threshold values at a domain edge are conservatively open.

`getIntervalFits` preserves `[WC, best, lower_edges, upper_edges]`, with the
historical fifth local-minima list for two components. `siginterval=1` and `2`
select 2ΔNLL = 1 and 4. `nll_max` overrides the second threshold; `threshold`
is an explicit numerical override. These are **asymptotic reference thresholds**.
The familiar 1σ/2σ interpretation remains an asymptotic reference until Wilks
and coverage qualification; the implementation does not establish coverage.

The default legacy adapter is deliberately lossy: it maps open lower/upper
boundaries to -999/+999 **only in the returned compatibility lists**, with a
warning. It selects the leftmost tied best point and warns for ties or more
than two components. All components are retained, never truncated; old callers
that assume at most two must use `structured=True` and handle each component.
No-component/insufficient-resolution results cannot be represented as historical
lists and raise an error. Use structured results for scientific interpretation.
The historical `2sigma_struct_maker_wrapper.py` consumes this list shape and
retains its historical analysis/execution assumptions; it is not a configured
TOP-26-006 campaign consumer.

`GetWCsNLLFromRoot(..., unique=False)` returns raw coordinate and **raw
2*deltaNLL** lists, including special entries. `unique=True` sorts coordinates
and uses minimum raw deltaNLL. `LLPlot1DEFT`, `OverlayLLPlot1DEFT`,
`BatchOverlayLLPlot1DEFT`, and `BestScanPlot` retain their original argument
names and defaults. They now return structured results plus written paths.
Unknown keywords raise errors. An overlay validates both input families and
uses their union domain unless an explicit display range is supplied.
`BestScanPlot` accepts `profiled_options` and `frozen_options` for separate
directories/postfixes/identities and renders all 1/4-threshold components.

Display scale factors belong only in `display_config["scales"]`, for example
`{"ctG": 5}` if explicitly desired. They annotate axis/row labels and transform
only rendered coordinates. Scientific entries, intervals, and width ratios
remain in unscaled physical units. Open interval ends use arrows.
The optional FoM is **frozen interval width / profiled interval width** at the
same threshold. It is omitted (`None`) for open or disconnected sets or a zero
profiled width. It is descriptive, not a significance or coverage measure.

The renderer writes PNG/PDF by default directly through Matplotlib (optional
EPS/SVG); it does not create ROOT histograms or separate legend files. Existing
output paths fail rather than being overwritten. `filename` is a basename
suffix; set the output directory through display configuration. `final` retains
its output-suffix role where historically supported and grants no scientific
qualification. Old SM/2D/correlation rendering still requires PyROOT.
The separate `BestFitPlot()` demonstration previously rendered embedded
historical fit numbers. Those constants are retired; it now raises a clear
error directing callers to `BestScanPlot` with actual result inputs.

`nesting_differences` deliberately requires explicit common-convention NLL
reference offsets for both curves and exactly matched grid coordinates. It
computes `2*(profiled_NLL - frozen_NLL)` using raw values, not independently
normalized display curves. Without reference evidence, omit the numerical
nesting claim. The 17-point representative ctG canary supports consumer and
bracketing checks only; its coarse interpolations are not final intervals.

`parse_nll.py` best-point helpers also require explicit WC lists/config and
validate requested branches. Its deduplication delegates to the maintained
core. The CLI requires `--fit-config` and `--basename`; the historical embedded
WC population and example result name no longer select scientific inputs.

Distributed retrieval APIs are unchanged. Member completeness, merge/retry
architecture, tracked-result branches, grid density, and profiled/frozen pairing
remain fit-side campaign qualification responsibilities. A readable finite
merged curve is not proof of member completeness or per-point fit convergence.
