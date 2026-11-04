"""Reject altered release archives without installing or extracting them."""
import importlib.util
import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("p4_package_check", ROOT / "scripts/p4_package_check.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def archives(tmp_path):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    stem = "slack_codex_bridge-" + version
    modules = list((ROOT / "src/slack_codex_bridge").glob("*.py"))
    files = ["README.md", "bridge.example.json", "requirements.lock", "requirements-dev.lock",
             "pyproject.toml", "MANIFEST.in", "docs/operations.md",
             "docs/architecture.md", "docs/verification.md", "docs/user-guide.zh-CN.md",
             "scripts/p4_package_check.py", "scripts/launch_supervisor.pyw",
             "scripts/install_supervisor_shortcut.ps1"]
    files += ["src/slack_codex_bridge/" + p.name for p in modules]
    wheel = tmp_path / (stem + "-py3-none-any.whl")
    with zipfile.ZipFile(wheel, "w") as z:
        for p in modules:
            z.writestr("slack_codex_bridge/" + p.name, p.read_bytes())
        for name in ("METADATA", "WHEEL", "RECORD", "top_level.txt"):
            z.writestr(stem + ".dist-info/" + name, "fixture\n")
        z.writestr(stem + ".dist-info/entry_points.txt",
                   "[console_scripts]\nbridge = slack_codex_bridge.cli:main\n"
                   "[gui_scripts]\nbridge-background = slack_codex_bridge.cli:main\n"
                   "bridge-supervisor = slack_codex_bridge.supervisor:main\n")
    contents = {stem + "/" + f: (ROOT / f).read_bytes() for f in files}
    return tmp_path, stem, wheel, contents


def write_source(directory, stem, contents, extras=()):
    with tarfile.open(directory / (stem + ".tar.gz"), "w:gz") as t:
        for name, data in list(contents.items()) + list(extras):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


def test_valid_archives(archives):
    directory, stem, _, contents = archives
    write_source(directory, stem, contents)
    assert checker.verify(ROOT, directory)["entry_points_verified"]


def test_reject_extra_wheel_startup_file(archives):
    directory, stem, wheel, contents = archives
    write_source(directory, stem, contents)
    with zipfile.ZipFile(wheel, "a") as z:
        z.writestr(stem + ".data/purelib/unexpected.pth", "# inert fixture; never executed\n")
    with pytest.raises(ValueError, match="wheel_.*inventory"):
        checker.verify(ROOT, directory)


@pytest.mark.parametrize("relative", ["pyproject.toml", "MANIFEST.in", "docs/verification.md"])
def test_reject_changed_build_or_acceptance_input(archives, relative):
    directory, stem, _, contents = archives
    contents[stem + "/" + relative] += b"\n# altered fixture\n"
    write_source(directory, stem, contents)
    with pytest.raises(ValueError, match="sdist_required_file_mismatch"):
        checker.verify(ROOT, directory)


@pytest.mark.parametrize("name", ["docs\\..\\outside.txt", ".GIT/config", ".WORK-private/evidence.txt",
                                  "docs/./alias.txt", "docs//alias.txt", "Token.env."])
def test_reject_windows_aliases_and_hidden_evidence(name):
    assert not checker.safe_name(name)


def test_reject_duplicate_wheel_member(archives):
    directory, stem, wheel, contents = archives
    write_source(directory, stem, contents)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(wheel, "a") as z:
            z.writestr(stem + ".dist-info/METADATA", "fixture\n")
    with pytest.raises(ValueError, match="wheel_duplicate_member"):
        checker.verify(ROOT, directory)


@pytest.mark.parametrize("extra", ["duplicate", "case", "outside", "fifo", "setup"])
def test_reject_ambiguous_or_executable_source_members(archives, extra):
    directory, stem, _, contents = archives
    extras = []
    if extra == "duplicate":
        extras = [(stem + "/README.md", contents[stem + "/README.md"])]
    elif extra == "case":
        extras = [(stem + "/readme.md", b"altered case-colliding fixture\n")]
    elif extra == "outside":
        extras = [("another-root/fixture.txt", b"inert\n")]
    elif extra == "setup":
        extras = [(stem + "/setup.py", b"# inert fixture; never executed\n")]
    write_source(directory, stem, contents, extras)
    if extra == "fifo":
        source = directory / (stem + ".tar.gz")
        with tarfile.open(source) as t:
            members = [(m, t.extractfile(m).read()) for m in t.getmembers()]
        with tarfile.open(source, "w:gz") as t:
            for member, data in members:
                t.addfile(member, io.BytesIO(data))
            info = tarfile.TarInfo(stem + "/fixture-pipe")
            info.type = tarfile.FIFOTYPE
            t.addfile(info)
    with pytest.raises(ValueError, match="sdist_"):
        checker.verify(ROOT, directory)
