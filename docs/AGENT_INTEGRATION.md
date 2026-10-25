# Coding-agent integration

bzlog 0.0.1 supplies the same Bazel workflow to ChatGPT Codex (OpenAI), Claude
Code, and Cursor. The instruction source is [`rules/bzlog.md`](../rules/bzlog.md).
The installer renders the format each agent expects instead of maintaining
three independent copies of the advice.

## Install

Run these commands from a checkout or extracted release inside Linux, macOS,
or WSL. Python 3.10+ is required. Windows users should install and execute
bzlog and Bazel inside the same WSL distribution.

```sh
./install.sh --agent all --project /path/to/bazel/workspace --dry-run
./install.sh --agent all --project /path/to/bazel/workspace
export PATH="$HOME/.local/bin:$PATH"
bzlog --version
```

Use `--agent codex`, `--agent claude`, or `--agent cursor` for one agent; the
option is repeatable. The project directory must already exist. Omitting
`--agent` and `--project` installs only the executable. `--prefix DIR` controls
the executable/library destination, independently of the project directory.
The installer neither changes PATH nor edits shell profiles or global agent
settings. Configure PATH in the shell or cloud setup used by the agent.

The launcher records the installing Python interpreter's absolute path and
runs the installed `bzlog.py`; `bzlog_execlog.py` lives alongside it. Neither a
Python package index nor a network download is involved. Both modules and the
license are installed even if no agent is selected. Bazel's execution-log
converter is a separate optional tool; it is not included or installed.

## Instruction destinations

- **Codex:** the installer adds/replaces only the `<!-- bzlog:begin -->` through
  `<!-- bzlog:end -->` block in the root `AGENTS.md`. If a nonempty root
  `AGENTS.override.md` exists, that effective file receives the block instead.
  Codex selects one instruction file per directory; a block in the ignored
  base file would not help. Nested overrides and the instruction size limit
  still apply. See [OpenAI's instruction discovery documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
- **Claude Code:** `.claude/rules/bzlog.md` has no `paths` restriction, so the
  project rule is available for Bazel commands even before any BUILD file is
  opened. Existing `CLAUDE.md` files are untouched. See [Claude Code memory and rules](https://code.claude.com/docs/en/memory).
- **Cursor:** `.cursor/rules/bzlog.mdc` contains a description and
  `alwaysApply: true`. Its body limits the advice to Bazel work; a filename glob
  could miss a build launched while editing an ordinary source file. See
  [Cursor project rule formats](https://cursor.com/docs/rules).

Restart sessions after installation and inspect their loaded instruction
sources. In a repository shared by several agents, an agent may also discover
another agent's instruction file; the bzlog advice is identical and harmless
to repeat. Installing only the agent you use avoids unnecessary repetition.
Rule discovery is not a guarantee that a model will follow every instruction.
This release validates the generated files and commands; it does not run paid
end-to-end model sessions in all three products.

For ChatGPT Codex cloud work, install bzlog in the task's execution environment
and keep the project rule in the repository. Installing on your laptop alone
does not install software in the cloud or connect ordinary ChatGPT web chats.

## Updates and conflict handling

Run the installer from the new release with the same prefix and selections.
Unchanged files are left untouched, including their timestamps. Text outside
the managed instruction block, CRLF line endings, and existing instruction
file permissions are preserved. Customize project policy outside the block;
the block's contents are replaced on upgrade.

All destinations are checked before writes. Unmanaged commands or dedicated
rules, malformed markers, changed Cursor frontmatter, symlinked destinations
or intermediate directories beneath the selected roots, and nonregular files
are rejected. A conflict does not partially install other planned files.
Reconcile an old manual installation explicitly; there is no force-overwrite
option. Writes replace individual files atomically. The whole installation is
not a filesystem transaction: an I/O failure partway through can leave a mix
of versions; fix the storage issue and rerun. Do not run concurrent installers.

To uninstall, remove `PREFIX/bin/bzlog` and `PREFIX/lib/bzlog`, then remove the
managed block from the effective Codex file and the dedicated Claude/Cursor
rule files if they have no custom content. Preserve all other project rules.

## Choices and alternatives

| Choice | Alternatives considered | Why this release uses it |
|---|---|---|
| Project-local rules, explicitly selected | Global rules; automatic agent detection | Reviewable in Git, no unrelated projects changed, no dependence on guessing installed editors |
| Native instruction locations per agent | Only a shared AGENTS.md; CLAUDE.md import | Works with each documented rule system without changing agent settings or requiring cross-agent compatibility modes |
| One shared rule body | Three handwritten rules | Keeps command syntax, caution about incomplete evidence, and feature guidance consistent |
| Always-available advice scoped by its text | BUILD-file globs; agent-selected description | Builds are often launched while looking at non-Bazel source files |
| Standard-library Python installer with POSIX shell entrypoint | pip package, curl-to-shell download, three shell installers | Reuses the existing runtime, handles paths and preservation in one implementation, installs the companion module, and runs offline from a pinned release |
| Explicit existing-override handling | Blindly append to AGENTS.md; edit global config | Avoids a successful-looking installation into a file Codex will ignore |

The first release is validated on Ubuntu under WSL with isolated prefixes,
spaces/quotes/shell metacharacters in paths, all-agent and selective installs,
repeat installs, upgrades, instruction preservation, conflict preflight,
nonregular files, and installed execution-log analysis. Linux/macOS use the
same POSIX implementation; native Windows launchers are outside this release.
