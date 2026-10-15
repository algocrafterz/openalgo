"""Behaviour of .claude/portable/bootstrap.sh, run hermetically.

The script derives the repo root from its own location, so each test copies the portable
directory into a throwaway repo layout and points CLAUDE_HOME at a throwaway home. The real
repository and ~/.claude are never read or written. The plugin step is skipped and the
`claude` / `uv` binaries are stubbed so the result does not depend on the machine.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REAL_PORTABLE = Path(__file__).resolve().parents[2] / ".claude" / "portable"
AGENTS = Path("rules/common/agents.md")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="bootstrap.sh needs bash and python3",
)


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(REAL_PORTABLE, repo / ".claude" / "portable")
    (repo / "CLAUDE.md").write_text("project rules\n")
    skill = repo / ".claude" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "uv"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\necho stub 0.0.0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    home = tmp_path / "home"
    home.mkdir()
    return {
        "script": repo / ".claude" / "portable" / "bootstrap.sh",
        "source": repo / ".claude" / "portable" / "user",
        "home": home,
        "path": f"{bin_dir}:{os.environ['PATH']}",
    }


def run(env, *args):
    result = subprocess.run(
        ["bash", str(env["script"]), *args, "--skip-plugin"],
        env={"PATH": env["path"], "CLAUDE_HOME": str(env["home"]), "HOME": str(env["home"])},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def to_crlf(path: Path) -> None:
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def source_digests(env) -> dict[str, str]:
    return {str(p): digest(p) for p in sorted(env["source"].rglob("*")) if p.is_file()}


class TestSetup:
    def test_fresh_home_is_populated_then_checks_clean(self, env):
        code, out = run(env, "--check")
        assert code == 1 and "missing rules/common/agents.md" in out

        code, out = run(env, "--apply")
        assert code == 0, out
        assert (env["home"] / AGENTS).is_file()
        assert (env["home"] / "CLAUDE.md").is_file()

        code, out = run(env, "--check")
        assert code == 0 and "0 failure(s)" in out

    def test_a_second_apply_changes_nothing(self, env):
        run(env, "--apply")
        before = {str(p): digest(p) for p in env["home"].rglob("*") if p.is_file()}
        code, out = run(env, "--apply")
        assert code == 0
        assert "installed rules" not in out and "replaced" not in out
        after = {str(p): digest(p) for p in env["home"].rglob("*") if p.is_file()}
        assert after == before


class TestLineEndings:
    def test_crlf_copy_of_identical_content_is_not_drift(self, env):
        run(env, "--apply")
        installed = [p for p in env["home"].rglob("*.md") if "rules" in p.parts]
        for path in installed:
            to_crlf(path)
        crlf_bytes = {str(p): p.read_bytes() for p in installed}

        code, out = run(env, "--check")
        assert code == 0, out
        assert "differs" not in out

        # an apply (with force) must not rewrite or normalize an equivalent file
        code, out = run(env, "--apply", "--force")
        assert code == 0 and "replaced" not in out
        assert {str(p): p.read_bytes() for p in installed} == crlf_bytes

    def test_a_crlf_source_matches_an_lf_install(self, env):
        run(env, "--apply")
        for path in env["source"].rglob("*.md"):
            to_crlf(path)
        code, out = run(env, "--check")
        assert code == 0 and "differs" not in out

    def test_the_comparison_never_modifies_the_source_files(self, env):
        before = source_digests(env)
        run(env, "--apply")
        for path in (env["home"] / "rules").rglob("*.md"):
            to_crlf(path)
        run(env, "--check")
        run(env, "--apply", "--force")
        assert source_digests(env) == before


class TestDriftProtection:
    def test_genuine_content_difference_is_still_reported(self, env):
        run(env, "--apply")
        target = env["home"] / AGENTS
        target.write_text(target.read_text() + "local edit\n")
        code, out = run(env, "--check")
        assert code == 1
        assert f"differs {AGENTS}" in out

    def test_crlf_does_not_hide_a_genuine_difference(self, env):
        run(env, "--apply")
        target = env["home"] / AGENTS
        target.write_text(target.read_text() + "local edit\n")
        to_crlf(target)
        code, out = run(env, "--check")
        assert code == 1 and f"differs {AGENTS}" in out

    def test_apply_leaves_a_drifted_file_alone(self, env):
        run(env, "--apply")
        target = env["home"] / AGENTS
        target.write_text(target.read_text() + "local edit\n")
        drifted = target.read_bytes()
        code, _ = run(env, "--apply")
        assert code == 1
        assert target.read_bytes() == drifted

    def test_force_replaces_and_backs_up_the_drifted_file(self, env):
        run(env, "--apply")
        target = env["home"] / AGENTS
        target.write_text(target.read_text() + "local edit\n")
        drifted = target.read_bytes()
        code, out = run(env, "--apply", "--force")
        assert code == 0 and f"replaced {AGENTS}" in out
        assert b"local edit" not in target.read_bytes()
        backups = list((env["home"] / "backups").glob(f"portable-*/{AGENTS}"))
        assert len(backups) == 1 and backups[0].read_bytes() == drifted
