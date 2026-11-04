"""Verify current release archives against source; never reads credentials."""

import argparse
import configparser
import hashlib
import json
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


def safe_name(name):
    path = PurePosixPath(name)
    parts = tuple(p.casefold() for p in path.parts)
    return (bool(parts) and "\\" not in name and str(path) == name.removesuffix("/")
            and not name.startswith("/")
            and all(p not in {"..", ".git", ".venv", "__pycache__"}
                    and not p.startswith((".work", ".env")) and ":" not in p
                    and not p.endswith((".env", ".pyc", ".", " ")) and ".sqlite" not in p
                    for p in parts))


def verify(root, directory):
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    stem = "slack_codex_bridge-" + version
    wheel = directory / (stem + "-py3-none-any.whl")
    source = directory / (stem + ".tar.gz")
    modules = list((root / "src/slack_codex_bridge").glob("*.py"))
    if not modules:
        raise ValueError("source_modules_missing")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len({n.casefold() for n in names}):
            raise ValueError("wheel_duplicate_member")
        if not all(safe_name(n) for n in names):
            raise ValueError("wheel_path_outside_release_scope")
        expected = {"slack_codex_bridge/" + p.name for p in modules}
        metadata = {stem + ".dist-info/" + n for n in
                    ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")}
        if (stem + ".dist-info/licenses/LICENSE") in names:
            metadata.add(stem + ".dist-info/licenses/LICENSE")
        if set(names) != expected | metadata:
            raise ValueError("wheel_module_inventory_mismatch")
        for module in modules:
            if archive.read("slack_codex_bridge/" + module.name) != module.read_bytes():
                raise ValueError("wheel_source_mismatch")
        entry = configparser.ConfigParser()
        entry.read_string(archive.read(stem + ".dist-info/entry_points.txt").decode())
        if (dict(entry["console_scripts"]) != {"bridge": "slack_codex_bridge.cli:main"}
                or dict(entry["gui_scripts"]) != {"bridge-background": "slack_codex_bridge.cli:main",
                                                 "bridge-supervisor": "slack_codex_bridge.supervisor:main"}):
            raise ValueError("release_entry_points_mismatch")
    required = ["README.md", "bridge.example.json", "requirements.lock", "requirements-dev.lock",
                "pyproject.toml", "MANIFEST.in", "docs/verification.md",
                "docs/operations.md", "docs/architecture.md", "docs/user-guide.zh-CN.md",
                "scripts/p4_package_check.py", "scripts/launch_supervisor.pyw",
                "scripts/install_supervisor_shortcut.ps1"] + ["src/slack_codex_bridge/" + p.name for p in modules]
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len({n.casefold() for n in names}):
            raise ValueError("sdist_duplicate_member")
        if not all(safe_name(m.name) and (m.isfile() or m.isdir())
                   and (m.name == stem or m.name.startswith(stem + "/")) for m in members):
            raise ValueError("sdist_path_outside_release_scope")
        for member in members:
            if member.isfile() and member.name.endswith(".py"):
                original = root / member.name.removeprefix(stem + "/")
                if not original.is_file() or archive.extractfile(member).read() != original.read_bytes():
                    raise ValueError("sdist_python_source_mismatch")
        for relative in required:
            stream = archive.extractfile(stem + "/" + relative)
            if stream is None or stream.read() != (root / relative).read_bytes():
                raise ValueError("sdist_required_file_mismatch")
    return {"status": "passed_local_package_check", "version": version, "wheel_modules_match": len(modules),
            "sdist_required_files_match": len(required), "entry_points_verified": True,
            "local_evidence_excluded": True, "live_acceptance_claimed": False,
            "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (wheel, source)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.root.resolve(), args.directory.resolve())
    data = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(data + "\n", encoding="utf-8")
    print(data)


if __name__ == "__main__":
    main()
