"""Install the pinned public Argos Chinese → English model for offline viewing.

Only the model is downloaded. Saved runs, dialogue and provider keys stay local.
"""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = "https://argos-net.com/v1/translate-zh_en-1_9.argosmodel"
SHA256 = "62e7af5a3a48b530e47b7b3e5c78c2de79073ecd815750d2bf3ab35b4a67da2d"


def main():
    folder = ROOT / ".local/translation-models"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "translate-zh_en-1_9/model/model.bin"
    if target.is_file():
        print("Local English model is already installed.")
        return
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get(URL)
        response.raise_for_status()
    content = response.content
    if hashlib.sha256(content).hexdigest() != SHA256:
        raise RuntimeError("The model checksum changed; no files were installed.")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for item in archive.infolist():
            destination = (folder / item.filename).resolve()
            if not destination.is_relative_to(folder.resolve()) or item.file_size > 150_000_000:
                raise RuntimeError("Invalid model archive member.")
        archive.extractall(folder)
    (folder / "zh-en-source.json").write_text(
        json.dumps({"source": URL, "sha256": SHA256, "bytes": len(content)}, indent=2) + "\n"
    )
    print("Installed offline Chinese → English model. No dialogue was transmitted.")


if __name__ == "__main__":
    main()
