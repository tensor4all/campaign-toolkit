# Review receipts

`20261017-corpus-design-pre-review.md` — independent `openai/gpt-6.1-sol` pre-implementation review
(fresh `pi --print` process, read-only tools) of `docs/corpus-suites-design.md`, run before any
implementation. Verdict: **change the design**; two blockers (the proposed case record does not
describe the real workload; the census's declaration comparison strips `note`, so a variant declared
only as a note can vanish unnoticed) and five further findings (einsum path/dtype/layout mapping,
recorder/schema/report dependencies, external TBLIS provenance, missing-feature admission policy,
report identity migration, and an unsupported delivery estimate). The design was revised in place and
the reviewed proposal is kept below the revision as the record. The reviewer ran no builds, tests or
measurements.

SHA-256: `445712a25ec96a1a3eead70e594afbdea7fbef53d2f3c9cfddb4e461e08afc89`
