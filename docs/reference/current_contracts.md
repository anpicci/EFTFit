# Current contracts

This page describes behavior supported by the maintained source. It is a
contract reference, not a second fit configuration.

## Configured 1D scanning

Status: **Implemented now**, with **configured input required**.

The maintained entry point is:

```python
fitter.batch1DScanEFT(..., fit_config="fit_config.yml")
```

For the accepted TOP-26-006 population, the fit config identifies 26 scientific
WCs and 10 technical fixed-zero WCs. It, not this page, owns their exact
membership, scientific/fixed-zero disposition, and numerical ranges. The
stored lists also provide deterministic technical serialization order; that
order is not independent scientific authority.

For each selected target:

| Mode | Target | Other 25 scientific WCs | Technical 10 |
|---|---|---|---|
| profiled (`freeze=False`) | scanned | profiled | initialized to zero and fixed |
| frozen (`freeze=True`) | scanned | initialized to zero and fixed | initialized to zero and fixed |

All scientific WCs are initialized to zero. Configured controls define the
scientific population as the signal-POI population, set the complete configured
range map, and prevent a caller from contradicting config-owned population,
start, freeze, or range state. `scan_wcs` chooses only the invocation-local scan
subset; it does not narrow the configured range map.

## Fit-configuration schema and authority

`Fitter/python/wc_fit_config.py` accepts exactly:

```text
analysis
wc_population.scientific
wc_population.fixed_zero
wc_ranges
```

The two WC populations must be nonempty, duplicate-free, and disjoint. Their
list order is retained for deterministic serialization, while membership and
disposition define the scientific populations; reordering alone does not
change that scientific meaning. Range keys must exactly equal the scientific
membership set, and every range must contain two finite increasing bounds.

The current TOP-26-006 numerical range payload requires an analysis-owned
correction and qualification update. No range numbers in old source, prose, or
command examples are final authority.

## Configured and legacy range modes

`batch1DScanEFT` selects range authority from whether `fit_config` is present:

- With `fit_config=<path>`, the fit config is the only scientific range
  authority. `wc_ranges` must remain `None`. A direct caller-supplied range for
  a scientific WC is accepted only when semantically identical and is then
  canonicalized; a conflict or scientific regex selector fails closed.
- With `fit_config=None`, legacy behavior remains available for historical and
  unconfigured callers. `wc_ranges=None` selects the maintained legacy default
  table, `wc_ranges=True` selects the legacy differential table, and an
  explicit mapping applies to the scanned legacy WCs.

The historical TOP-26-006 invocation used `wc_ranges=True`. That fact records
legacy provenance; it does not make the legacy table the current configured
analysis authority.

## EFT workspace build receipt

`Fitter/python/workspace_build_recipe.py --recipe <path>` constructs the EFT
workspace command and always writes a YAML receipt beside the resolved workspace.
For `workspace.root` the receipt is `workspace.build_receipt.yml`; for
`workspace.v001.root` it is `workspace.v001.build_receipt.yml`. Standard output
prints the two artifact locators. The receipt records the
normalized model options, requested and resolved output paths, collision
policy, final workspace SHA-256 and size, predecessor identity when replaced,
and observed software commits for qualified builds. Optional
`input.input_snapshot_sha256` in the recipe or
`--input-snapshot-sha256 <value>` carries an upstream package identity into
the receipt without recomputing it. The workspace SHA-256 remains the artifact
identity; paths and software commits are provenance.
`fail` rejects a collision at either artifact path; `new_version` chooses the
first suffix with both paths free. `overwrite` requires explicit selection and
builds both temporary artifacts before replacing the published pair.

This is the canonical command-construction owner for EFT/TOP-26-006 workspace
builds. `EFTFitter.makeWorkspaceSM` remains a separate SM-specific path; this
contract does not claim that all workspace modes are unified.

## Maintained 1D execution identity

Status: **Implemented now** for the maintained Condor 1D path.

- Workspace content identity is the SHA-256 digest of exact workspace bytes.
  Requested/resolved paths are provenance locators and size is diagnostic
  metadata; neither is content identity.
- `job_set_id` identifies one logical execution/recovery set from the
  path-independent statistical command, scan identity, backend, and workspace
  SHA-256.
- `attempt_id` identifies each original or recovery attempt within the same
  job set.
- Recovery preserves `job_set_id`; changed workspace bytes or fit semantics
  create a different job set.
- The worker verifies the workspace SHA-256 before Combine and publishes only
  its exact expected split-member output.

Runtime checks are deliberately minimum sufficient: workers verify the input
boundary, retrieval verifies the exact recorded member set, and consumers
validate the selected canonical product. Workers do not reconstruct the full
recipe or require an exact directory-content census.

The recorded member set is strict: a missing or empty expected member,
duplicate expected file identity, or conflicting member within the
identity-owned result-name pattern fails retrieval. Files outside that owned
pattern do not participate in result selection and do not fail retrieval merely
because they share the directory.

## Retrieval and canonical results

Successful maintained retrieval publishes:

```text
higgsCombine<scan_name>.job_<job_set_id>.MultiDimFit.root
higgsCombine<scan_name>.job_<job_set_id>.MultiDimFit.root.provenance.json
```

The sidecar binds the aggregate bytes, workspace identity, path-independent
invocation, expected members, and attempts. Republishing the same identity is a
verified no-op only when bytes agree; conflicting bytes fail closed.

`resolve_canonical_aggregate` returns a result only after validating the
sidecar and adjacent aggregate. With no explicit `job_set_id`, zero candidates
fail as missing and multiple candidates fail as ambiguous. No consumer may use
mtime or lexical ordering as an implicit scientific choice.

Maintained 1D plotting paths use this resolver. A stable pre-identity filename
is not persistent authority and may be exposed only through the repository's
locked, relative, context-bounded compatibility helper.

## Scope boundary

The identity/recovery contract above does not extend to historical CRAB, 2D,
or 3D retrieval. Configured 2D and impacts status is documented in
[pending features and limitations](../explanation/pending_and_limitations.md).
