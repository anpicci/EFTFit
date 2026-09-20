Merged Combine limits go here (from, for example,
`batchRetrieve1DScansEFT`). Maintained 1D Condor results use the canonical
identity-bearing form:

```
higgsCombine<scan_name>.job_<full_job_set_id>.MultiDimFit.root
higgsCombine<scan_name>.job_<full_job_set_id>.MultiDimFit.root.provenance.json
```

The sidecar binds the exact result bytes, workspace content identity, semantic
invocation, expected members, and attempt history. Consumers must resolve the
canonical result through the repository helper. A scan with multiple valid
identities requires an explicit `job_set_id`; no result is selected by mtime or
name ordering. The historical stable filename is not persistent canonical
state. It may be materialized only as a locked, transient relative symlink for
one explicit compatibility operation.
