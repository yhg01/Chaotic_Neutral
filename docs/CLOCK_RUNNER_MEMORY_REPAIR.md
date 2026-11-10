# Public Clock runner and private agenda repair

This update closes the public runner configuration gap and reproduces the
[E-62 short-run findings](ACTOR_AGENT_MEMORY_PROBE.md) with supplied responses.
The elapsed Clock, model IDs, provider routes, API files and prior evidence stay
unchanged. No new model experiment or seven-day run is authorized by this guide.

## Runner settings

- `--budget-usd 25` is the default for new invocations. It is the expected ceiling
  of the **existing** campaign `635aa42e-386f-43dc-b679-b73cd0cd276f`, not a
  separate budget. The runner refuses a value above USD25 or a mismatch with
  the stored ceiling. It does not create a campaign, change its limit or clear
  historical and uncertain charges. Each live reservation checks the expected
  ceiling again while holding the campaign row lock.
- `--actor-memory` opts a new elapsed world into the existing private memory
  store and agenda output. The default for new worlds is off. The profile and
  budget are included in frozen execution configuration. Resume inherits them
  and refuses a conflicting flag or missing saved profile. It preserves the
  existing credential manifest and writes a new invocation report.
- `--prepare FILE` writes the selected configuration without credentials,
  PostgreSQL or provider access. `--preflight FILE` checks the existing ledger
  before fetching price metadata and does not generate output or migrate the
  database. A live invocation remains a separate operation.
- Older manifests keep their original evidence and source/configuration
  checks. Do not edit an old manifest to make it pass the new runner.

## Declare a private agenda without delegating it

For a new one-day `--scenario` with `--actor-memory`, `--private-cues FILE`
accepts an array of [typed Clock cues](experiments/actor-agent-memory-repair-cues.json).
Each cue targets Actor lanes, stays inside the existing Clock horizon, and
separates two inputs:

1. `payload.text` describes the owner's situation and any genuine request for
   the assistant. It must not contain private cognition instructions.
2. `payload.private_agenda` is the exact structured agenda the **Actor** must
   return in `cognition.agendas` for the declared UTC day. The current or next
   day is permitted. `slots` describes the complete revision for that day; an
   explicitly empty revision clears it. If several pending cues address one day,
   the latest delivered declaration for that day is authoritative.

`owner_response_required: true` independently requires spoken owner dialogue.
An agenda entry alone cannot satisfy that requirement, and spoken dialogue
alone cannot satisfy an agenda declaration. The requirement is extracted only
from the saved private cue and frozen in the Actor's memory context. It is not
copied to the personal Agent's activation. A required agenda is rejected when
missing, on the wrong day, or different from the declared slots. The runtime
does not invent or fill model output. Cues without this declaration keep
optional cognition.

The existing reviewer call now separately checks structured cognition and
the private-planning boundary. An absent or negative planning verdict is a
failed review, even if its overall acceptance is true. A deterministic check
rejects the known private planning delegation before delivery. For a
memory-enabled public run, Agent owner-message and finish/wait tools reject
explicit unsupported private cognition completion claims and return a repair
error. Legitimate workspace-file writes, readbacks and reports continue through
the same graph and tools. These checks cover the reproduced failure class;
they do not establish general natural-language claim verification.

## Free validation

Use a separate PostgreSQL test database whose name contains `test`:

```sh
uv run python scripts/test_local.py tests/test_clock_runner_memory.py
```

The integration case uses the public runner, a real PostgreSQL ledger and
checkpoints, saved Actor/reviewer fixtures and the real local Agent graph.
It writes and reads the initial Birch Room file, applies the Willow Room
revision, and confirms that the Actor receives both actual reports. It injects
the missing agenda, wrong delegation and unsupported Agent claims and verifies
that the rejected outputs do not reach owner dialogue. Separate cases reject
stale agenda slots and incomplete or negative review verdicts. Resume preserves
the original manifest and trajectory. The recorded prior real run still has
strict acceptance false; this correction has free local evidence only.

See [verification and preservation](clock-runner-memory-repair-results.json).
