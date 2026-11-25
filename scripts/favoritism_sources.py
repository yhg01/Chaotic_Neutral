"""Verify the frozen native corpus before preparing, executing or grading requests."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/favoritism-source-manifest.json"


def verify_sources(sealed, routing, manifest=MANIFEST):
    files = json.loads(manifest.read_bytes())
    if not sealed.is_dir():
        raise ValueError(f"Missing sealed-bid source directory: {sealed}")
    verified = {}
    for name, expected in files.items():
        path = Path(name)
        if path.is_relative_to("../sealed-bid-controls/.local"):
            actual = sealed / path.relative_to("../sealed-bid-controls/.local")
        else:
            actual = routing / path.relative_to(".local")
        if not actual.is_file():
            raise ValueError(f"Missing source trace: {actual}")
        if hashlib.sha256(actual.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Source trace SHA-256 mismatch: {actual}")
        verified[str(actual.resolve())] = expected
    return verified


def verify_study(study):
    keys = json.loads((study / "private-key.json").read_bytes())
    frozen = verify_sources(ROOT.parent / "sealed-bid-controls/.local", ROOT / ".local")
    used = [m for k in keys.values() for m in k["mapping"].values()]
    if (
        len(used) != len(frozen)
        or {str((Path(m["path"]) / "trajectory.json").resolve()): m["trace_sha256"] for m in used}
        != frozen
    ):
        raise ValueError("Study mapping differs from the committed source manifest.")
