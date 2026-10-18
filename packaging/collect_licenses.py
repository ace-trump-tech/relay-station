"""Collect runtime package notices for redistribution; run in the build venv."""
import importlib.metadata as metadata
import sys
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ["fastapi", "uvicorn", "cryptography", "httpx", "tzdata", "pywebview", "pystray", "Pillow"]


def main():
    pending, seen, sections = list(SEEDS), set(), []
    while pending:
        name = pending.pop()
        dist = metadata.distribution(name)
        name = dist.metadata["Name"].lower()
        if name in seen:
            continue
        seen.add(name)
        parts = [f"{dist.metadata['Name']} {dist.version}",
                 "License: " + (dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See license files below")]
        for file in dist.files or []:
            lower = str(file).lower()
            if ".dist-info/" in lower and ("/licenses/" in lower or Path(lower).name.startswith(("license", "copying", "notice"))):
                path = Path(dist.locate_file(file))
                if path.is_file():
                    parts += ["\n--- " + str(file) + " ---", path.read_text(encoding="utf-8", errors="replace")]
        sections.append("\n".join(parts))
        for requirement in dist.requires or []:
            item = Requirement(requirement)
            if item.marker is None or item.marker.evaluate({"extra": ""}):
                pending.append(item.name)
    for candidate in [Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE_PYTHON.txt",
                      Path(sys.base_prefix) / "Library/lib/tcl8.6/license.terms",
                      Path(sys.base_prefix) / "Library/lib/tk8.6/license.terms"]:
        if candidate.exists():
            sections.append(candidate.name + "\n" + candidate.read_text(encoding="utf-8", errors="replace"))
    output = ROOT / "packaging/THIRD_PARTY_LICENSES.txt"
    output.write_text("Relay runtime third-party notices\n\n" + ("\n\n" + "="*76 + "\n\n").join(sections), encoding="utf-8")
    print(f"Collected notices for {len(seen)} runtime distributions.")


if __name__ == "__main__":
    main()
