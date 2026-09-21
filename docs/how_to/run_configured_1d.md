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
  scientific: <ordered scientific WC list>
  fixed_zero: <ordered technical fixed-zero WC list>
wc_ranges: <mapping with exactly one entry per scientific WC>
```

Do not copy WC lists or numerical ranges from documentation. The supplied fit
configuration is their authority. For TOP-26-006, the accepted scientific
population has 26 WCs and the technical fixed-zero population has 10 WCs, but
the final numerical range payload is not yet frozen.

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

Retrieval fails before aggregation when an expected member is missing,
duplicated, stale, unexpected, or inconsistent. The result is stored below
`Fitter/fit_files` with its full `job_set_id` and adjacent provenance sidecar.

## 5. Consume results

Maintained 1D plotting readers validate canonical aggregates through the
repository resolver. Resolution without an explicit identity succeeds only
when exactly one valid result exists for the requested scan name. If several
valid job sets coexist, use a plotting surface that accepts `job_set_ids` and
provide the exact scan-name-to-identity mapping.

See [current contracts](../reference/current_contracts.md) for the precise
profiled/frozen, range-authority, provenance, and ambiguity rules.
