"""Shared tooling for the tensor4all benchmark campaigns.

Seven entry points -- `record_run`, `validate_run`, `gen_index`,
`publish_report`, `compare_runs`, `campaign` and `report_run` -- parameterised by
a per-campaign descriptor (`benchmarks/campaign.yaml`). Nothing campaign-specific
lives in this package; it all comes from the descriptor.

A campaign's thin `scripts/*.py` shims call these entry points with their own
root, and each is also runnable as `python -m campaign_toolkit.<name>` with
`CAMPAIGN_ROOT` naming the campaign.
"""

__version__ = "0.1.0"
