"""Verify the P2 candidate archives without reading credentials or contacting services."""

import argparse
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path


def verify(root: Path, directory: Path) -> dict:
    wheel, = directory.glob("*.whl")
    source, = directory.glob("*.tar.gz")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        modules = list((root / "src" / "slack_codex_bridge").glob("*.py"))
        for module in modules:
            assert archive.read("slack_codex_bridge/" + module.name) == module.read_bytes(), module.name
        entries, = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        text = archive.read(entries).decode()
        assert "[console_scripts]" in text and "[gui_scripts]" in text
        assert "bridge-background = slack_codex_bridge.cli:main" in text
    with tarfile.open(source) as archive:
        source_names = archive.getnames()
        for relative in ["p2-plan.md", "p2-handoff.md", "docs/p2-verification.md", "docs/p2-live-acceptance.md",
                         "bridge.example.json", "requirements.lock", "requirements-dev.lock"]:
            member, = [n for n in source_names if n.endswith("/" + relative)]
            assert archive.extractfile(member).read() == (root / relative).read_bytes(), relative
    for name in names + source_names:
        parts = name.replace("\\", "/").lower().split("/")
        assert not any(p.startswith((".work-", ".env")) or p in {".git", ".venv", "bridge.local.json"}
                       or p.endswith((".env", ".sqlite", ".sqlite3", ".pyc")) for p in parts), name
        assert not name.startswith(("/", "\\")) and ".." not in parts, name
    checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (wheel, source)}
    (directory / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in checksums.items()),
                                          encoding="ascii")
    return {"status": "passed", "wheel": wheel.name, "source": source.name,
            "source_modules_matched": len(modules), "archive_paths_clean": True,
            "console_and_gui_entries": True, "sha256": checksums,
            "external_calls": False, "runtime_switched": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    directory, output = args.directory.resolve(), args.output.resolve()
    assert directory.is_relative_to(root / "dist") and output.is_relative_to(root / ".work-p2")
    result = verify(root, directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
