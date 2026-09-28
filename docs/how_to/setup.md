# Set up EFTFit

Status: **Implemented now**, with exact analysis/package qualification still
required for final inference.

EFTFit runs inside CMSSW with CombinedLimit and CombineHarvester. The locally
established reference environment is:

```text
CMSSW release: CMSSW_14_1_0_pre4
SCRAM_ARCH: el9_amd64_gcc12
CombinedLimit tag: v10.5.1
CombineHarvester tag: v3.1.0
```

Create the CMSSW area outside a Conda environment and place CombinedLimit,
CombineHarvester, and EFTFit below its `src` directory:

```bash
export SCRAM_ARCH=el9_amd64_gcc12
cmsrel CMSSW_14_1_0_pre4
cd CMSSW_14_1_0_pre4/src
cmsenv

git clone https://github.com/cms-analysis/HiggsAnalysis-CombinedLimit.git HiggsAnalysis/CombinedLimit
git -C HiggsAnalysis/CombinedLimit checkout v10.5.1
git clone https://github.com/cms-analysis/CombineHarvester.git CombineHarvester
git -C CombineHarvester checkout v3.1.0
git clone https://github.com/TopEFT/EFTFit.git EFTFit

scram b
```

Select an EFTFit revision that has been reviewed for the analysis. The versions
above record the current local reference environment, not universal package
qualification. The eventual portable package must record and qualify all exact
software revisions it uses; the commands above do not by themselves qualify an
exact-final analysis environment.

Do not use the repository `install.sh` for this maintained setup. It installs
the older CMSSW 10_2_13 / CombinedLimit v8.2.0 workflow and is retained only as
legacy source history. Likewise, historical custom CombineHarvester forks and
CRAB-specific patches are not part of the maintained setup.

CombineHarvester is part of the supported global EFTFit environment.
`DatacardReader`, `CombineHelper`, and related utilities use its reusable
datacard and Combine tooling, and maintained or future workflows should reuse
that established functionality where appropriate. The configured 1D entry
point in `EFTFitter.py` does not directly import CombineHarvester and may not
exercise every CombineHarvester utility at runtime; that narrower call path
does not make the global dependency legacy-only.

## Analysis-local inputs

The maintained configured 1D consumer needs:

- a regular-file Combine workspace; and
- a YAML fit configuration accepted by `Fitter/python/wc_fit_config.py`.

The fit configuration owns scientific and fixed-zero WC membership and
disposition, plus the complete scientific range map. Those are the scientific
semantics. The lists retain a deterministic technical serialization order, but
reordering alone is not a scientific-model change. EFTFit does not ship the
accepted final analysis payload; the analysis must provide it after its
numerical values are corrected, frozen, and qualified.

To rebuild a workspace through the maintained interference-model helper, the
working directory must also contain analysis-supplied `combinedcard.txt` and
`scalings.json` files:

```bash
source "$CMSSW_BASE/src/EFTFit/Fitter/scripts/text2workspace.sh" --output workspace.root
```

The helper defaults to `workspace.root` and supports `--output PATH`,
`--overwrite`, `--new-version`, and `--executable PATH`. The replacement and
new-version flags are mutually exclusive. It refuses to replace an existing
regular target unless `--overwrite` is explicit and rejects non-regular or
symlink targets. Generation occurs at a temporary same-directory path and is
published only when the result is nonempty; failed or empty generation
preserves an existing workspace. `--new-version` keeps the existing file and
chooses the first free `.v001`, `.v002`, ... sibling; these suffixes are path
conveniences, not scientific or content versions. A rebuild is not required
when an already qualified frozen workspace is supplied.

For an explicit build, use a YAML recipe with the maintained builder:

```bash
python "$CMSSW_BASE/src/EFTFit/Fitter/python/workspace_build_recipe.py" --recipe path/to/workspace_build.yml
```

See `docs/reference/workspace_build_recipe_v1.example.yml` for the field
shape. Relative card, parameterization, and output locators resolve from the
recipe directory. The recipe names the plugin and build options; it does not
define WC membership or ranges. `output.existing_policy` selects `fail` (the
default), `overwrite`, or `new_version`. The workspace SHA-256 identifies its
bytes; its path is a locator. The recipe's `software` field states declared
compatibility only. A qualified build receipt records observed Git commit hashes
and environment identity; those hashes never gate later workspace consumers.
Only the maintained EFTFit and CombinedLimit plugin paths with the standard
`text2workspace.py` entry point claim qualified commit provenance; custom
executables or plugins return an unqualified receipt. The qualified path
checks that the resolved executable belongs to the recorded CombinedLimit
checkout and rechecks source provenance after the build.
The legacy `wcs=` option remains a programmatic CombineHelper adapter input,
outside the YAML recipe and outside the fit-config WC authority.

`docs/reference/inference_recipe_v1.examples.yml`
shows the six supported inference families. Scan axes and impact report targets
are supplied when binding a concrete invocation, and the fit configuration
remains their WC authority. These examples contain no production inputs.
Impact reference values default to zero and may be explicitly nonzero within
the fit range. Fit starts initialize numerical minimization and never replace
references or fixed values. Profiled scans and impacts accept starts for active
scientific WCs; frozen scans accept starts only on scan axes, and frozen impacts
only on their report target. Scan-axis starts can affect an initial fit, but do
not set grid values or fit-config ranges.

Continue with [the configured 1D workflow](run_configured_1d.md).
