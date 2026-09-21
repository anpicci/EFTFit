# EFTFit documentation

This is the canonical documentation index. Pages use these status labels:

- **Implemented now** — maintained source supports the documented behavior.
- **Configured input required** — the consumer is implemented, but an analysis
  must supply and qualify its own inputs.
- **Design closed, not implemented** — semantics are settled, but the current
  source is not a runnable implementation.
- **Historical** — retained for provenance and not recommended for current
  TOP-26-006 work.

## Start here

- [Set up the maintained environment](how_to/setup.md)
- [Run and retrieve configured 1D scans](how_to/run_configured_1d.md)

## Exact current behavior

- [Current configured 1D, range, execution, and result contracts](reference/current_contracts.md)

## Architecture and remaining work

- [Design-closed features and exact-final limitations](explanation/pending_and_limitations.md)

## Historical material

- [Legacy workflow index](historical/README.md)

There is no tutorial directory: the configured 1D how-to is the smallest
supported first-use path. The fit configuration remains the only authority for
WC identities and numerical ranges; documentation must not become a duplicate
configuration source.
