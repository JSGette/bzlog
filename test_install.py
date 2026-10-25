"""Exercise installed commands and safe, repeatable project instruction updates."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import bzlog

SOURCE = Path(__file__).resolve().parent


@unittest.skipUnless(os.name == "posix", "installer targets Linux/macOS/WSL")
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Quotes, spaces, and shell metacharacters must remain literal path bytes.
        self.prefix = self.root / "tool's prefix $(touch INJECTED)"
        self.project = self.root / "my project"
        self.project.mkdir()
        self.command = self.prefix / "bin" / "bzlog"

    def install(self, *args):
        return subprocess.run([sys.executable, str(SOURCE / "install.py"),
                               "--prefix", str(self.prefix), *args],
                              cwd=self.root, text=True, capture_output=True, timeout=15)

    def all_agents(self, *extra):
        return self.install("--project", str(self.project), "--agent", "all", *extra)

    def invoke(self, *args):
        return subprocess.run([str(self.command), *args], cwd=self.root,
                              env=dict(os.environ, PYTHONPATH=""), text=True,
                              capture_output=True, timeout=15)

    def test_standalone_command_version_help_and_companion_module(self):
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.invoke("--version")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), f"bzlog {bzlog.__version__}")
        p = self.invoke("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("exec-log", p.stdout)
        p = self.invoke("exec-log", str(SOURCE / "testdata/bazel-9.3/execlog/baseline.json"),
                        "--input-format", "json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["schema"], "bzlog/exec-log-v1")
        self.assertFalse((self.root / "INJECTED").exists())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_all_agents_preserve_existing_crlf_and_are_idempotent(self):
        instructions = self.project / "AGENTS.md"
        before = b"# Existing instructions\r\n\r\nKeep these rules.\r\n"
        instructions.write_bytes(before)
        instructions.chmod(0o600)
        p = self.all_agents()
        self.assertEqual(p.returncode, 0, p.stderr)
        rules = [instructions, self.project / ".claude/rules/bzlog.md",
                 self.project / ".cursor/rules/bzlog.mdc"]
        saved = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in rules}
        self.assertTrue(instructions.read_bytes().startswith(before))
        self.assertNotIn(b"\n", instructions.read_bytes().replace(b"\r\n", b""))
        self.assertEqual(instructions.stat().st_mode & 0o777, 0o600)
        self.assertTrue(rules[2].read_text().startswith("---\ndescription:"))
        self.assertIn("alwaysApply: true\n---", rules[2].read_text())
        for path in rules:
            self.assertEqual(path.read_text().count("<!-- bzlog:begin -->"), 1)
            self.assertIn("bzlog exec-log", path.read_text())
        p = self.all_agents()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("0 files changed", p.stdout)
        self.assertEqual(saved, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in rules})

    def test_rule_upgrade_changes_only_managed_body(self):
        path = self.project / "AGENTS.md"
        path.write_text("Before\n<!-- bzlog:begin -->\nOld rule\n<!-- bzlog:end -->\nAfter\n")
        p = self.all_agents()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(path.read_text().startswith("Before\n<!-- bzlog:begin -->\n"))
        self.assertTrue(path.read_text().endswith("<!-- bzlog:end -->\nAfter\n"))
        self.assertNotIn("Old rule", path.read_text())

    def test_codex_existing_override_gets_effective_rule(self):
        base = self.project / "AGENTS.md"
        base.write_text("Base instructions\n")
        override = self.project / "AGENTS.override.md"
        override.write_text("Override instructions\n")
        p = self.install("--agent", "codex", "--project", str(self.project))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(base.read_text(), "Base instructions\n")
        self.assertIn("<!-- bzlog:begin -->", override.read_text())
        self.assertFalse((self.project / ".claude").exists())

    def test_dry_run_creates_nothing(self):
        p = self.all_agents("--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Would write", p.stdout)
        self.assertFalse(self.prefix.exists())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_conflict_preflight_leaves_all_files_unchanged(self):
        conflict = self.project / ".cursor/rules/bzlog.mdc"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("My custom bzlog rule\n")
        p = self.all_agents()
        self.assertEqual(p.returncode, 2)
        self.assertIn("unmanaged rule", p.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertFalse((self.project / "AGENTS.md").exists())
        self.assertEqual(conflict.read_text(), "My custom bzlog rule\n")

    def test_malformed_markers_and_custom_frontmatter_are_preserved(self):
        path = self.project / "AGENTS.md"
        path.write_text("<!-- bzlog:begin -->\nUnclosed block\n")
        p = self.all_agents()
        self.assertEqual(p.returncode, 2)
        self.assertIn("Malformed", p.stderr)
        self.assertFalse(self.prefix.exists())
        path.unlink()
        self.assertEqual(self.all_agents().returncode, 0)
        cursor = self.project / ".cursor/rules/bzlog.mdc"
        changed = cursor.read_text().replace("alwaysApply: true", "alwaysApply: false")
        cursor.write_text(changed)
        p = self.all_agents()
        self.assertEqual(p.returncode, 2)
        self.assertIn("frontmatter", p.stderr)
        self.assertEqual(cursor.read_text(), changed)

    def test_symlinked_rule_parent_is_not_followed(self):
        outside = self.root / "external rules"
        outside.mkdir()
        (self.project / ".claude").symlink_to(outside, target_is_directory=True)
        p = self.all_agents()
        self.assertEqual(p.returncode, 2)
        self.assertIn("symlink", p.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_fifo_rule_is_rejected_without_reading(self):
        os.mkfifo(self.project / "AGENTS.md")
        p = self.all_agents()
        self.assertEqual(p.returncode, 2)
        self.assertIn("regular file", p.stderr)

    def test_unknown_command_is_preserved(self):
        self.command.parent.mkdir(parents=True)
        self.command.write_text("Custom launcher\n")
        p = self.install()
        self.assertEqual(p.returncode, 2)
        self.assertIn("unmanaged command", p.stderr)
        self.assertEqual(self.command.read_text(), "Custom launcher\n")
        self.assertFalse((self.prefix / "lib").exists())

    def test_project_selection_requires_both_options(self):
        for args in [("--agent", "codex"), ("--project", str(self.project))]:
            with self.subTest(args=args):
                self.assertEqual(self.install(*args).returncode, 2)
        self.assertFalse(self.prefix.exists())

    def test_shell_entrypoint_works_outside_source_directory(self):
        source = self.root / "source with spaces"
        source.mkdir()
        for name in ("install.sh", "install.py", "bzlog.py", "bzlog_execlog.py", "LICENSE"):
            shutil.copy2(SOURCE / name, source / name)
        p = subprocess.run(["sh", str(source / "install.sh"), "--prefix", str(self.prefix)],
                           cwd=self.project, env=dict(os.environ, PYTHON=sys.executable),
                           capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        shutil.rmtree(source)
        self.assertEqual(self.invoke("--version").returncode, 0)

    def test_help_and_version_after_bazel_boundary_are_forwarded(self):
        for argv in (["test", "//:test", "--help"], ["run", "//:app", "--", "--version"],
                     ["--", "--version"], ["--output_base=/tmp/bzlog-test", "--help"]):
            own, forwarded = bzlog.split_argv(argv)
            self.assertEqual(own, [])
            self.assertEqual(forwarded, argv[1:] if argv[0] == "--" else argv)


if __name__ == "__main__":
    unittest.main()
