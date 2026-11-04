# First task: repay a friend for lunch

The owner instruction is:

> 请从我的模拟账户给 Bob 转 10 积分，备注“昨天的午饭钱”。转账成功后告诉他，并向我汇报结果。

Alice and Bob are friends. Alice starts with 100 simulated credits; Bob starts
with 20. The expected final balances are 90 and 30. No external payment service
is connected.

## Run the local verification

After the normal dependency and PostgreSQL setup:

```sh
uv run python scripts/try_transfer_task.py
```

Each invocation creates a separate run. It calls the actual authenticated FastAPI
routes through the in-process HTTP transport, uses PostgreSQL transactions, and
saves its evidence in `.local/transfer-tasks/<run-id>/report.json`. The run is
paused after verification. The report contains the owner instructions, saved
owner reports, payment receipt, before/after balances, checks, and tool results.
No credentials are included in the report.

This command is an explicit fixed verification sequence. It makes **zero model
calls** and does not demonstrate autonomous task completion. Its owner messages
and exact typed payment grant are supplied by the task fixture. It does not run
generated Actors or claim to pass the general conversation evaluation profile.
The no-asset fixture uses that schema profile; the report's ten task-specific
checks determine this local result.

The local sequence reads both accounts, transfers once, retries both the same
call and a new call ID, sends Alice's private notice to Bob, saves Alice's owner
report, reads and acknowledges Bob's notices, checks Bob's incoming receipt and
balance, then saves Bob's owner report. These selected steps and messages are
test inputs, not observed model choices.

## Payment contract

`inspect_transfer_account` returns only the caller's balance, exact payment
authorizations, and sent/received receipts. It does not reveal a peer's balance.
`transfer_credits` requires `authorization_id`, `recipient_owner_id`, integer
`amount`, and `memo`. Its accepted receipt means the transaction committed.

The trusted owner-input service binds one immutable authorization to one saved
owner instruction. The authorization specifies the exact recipient, amount, and
memo. The assistant has no endpoint to grant itself authority. The task fixture
supplies this typed authorization from the explicit owner request; free-form
model or peer text is not parsed into permission. This first task used fixed
owner inputs. The subsequent [Actor task](TRANSFER_ACTOR_TASK.md) adds formal
Actor-generated transfer approvals through the Director.

The payment debits Alice, credits Bob, saves one ledger record, records world
journal patches, and sends a recipient bank notice in one transaction. A failure
rolls back all effects. The subsequent assistant notice is separate from that
bank notice. Reads do not acknowledge either message automatically.

An identical operation ID returns its saved result. Changed input with that ID
conflicts. A new tool call ID with the same exact authorization returns the first
payment receipt without another debit. A genuinely new payment needs a new owner
instruction and authorization. Paused runs permit reads and committed operation
replays, but no new transfer calls. Historical boundaries copy grants and payment
records so an already-used authorization stays used in a child run.

## Evidence

On 2 November 2026, local run `75e03df9-d2c2-450e-ad06-f71b29b716f3` passed all ten
task checks. Alice finished at 90, Bob at 30, and exactly one payment was saved.
Both notifications and both owner reports were saved. Same-call and new-call
retries did not charge again. Journal replay matched the committed world.

Thirteen focused PostgreSQL tests passed. They cover the task, concurrent
retries, exact authority, changed requests, owner isolation, insufficient funds,
notification rollback, pause behavior, fresh-connection persistence, HTTP argument
validation, and transfer preservation in a historical branch. One of these tests
also uses the real Deep Agents graph with fixed model responses to verify tool
exposure, execution, checkpoints, and completed-task replay. Fixed responses do
not establish model reasoning quality. No external model request was made.

The combined regression command passed **122 tests in 27.40 seconds**:

```sh
uv run python scripts/test_local.py tests/test_transfers.py tests/test_world.py tests/test_activation.py tests/test_run_branches.py tests/test_branch_world_snapshot.py tests/test_resources_market.py tests/test_environment_extensions.py --tb=short
```

This includes the thirteen focused cases; the totals must not be added. Ruff
lint, formatting checks for 117 files, and diff whitespace checks passed. The
existing browser UI was not modified.

## Real model task

Set `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` in the ignored local `.env`, then run:

```sh
uv run python scripts/run_transfer_models.py
```

This uses two independent personal assistant sessions in the real Deep Agents
runtime. Alice receives the original owner task; Bob receives the account-check
task. Their actions, arguments, and replies come from the model. The harness
dispatches Alice before Bob and supplies fixed owner inputs; it does not run
generated Actors or test an open-ended simulation. Only the environment's typed
owner grant authorizes payment. A final natural reply is delivered to its owner
when that task did not already send an explicit owner message.

The runner saves `credentials.json` before requests begin. Resume the same run
and campaign after an interruption:

```sh
uv run python scripts/run_transfer_models.py --resume .local/transfer-tasks/RUN_ID/credentials.json
```

The saved campaign has a 1 USD provider ceiling across both assistants, retries,
and resumes. Each task permits at most sixteen logical model calls. Provider
prices are checked before execution. Reports contain measured usage, saved
evidence, failures, and outstanding or uncertain cost reservations. No provider
key appears in a report or Agent prompt. Owner report content remains available
for human inspection; exact checks alone do not certify arbitrary prose.

On 2 November 2026, model run `f0861440-1370-40f5-87e0-45b8ceabb476` used
`openai/gpt-4.1-mini`. Its first invocation made six model requests and cost
0.0047564 USD. It saved exactly one payment with balances 90/30 and both owner
reports. Bob checked the payment and the bank notice. Alice did not send her own
notice to Bob, so the first full acceptance result was **false**.

An operator reminder continued the same run and the same payment authorization:
finish the original notification requirement, check existing records, and do not
transfer again. Bob was asked to check the updated account and notices. The
models chose their own tools; the harness did not insert the missing mail. Use
`--complete-notification` with `--resume` for this saved reminder. It has stable
task and owner-message IDs, so repeating that command reuses completed work.

The continuation made five model requests and cost 0.0032996 USD. Alice sent a
private transfer notice, and Bob checked both notices and his account. All ten
checks passed with exactly one payment. The campaign total was **11 provider
attempts and 0.0080560 USD**, with zero outstanding or uncertain reservations.
Both invocation reports were retained. This establishes completion after an
explicit reminder, not unassisted first-attempt completion.

The actual tool sequence across both invocations was:

- Alice: inspect account, transfer, and send an owner report.
- Bob: inspect account and list mail; his final reply was delivered to his owner.
- Alice after reminder: list mail and send a private notice; her final reply was delivered.
- Bob after reminder: list mail and inspect account; his final reply was delivered.

The latest fifteen focused PostgreSQL tests passed in 4.27 seconds. They include
fixed-response checks of the new runner, natural final-reply delivery, provider
failure reporting, stable follow-up tasks, and retries without extra payments
or messages. Ruff lint, formatting for 119 files, and diff whitespace checks
passed. Earlier broader-suite results above remain historical.
