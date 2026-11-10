# Local Director–Clock recovery trace

Date: 9 November 2026. All executor responses are supplied locally. Provider calls: 0.

Test run: `d05c6c18-ee7d-4399-b242-f825f92dc202`. Test Clock: `89d7dabf-6bb6-5fe4-935b-2a8d7315afab`.
The owned test schema was removed after exporting the records. These IDs are not development console URLs.

| Recorded stage | Original identity and evidence |
| --- | --- |
| Prepare/dispatch Agent job | `de33430e-6314-565d-9012-010e60b917ab`; owner `owner-1`; one attempt. |
| Kill worker after output commit | Saved text and output-token count survive; no new executor call on arm. |
| Pause/resume | Saved output remains undelivered until explicit resume and generation completion. |
| Simulated second 1 | Exactly one private message `e0b58d22-dd7e-593c-a06c-ab5a8cbb3017` commits with source `agent`. |
| Prepare paired Actor job | `1c6d2500-0d99-5762-8b49-f672e4108c7d` consumes only its targeted notice and private owner conversation. |
| Kill worker before output commit | Recover the original job as uncertain; the Clock stays at second 1 and retry is rejected. |
| Retained failure audit | `worker_lost_before_saved_output` remains in the Director event trace after successful reconciliation. |
| Explicit supplied reconciliation | Evidence reference `supplied-local-recovery-fixture-no-provider`; original attempt count remains one. This is a local fixture, not a real provider response lookup. |
| Simulated second 2 | Recovered empty result completes and selects its timed wait; it creates no extra message. |
| Simulated second 3600 | Horizon cutoff; six events remain pending. Business task success remains unspecified. |

Saved private reply:

Saved local control reply.

The raw private export includes jobs, scoped inputs/outputs, Clock command receipts, targeted delivery receipts, Director events, owner chat and reconciliation records. It is stored in the ignored `.local/director-clock/traces/` directory and included in the private evidence archive. No provider credential is present in the public summary.

The separate fifteen-owner supplied control advances 604800 seconds through 197 jobs and 366 Clock steps, retaining 32 pending events. It is a dispatch test, not a medium-chaos model experiment.
