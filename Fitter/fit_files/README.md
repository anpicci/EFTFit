Merged Combine limits go here (from, for example,
`batchRetrieve1DScansEFT`). Maintained 1D Condor results use the canonical
identity-bearing form:

```
higgsCombine<scan_name>.job_<full_job_set_id>.MultiDimFit.root
higgsCombine<scan_name>.job_<full_job_set_id>.MultiDimFit.root.provenance.json
```

The sidecar binds the exact result bytes, SHA-256-only workspace content
identity, path-independent statistical invocation, expected members, and
attempt history. Workspace paths remain provenance locators rather than
logical identity. Sidecar validation recomputes `job_set_id` and validates the
adjacent canonical file, so neither the original workspace path nor the
original publication directory must exist after relocation. Consumers must
resolve the canonical result through the repository helper. A scan with
multiple valid identities requires an explicit `job_set_id`; no result is
selected by mtime or name ordering. The historical stable filename is not
persistent canonical state. It may be materialized only as a locked, transient
relative symlink for one explicit compatibility operation.
