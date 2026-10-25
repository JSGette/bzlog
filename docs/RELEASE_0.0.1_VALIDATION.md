# Release 0.0.1 validation

2026-10-25, Ubuntu under WSL, Python 3.12.3, Bazel 9.3.0.

## Release changes

The release includes the previously reviewed BEP, test-audit, rerun, and bundle
work plus the execution-log feature from `codex/readme-backlog`. New release
work adds version/help discovery, the installer, a shared rule for three
coding agents, a complete feature inventory, and the first changelog.
No existing tracked installer or rule was present in the repository/history
or either local checkout, so these files are new.

## Checks

- `python3 -m unittest discover`: **182 tests passed**, including 13 installer
  tests that execute the installed command and companion module.
- `python3 test_truncate.py`: **4 checks passed** against framework-log goldens.
- `sh -n install.sh` and `git diff --check`: passed.
- Installer exercised all three rule destinations, selective installation,
  Codex overrides, CRLF preservation, unchanged timestamps on repeat install,
  managed-block upgrades, dry run, shell metacharacters in paths, unmanaged
  file conflicts, modified Cursor frontmatter, symlinked directories, and FIFO
  rejection. The shell entrypoint was run from outside its source directory;
  the installed command remained usable after that directory was removed.
- Installed `bzlog --version` reports **0.0.1**; top-level and subcommand help
  work. Help/version tokens after the Bazel argument boundary remain forwarded.
- The installed wrapper ran Bazel's real
  `//src/test/cpp/util:md5_test` with audit, command recording, and bundling:
  **1 target passed**, **25 BEP events**, **complete stream**.
- The installed verifier checked **11 saved artifact references**, with
  integrity `ok` and complete capture/stream status.
- The installed `exec-log` command used the official converter on the prior
  controlled before/after captures: **21 uniquely matched spawns, 21 changed**.
  Earlier cold/disk-cache/local-reuse and binary/JSON checks are recorded in
  [execution-log validation](EXECUTION_LOGS.md).
- Bazel source remained unchanged; the dedicated validation server was stopped.

Raw local smoke artifacts are under
`/home/jgett/REPOS/.work/bzlog-release-20261025`; they are not part of the public
release. See [agent setup](AGENT_INTEGRATION.md) for the platform and agent-session
validation limits. The release source archive is built from the committed Git
tree and installed again before publication; its checksum accompanies the asset.
