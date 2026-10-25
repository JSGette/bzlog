#!/usr/bin/env python3
"""Install bzlog and optional project instructions on Linux, macOS, and WSL."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

if sys.version_info < (3, 10):
    raise SystemExit("bzlog requires Python 3.10 or newer")

from bzlog import __version__

SOURCE = Path(__file__).resolve().parent
BEGIN = "<!-- bzlog:begin -->"
END = "<!-- bzlog:end -->"
LAUNCHER_MARKER = "# Installed by bzlog install.py"
CURSOR_HEADER = ('---\ndescription: Use bzlog for Bazel builds, tests, and cache investigations\n'
                 'alwaysApply: true\n---\n')


def existing_bytes(path: Path) -> bytes | None:
    """Reject non-regular targets and symlinked parent directories before writes."""
    for candidate in (path, *path.parents):
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"Refusing symlink: {candidate}")
        if candidate == path:
            if not stat.S_ISREG(mode):
                raise ValueError(f"Expected a regular file: {path}")
        elif not stat.S_ISDIR(mode):
            raise ValueError(f"Expected a directory: {candidate}")
    return path.read_bytes() if path.exists() else None


def managed_rule(path: Path, body: str, *, shared: bool = False,
                 header: str = "") -> bytes:
    original = existing_bytes(path)
    text = original.decode("utf-8") if original is not None else ""
    # Preserve existing line endings and every byte outside the managed block.
    newline = "\r\n" if "\r\n" in text else "\n"
    block = (BEGIN + "\n" + body.rstrip() + "\n" + END).replace("\n", newline)
    if BEGIN in text or END in text:
        if (text.count(BEGIN) != 1 or text.count(END) != 1
                or text.index(END) < text.index(BEGIN)):
            raise ValueError(f"Malformed bzlog instruction markers in {path}")
        if header and not text.startswith(header.replace("\n", newline)):
            raise ValueError(f"Customized Cursor frontmatter in {path}; reconcile it manually")
        start, stop = text.index(BEGIN), text.index(END) + len(END)
        return (text[:start] + block + text[stop:]).encode("utf-8")
    if original is not None and not shared:
        raise ValueError(f"Refusing to replace an unmanaged rule: {path}")
    separator = "" if not text else (newline if text.endswith(newline) else newline * 2)
    return (text + separator + header.replace("\n", newline) + block + newline).encode("utf-8")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bzlog-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", type=Path, default=Path.home() / ".local",
                    help="installation prefix (default: ~/.local)")
    ap.add_argument("--agent", action="append", choices=["codex", "claude", "cursor", "all"],
                    default=[], help="install project instructions; repeat for multiple agents")
    ap.add_argument("--project", type=Path, help="existing workspace root for selected agent rules")
    ap.add_argument("--dry-run", action="store_true", help="validate and list changes without writing")
    ap.add_argument("--version", action="version", version=f"bzlog installer {__version__}")
    args = ap.parse_args(argv)
    if os.name != "posix":
        ap.error("Run this installer inside WSL on Windows (or on Linux/macOS).")
    if bool(args.agent) != bool(args.project):
        ap.error("Use --agent and --project together; omit both to install only the command.")

    try:
        prefix = args.prefix.expanduser().resolve()
        library, launcher = prefix / "lib" / "bzlog", prefix / "bin" / "bzlog"
        # Keep one complete, standalone copy; sys.path then finds the companion module.
        files = {library / name: (SOURCE / name).read_bytes()
                 for name in ("bzlog.py", "bzlog_execlog.py", "LICENSE")}
        old_launcher = existing_bytes(launcher)
        if old_launcher is not None and not old_launcher.startswith(
                ("#!/bin/sh\n" + LAUNCHER_MARKER + "\n").encode()):
            raise ValueError(f"Refusing to replace an unmanaged command: {launcher}")
        files[launcher] = (f"#!/bin/sh\n{LAUNCHER_MARKER}\n"
                           f"exec {shlex.quote(sys.executable)} "
                           f"{shlex.quote(str(library / 'bzlog.py'))} \"$@\"\n").encode()
        if args.agent:
            project = args.project.expanduser().resolve(strict=True)
            if not project.is_dir():
                raise ValueError(f"Project is not a directory: {project}")
            body = (SOURCE / "rules" / "bzlog.md").read_text(encoding="utf-8")
            selected = set(args.agent)
            if "all" in selected:
                selected = {"codex", "claude", "cursor"}
            if "codex" in selected:
                override = project / "AGENTS.override.md"
                # Codex loads only the first nonempty instruction file at this level.
                override_data = existing_bytes(override)
                target = override if override_data and override_data.strip() else project / "AGENTS.md"
                files[target] = managed_rule(target, body, shared=True)
            if "claude" in selected:
                target = project / ".claude" / "rules" / "bzlog.md"
                files[target] = managed_rule(target, body)
            if "cursor" in selected:
                target = project / ".cursor" / "rules" / "bzlog.mdc"
                files[target] = managed_rule(target, body, header=CURSOR_HEADER)

        # Preflight every destination before modifying any of them.
        changes = []
        for path, data in files.items():
            previous = existing_bytes(path)
            mode = 0o755 if path == launcher else (
                stat.S_IMODE(path.stat().st_mode) if previous is not None else 0o644)
            if previous != data or (path == launcher and path.stat().st_mode & 0o111 != 0o111):
                changes.append((path, data, mode))
        for path, data, mode in changes:
            if not args.dry_run:
                atomic_write(path, data, mode)
            print(f"{'Would write' if args.dry_run else 'Wrote'} {path}")
        print(f"bzlog {__version__}: {'dry run complete' if args.dry_run else 'installation complete'}"
              f" ({len(changes)} files {'planned' if args.dry_run else 'changed'}).")
        print(f"Command: {shlex.quote(str(launcher))}")
        print(f"Ensure {shlex.quote(str(launcher.parent))} is on PATH in the agent's execution environment.")
        if args.agent:
            print("Restart the agent session to load project instructions; review any nested overrides.")
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"bzlog install: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
