# Autonomy acceptance

Unique runs: 15.
Local control repeats: True.
Model repeat gate: False.
Long-term autonomy: not verified.

| Case | Scope | Runs | Functional passes | Unattended passes |
| --- | --- | ---: | ---: | ---: |
| local-lifecycle/ack | supplied_control | 3 | 3 | 3 |
| local-lifecycle/amount | supplied_control | 3 | 3 | 3 |
| local-lifecycle/cancel | supplied_control | 3 | 3 | 3 |
| local-payment/baseline | supplied_control | 3 | 3 | 0 |
| shopping/3 | supplied_control | 3 | 3 | 3 |

## Trajectories

- [local-payment/baseline · 12f0984c-7372-4ebe-b10b-53e94f2cd47b](http://127.0.0.1:8000/console?run=12f0984c-7372-4ebe-b10b-53e94f2cd47b&turn=0)
- [local-lifecycle/ack · f0e904ad-f2f6-4039-a230-366f1a2c2ab3](http://127.0.0.1:8000/console?run=f0e904ad-f2f6-4039-a230-366f1a2c2ab3&turn=0)
- [local-lifecycle/amount · a2a8b103-a7c7-47dd-acef-34da97d240b3](http://127.0.0.1:8000/console?run=a2a8b103-a7c7-47dd-acef-34da97d240b3&turn=0)
- [local-lifecycle/cancel · b2ce8964-088f-4b6b-b06b-b23049af8a76](http://127.0.0.1:8000/console?run=b2ce8964-088f-4b6b-b06b-b23049af8a76&turn=0)
- [shopping/3 · 133b9c45-5791-4a5d-9cf5-498b39fe58f5](http://127.0.0.1:8000/console?run=133b9c45-5791-4a5d-9cf5-498b39fe58f5&turn=0)
- [local-payment/baseline · f3f681d2-0024-47bd-857d-0e827aaacb4b](http://127.0.0.1:8000/console?run=f3f681d2-0024-47bd-857d-0e827aaacb4b&turn=0)
- [local-lifecycle/ack · 12bc0d8f-418d-4d80-abad-9674852a4666](http://127.0.0.1:8000/console?run=12bc0d8f-418d-4d80-abad-9674852a4666&turn=0)
- [local-lifecycle/amount · 5b9b3b41-cfd9-40a1-81bf-bfabec805305](http://127.0.0.1:8000/console?run=5b9b3b41-cfd9-40a1-81bf-bfabec805305&turn=0)
- [local-lifecycle/cancel · a6dd86d3-f306-453c-b741-91ed239bdc90](http://127.0.0.1:8000/console?run=a6dd86d3-f306-453c-b741-91ed239bdc90&turn=0)
- [shopping/3 · 03081e5b-ea39-45e7-9f11-f2e365994478](http://127.0.0.1:8000/console?run=03081e5b-ea39-45e7-9f11-f2e365994478&turn=0)
- [local-payment/baseline · 9f98ca34-ed7a-4f90-805d-3e4fb5a4145f](http://127.0.0.1:8000/console?run=9f98ca34-ed7a-4f90-805d-3e4fb5a4145f&turn=0)
- [local-lifecycle/ack · 321b36b7-11c4-4880-b059-348824c71cbb](http://127.0.0.1:8000/console?run=321b36b7-11c4-4880-b059-348824c71cbb&turn=0)
- [local-lifecycle/amount · d5e035c3-f553-4660-9fbe-01b01a8e9497](http://127.0.0.1:8000/console?run=d5e035c3-f553-4660-9fbe-01b01a8e9497&turn=0)
- [local-lifecycle/cancel · 2e6e0394-9dba-4b66-b99b-8ffb4d8ec813](http://127.0.0.1:8000/console?run=2e6e0394-9dba-4b66-b99b-8ffb4d8ec813&turn=0)
- [shopping/3 · bdd3a163-a843-466c-a6b7-b7d0b4fd7e39](http://127.0.0.1:8000/console?run=bdd3a163-a843-466c-a6b7-b7d0b4fd7e39&turn=0)

## Limits

- Controls supply decisions and never establish model autonomy.
- A safe wait is assessed by its case rubric, not counted as a completed payment.
- Missing historical audits are unknown, not zero operator interventions.
- Invocation time is real elapsed time, not simulated time or cumulative uptime.
- Operator counts cover application controls, not unobserved external DB or host edits.
- Intervals are descriptive; selected or correlated trials are not population guarantees.
- Automatic receipt checks do not establish semantic honesty or naturalness.
