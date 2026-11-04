# Autonomy acceptance

Unique runs: 3.
Local control repeats: False.
Model repeat gate: False.
Long-term autonomy: not verified.

| Case | Scope | Runs | Functional passes | Unattended passes |
| --- | --- | ---: | ---: | ---: |
| shopping/3 | full_model | 1 | 1 | 0 |
| transfer/ambiguous | full_model | 1 | 1 | 0 |
| transfer/baseline | full_model | 1 | 1 | 0 |

## Trajectories

- [transfer/baseline · f4c83fac-c156-4e51-bc3f-d70d1dc76650](http://127.0.0.1:8000/console?run=f4c83fac-c156-4e51-bc3f-d70d1dc76650&turn=0)
- [transfer/ambiguous · 10ccb88b-9707-4520-97ad-831ff7c8242e](http://127.0.0.1:8000/console?run=10ccb88b-9707-4520-97ad-831ff7c8242e&turn=0)
- [shopping/3 · cf53ba50-1506-4b17-8b49-d0a79bdf9af9](http://127.0.0.1:8000/console?run=cf53ba50-1506-4b17-8b49-d0a79bdf9af9&turn=0)

## Limits

- Controls supply decisions and never establish model autonomy.
- A safe wait is assessed by its case rubric, not counted as a completed payment.
- Missing historical audits are unknown, not zero operator interventions.
- Invocation time is real elapsed time, not simulated time or cumulative uptime.
- Operator counts cover application controls, not unobserved external DB or host edits.
- Intervals are descriptive; selected or correlated trials are not population guarantees.
- Automatic receipt checks do not establish semantic honesty or naturalness.
