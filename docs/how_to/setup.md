# Set up EFTFit

Status: **Implemented now**, with exact analysis/package qualification still
required for final inference.

EFTFit runs inside CMSSW with Combine. The locally accepted lane_09 baseline is:

```text
CMSSW release: CMSSW_14_1_0_pre4
SCRAM_ARCH: el9_amd64_gcc12
CombinedLimit tag: v10.5.1
```

Create the CMSSW area outside a Conda environment and place CombinedLimit and
EFTFit below its `src` directory:

```bash
export SCRAM_ARCH=el9_amd64_gcc12
cmsrel CMSSW_14_1_0_pre4
cd CMSSW_14_1_0_pre4/src
cmsenv

git clone https://github.com/cms-analysis/HiggsAnalysis-CombinedLimit.git HiggsAnalysis/CombinedLimit
git -C HiggsAnalysis/CombinedLimit checkout v10.5.1
git clone https://github.com/TopEFT/EFTFit.git EFTFit

scram b
```

Select an EFTFit revision that has been reviewed for the analysis. The eventual
portable package must record exact software revisions; the commands above do
not by themselves qualify an exact-final TOP-26-006 environment.

Do not use the repository `install.sh` for this maintained setup. It installs
the older CMSSW 10_2_13 / CombinedLimit v8.2.0 workflow and is retained only as
legacy source history.

## Analysis-local inputs

The maintained configured 1D consumer needs:

- a regular-file Combine workspace; and
- a YAML fit configuration accepted by `Fitter/python/wc_fit_config.py`.

The fit configuration owns the ordered scientific WC population, fixed-zero WC
population, and the complete scientific range map. EFTFit does not ship the
accepted final TOP-26-006 payload. The analysis must provide it after its
numerical values are corrected, frozen, and qualified.

To rebuild a workspace through the maintained interference-model helper, the
working directory must also contain analysis-supplied `combinedcard.txt` and
`scalings.json` files:

```bash
source "$CMSSW_BASE/src/EFTFit/Fitter/scripts/text2workspace.sh" --output workspace.root
```

The helper refuses to replace an existing target unless `--overwrite` is
explicit. A failed or empty generation preserves the existing workspace. A
workspace rebuild is not required when an already qualified frozen workspace
is supplied.

Continue with [the configured 1D workflow](run_configured_1d.md).
