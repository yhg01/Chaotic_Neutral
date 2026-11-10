# Timed transfer recovery trace

Date: 9 November 2026. These are simulated-credit worlds with supplied owner decisions. Provider calls: 0.

| Case | Visible before child termination | After recovery | Actual native receipt |
| --- | --- | --- | --- |
| before_commit | {'alice': 100, 'bob': 20}; 0 payments | {'alice': 90, 'bob': 30}; 1 payments | `e294a5fc-247d-5399-83a4-884a1bd79562` |
| after_commit | {'alice': 90, 'bob': 30}; 1 payments | {'alice': 90, 'bob': 30}; 1 payments | `1faa6562-351d-5ed7-9d4c-a30f4afc6448` |
| insufficient_funds | {'alice': 5, 'bob': 20}; 0 payments | {'alice': 5, 'bob': 20}; 0 payments | `rejected` |
| horizon | {'alice': 100, 'bob': 20}; 0 payments | {'alice': 100, 'bob': 20}; 0 payments | `pending` |

Each case has its own run and accounts. The two accepted cases do not repay one debt twice. A completion step is replayed eight times and call IDs are retried without changing the native payment count.

## before_commit

Run: `55678efb-2f01-48b9-ac0b-9faaac8cdd5c`; operation: `e294a5fc-247d-5399-83a4-884a1bd79562`; due simulated second: 8.
Final operation state: `applied`; final Clock second: 8.

Native bank notice `98c603cf-bcb5-578a-b035-aa6858986e6d`:

alice transferred 10 simulated credits. Memo: Lunch repayment. Receipt: e294a5fc-247d-5399-83a4-884a1bd79562.

The full private export retains the Actor job and grant, scoped inputs/outputs, Clock operations, native ledger rows, bank notice and timed-operation/call receipts. The test schema is removed after export; these run IDs are not console URLs.

## after_commit

Run: `52b71ab3-6f53-4445-981b-d0a9a072339e`; operation: `1faa6562-351d-5ed7-9d4c-a30f4afc6448`; due simulated second: 8.
Final operation state: `applied`; final Clock second: 8.

Native bank notice `bcb7ca20-ecef-5da7-8d96-10416263e0cc`:

alice transferred 10 simulated credits. Memo: Lunch repayment. Receipt: 1faa6562-351d-5ed7-9d4c-a30f4afc6448.

The full private export retains the Actor job and grant, scoped inputs/outputs, Clock operations, native ledger rows, bank notice and timed-operation/call receipts. The test schema is removed after export; these run IDs are not console URLs.

## insufficient_funds

Run: `e69378bf-bc9f-4a97-9a4c-a51140ec6904`; operation: `9f5c56d1-6052-5925-bfe9-249dc72d67f7`; due simulated second: 8.
Final operation state: `rejected`; final Clock second: 8.

The full private export retains the Actor job and grant, scoped inputs/outputs, Clock operations, native ledger rows, bank notice and timed-operation/call receipts. The test schema is removed after export; these run IDs are not console URLs.

## horizon

Run: `244a801e-9ff3-425b-aed9-f2147cc8e1ff`; operation: `662054f5-b80d-52ff-851e-bf1d5a0bd304`; due simulated second: 8.
Final operation state: `pending`; final Clock second: 5.

The full private export retains the Actor job and grant, scoped inputs/outputs, Clock operations, native ledger rows, bank notice and timed-operation/call receipts. The test schema is removed after export; these run IDs are not console URLs.
