# Historical EFTFit workflows

> **Historical only:** the material indexed here is not the recommended
> TOP-26-006 workflow. Do not use it as WC-population, range, impacts, result,
> or package authority.

These workflows remain useful for provenance and archaeology:

| Surface | Historical role | Why it is not current |
|---|---|---|
| `README_TOP19001_FITS.md` | TOP-19-001 fitter workflow | Old analysis model and submission assumptions |
| `Fitter/test/README.md` | CMSSW 10_2_13 CRAB/DNN installation and submission | Remote scripts, CERN/site setup, CRAB-first execution, and incomplete result guarantees |
| repository `install.sh` | One-command legacy environment bootstrap | Installs CMSSW 10_2_13 and CombinedLimit v8.2.0 |
| former root README TOP-22-006 section | Mixed-card workspace reproduction | AAC `selectedWCs` path rather than the maintained interference/configured path |
| former root README impacts section | Three-stage hard-coded impacts workflow | Site paths, WC lists, ranges, nuisance list, and unintended `ctW=1` state are embedded in legacy source |
| former root README postfit section | TOP-22-006 11-region plotting workflow | Depends on old directories, scripts, manual card/workspace surgery, and region counts |

## TOP-22-006 and AAC provenance

The historical reproduction flow combined selected category cards and used
`AnomalousCouplingEFTNegative` with an external `selectedWCs.txt`. That explains
old workspaces and commands but does not describe current TOP-26-006 workspace
construction. The maintained interference helper consumes
`combinedcard.txt` and `scalings.json`; configured inference receives WC state
from a fit config.

## CRAB provenance

CRAB submission, resubmission, retrieval, and the older DNN helpers remain in
the repository for legacy consumers. They do not implement the maintained 1D
Condor identity/recovery contract: historical CRAB archives lack the exact
repository-local expected-member record, and CRAB retrieval does not carry the
same completeness guarantee.

## Legacy impacts warning

The current source still contains the historical `ImpactInitialFit`,
`ImpactNuisance`, and `ImpactCollect` methods. Their hard-coded paths, lists,
ranges, nuisance population, and `ctW=1` behavior are historical evidence, not
the configured impacts recipe. See [pending features and limitations](../explanation/pending_and_limitations.md)
for the accepted future contract.

## Legacy postfit warning

The old postfit notes assumed a particular TOP-22-006 workspace, fixed WC
command, Condor submit file, directory layout, and signal-region count. No
current replacement is claimed. Establish and validate a separate TOP-26-006
postfit contract before using those procedures on current inputs.

Git history preserves the detailed commands removed from the root README. They
are intentionally not copied here because doing so would create a second,
easy-to-misread operational manual.
