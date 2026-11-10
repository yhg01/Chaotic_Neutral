"""Freeze the mixed protocol without loading credentials or launching a run."""

import argparse
import json
from pathlib import Path

from agentrpg.medium_chaos import MediumChaosPlan
from agentrpg.simulation_clock import digest
from agentrpg.trial_audit import source_fingerprint


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    definition = MediumChaosPlan().manifest()
    document = {
        "definition": definition,
        "definition_hash": digest(definition),
        "preparation_source_hash": source_fingerprint(Path(__file__).resolve().parents[1]),
        "artifact_scope": "Operator definition with simulated private premises; "
        "never supply this complete file to an Actor or Agent.",
        "paid_execution_authorized": False,
    }
    if args.output.exists():
        if json.loads(args.output.read_text()) != document:
            raise SystemExit("Frozen output differs. Preserve it and select a new version/path.")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(
        json.dumps(
            {
                "definition_hash": document["definition_hash"],
                "paid_execution_authorized": False,
                "provider_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
