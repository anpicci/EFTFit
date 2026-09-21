# Maintained 1D fit results

This directory stores aggregates produced by maintained 1D Condor retrieval.
The canonical result and its adjacent provenance sidecar are:

```text
higgsCombine<scan_name>.job_<full_job_set_id>.MultiDimFit.root
higgsCombine<scan_name>.job_<full_job_set_id>.MultiDimFit.root.provenance.json
```

The sidecar binds the aggregate bytes, SHA-256 workspace content identity,
path-independent statistical invocation, expected split members, and attempt
history. A result is selected only after the sidecar and adjacent aggregate
validate. When more than one valid identity exists for a scan name, the caller
must supply the intended `job_set_id`; modification time and filename ordering
are never selection rules.

The pre-identity stable filename is not canonical state. It may exist only as
a locked, relative, context-bounded compatibility link for a specialist
consumer.

See [current contracts](../../docs/reference/current_contracts.md) for the full
execution, recovery, retrieval, and resolver contract.
