# `campaign-toolkit`

Shared tooling for the tensor4all benchmark campaigns. Five campaigns each
carried their own copy of the same scripts (`record_run.py`, `validate_run.py`,
`gen_index.py`, `publish_report.py`, `compare_runs.py`, `campaign.py`,
`report_run.py`) and the copies had drifted. This package holds that logic once,
parameterised by a per-campaign **descriptor**; a campaign keeps only a thin
`scripts/*.py` shim per entry point, a `benchmarks/campaign.yaml`, and its data.

## Install

```bash
pip install -e /path/to/campaign-toolkit          # local development
# or, from a campaign's requirements.txt, pinned by revision:
#   campaign-toolkit @ git+https://github.com/tensor4all/campaign-toolkit@<rev>
```

The only runtime dependency is `pyyaml`. JSON Schemas are checked by the small
built-in validator (`campaign_toolkit.jsonschema_lite`), not by `jsonschema`.

## The descriptor

Every campaign-specific value lives in `benchmarks/campaign.yaml` at the campaign
root, validated against `schemas/campaign.schema.json`. It declares the measured
project (name, url, pin file, checkout, build script, binary, the feature per
vendor arm), which revisions a run records (`identities: [library]` or
`[library, upstream]`), the profiles/suites/data/result directories, the engine
name -> provider map, the vendor providers, the published-page name template, the
default invalidated paths, the CSV columns, and the strings the index and report
headers print. The package itself holds no campaign's specifics.

A campaign's entry points are its `scripts/*.py` shims:

```python
from campaign_toolkit.gen_index import main
if __name__ == "__main__":
    sys.exit(main(root=pathlib.Path(__file__).resolve().parent.parent))
```

Each is also runnable directly, with `CAMPAIGN_ROOT` naming the campaign:

```bash
CAMPAIGN_ROOT=/path/to/campaign python3 -m campaign_toolkit.gen_index --check
```

Console entry points (`campaign-record-run`, `campaign-validate-run`,
`campaign-gen-index`, `campaign-publish-report`, `campaign-compare-runs`,
`campaign-report-run`, `campaign-descriptor`,
`campaign-validate-declarations`) are declared in `pyproject.toml`.

## Layout

| path | what |
|---|---|
| `campaign_toolkit/` | the package: the entry points and `descriptor.py`, `campaign.py`, `jsonschema_lite.py`, `declarations.py` |
| `campaign_toolkit/schemas/` | the packaged copies of the schemas, used at runtime and by an installed wheel |
| `schemas/` | the same schemas for review; a test fails if the two copies differ |
| `benchmarks/scripts/` | the pinned, idle-checked timing protocol, copied from the measured library |
| `tests/fixture/` | a synthetic campaign: declarations and two runs, no build |
| `docs/extraction.md` | provenance: what was taken from where, at which commit |

## CI

`.github/workflows/ci.yml` is content-only — the measurement itself runs on the
host that owns the numbers. It validates the descriptor and suite schemas
against the fixture, compiles every module, and runs the self-test in `tests/`.
