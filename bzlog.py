#!/usr/bin/env python3
"""bzlog - collapse a Bazel Build Event Protocol stream into an agent-sized JSON container.

Usage:
    bazel test //... --build_event_json_file=bep.json
    bzlog bep.json > result.json

Design notes
------------
Unlike line-oriented compressors (rtk, sqz), this works on Bazel's *structured*
event stream. Two consequences:

  1. Classification is read from the build system, not guessed with regexes.
     ActionExecuted.type tells us "CppCompile"; FailureDetail gives a machine
     category; TestResult carries an explicit status enum.

  2. Root-cause collapsing. In a monorepo one broken header fails N targets with
     byte-identical stderr. Line-based tools emit N copies. We emit one
     diagnostic with affected_count=N. This is the main compression win and it
     is only possible because the events carry target labels.
"""

from __future__ import annotations

import json
import os
import re
import sys
import hashlib
import argparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse, unquote

__version__ = "0.0.1"
SCHEMA = "bzlog/v2"
MAX_EVIDENCE_LINES = 40
MAX_EVIDENCE_BYTES = 4096
# Framework-extracted logs are already distilled, so they get a higher cap
# than the blind tail: cutting a distilled log loses signal, cutting a raw
# one loses noise.
MAX_EXTRACTED_LINES = 160
MAX_EXTRACTED_BYTES = 16384
MAX_DIAGNOSTICS = 25
MAX_TEST_ATTEMPTS = 10
FAILED_TEST_STATUSES = {"FAILED", "TIMEOUT", "REMOTE_FAILURE", "FAILED_TO_BUILD"}


# --------------------------------------------------------------------------
# diagnostic line parsers
# --------------------------------------------------------------------------

# gcc/clang:  path/to/file.cc:42:7: error: 'bar' was not declared in this scope
_GCC = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<severity>error|warning|note|fatal error):\s*(?P<message>.*)$"
)
# go:  ./pkg/foo.go:12:2: undefined: Bar
_GO = re.compile(
    r"^(?P<file>\.?/?[\w./+-]+\.go):(?P<line>\d+):(?P<col>\d+):\s+(?P<message>.+)$"
)
# javac:  pkg/Foo.java:12: error: cannot find symbol
_JAVAC = re.compile(
    r"^(?P<file>[\w./$+-]+\.java):(?P<line>\d+):\s*"
    r"(?P<severity>error|warning):\s*(?P<message>.*)$"
)
# rustc:  error[E0433]: failed to resolve
_RUSTC = re.compile(r"^(?P<severity>error|warning)(\[(?P<code>E\d+)\])?:\s*(?P<message>.+)$")
_BAZEL = re.compile(
    r"^ERROR:\s+(?P<file>[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<message>.+)$"
)

_PARSERS = [
    ("bazel", _BAZEL),
    ("gcc", _GCC),
    ("javac", _JAVAC),
    ("go", _GO),
    ("rustc", _RUSTC),
]

# Noise that never helps an agent reason about the failure.
_NOISE = re.compile(
    r"^(INFO:|Loading:|Analyzing:|\[\d+ / \d+\]|Computing main repo mapping|"
    r"WARNING: Streamed test output|.*\bcompilation terminated\b.*|"
    r"In file included from |\s+from |\s*\|\s*$|\s*\^~*\s*$)"
)


def parse_diagnostics(text: str, limit: int = 12) -> list[dict[str, Any]]:
    """Extract structured compiler diagnostics from a stderr blob."""
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or _NOISE.match(line):
            continue
        for kind, rx in _PARSERS:
            m = rx.match(line)
            if not m:
                continue
            g = m.groupdict()
            sev = g.get("severity") or "error"
            if sev in ("note", "warning"):
                break  # keep only hard failures in the compact view
            d: dict[str, Any] = {"severity": sev, "message": g["message"].strip()}
            if g.get("file"):
                path = g["file"]
                d["file"] = path[2:] if path.startswith("./") else path
            if g.get("line"):
                d["line"] = int(g["line"])
            if g.get("col"):
                d["col"] = int(g["col"])
            if g.get("code"):
                d["code"] = g["code"]
            d["parser"] = kind
            out.append(d)
            break
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _dur_s(d: Any) -> float:
    """protobuf Duration in JSON is a string like '12.5s'; sometimes an object."""
    if not d:
        return 0.0
    if isinstance(d, str):
        try:
            return float(d.rstrip("s"))
        except ValueError:
            return 0.0
    if isinstance(d, dict):
        return float(d.get("seconds", 0)) + float(d.get("nanos", 0)) / 1e9
    return 0.0


def uri_to_path(uri: str) -> Optional[str]:
    if not isinstance(uri, str) or not uri:
        return None
    if os.path.isabs(uri):
        return uri
    try:
        parsed = urlparse(uri)
    except ValueError:
        return None
    if (parsed.scheme and parsed.scheme != "file") or parsed.netloc not in ("", "localhost"):
        return None  # Remote references must never fall back to a local path.
    # Resolve against --root before checking whether a relative path exists.
    return unquote(parsed.path) if parsed.scheme == "file" else uri


def read_file_message(fm: dict[str, Any], root: Optional[str]) -> str:
    """A BEP `File` may inline its bytes or point at a URI."""
    if not fm:
        return ""
    if "contents" in fm:
        import base64
        try:
            return base64.b64decode(fm["contents"]).decode("utf-8", "replace")
        except Exception:
            return ""
    path = uri_to_path(fm.get("uri", ""))
    if path and root and not os.path.isabs(path):
        path = os.path.join(root, path)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""
    return ""


def tail(text: str, lines: int = MAX_EVIDENCE_LINES, nbytes: int = MAX_EVIDENCE_BYTES):
    """Keep the tail: for build tools the useful part is almost always at the end."""
    body = text.splitlines()
    truncated = len(body) > lines
    kept = "\n".join(body[-lines:])
    if len(kept.encode()) > nbytes:
        kept = kept.encode()[-nbytes:].decode("utf-8", "replace")
        truncated = True
    return kept, truncated


def bounded_log(text: str, nbytes: int = 65536) -> str:
    """Bound invocation context while retaining both early errors and final output."""
    raw = text.encode("utf-8")
    if len(raw) <= nbytes:
        return text
    marker = b"\n[... invocation output omitted ...]\n"
    half = (nbytes - len(marker)) // 2
    return (raw[:half].decode("utf-8", "ignore") + marker.decode() +
            raw[-half:].decode("utf-8", "ignore"))


def parse_junit(xml_text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Pull failing cases out of a JUnit test.xml."""
    cases: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return cases
    for tc in root.iter("testcase"):
        for tag in ("failure", "error"):
            node = tc.find(tag)
            if node is None:
                continue
            msg = (node.get("message") or (node.text or "")).strip()
            cases.append({
                "case": tc.get("name", "?"),
                "class": tc.get("classname", ""),
                "type": tag,
                "message": msg.splitlines()[0][:400] if msg else "",
            })
            break
        if len(cases) >= limit:
            break
    return cases


# --------------------------------------------------------------------------
# framework-aware test log truncation
# --------------------------------------------------------------------------
# A registry of (name, detector, extractor) pairs. Detection is content-based:
# the rule mnemonic lies on custom test wrappers, the log itself does not.
# Each extractor returns the distilled text or None; anything unrecognized
# falls back to the blind tail() in Evidence.add.
#
# The main byte win is stack filtering: frames whose path lies outside the
# workspace are collapsed to a one-line marker.

# Paths that are framework internals regardless of where the workspace is.
_FRAMEWORK_DIRS = re.compile(
    r"(site-packages|dist-packages|/usr/lib/python|/importlib/|/unittest/|"
    r"/_pytest/|/pluggy/)"
)


def _in_workspace(path: str, workspace: Optional[str]) -> bool:
    p = path.replace("\\", "/")
    if _FRAMEWORK_DIRS.search(p):
        return False
    if not (p.startswith("/") or re.match(r"^[A-Za-z]:", p)):
        return True  # relative paths are workspace-relative in a sandbox
    if workspace:
        w = workspace.replace("\\", "/").rstrip("/") + "/"
        return p.startswith(w)
    return True  # absolute, workspace unknown: keep rather than guess


# -- gtest -----------------------------------------------------------------

_GTEST_RUN = re.compile(r"^\[ RUN      \] (\S+)")
_GTEST_FAILED = re.compile(r"^\[  FAILED  \] (\S+)")
_GTEST_OK = re.compile(r"^\[       OK \] ")
_GTEST_SUMMARY = re.compile(r"^\[==========\] .*ran\.")


def _detect_gtest(text: str) -> bool:
    return "[ RUN      ]" in text


def _extract_gtest(text: str, workspace: Optional[str]) -> Optional[str]:
    """Block from [ RUN ] of a failing case to its [ FAILED ], plus the
    trailing summary list."""
    lines = text.splitlines()
    out: list[str] = []
    block: list[str] = []
    in_block = False
    summary_at = None
    for i, ln in enumerate(lines):
        if _GTEST_SUMMARY.match(ln):
            summary_at = i
        if _GTEST_RUN.match(ln):
            block, in_block = [ln], True
            continue
        if in_block:
            block.append(ln)
            if _GTEST_FAILED.match(ln):
                out.extend(block)
                out.append("")
                in_block = False
            elif _GTEST_OK.match(ln):
                in_block = False
    if summary_at is not None:
        out.extend(lines[summary_at:])
    return "\n".join(out).strip("\n") if out else None


# -- go test ---------------------------------------------------------------

def _detect_go(text: str) -> bool:
    return ("\n--- FAIL: " in text or text.startswith("--- FAIL: ")
            or "\npanic: " in text or text.startswith("panic: "))


def _extract_go(text: str, workspace: Optional[str]) -> Optional[str]:
    """--- FAIL: block with its indented output; for panics the message and
    the first ~5 frames; plus the trailing FAIL lines."""
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("--- FAIL: "):
            out.append(ln)
            i += 1
            while i < n and lines[i].startswith((" ", "\t")):
                out.append(lines[i])
                i += 1
            out.append("")
            continue
        if ln.startswith("panic: "):
            out.append(ln)
            i += 1
            while i < n and not lines[i].startswith("goroutine "):
                if lines[i].strip():
                    out.append(lines[i])
                i += 1
            if i < n:
                out.append(lines[i])  # goroutine header
                i += 1
                frames = 0
                while i < n and frames < 5 and lines[i].strip():
                    out.append(lines[i])
                    if lines[i].startswith("\t"):
                        frames += 1  # each frame is a func line + a \tfile:line
                    i += 1
            out.append("")
            continue
        if ln.startswith("FAIL"):
            out.append(ln)
        i += 1
    return "\n".join(out).strip("\n") if out else None


# -- pytest ----------------------------------------------------------------

_PYTEST_FAILURES = re.compile(r"^=+ FAILURES =+$")
_PYTEST_SECTION = re.compile(r"^=+ .* =+$")
_PYTEST_CASE = re.compile(r"^_+ .+ _+$")
_PYTEST_SUMMARY = re.compile(r"^=+ short test summary info =+$")
_PYTEST_LOC = re.compile(r"^\S.*:\d+: \w+")


def _detect_pytest(text: str) -> bool:
    return any(_PYTEST_FAILURES.match(l) for l in text.splitlines())


def _extract_pytest(text: str, workspace: Optional[str]) -> Optional[str]:
    """The FAILURES section, each case cut at its first assertion block,
    plus short test summary info."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _PYTEST_FAILURES.match(ln)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines))
                if _PYTEST_SECTION.match(lines[i])), len(lines))
    out = [lines[start]]
    case: list[str] = []

    def flush() -> None:
        if not case:
            return
        out.append(case[0])
        seen_assert = False
        for k in range(1, len(case)):
            ln = case[k]
            if ln.startswith("E "):
                seen_assert = True
                out.append(ln)
            elif seen_assert:
                # assertion block over: keep the file:line marker, drop the rest
                # (captured stdout/stderr is what bloats these logs)
                loc = next((l for l in case[k:] if _PYTEST_LOC.match(l)), None)
                if loc:
                    out.append(loc)
                break
            else:
                out.append(ln)
        out.append("")

    for i in range(start + 1, end):
        if _PYTEST_CASE.match(lines[i]):
            flush()
            case = [lines[i]]
        elif case:
            case.append(lines[i])
    flush()

    si = next((i for i in range(end, len(lines)) if _PYTEST_SUMMARY.match(lines[i])), None)
    if si is not None:
        out.extend(lines[si:])
    return "\n".join(out).strip("\n") if len(out) > 1 else None


# -- python traceback (unittest et al.) ------------------------------------

_PY_TB = "Traceback (most recent call last):"
_PY_FRAME = re.compile(r'^\s+File "(?P<path>[^"]+)", line \d+')
_PYTB_KEEP = re.compile(r"^(FAIL: |ERROR: |FAILED|OK\b|Ran \d+ tests?)")


def _detect_pytb(text: str) -> bool:
    return _PY_TB in text


def _extract_pytb(text: str, workspace: Optional[str]) -> Optional[str]:
    """Tracebacks with out-of-workspace frames collapsed, plus the FAIL/ERROR
    headers and the runner's verdict lines."""
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if _PY_TB in ln:
            out.append(ln)
            i += 1
            dropped = 0
            while i < n:
                m = _PY_FRAME.match(lines[i])
                if m:
                    frame = [lines[i]]
                    if i + 1 < n and lines[i + 1].startswith((" ", "\t")) \
                            and not _PY_FRAME.match(lines[i + 1]):
                        frame.append(lines[i + 1])
                        i += 1
                    i += 1
                    if _in_workspace(m.group("path"), workspace):
                        if dropped:
                            out.append(f"  [... {dropped} frames outside workspace ...]")
                            dropped = 0
                        out.extend(frame)
                    else:
                        dropped += 1
                    continue
                if dropped:
                    out.append(f"  [... {dropped} frames outside workspace ...]")
                    dropped = 0
                out.append(lines[i])
                i += 1
                if not lines[i - 1].startswith((" ", "\t")):
                    break  # unindented line = the exception itself; traceback over
            out.append("")
            continue
        if _PYTB_KEEP.match(ln):
            out.append(ln)
        i += 1
    return "\n".join(out).strip("\n") if out else None


# -- JUnit / java stack traces ---------------------------------------------

_JAVA_FRAME = re.compile(r"^\s+at (?:app//|platform//)?(?P<cls>[\w.$/]+)\(")
_JAVA_FRAMEWORK = re.compile(
    r"^(org\.junit|junit\.|org\.testng|org\.hamcrest|jdk\.|java\.|javax\.|"
    r"sun\.|com\.sun\.|org\.gradle|com\.google\.testing|org\.apache\.maven)"
)
_PROGRESS_DOTS = re.compile(r"^[.EF]+$")


def _detect_junit(text: str) -> bool:
    return ".java:" in text and re.search(r"^\s+at [\w.$/]+\(", text, re.M) is not None


def _extract_junit(text: str, workspace: Optional[str]) -> Optional[str]:
    """Keep the log but collapse runs of framework stack frames."""
    out: list[str] = []
    dropped = 0
    changed = False

    def flush() -> None:
        nonlocal dropped
        if dropped:
            out.append(f"\tat [... {dropped} framework frames ...]")
            dropped = 0

    for ln in text.splitlines():
        m = _JAVA_FRAME.match(ln)
        if m:
            cls = m.group("cls").split("/")[-1]  # strip java.base/ and app// prefixes
            if _JAVA_FRAMEWORK.match(cls):
                dropped += 1
                changed = True
                continue
            flush()
            out.append(ln)
            continue
        flush()
        if ln.startswith("exec ${PAGER") or re.match(r"^-{40,}$", ln) \
                or _PROGRESS_DOTS.match(ln.strip() or "x"):
            changed = True
            continue
        out.append(ln)
    flush()
    if not changed:
        return None
    return "\n".join(out).strip("\n")


# -- registry --------------------------------------------------------------

_EXTRACTORS = [
    ("gtest", _detect_gtest, _extract_gtest),
    ("go", _detect_go, _extract_go),
    ("pytest", _detect_pytest, _extract_pytest),
    ("python", _detect_pytb, _extract_pytb),
    ("junit", _detect_junit, _extract_junit),
]


def truncate_test_log(text: str, workspace: Optional[str] = None
                      ) -> Optional[tuple[str, int, str]]:
    """Distill a test.log with the first matching framework extractor.

    Returns (kept_text, kept_lines, framework) or None when no framework
    matched -- the caller then falls back to the plain tail.
    """
    if not text:
        return None
    for name, detect, extract in _EXTRACTORS:
        try:
            if not detect(text):
                continue
            kept = extract(text, workspace)
        except Exception:
            return None  # an extractor bug must never lose the log
        if kept and kept.strip():
            return kept, len(kept.splitlines()), name
    return None


# --------------------------------------------------------------------------
# container assembly
# --------------------------------------------------------------------------

@dataclass
class Evidence:
    store: dict[str, dict[str, Any]] = field(default_factory=dict)
    _by_hash: dict[str, str] = field(default_factory=dict)

    def add(self, kind: str, text: str, framework: Optional[str] = None,
            total_lines: Optional[int] = None) -> Optional[str]:
        if not text.strip():
            return None
        if kind == "invocation_log":
            lines = [line for line in text.splitlines() if not _NOISE.match(line)]
            if len(lines) > MAX_EVIDENCE_LINES:
                lines = (lines[:MAX_EVIDENCE_LINES // 2] +
                         ["[... invocation output omitted ...]"] +
                         lines[-MAX_EVIDENCE_LINES // 2:])
            body = bounded_log("\n".join(lines), MAX_EVIDENCE_BYTES)
            truncated = body != text
        elif framework:
            # already distilled by a framework extractor: generous cap only
            body, truncated = tail(text, MAX_EXTRACTED_LINES, MAX_EXTRACTED_BYTES)
        else:
            body, truncated = tail(text)
        # Different logs can have identical tails. Never dedupe on the excerpt.
        h = hashlib.sha256((kind + "\0" + text).encode()).hexdigest()
        if h in self._by_hash:
            return self._by_hash[h]
        ref = f"e{len(self.store) + 1}"
        self._by_hash[h] = ref
        entry: dict[str, Any] = {"kind": kind, "truncated": truncated, "text": body}
        if framework:
            entry["framework"] = framework
            entry["distilled"] = True
            entry["kept_lines"] = len(body.splitlines())
            if total_lines is not None:
                entry["total_lines"] = total_lines
                entry["truncated"] |= entry["kept_lines"] < total_lines
        self.store[ref] = entry
        return ref


@dataclass
class StreamStatus:
    """Transport integrity, separate from Bazel's build/test outcome."""
    events_read: int = 0
    malformed_records: int = 0
    skipped_events: int = 0
    trailing_fragment: bool = False
    finished: bool = False
    last_message: bool = False
    _seen: set[str] = field(default_factory=set)
    _pending: set[str] = field(default_factory=set)

    def observe(self, event: dict[str, Any]) -> None:
        eid = event.get("id")
        if isinstance(eid, dict) and eid:
            key = json.dumps(eid, sort_keys=True, separators=(",", ":"))
            self._seen.add(key)
            self._pending.discard(key)
        for child in event.get("children", []):
            key = json.dumps(child, sort_keys=True, separators=(",", ":"))
            if key not in self._seen:
                self._pending.add(key)

    def as_dict(self) -> dict[str, Any]:
        damaged = self.malformed_records or self.skipped_events or self.trailing_fragment
        status = ('degraded' if damaged else 'unavailable' if not self.events_read else
                  'complete' if self.finished and self.last_message and not self._pending else 'incomplete')
        return {"status": status, "events_read": self.events_read,
                "build_finished": self.finished, "last_message": self.last_message,
                **({"malformed_records": self.malformed_records} if self.malformed_records else {}),
                **({"skipped_events": self.skipped_events} if self.skipped_events else {}),
                **({"unfulfilled_events": len(self._pending)} if self._pending else {}),
                **({"trailing_fragment": True} if self.trailing_fragment else {})}


class Container:
    def __init__(self, root: Optional[str] = None, audit_tests: bool = False,
                 bundle: Optional[InvocationBundle] = None):
        self.root = root
        self.bundle = bundle
        self.ev = Evidence()
        self.invocation: dict[str, Any] = {}
        self.counts = {
            "actions_failed": 0,
            "targets_failed": 0,
        }
        self._diags: dict[str, dict[str, Any]] = {}  # fingerprint -> diagnostic
        self._order: list[str] = []
        self._abort_events: dict[str, str] = {}
        self._abort_children: dict[str, list[str]] = {}
        self._progress_stderr = ""
        self.console_output = ""
        self._tests: dict[str, dict[str, Any]] = {}
        self.audit_tests = audit_tests
        self._audit_candidates: dict[str, dict[str, Any]] = {}
        self._audit_completions: dict[str, bool] = {}
        self._audit_aborts: dict[str, str] = {}
        self.stream = StreamStatus()

    def consume(self, ev: dict[str, Any]) -> None:
        """Keep a bad event from hiding the rest of the stream, and disclose it."""
        try:
            self.stream.observe(ev)
            if self.bundle:
                try:
                    ev = self.bundle.capture_event(ev, self.root)
                except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                    # Optional archival must not hide a usable event from the report.
                    self.bundle.manifest["errors"].append(f"event capture: {exc}")
            self.handle(ev)
        except Exception as exc:
            self.stream.skipped_events += 1
            if self.stream.skipped_events <= 3:
                print(f"bzlog: skipped event: {exc}", file=sys.stderr)
        else:
            self.stream.finished |= "finished" in ev
            self.stream.last_message |= ev.get("lastMessage") is True

    @staticmethod
    def event_key(eid: dict[str, Any]) -> str:
        return json.dumps(eid, sort_keys=True, separators=(",", ":"))

    def _resolved_diagnostics(self) -> list[dict[str, Any]]:
        """Link empty analysis parents to descriptive causes, in either event order."""
        diags = {fp: dict(d, _targets=list(d["_targets"]))
                 for fp, d in self._diags.items() if d["kind"] != "test_failure"}
        dropped = set()
        for event, fp in self._abort_events.items():
            if diags[fp].get("description"):
                continue
            pending = list(self._abort_children.get(event, []))
            visited = {event}
            causes = set()
            while pending:
                child = pending.pop()
                if child in visited:
                    continue
                visited.add(child)
                child_fp = self._abort_events.get(child)
                if child_fp and diags[child_fp].get("description"):
                    causes.add(child_fp)
                else:
                    pending.extend(self._abort_children.get(child, []))
            if causes:
                dropped.add(fp)
                for cause in causes:
                    for target in diags[fp]["_targets"]:
                        if target not in diags[cause]["_targets"]:
                            diags[cause]["_targets"].append(target)
        return ([diags[fp] for fp in self._order if fp in diags and fp not in dropped]
                + self._test_diagnostics())

    def _test_record(self, tid: dict[str, Any]) -> dict[str, Any]:
        identity = {"label": tid.get("label"), "configuration": tid.get("configuration")}
        return self._tests.setdefault(self.event_key(identity), dict(identity, results={}))

    def _test_diagnostics(self) -> list[dict[str, Any]]:
        """Reconcile attempts with each configured test's authoritative summary."""
        grouped: dict[str, dict[str, Any]] = {}
        for test in self._tests.values():
            summary = test.get("summary", test.get("target_summary"))
            status = summary.get("overallStatus", "NO_STATUS") if summary is not None else "UNKNOWN"
            failures = [result for _, result in sorted(test["results"].items())
                        if result["status"] != "PASSED"]
            if status == "PASSED" and not failures:
                continue
            kind = {"PASSED": "test_recovered", "FLAKY": "test_flaky"}.get(status)
            if kind is None:
                kind = "test_failure" if status in FAILED_TEST_STATUSES else (
                    "test_attempt_failure" if failures else "test_incomplete")
            if not failures:
                diagnostic = {
                    "kind": kind, "status": status, "overall_status": status,
                    "final": summary is not None, "configuration": test["configuration"],
                    "description": (f"Test summary: {status}; per-attempt diagnostics unavailable."
                                    if summary is not None else
                                    "Test results observed, but the final summary is unavailable."),
                    "_targets": [test["label"]] if test["label"] else [],
                }
                if test.get("summary_evidence"):
                    diagnostic["evidence"] = test["summary_evidence"]
                grouped["summary:" + self.event_key({"label": test["label"],
                                                     "configuration": test["configuration"]})] = diagnostic
                continue
            for result in failures:
                d = dict(result, kind=kind, overall_status=status, final=summary is not None)
                # Equal messages without logs do not establish equivalent failures.
                if d.get("evidence"):
                    key = self.event_key({"kind": kind, "overall_status": status,
                                          "status": d["status"], "configuration": d["configuration"],
                                          "evidence": d["evidence"], "cases": d.get("cases")})
                else:
                    key = self.event_key({"label": test["label"], "configuration": test["configuration"],
                                          "run": d["run"], "shard": d["shard"], "attempt": d["attempt"]})
                if key not in grouped:
                    grouped[key] = dict(d, _targets=[], occurrences=0, attempts=[])
                group = grouped[key]
                label = test["label"]
                if label and label not in group["_targets"]:
                    group["_targets"].append(label)
                group["occurrences"] += 1
                if len(group["attempts"]) < MAX_TEST_ATTEMPTS:
                    group["attempts"].append({"target": label, **{
                        field: d[field] for field in
                        ("run", "shard", "attempt", "status", "cached_locally", "cached_remotely")
                    }})
                else:
                    group["attempts_omitted"] = group["occurrences"] - MAX_TEST_ATTEMPTS
        return list(grouped.values())

    def _test_counts(self) -> dict[str, int]:
        counts = {"tests_total": len(self._tests), "tests_passed": 0, "tests_flaky": 0,
                  "tests_failed": 0, "tests_incomplete": 0}
        for test in self._tests.values():
            status = test.get("summary", test.get("target_summary", {})).get("overallStatus", "NO_STATUS")
            if status == "PASSED":
                counts["tests_passed"] += 1
            elif status == "FLAKY":
                counts["tests_flaky"] += 1
            elif status in FAILED_TEST_STATUSES:
                counts["tests_failed"] += 1
            else:
                counts["tests_incomplete"] += 1
        return counts

    def _test_audit(self) -> dict[str, Any]:
        """Account for observed configured tests without treating a pass as execution."""
        rows = []
        counts = dict.fromkeys(("executed", "cached", "mixed", "not_run", "unknown"), 0)
        tests = {**self._audit_candidates, **self._tests}
        for key, test in sorted(tests.items()):
            summary = test.get("summary", test.get("target_summary"))
            status = summary.get("overallStatus", "NO_STATUS") if summary is not None else "UNKNOWN"
            source = ("test_summary" if "summary" in test else
                      "target_summary" if summary is not None else "unavailable")
            results = list(test.get("results", {}).values())
            attempts = {"observed": len(results), "executed": 0, "cached": 0,
                        "cached_locally": 0, "cached_remotely": 0, "unknown": 0}
            for result in results:
                local, remote = result["cached_locally"], result["cached_remotely"]
                attempts["cached_locally"] += bool(local)
                attempts["cached_remotely"] += bool(remote)
                if local or remote:
                    attempts["cached"] += 1
                elif result["status"] in {"PASSED", "FAILED", "TIMEOUT", "FLAKY"}:
                    attempts["executed"] += 1
                else:
                    attempts["unknown"] += 1
            reason = self._audit_aborts.get(key)
            if summary is None and not results:
                if reason in {"SKIPPED", "NO_BUILD", "NO_ANALYZE"}:
                    status, source = reason, "aborted"
                elif self._audit_completions.get(key) is False:
                    status, source = "FAILED_TO_BUILD", "target_completed"
            issues = []
            totals = {}
            if "summary" in test:
                # Defaults apply inside an existing TestSummary message only.
                try:
                    total = int(summary.get("totalRunCount", 0))
                    cached = int(summary.get("totalNumCached", 0))
                except (ValueError, TypeError, OverflowError):
                    total = cached = None
                totals = {"attempts": total, "cached": cached}
                if total is None or total < 0 or not 0 <= cached <= total:
                    issues.append("invalid_summary_counts")
                if total != len(results):
                    issues.append("attempt_count_mismatch")
                if len(results) == total and cached != attempts["cached"]:
                    issues.append("cache_count_mismatch")
            execution = "unknown"
            if results and not attempts["unknown"] and not issues:
                execution = ("mixed" if attempts["executed"] and attempts["cached"] else
                             "cached" if attempts["cached"] else "executed")
            elif not results:
                if ("invalid_summary_counts" not in issues and totals.get("attempts", 0) > 0
                        and totals["cached"] == totals["attempts"]):
                    execution = "cached"
                elif status in {"FAILED_TO_BUILD", "TOOL_HALTED_BEFORE_TESTING",
                                "SKIPPED", "NO_BUILD", "NO_ANALYZE"} and not issues:
                    execution = "not_run"
            counts[execution] += 1
            row = {"label": test["label"], "configuration": test["configuration"],
                   "status": status, "final": source != "unavailable", "status_source": source,
                   "execution": execution, "attempts": attempts}
            if totals:
                row["summary_counts"] = totals
            if reason:
                row["abort_reason"] = reason
            if issues:
                row["issues"] = issues
            rows.append(row)
        notices = []
        if not rows:
            notices.append("No tests are visible in this stream.")
        elif not any(row["attempts"]["executed"] for row in rows):
            notices.append("No fresh test execution observed; missing evidence does not prove nothing ran.")
        return {"scope": "Tests identified in this BEP; tests filtered out before emission are unknown.",
                "counts": {"total": len(rows), **counts}, "tests": rows, "notices": notices}

    # -- fingerprinting is what collapses the fan-out ----------------------
    @staticmethod
    def fingerprint(kind: str, action_type: str, errors: list[dict], evidence_ref) -> str:
        if errors:
            key = "|".join(
                f"{e.get('file','')}:{e.get('line','')}:{e.get('message','')}" for e in errors
            )
        else:
            key = f"{evidence_ref}"
        return hashlib.sha256(f"{kind}/{action_type}/{key}".encode()).hexdigest()[:12]

    def add_diag(self, diag: dict[str, Any], target: Optional[str],
                 event_id: Optional[dict[str, Any]] = None) -> str:
        if diag.get("kind") in ("analysis_error", "aborted", "test_failure") or not (
                diag.get("errors") or diag.get("evidence")):
            # Aborts are joined through BEP edges. Missing evidence proves no
            # equivalence, even if both failures have the same action mnemonic.
            fp = ("event:" + self.event_key(event_id) if event_id
                  else f"unknown:{len(self._order)}")
        else:
            fp = self.fingerprint(
                diag["kind"], diag.get("action_type", ""),
                diag.get("errors", []), diag.get("evidence")
            ) + self.event_key({"configuration": diag.get("configuration"),
                               "status": diag.get("status"),
                               "failure_category": diag.get("failure_category"),
                               "failure_code": diag.get("failure_code"),
                               "cases": diag.get("cases")})
        existing = self._diags.get(fp)
        if existing:
            if target and target not in existing["_targets"]:
                existing["_targets"].append(target)
            return fp
        diag["_targets"] = [target] if target else []
        self._diags[fp] = diag
        self._order.append(fp)
        return fp

    # -- event handlers ---------------------------------------------------
    def handle(self, ev: dict[str, Any]) -> None:
        eid = ev.get("id", {})
        if self.audit_tests:
            if ev.get("configured", {}).get("testSize") in {"SMALL", "MEDIUM", "LARGE", "ENORMOUS"}:
                for child in ev.get("children", []):
                    tid = child.get("targetCompleted", {})
                    if tid.get("label") and not tid.get("aspect"):
                        identity = {"label": tid["label"], "configuration": tid.get("configuration")}
                        self._audit_candidates[self.event_key(identity)] = identity
            tid = eid.get("targetCompleted", {})
            if tid.get("label") and not tid.get("aspect"):
                key = self.event_key({"label": tid["label"], "configuration": tid.get("configuration")})
                if "completed" in ev:
                    self._audit_completions[key] = ev["completed"].get("success", False)
                if "aborted" in ev:
                    self._audit_aborts[key] = ev["aborted"].get("reason", "UNKNOWN")
        if (ev.get("structuredCommandLine", {}).get("commandLineLabel") == "original"
                and "replay" in self.invocation):
            replay = self.invocation["replay"]
            try:
                replay["layout"] = command_layout(replay["argv"], ev["structuredCommandLine"])
            except ValueError as exc:
                replay["rerun_error"] = str(exc)
        if "pattern" in eid:
            self.invocation["patterns"] = eid["pattern"].get("pattern", [])
        if "progress" in ev:
            self._progress_stderr = bounded_log(
                self._progress_stderr + ev["progress"].get("stderr", ""))

        if "started" in ev:
            s = ev["started"]
            self.invocation.update({
                "uuid": s.get("uuid"),
                "command": s.get("command"),
                "bazel_version": s.get("buildToolVersion"),
                "workspace": s.get("workspaceDirectory"),
            })
            if self.root is None:
                self.root = s.get("workspaceDirectory")

        elif "action" in ev:
            a = ev["action"]
            if a.get("success"):
                return
            self.counts["actions_failed"] += 1
            label = eid.get("actionCompleted", {}).get("label") or a.get("label")
            stderr = read_file_message(a.get("stderr", {}), self.root)
            errors = parse_diagnostics(stderr)
            atype = a.get("type", "")
            kind = "compile_error" if errors and "Compile" in atype else "action_failure"
            ref = self.ev.add("stderr", stderr)
            diag: dict[str, Any] = {
                "kind": kind,
                "action_type": atype,
                "exit_code": a.get("exitCode"),
                "errors": errors,
                "configuration": eid.get("actionCompleted", {}).get("configuration"),
            }
            fd = a.get("failureDetail")
            if fd:
                cat = next((k for k in fd if k != "message"), None)
                diag["failure_category"] = cat
                diag["failure_code"] = (fd.get(cat) or {}).get("code")
                if fd.get("message"):
                    diag["failure_message"] = fd["message"][:300]
            if a.get("primaryOutput", {}).get("name"):
                diag["primary_output"] = a["primaryOutput"]["name"]
            if ref:
                diag["evidence"] = ref
            self.add_diag(diag, label, eid)

        elif "testResult" in ev:
            r = ev["testResult"]
            status = r.get("status", "NO_STATUS")
            tid = eid.get("testResult", {})
            label = tid.get("label")
            test = self._test_record(tid)
            attempt_id = (int(tid.get("run", 1)), int(tid.get("shard", 1)),
                          int(tid.get("attempt", 1)))
            if attempt_id in test["results"]:
                return
            ei = r.get("executionInfo", {})
            diag = {
                "kind": "test_failure", "status": status,
                "run": attempt_id[0], "shard": attempt_id[1], "attempt": attempt_id[2],
                "configuration": tid.get("configuration"),
                "cached_locally": r.get("cachedLocally", False),
                "cached_remotely": ei.get("cachedRemotely", False),
            }
            if status == "PASSED":
                test["results"][attempt_id] = diag
                return
            outputs = {o.get("name"): o for o in r.get("testActionOutput", [])}
            cases: list[dict[str, Any]] = []
            if "test.xml" in outputs:
                cases = parse_junit(read_file_message(outputs["test.xml"], self.root))
            log_text = read_file_message(outputs.get("test.log", {}), self.root)
            extracted = truncate_test_log(log_text, self.root)
            if extracted:
                kept_text, _, framework = extracted
                ref = self.ev.add("test.log", kept_text, framework=framework,
                                  total_lines=len(log_text.splitlines()))
            else:
                ref = self.ev.add("test.log", log_text)
            diag.update(cases=cases, errors=[],
                        description=r.get("statusDetails") or f"Test attempt status: {status}.")
            if ei.get("exitCode") is not None:
                diag["exit_code"] = ei["exitCode"]
            if ei.get("strategy"):
                diag["strategy"] = ei["strategy"]
            if ref:
                diag["evidence"] = ref
            test["results"][attempt_id] = diag
            self.add_diag(diag, label, {"testResult": {
                "label": label, "configuration": tid.get("configuration"),
                "run": attempt_id[0], "shard": attempt_id[1], "attempt": attempt_id[2]}})

        elif "testSummary" in ev:
            s = ev["testSummary"]
            test = self._test_record(eid.get("testSummary", {}))
            test["summary"] = s
            if s.get("failed") and not test.get("summary_evidence"):
                test["summary_evidence"] = self.ev.add(
                    "test.log", read_file_message(s["failed"][0], self.root))

        elif "targetSummary" in ev:
            # Bazel can omit TestSummary for tests that failed to build. A
            # non-test target has NO_STATUS and must not enter test counts.
            status = ev["targetSummary"].get("overallTestStatus", "NO_STATUS")
            if status != "NO_STATUS":
                test = self._test_record(eid.get("targetSummary", {}))
                test["target_summary"] = {"overallStatus": status}

        elif "aborted" in ev:
            a = ev["aborted"]
            label = next((value.get("label") for value in eid.values()
                          if isinstance(value, dict) and value.get("label")), None)
            description = a.get("description") or ""
            diag = {
                "kind": "analysis_error" if a.get("reason") in
                        ("ANALYSIS_FAILURE", "LOADING_FAILURE") else "aborted",
                "reason": a.get("reason"),
                "errors": parse_diagnostics(description),
                "description": description[:500],
                "event_id": eid,
            }
            if description:
                diag["evidence"] = self.ev.add("analysis", description)
            fp = self.add_diag(diag, label, eid)
            if eid:
                key = self.event_key(eid)
                self._abort_events[key] = fp
                self._abort_children[key] = [self.event_key(child)
                                             for child in ev.get("children", [])]

        elif "completed" in ev:
            # Proto JSON omits scalar defaults: missing success means false.
            if not ev["completed"].get("success", False):
                self.counts["targets_failed"] += 1

        elif "finished" in ev:
            f = ev["finished"]
            exit_status = f.get("exitCode")
            # Within an existing ExitCode message an omitted int32 is zero;
            # an absent message does not establish an invocation outcome.
            code = exit_status.get("code", 0) if isinstance(exit_status, dict) else None
            self.invocation["exit"] = {
                "code": code,
                "name": (exit_status or {}).get("name"),
            }
            if code is not None:
                self.invocation["overall_success"] = code == 0
            elif "overallSuccess" in f:
                self.invocation["overall_success"] = f["overallSuccess"]
            if f.get("failureDetail"):
                self.invocation["failure_detail"] = f["failureDetail"]

        elif "buildMetrics" in ev:
            self.invocation["metrics"] = self._metrics(ev["buildMetrics"])

    @staticmethod
    def _metrics(m: dict[str, Any]) -> dict[str, Any]:
        """BuildMetrics is aggregate: emitted regardless of publish_all_actions."""
        out: dict[str, Any] = {}
        asm = m.get("actionSummary", {})

        if asm.get("actionsExecuted") is not None:
            out["actions_executed"] = int(asm["actionsExecuted"])
        if asm.get("actionsCreated") is not None:
            out["actions_created"] = int(asm["actionsCreated"])

        # per-mnemonic breakdown, heaviest first
        mnem = []
        for ad in asm.get("actionData", []):
            executed = int(ad.get("actionsExecuted", 0))
            if not executed:
                continue
            row: dict[str, Any] = {"mnemonic": ad.get("mnemonic", "?"), "executed": executed}
            span_ms = int(ad.get("lastEndedMs", 0)) - int(ad.get("firstStartedMs", 0))
            if span_ms > 0:
                row["span_ms"] = span_ms
            cpu = _dur_s(ad.get("userTime")) + _dur_s(ad.get("systemTime"))
            if cpu:
                row["cpu_s"] = round(cpu, 1)
            mnem.append(row)
        mnem.sort(key=lambda r: r.get("cpu_s", 0) or r["executed"], reverse=True)
        if mnem:
            out["by_mnemonic"] = mnem[:12]

        # Runner counters describe spawn outcomes; `total`, `internal`, and
        # `action cache hit` count actions, so they must not enter this denominator.
        # These counters cannot reveal the remote cache's lookup hit rate.
        runners = {r.get("name", "?"): int(r.get("count", 0))
                   for r in asm.get("runnerCount", []) if int(r.get("count", 0)) > 0}
        if runners:
            out["runners"] = dict(sorted(runners.items(), key=lambda kv: -kv[1]))
            kinds = {r.get("name"): r.get("execKind", "").lower()
                     for r in asm.get("runnerCount", [])}
            cache = {"remote_hits": 0, "disk_hits": 0, "other_hits": 0,
                     "executions": 0, "remote_executions": 0}
            for name, count in runners.items():
                normalized = name.lower()
                if normalized in ("total", "internal", "action cache hit"):
                    continue
                if normalized == "remote cache hit":
                    cache["remote_hits"] += count
                elif normalized == "disk cache hit":
                    cache["disk_hits"] += count
                elif normalized.endswith("cache hit"):
                    cache["other_hits"] += count
                else:
                    cache["executions"] += count
                    if kinds.get(name) == "remote" or normalized == "remote":
                        cache["remote_executions"] += count
            hits = cache["remote_hits"] + cache["disk_hits"] + cache["other_hits"]
            outcomes = hits + cache["executions"]
            if outcomes:
                out["spawn_cache"] = {**cache, "outcomes": outcomes,
                                      "hit_rate": round(hits / outcomes, 3)}

        # local action cache, with miss reasons
        acs = asm.get("actionCacheStatistics", {})
        if acs:
            hits, misses = int(acs.get("hits", 0)), int(acs.get("misses", 0))
            ac: dict[str, Any] = {"hits": hits, "misses": misses}
            if hits + misses:
                ac["hit_rate"] = round(hits / (hits + misses), 3)
            reasons = {d.get("reason", "?"): int(d.get("count", 0))
                       for d in acs.get("missDetails", []) if int(d.get("count", 0))}
            if reasons:
                ac["miss_reasons"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1]))
            out["action_cache"] = ac

        tm = m.get("timingMetrics", {})
        timing = {k: int(v) for k, v in {
            "wall_ms": tm.get("wallTimeInMs"),
            "analysis_ms": tm.get("analysisPhaseTimeInMs"),
            "execution_ms": tm.get("executionPhaseTimeInMs"),
        }.items() if v}
        crit = _dur_s(tm.get("criticalPathTime"))
        if crit:
            timing["critical_path_s"] = round(crit, 1)
        if timing:
            out["timing"] = timing

        tgt = m.get("targetMetrics", {})
        if tgt.get("targetsConfigured"):
            out["targets_configured"] = int(tgt["targetsConfigured"])
        if m.get("packageMetrics", {}).get("packagesLoaded"):
            out["packages_loaded"] = int(m["packageMetrics"]["packagesLoaded"])
        peak = m.get("memoryMetrics", {}).get("peakPostGcHeapSize")
        if peak:
            out["peak_heap_mb"] = int(peak) // (1024 * 1024)
        return out

    # -- output -----------------------------------------------------------
    def finish(self, input_bytes: int) -> dict[str, Any]:
        self.invocation["stream"] = self.stream.as_dict()
        resolved = self._resolved_diagnostics()
        failure = self.invocation.get("failure_detail", {})
        failed = self.invocation.get("exit", {}).get("code") not in (None, 0)
        if failed and not resolved:
            resolved.append({"kind": "invocation_failure", "_targets": [],
                             "description": failure.get("message") or
                             "Bazel failed without a structured diagnostic."})
        # Global logs are evidence about the invocation, not proof of which
        # target caused an error. Keep that distinction explicit in the output.
        fallback = self._progress_stderr
        if self.console_output and self.console_output != fallback:
            fallback = self.console_output + ("\n[BEP progress]\n" + fallback if fallback else "")
        if fallback and (failed or resolved):
            lines = [line for line in fallback.splitlines() if not _NOISE.match(line)]
            start = next((i for i, line in enumerate(lines) if line.startswith("ERROR:")), 0)
            self.invocation["failure_context"] = bounded_log("\n".join(lines[start:start + 20]), 2048)
        for d in resolved:
            if not (d.get("description") or d.get("failure_message") or
                    d.get("errors") or d.get("cases")):
                d["description"] = (failure.get("message") or
                                    f"{d.get('reason', 'Failure')}: no detailed message available.")
            if not d.get("evidence") and fallback:
                d["evidence"] = self.ev.add("invocation_log", fallback)
                d["evidence_scope"] = "invocation"
        diags = []
        for d in resolved[:MAX_DIAGNOSTICS]:
            targets = d.pop("_targets", [])
            if targets:
                d["target"] = targets[0]
                if len(targets) > 1:
                    d["also_affects"] = targets[1:11]
                    d["affected_count"] = len(targets)
            d = {k: v for k, v in d.items() if v not in ([], "", None, {})}
            d["id"] = f"d{len(diags) + 1}"
            diags.append(d)

        omitted = max(0, len(resolved) - MAX_DIAGNOSTICS)
        # evidence for dropped diagnostics is dead weight
        live = {d["evidence"] for d in diags if "evidence" in d}
        evidence = {k: v for k, v in self.ev.store.items() if k in live}

        out = {
            "schema": SCHEMA,
            "invocation": self.invocation,
            "summary": {k: v for k, v in {**self.counts, **self._test_counts()}.items() if v},
            "diagnostics": diags,
            "evidence": evidence,
        }
        if omitted:
            out["summary"]["diagnostics_omitted"] = omitted
        if self.audit_tests:
            out["test_audit"] = self._test_audit()
        blob = json.dumps(out, separators=(",", ":"))
        out["stats"] = {
            "input_bytes": input_bytes,
            "output_bytes": len(blob),
            "ratio": round(len(blob) / input_bytes, 4) if input_bytes else None,
        }
        return out


def _parse_event_lines(lines: Iterator[bytes], status: StreamStatus) -> Iterator[dict[str, Any]]:
    """Fast NDJSON path, with string-aware framing for multiline objects."""
    parts: list[bytes] = []
    depth = 0
    quoted = escaped = False
    for line in lines:
        if not parts:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if not line.lstrip().startswith(b'{'):
                    status.malformed_records += 1
                    continue
            else:
                if isinstance(event, dict):
                    status.events_read += 1
                    yield event
                else:
                    status.malformed_records += 1
                continue
        parts.append(line)
        for char in line:
            if quoted:
                if escaped:
                    escaped = False
                elif char == 92:  # backslash
                    escaped = True
                elif char == 34:
                    quoted = False
            elif char == 34:
                quoted = True
            elif char in (123, 91):
                depth += 1
            elif char in (125, 93):
                depth -= 1
        if depth <= 0:
            try:
                event = json.loads(b''.join(parts))
            except (ValueError, UnicodeDecodeError):
                status.malformed_records += 1
            else:
                status.events_read += 1
                yield event
            parts, depth, quoted, escaped = [], 0, False, False
    if parts:
        status.trailing_fragment = True


def iter_events(path: str, status: Optional[StreamStatus] = None) -> Iterator[dict[str, Any]]:
    """Read NDJSON or pretty-printed BEP without silently losing malformed input."""
    with open(path, "rb") as fh:
        yield from _parse_event_lines(fh, status if status is not None else StreamStatus())


def _file_signature(path: str):
    try:
        st = os.stat(path)
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
    except FileNotFoundError:
        return None


def _tail_lines(path: str, is_alive, previous_signature=None) -> Iterator[bytes]:
    """Follow a BEP file while Bazel writes it.

    FileTransport flushes a BufferedOutputStream every 250ms
    (EXPERIMENTAL_BEP_FILE_FLUSH_MILLIS), so a flush can land mid-line: the
    buffer is byte-oriented, not line-oriented. We therefore only parse on a
    trailing newline and carry the remainder forward.
    """
    import time
    pending = b""
    while (signature := _file_signature(path)) is None or signature == previous_signature:
        if not is_alive():
            # The writer may have created/replaced the file between stat and poll.
            signature = _file_signature(path)
            if signature is not None and signature != previous_signature:
                break
            return
        time.sleep(0.05)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if chunk:
                pending += chunk
                *complete, pending = pending.split(b"\n")
                for line in complete:
                    yield line + b"\n"
                continue
            if not is_alive():
                # drain whatever landed between the last read and process exit
                # The process may have written between our read and poll.
                while chunk := fh.read(65536):
                    pending += chunk
                    *complete, pending = pending.split(b"\n")
                    for line in complete:
                        yield line + b"\n"
                if pending:
                    yield pending
                return
            time.sleep(0.05)


def tail_events(path: str, is_alive, status: Optional[StreamStatus] = None,
                previous_signature=None) -> Iterator[dict[str, Any]]:
    # Keep partial UTF-8 bytes intact until a complete line is available.
    yield from _parse_event_lines(_tail_lines(path, is_alive, previous_signature),
                                 status if status is not None else StreamStatus())


def may_add_failure(ev: dict[str, Any]) -> bool:
    """Only these events can increase the number of actionable root causes.

    In particular, scanning every accumulated test on progress and passing
    events makes a healthy invocation quadratic when --fail-fast is enabled.
    Failed attempts still need reconciliation: a summary can arrive first.
    """
    return (isinstance(ev.get("aborted"), dict) or
            (isinstance(ev.get("action"), dict) and not ev["action"].get("success")) or
            any(isinstance(ev.get(key), dict) and ev[key].get(field) in FAILED_TEST_STATUSES
                for key, field in (("testResult", "status"),
                                   ("testSummary", "overallStatus"),
                                   ("targetSummary", "overallTestStatus"))))


_BUNDLE_COMPANIONS = {"build_event_binary_file": "bep_binary", "build_event_text_file": "bep_text",
                      "profile": "profile", "execution_log_json_file": "execution_log_json",
                      "execution_log_binary_file": "execution_log_binary",
                      "execution_log_compact_file": "execution_log_compact", "explain": "explain"}


class InvocationBundle:
    """A new, private directory holding immutable snapshots and a versioned manifest."""

    def __init__(self, directory: str, mode: str):
        from datetime import datetime, timezone
        self.root = Path(directory).absolute()
        self.root.mkdir(mode=0o700)  # Never reuse or overwrite an existing directory.
        (self.root / "blobs").mkdir(mode=0o700)
        self.manifest: dict[str, Any] = {
            "schema": "bzlog/bundle-v1", "state": "collecting", "mode": mode,
            "created_at": datetime.now(timezone.utc).isoformat(), "capture_cwd": os.getcwd(),
            "scope": "Selected diagnostic files and raw captures; not a hermetic source/environment archive. "
                     "Post-hoc file references reflect files available at capture time.",
            "artifacts": [], "errors": [],
        }
        self._event_files: dict[str, dict[str, Any]] = {}
        self._pending_logs: list[tuple[dict, dict, Optional[str]]] = []
        self._before: dict[tuple[str, str], Any] = {}
        self._effective: dict[str, str] = {}
        self.evidence_saved = False
        self._write_json("manifest.json", self.manifest)
        if mode == "live":
            self._source_state()

    def _source_state(self) -> None:
        import subprocess
        try:
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL,
                                           text=True, timeout=3).strip()
            dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"],
                                            stderr=subprocess.DEVNULL, timeout=3)
            self.manifest["source_state"] = {"git_head": head, "tracked_dirty": bool(dirty),
                                              "untracked_files_checked": False,
                                              "observed_at": "before_invocation", "contents_saved": False}
        except (OSError, subprocess.SubprocessError):
            self.manifest["source_state"] = {"status": "unavailable"}

    def _write_json(self, name: str, value: Any) -> dict[str, Any]:
        import tempfile
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            os.replace(temporary, self.root / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def _blob(self, source, limit: Optional[int] = None) -> dict[str, Any]:
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix=".copying-", dir=self.root)
        digest, size, suffix = hashlib.sha256(), 0, ""
        try:
            with os.fdopen(fd, "wb") as out:
                while limit is None or size < limit:
                    chunk = source.read(65536 if limit is None else min(65536, limit - size))
                    if not chunk:
                        break
                    if size == 0 and chunk.startswith(b"\x1f\x8b"):
                        suffix = ".gz"
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            relative = "blobs/" + digest.hexdigest() + suffix
            if not (self.root / relative).exists():
                os.replace(temporary, self.root / relative)
            return {"path": relative, "bytes": size, "sha256": digest.hexdigest()}
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _entry(self, kind: str, **fields) -> dict[str, Any]:
        artifacts = self.manifest["artifacts"]
        entry = {"id": f"a{len(artifacts) + 1}", "kind": kind, "status": "capture_error", **fields}
        artifacts.append(entry)
        return entry

    def capture_stream(self, stream, kind: str, **fields) -> dict[str, Any]:
        entry = self._entry(kind, **fields)
        try:
            entry.update(self._blob(stream), status="saved")
        except OSError as exc:
            entry.update(status="write_error", error=str(exc))
        return entry

    def capture_file(self, filename: str, kind: str, **fields) -> dict[str, Any]:
        import stat
        entry = self._entry(kind, source_uri=Path(filename).absolute().as_uri(), **fields)
        try:
            # Nonblocking open + fstat avoids hanging on a FIFO or reading a device.
            fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    entry["status"] = "not_regular"
                    return entry
                try:
                    snapshot = self._blob(source, before.st_size)
                except OSError as exc:
                    entry.update(status="copy_error", error=str(exc))
                    return entry
                after = os.fstat(source.fileno())
                signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                stable = (signature(before) == signature(after)
                          and signature(after) == _file_signature(filename)
                          and snapshot["bytes"] == after.st_size)
                entry.update(snapshot, status="saved" if stable else "changed_during_copy")
        except FileNotFoundError:
            entry["status"] = "missing"
        except (OSError, ValueError) as exc:
            entry.update(status="unreadable", error=str(exc))
        return entry

    def capture_reference(self, fm: dict, kind: str, root: Optional[str], **fields) -> dict[str, Any]:
        import base64
        import io
        if "contents" in fm:
            try:
                raw = base64.b64decode(fm["contents"], validate=True)
            except (ValueError, TypeError):
                return self._entry(kind, status="invalid_inline", **fields)
            return self.capture_stream(io.BytesIO(raw), kind, source="inline", **fields)
        uri = fm.get("uri", "")
        if not isinstance(uri, str) or not uri:
            return self._entry(kind, status="no_reference", **fields)
        try:
            parsed = urlparse(uri)
        except ValueError as exc:
            return self._entry(kind, status="invalid_uri", source_uri=uri, error=str(exc), **fields)
        if parsed.scheme and parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            return self._entry(kind, status="remote_only", source_uri=uri, **fields)
        path = unquote(parsed.path) if parsed.scheme == "file" else uri
        if not os.path.isabs(path):
            path = os.path.join(root or os.getcwd(), path)
        return self.capture_file(path, kind, **fields)

    def capture_event(self, event: dict[str, Any], root: Optional[str]) -> dict[str, Any]:
        import copy
        structured = event.get("structuredCommandLine", {})
        if structured.get("commandLineLabel") == "canonical":
            for section in structured.get("sections", []):
                if section.get("sectionLabel") == "command options":
                    for option in section.get("optionList", {}).get("option", []):
                        if option.get("optionName") in _BUNDLE_COMPANIONS:
                            value = option.get("optionValue", "")
                            if isinstance(value, str):
                                self._effective[option["optionName"]] = value
        if not any(key in event for key in ("action", "testResult", "testSummary", "buildToolLogs")):
            return event
        snapshot = copy.deepcopy(event)
        files: list[tuple[str, str, dict]] = []
        for name in ("stdout", "stderr"):
            if name in snapshot.get("action", {}):
                files.append(("action." + name, "action_" + name, snapshot["action"][name]))
        for i, fm in enumerate(snapshot.get("testResult", {}).get("testActionOutput", [])):
            if fm.get("name") in ("test.log", "test.xml"):
                files.append((f"testResult.testActionOutput[{i}]", fm["name"], fm))
        for outcome in ("passed", "failed"):
            for i, fm in enumerate(snapshot.get("testSummary", {}).get(outcome, [])):
                files.append((f"testSummary.{outcome}[{i}]", "test_summary_log", fm))
        for i, fm in enumerate(snapshot.get("buildToolLogs", {}).get("log", [])):
            self._pending_logs.append((fm, {"event_id": event.get("id", {}),
                                       "field": f"buildToolLogs.log[{i}]", "name": fm.get("name")}, root))
        for field_name, kind, fm in files:
            key = json.dumps([event.get("id", {}), field_name], sort_keys=True)
            if key not in self._event_files:
                self._event_files[key] = self.capture_reference(
                    fm, kind, root, event_id=event.get("id", {}), field=field_name,
                    provenance="bep_reference")
            entry = self._event_files[key]
            if entry["status"] == "saved":
                # Diagnostics and full evidence now use the same immutable bytes.
                fm.pop("contents", None)
                fm["uri"] = (self.root / entry["path"]).as_uri()
        return snapshot

    def prepare_companions(self, argv: list[str]) -> None:
        # These are only candidate paths until Bazel's canonical metadata
        # confirms an effective option. A test_arg value can look like a flag.
        for i, token in enumerate(argv):
            name, sep, value = token.lstrip("-").partition("=")
            if token.startswith("--") and name in _BUNDLE_COMPANIONS:
                if not sep and i + 1 < len(argv):
                    value = argv[i + 1]
                if value:
                    try:
                        self._before[(name, value)] = _file_signature(value)
                    except OSError:
                        self._before[(name, value)] = "unreadable"

    def finish_captures(self) -> None:
        for fm, fields, root in self._pending_logs:
            kind = "profile" if fm.get("name") == "command.profile.gz" else "build_tool_log"
            self.capture_reference(fm, kind, root, provenance="bep_reference", **fields)
        self._pending_logs.clear()
        for name, value in self._effective.items():
            if not value:
                continue
            # Profiles and other tool logs can already be associated by BEP.
            uri = Path(value).absolute().as_uri()
            if any(a.get("source_uri") == uri for a in self.manifest["artifacts"]):
                continue
            before = self._before.get((name, value), "not_observed")
            try:
                current = _file_signature(value)
            except OSError:
                current = "unreadable"
            if before in ("not_observed", "unreadable") or current == "unreadable":
                self._entry(_BUNDLE_COMPANIONS[name], status="unverified", source_uri=uri,
                            reason="No usable pre-invocation observation of this effective capture path.")
            elif current is None or current == before:
                self._entry(_BUNDLE_COMPANIONS[name], status="not_produced", source_uri=uri)
            else:
                self.capture_file(value, _BUNDLE_COMPANIONS[name], provenance="effective_option_and_fresh_file")

    def finalize(self, report: dict[str, Any]) -> None:
        self.finish_captures()
        self.manifest["invocation"] = {k: report["invocation"][k] for k in
            ("uuid", "command", "bazel_version", "workspace", "exit", "stream") if k in report["invocation"]}
        saved_report = dict(report, bundle={"manifest": "manifest.json", "paths_relative_to": "bundle_directory"})
        for name, value in (("evidence.json", report.get("evidence", {})), ("report.json", saved_report)):
            try:
                artifact = self._write_json(name, value)
                self._entry(name, status="saved", **artifact)
                if name == "evidence.json":
                    self.evidence_saved = True
            except OSError as exc:
                self.manifest["errors"].append(f"{name}: {exc}")
        self.manifest["state"] = "finalized"
        self.manifest["capture_status"] = ("partial" if self.manifest["errors"] or any(
            a["status"] != "saved" for a in self.manifest["artifacts"]) else "complete")
        try:
            self._write_json("manifest.json", self.manifest)
            persisted = True
        except OSError as exc:
            self.manifest["errors"].append(f"manifest.json: {exc}")
            persisted = False
        report["bundle"] = {"directory": str(self.root), "manifest": str(self.root / "manifest.json"),
                            "capture_status": self.manifest["capture_status"] if persisted else "partial",
                            "manifest_saved": persisted,
                            "unavailable_artifacts": sum(a["status"] != "saved" for a in self.manifest["artifacts"])}
        if self.manifest["errors"]:
            report["bundle"]["errors"] = self.manifest["errors"]
            print("bzlog: bundle could not be fully saved; see bundle errors", file=sys.stderr)


def verify_bundle(argv: list[str]) -> int:
    import stat
    ap = argparse.ArgumentParser(prog="bzlog verify", description="Verify the saved bytes in an invocation bundle.")
    ap.add_argument("directory")
    args = ap.parse_args(argv)
    errors, checked = [], 0
    manifest = {}
    try:
        root = Path(args.directory).resolve()
        manifest_path = (root / "manifest.json").resolve()
        if not manifest_path.is_relative_to(root):
            raise ValueError("Manifest path leaves bundle directory.")
        fd = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Manifest is not a regular file.")
            manifest = json.loads(fh.read(info.st_size))
        if manifest.get("schema") != "bzlog/bundle-v1" or manifest.get("state") != "finalized":
            raise ValueError("Not a finalized bzlog/bundle-v1 manifest.")
        if not isinstance(manifest.get("artifacts"), list):
            raise ValueError("Manifest artifacts must be a list.")
        for artifact in manifest["artifacts"]:
            if not isinstance(artifact, dict):
                raise ValueError("Each manifest artifact must be an object.")
            if "path" not in artifact:
                if artifact.get("status") == "saved":
                    errors.append(f"{artifact['id']}: saved artifact has no path")
                continue
            relative = Path(artifact["path"])
            path = (root / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(root):
                errors.append(f"{artifact['id']}: path leaves bundle directory")
                continue
            try:
                digest, size = hashlib.sha256(), 0
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                with os.fdopen(fd, "rb") as fh:
                    info = os.fstat(fh.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        errors.append(f"{artifact['id']}: not a regular file")
                        continue
                    while size < info.st_size:
                        chunk = fh.read(min(65536, info.st_size - size))
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                    after = os.fstat(fh.fileno())
                    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                    if signature(info) != signature(after) or signature(after) != _file_signature(str(path)):
                        errors.append(f"{artifact['id']}: file changed during verification")
                checked += 1
                if digest.hexdigest() != artifact["sha256"] or size != artifact["bytes"]:
                    errors.append(f"{artifact['id']}: size or SHA-256 mismatch")
            except OSError as exc:
                errors.append(f"{artifact['id']}: {exc}")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        errors.append(str(exc))
    if not isinstance(manifest, dict):
        manifest = {}
    invocation = manifest.get("invocation", {})
    stream = invocation.get("stream", {}) if isinstance(invocation, dict) else {}
    print(json.dumps({"schema": "bzlog/verify-v1", "integrity": "failed" if errors else "ok",
                      "files_checked": checked, "capture_status": manifest.get("capture_status"),
                      "stream_status": stream.get("status") if isinstance(stream, dict) else None,
                      "errors": errors}, separators=(",", ":")))
    return 2 if errors else 0


def run_bazel(argv: list[str], c: "Container", emit_progress: bool,
              fail_fast: int) -> tuple[int, int]:
    """Spawn Bazel and consume its event stream live. Returns (exit_code, bep_bytes)."""
    import subprocess
    import tempfile

    # Respect a BEP path the user already asked for; otherwise use our own.
    cut = argv.index("--") if "--" in argv else len(argv)
    bazel_args = argv[:cut]
    if c.bundle:
        c.bundle.prepare_companions(bazel_args)
    user_bep = None
    for i, arg in enumerate(bazel_args):
        if arg.startswith("--build_event_json_file="):
            user_bep = arg.split("=", 1)[1]
        elif arg == "--build_event_json_file" and i + 1 < len(bazel_args):
            user_bep = bazel_args[i + 1]
    own_flags: list[str] = []
    if user_bep:
        bep, ephemeral = user_bep, False
    else:
        fd, bep = tempfile.mkstemp(prefix="bzlog-", suffix=".json")
        os.close(fd)
        ephemeral = True
        own_flags.append(f"--build_event_json_file={bep}")
    if not any(a.split("=", 1)[0] in ("--build_event_json_file_path_conversion",
                                     "--nobuild_event_json_file_path_conversion") for a in bazel_args):
        # keep file:// URIs so stderr and test.log stay readable from disk
        own_flags.append("--build_event_json_file_path_conversion=false")

    # `bazel run //x -- args` : our flags must land before the separator,
    # otherwise they are handed to the target binary.
    cmd = ["bazel", *bazel_args, *own_flags, *argv[cut:]]
    previous_signature = _file_signature(bep)

    # A file avoids pipe backpressure while preserving startup and fetch errors
    # that may never reach the BEP. Only bounded context enters the report.
    console = tempfile.TemporaryFile(mode="w+b")
    try:
        proc = subprocess.Popen(cmd, stdout=console, stderr=console)
    except OSError as exc:
        console.close()
        c.console_output = f"ERROR: cannot start Bazel: {exc}"
        if c.bundle:
            import io
            c.bundle.capture_stream(io.BytesIO(c.console_output.encode()), "console", provenance="wrapper")
            c.bundle._entry("bep_json", status="not_produced")
        if ephemeral:
            os.unlink(bep)
        return (127 if isinstance(exc, FileNotFoundError) else 126), 0

    seen = 0
    killed = False
    try:
        for ev in tail_events(bep, lambda: proc.poll() is None, c.stream, previous_signature):
            c.consume(ev)
            if emit_progress and len(c._order) > seen:
                for fp in c._order[seen:]:
                    d = c._diags[fp]
                    print(json.dumps({
                        "event": "diagnostic",
                        "kind": d.get("kind"),
                        "target": (d.get("_targets") or [None])[0],
                        "first_error": ((d.get("errors") or [{}])[0].get("message")
                                        or d.get("description") or d.get("failure_message")
                                        or d.get("reason")),
                        **({"provisional": True, **{field: d.get(field) for field in
                             ("configuration", "run", "shard", "attempt")}}
                           if d.get("kind") == "test_failure" else {}),
                    }, separators=(",", ":")), file=sys.stderr, flush=True)
                seen = len(c._order)
            if emit_progress and "testSummary" in ev:
                print(json.dumps({"event": "test_summary",
                                  "target": ev.get("id", {}).get("testSummary", {}).get("label"),
                                  "configuration": ev.get("id", {}).get("testSummary", {}).get("configuration"),
                                  "status": ev["testSummary"].get("overallStatus", "NO_STATUS")},
                                 separators=(",", ":")), file=sys.stderr, flush=True)
            # Empty analysis parents are placeholders, not distinct causes.
            causes = sum(bool(d.get("errors") or d.get("cases") or d.get("evidence")
                              or d.get("description") or d.get("failure_message"))
                         for d in c._resolved_diagnostics()
                         if d["kind"] not in ("test_flaky", "test_recovered", "test_attempt_failure",
                                              "test_incomplete")) if (
                             fail_fast and not killed and may_add_failure(ev)) else 0
            if fail_fast and causes >= fail_fast and not killed and proc.poll() is None:
                # enough distinct root causes; stop burning CI time.
                # SIGINT lets Bazel finish writing BuildMetrics and Aborted events.
                proc.send_signal(2)
                killed = True
                c.invocation["stopped_early"] = {"reason": "fail_fast",
                                                 "distinct_causes": causes}
    finally:
        code = proc.wait()
        console.seek(0, os.SEEK_END)
        console_size = console.tell()
        console.seek(0)
        captured = console.read(32768)
        if console_size > 32768:
            console.seek(max(32768, console_size - 32768))
            if console_size > 65536:
                captured += b"\n[... invocation output omitted ...]\n"
            captured += console.read(32768)
        c.console_output = captured.decode("utf-8", "replace")
        if c.bundle:
            console.seek(0)
            c.bundle.capture_stream(console, "console", provenance="wrapper")
        console.close()
        signature = _file_signature(bep)
        size = signature[2] if signature and signature != previous_signature else 0
        if c.bundle:
            if signature and signature != previous_signature:
                c.bundle.capture_file(bep, "bep_json", provenance="wrapper_fresh_file")
            else:
                c.bundle._entry("bep_json", status="not_produced")
        if ephemeral:
            try:
                os.unlink(bep)
            except OSError:
                pass
    return code, size


BAZEL_VERBS = {
    "analyze-profile", "aquery", "build", "canonicalize-flags", "clean", "coverage",
    "cquery", "dump", "fetch", "help", "info", "license", "mobile-install", "mod",
    "print_action", "query", "run", "shutdown", "sync", "test", "vendor", "version",
}

_OWN_BOOL = {"--pretty", "--progress", "--all-metrics", "--record-command", "--audit-tests",
             "--version", "--help", "-h"}
_OWN_VALUE = {"--root", "--fail-fast", "--max-diagnostics", "--format", "--evidence", "--bundle"}


def split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split our flags from Bazel's argv.

    argparse.REMAINDER is unreliable, and requiring `--run` is ceremony. Rule:
    our flags come first; the first token we do not own starts Bazel's command
    line and everything after it is passed through untouched. An explicit `--`
    also works.
    """
    own: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return own, argv[i + 1:]
        if a == "--run":  # accepted for back-compat
            return own, argv[i + 1:]
        if a in _OWN_BOOL:
            own.append(a)
            i += 1
            continue
        if a in _OWN_VALUE:
            own.extend(argv[i:i + 2])
            i += 2
            continue
        if any(a.startswith(f + "=") for f in _OWN_VALUE):
            own.append(a)
            i += 1
            continue
        return own, argv[i:]
    return own, []


def enrich(result: dict[str, Any], root: Optional[str], snippet_lines: int = 2) -> None:
    """Post-process: add the things an agent would otherwise spend a tool call on."""
    for d in result.get("diagnostics", []):
        # 1. the source line itself. An agent's next move after "conn.h:88: error"
        #    is always to read conn.h. ~30 tokens here saves a read_file round trip.
        for e in d.get("errors", [])[:1]:
            path = e.get("file")
            line = e.get("line")
            if not path or not line:
                continue
            full = os.path.join(root, path) if root and not os.path.isabs(path) else path
            if not os.path.isfile(full):
                continue
            try:
                with open(full, errors="replace") as fh:
                    src = fh.read().splitlines()
            except OSError:
                continue
            lo, hi = max(0, line - 1 - snippet_lines), min(len(src), line + snippet_lines)
            e["context"] = [f"{i + 1}: {src[i].rstrip()[:120]}" for i in range(lo, hi)]

        # 2. A suggested narrow command. Original flags live in opt-in replay
        # metadata; a framework-specific case filter cannot be inferred safely.
        tgt = d.get("target")
        if not tgt:
            continue
        if d.get("kind") in ("test_failure", "test_flaky", "test_recovered",
                             "test_attempt_failure", "test_incomplete"):
            import shlex
            d["repro"] = f"bazel test {shlex.quote(tgt)} --test_output=streamed"
        elif d.get("kind") in ("compile_error", "action_failure"):
            import shlex
            d["repro"] = f"bazel build {shlex.quote(tgt)}"
        if d.get("repro"):
            d["repro_scope"] = "suggested; original configuration and environment required"


def prune_metrics(inv: dict[str, Any]) -> None:
    """Report metrics by exception. A healthy 94% cache hit rate is not news;
    a 40% one is. Emitting the full block every run is pure token tax."""
    m = inv.get("metrics")
    if not m:
        return
    keep: dict[str, Any] = {}
    cache = m.get("spawn_cache", {})
    hit = cache.get("hit_rate")
    cache_activity = sum(cache.get(k, 0) for k in
                         ("remote_hits", "disk_hits", "other_hits", "remote_executions"))
    if hit is not None and hit < 0.7 and cache_activity:
        keep["spawn_cache"] = cache
        keep["runners"] = m.get("runners")
    # Digest mismatches include ordinary source edits. Keep the raw miss reasons
    # under --all-metrics, but do not infer non-hermeticity from this count alone.
    t = m.get("timing", {})
    if t.get("wall_ms", 0) > 120_000:
        keep["timing"] = t
    if keep:
        inv["metrics"] = keep
    else:
        inv.pop("metrics", None)


def render_text(result: dict[str, Any]) -> str:
    """Terse rendering. JSON spends ~30% of its tokens on punctuation the model
    does not need; it only matters if a harness parses the output."""
    out: list[str] = []
    inv = result.get("invocation", {})
    s = result.get("summary", {})
    head = f"{inv.get('command','build')} {' '.join(inv.get('patterns', []))}".strip()
    ex = inv.get("exit", {}).get("name") or inv.get("exit", {}).get("code")
    out.append(f"{head} -> {ex}")
    bits = [f"{v} {k.replace('_', ' ')}" for k, v in s.items()]
    if bits:
        out.append("  " + ", ".join(bits))
    if inv.get("stopped_early"):
        out.append(f"  stopped early: {inv['stopped_early']['reason']}")
    stream = inv.get("stream", {})
    if stream and stream.get("status") != "complete":
        out.append("  stream: " + stream["status"] + " " + json.dumps(stream, separators=(",", ":")))
    if inv.get("failure_context"):
        out.append("invocation context:")
        out.extend("  " + line for line in inv["failure_context"].splitlines())
    if inv.get("replay"):
        out.append(f"replay argv (cwd {inv['replay']['cwd']}): {inv['replay']['command']}")
    if result.get("bundle"):
        bundle = result["bundle"]
        out.append(f"bundle: {bundle['manifest']} ({bundle.get('capture_status', 'see manifest')})")
    if "test_audit" in result:
        audit = result["test_audit"]
        out.append("test audit: " + ", ".join(f"{v} {k}" for k, v in audit["counts"].items()))
        out.append("  " + audit["scope"])
        out.extend("  " + notice for notice in audit["notices"])
        for row in audit["tests"]:
            config = (row.get("configuration") or {}).get("id", "unknown")
            out.append(f"  {row['label']} [{config}]: {row['status']}, {row['execution']}"
                       f"; observed attempts {row['attempts']['observed']}")
            if row.get("issues"):
                out.append("    " + ", ".join(row["issues"]))

    shown_evidence = set()
    for d in result.get("diagnostics", []):
        n = d.get("affected_count")
        tag = f"[{d['id']}] {d.get('kind')}"
        if n:
            tag += f" x{n} targets"
        out.append(tag + f"  {d.get('target','')}")
        if "overall_status" in d:
            note = "" if d.get("final") else " (final summary unavailable)"
            out.append(f"  test status: {d['overall_status']}{note}")
            if d.get("occurrences"):
                out.append(f"  {d['occurrences']} unsuccessful attempt(s)")
            for attempt in d.get("attempts", []):
                cached = (" [local cache]" if attempt["cached_locally"] else
                          " [remote cache]" if attempt["cached_remotely"] else "")
                out.append(f"    {attempt['target']} run={attempt['run']} shard={attempt['shard']}"
                           f" attempt={attempt['attempt']}: {attempt['status']}{cached}")
            if d.get("attempts_omitted"):
                out.append(f"    {d['attempts_omitted']} additional attempt(s) omitted")
        message = d.get("description") or d.get("failure_message")
        if message:
            out.extend("  " + line for line in message.splitlines())
        for e in d.get("errors", [])[:3]:
            loc = e.get("file", "")
            if e.get("line"):
                loc += f":{e['line']}"
            out.append(f"  {loc}: {e.get('message','')}")
            for c in e.get("context", []):
                out.append(f"    {c}")
        for c in d.get("cases", [])[:5]:
            out.append(f"  FAIL {c.get('class','')}.{c['case']}: {c.get('message','')[:160]}")
        if d.get("failure_category"):
            category = d["failure_category"]
            if d.get("failure_code"):
                category += f"/{d['failure_code']}"
            out.append(f"  category: {category}")
        if d.get("repro"):
            out.append(f"  suggested repro: {d['repro']}")
        if d.get("evidence"):
            ref = d["evidence"]
            if d.get("evidence_scope") == "invocation":
                out.append("  log scope: invocation (not attributed to this target)")
            blob = result.get("evidence", {}).get(ref)
            if blob:
                if ref in shown_evidence:
                    out.append(f"  log: {ref} (shown above)")
                else:
                    out.append(f"  log: {ref}")
                    out.extend("    " + line for line in blob["text"].splitlines())
                    shown_evidence.add(ref)
            elif result.get("evidence_sidecar"):
                import shlex
                out.append(f"  log: bzlog show {shlex.quote(result['evidence_sidecar'])} {ref}")

    m = inv.get("metrics")
    if m:
        out.append("metrics: " + json.dumps(m, separators=(",", ":")))
    return "\n".join(out)


def show_evidence(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="bzlog show", description="Read a saved evidence excerpt.")
    ap.add_argument("sidecar", help="evidence_sidecar path from a bzlog report, or a bundle directory")
    ap.add_argument("evidence_id", help="evidence reference, e.g. e1")
    args = ap.parse_args(argv)
    try:
        sidecar = os.path.join(args.sidecar, "evidence.json") if os.path.isdir(args.sidecar) else args.sidecar
        with open(sidecar) as fh:
            blobs = json.load(fh)
        entry = blobs[args.evidence_id]
        text = entry["text"]
        if not isinstance(text, str):
            raise ValueError("evidence text must be a string")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"bzlog show: cannot read {args.evidence_id}: {exc}", file=sys.stderr)
        return 2
    print(text)
    if entry.get("truncated"):
        print("bzlog show: this is a truncated evidence excerpt", file=sys.stderr)
    return 0


# Remove explicitly recorded capture destinations from a narrowed preview. Rc
# files may still supply destinations; the planner never runs the command.
_CAPTURE_OPTIONS = {"build_event_json_file", "build_event_binary_file", "build_event_text_file",
                    "profile", "explain", "execution_log_json_file", "execution_log_binary_file",
                    "execution_log_compact_file"}


def command_layout(argv: list[str], structured: dict[str, Any]) -> dict[str, Any]:
    """Locate target tokens using Bazel's parsed original options, never shell splitting."""
    sections = {s.get("sectionLabel"): s for s in structured.get("sections", [])}
    command = sections.get("command", {}).get("chunkList", {}).get("chunk", [])
    if len(command) != 1 or command[0] not in {"test", "coverage"}:
        raise ValueError("Only recorded test/coverage commands can be narrowed.")
    positions = [i for i, token in enumerate(argv) if token == command[0]]
    if len(positions) != 1:
        raise ValueError("The command boundary is ambiguous in the recorded argv.")
    command_index = positions[0]
    residual = sections.get("residual", {}).get("chunkList", {}).get("chunk", [])
    if not residual:
        raise ValueError("No explicit target patterns were recorded; target files/defaults are unsupported.")
    options = sections.get("command options", {}).get("optionList", {}).get("option", [])
    target_indices, output_indices, output_flags = [], [], set()
    cursor, ri, separator = command_index + 1, 0, None
    while cursor < len(argv):
        token = argv[cursor]
        if token == "--":
            if argv[cursor + 1:] != residual[ri:]:
                raise ValueError("The recorded target suffix does not match Bazel's residual arguments.")
            separator = cursor
            target_indices.extend(range(cursor + 1, len(argv)))
            ri = len(residual)
            break
        matches = set()
        for option in options:
            form, name = option.get("combinedForm"), option.get("optionName")
            if not name or not token.startswith("-"):
                continue
            if form == token:
                matches.add((1, name))
            if (cursor + 1 < len(argv) and option.get("optionValue") == argv[cursor + 1]
                    and form == token + " " + argv[cursor + 1]):
                matches.add((2, name))
        is_target = ri < len(residual) and token == residual[ri]
        if len(matches) + int(is_target) != 1:
            raise ValueError("An argument is ambiguous or absent/redacted in Bazel's original command metadata.")
        if is_target:
            target_indices.append(cursor)
            ri += 1
            cursor += 1
        else:
            width, name = matches.pop()
            if name == "target_pattern_file":
                raise ValueError("Target-pattern files are unsupported for narrowed reruns.")
            if name in _CAPTURE_OPTIONS:
                output_indices.extend(range(cursor, cursor + width))
                output_flags.add(name)
            cursor += width
    if ri != len(residual):
        raise ValueError("Some target patterns could not be located in the original argv.")
    return {"version": 1, "command_index": command_index, "target_indices": target_indices,
            "output_indices": output_indices, "output_flags": sorted(output_flags),
            "separator_index": separator}


def rerun_plan(report: dict[str, Any]) -> dict[str, Any]:
    """Preview a label-level rerun; this function does not execute anything."""
    if report.get("schema") != SCHEMA:
        raise ValueError("Unsupported report schema.")
    inv = report.get("invocation", {})
    if inv.get("stream", {}).get("status") != "complete":
        raise ValueError("A complete BEP stream is required for a rerun plan.")
    audit = report.get("test_audit")
    if not isinstance(audit, dict) or not isinstance(audit.get("tests"), list):
        raise ValueError("Capture a JSON report with --audit-tests first.")
    failed = [row for row in audit["tests"] if row.get("final") is True
              and row.get("status") in FAILED_TEST_STATUSES]
    labels = sorted({row["label"] for row in failed})
    if any(not isinstance(label, str) or not label.startswith(("//", "@")) for label in labels):
        raise ValueError("Failed tests must have absolute Bazel labels.")
    plan = {"schema": "bzlog/rerun-v1", "status": "preview" if labels else "nothing_to_rerun",
            "targets": labels, "failed_tests": [{k: row[k] for k in
                ("label", "configuration", "status")} for row in failed]}
    if not labels:
        return plan
    replay = inv.get("replay", {})
    if replay.get("rerun_error"):
        raise ValueError(replay["rerun_error"])
    layout, argv, cwd = replay.get("layout", {}), replay.get("argv"), replay.get("cwd")
    if (layout.get("version") != 1 or not isinstance(argv, list)
            or not all(isinstance(token, str) for token in argv) or not isinstance(cwd, str) or not cwd):
        raise ValueError("Capture with --record-command; unambiguous original command metadata is required.")
    ci = layout.get("command_index")
    targets, outputs = layout.get("target_indices"), layout.get("output_indices")
    separator = layout.get("separator_index")
    if (type(ci) is not int or not 0 < ci < len(argv) or argv[ci] not in {"test", "coverage"}
            or not isinstance(targets, list) or not targets or not isinstance(outputs, list)):
        raise ValueError("Invalid recorded command layout.")
    removed = targets + outputs + ([] if separator is None else [separator])
    if (any(type(i) is not int or not ci < i < len(argv) for i in removed)
            or len(set(removed)) != len(removed)
            or separator is not None and argv[separator] != "--"):
        raise ValueError("Invalid recorded argument indices.")
    omitted = set(removed)
    narrowed = [token for i, token in enumerate(argv) if i not in omitted] + ["--", *labels]
    import shlex
    plan.update({"argv": narrowed, "cwd": cwd, "command": shlex.join(narrowed),
                 "omitted_capture_options": layout.get("output_flags", []),
                 "scope": "Final failing labels only; their other configurations may also run. "
                          "Original flags, including test filters and cache policy, are preserved. "
                          "Environment, rc files, executable and sources are not pinned. "
                          "Explicit capture destinations are omitted; rc files may still supply them. "
                          "Preview only: no command was executed."})
    return plan


def show_rerun(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="bzlog rerun", description="Preview a rerun of final failing test labels.")
    ap.add_argument("report", help="JSON report captured with --audit-tests --record-command")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    args = ap.parse_args(argv)
    try:
        with open(args.report) as fh:
            plan = rerun_plan(json.load(fh))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"bzlog rerun: cannot create plan: {exc}", file=sys.stderr)
        return 2
    if args.format == "text":
        if plan["status"] == "nothing_to_rerun":
            print("No confirmed final test failures to rerun.")
        else:
            print(f"Rerun preview (cwd {plan['cwd']}):\n{plan['command']}\n{plan['scope']}")
            if plan["omitted_capture_options"]:
                print("Omitted capture flags: " + ", ".join(plan["omitted_capture_options"]))
    else:
        print(json.dumps(plan, separators=(",", ":")))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["exec-log"]:
        from bzlog_execlog import main as exec_log_main
        return exec_log_main(argv[1:])
    if argv[:1] == ["show"]:
        return show_evidence(argv[1:])
    if argv[:1] == ["rerun"]:
        return show_rerun(argv[1:])
    if argv[:1] == ["verify"]:
        return verify_bundle(argv[1:])
    own, rest = split_argv(argv)

    ap = argparse.ArgumentParser(
        prog="bzlog",
        description="Compact a Bazel BEP stream for an LLM agent.",
        epilog="bzlog [flags] <bazel command...> | bzlog [flags] <bep.json> | "
               "bzlog {show,rerun,verify,exec-log} --help. "
               "Put bzlog flags before the Bazel command; use `bzlog -- --help` for Bazel help.",
    )
    ap.add_argument("--version", action="version", version=f"bzlog {__version__}")
    ap.add_argument("--root", help="workspace root for resolving relative log paths")
    ap.add_argument("--progress", action="store_true",
                    help="emit NDJSON diagnostics to stderr as they occur")
    ap.add_argument("--fail-fast", type=int, default=0, metavar="N",
                    help="interrupt the build after N distinct root causes")
    global MAX_DIAGNOSTICS
    ap.add_argument("--max-diagnostics", type=int, default=MAX_DIAGNOSTICS)
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    ap.add_argument("--evidence", choices=["inline", "ref", "none"], default="ref",
                    help="inline: evidence excerpts in the report; ref: keep them in a "
                         "sidecar for on-demand fetch (default); none: drop")
    ap.add_argument("--all-metrics", action="store_true",
                    help="always emit build metrics instead of only anomalies")
    ap.add_argument("--record-command", action="store_true",
                    help="include original Bazel argv and cwd for replay (may contain secrets)")
    ap.add_argument("--audit-tests", action="store_true",
                    help="include every observed configured test with execution/cache evidence")
    ap.add_argument("--bundle", metavar="NEW_DIR",
                    help="save raw captures, full diagnostic files, and a portable report in a new directory")
    args = ap.parse_args(own)
    if args.max_diagnostics < 1:
        ap.error("--max-diagnostics must be at least 1")
    if args.fail_fast < 0:
        ap.error("--fail-fast must be nonnegative")
    MAX_DIAGNOSTICS = args.max_diagnostics

    if not rest:
        ap.error("need a Bazel command (e.g. `bzlog test //...`) or a BEP file")

    posthoc = len(rest) == 1 and os.path.isfile(rest[0]) and rest[0] not in BAZEL_VERBS
    if not posthoc and rest[0] not in BAZEL_VERBS and not rest[0].startswith("-"):
        print(f"bzlog: '{rest[0]}' is not a Bazel command or an existing file", file=sys.stderr)
        return 2
    bundle = None
    if args.bundle:
        try:
            bundle = InvocationBundle(args.bundle, "posthoc" if posthoc else "live")
        except OSError as exc:
            print(f"bzlog: cannot create new bundle: {exc}", file=sys.stderr)
            return 2
    c = Container(root=args.root, audit_tests=args.audit_tests, bundle=bundle)
    exit_code = 0

    # A lone existing file means post-hoc analysis; anything else is a build.
    if posthoc:
        source = rest[0]
        if bundle:
            entry = bundle.capture_file(source, "bep_json", provenance="posthoc_input")
            if entry["status"] == "saved":
                source = str(bundle.root / entry["path"])
        for ev in iter_events(source, c.stream):
            c.consume(ev)
        size = os.path.getsize(source)
    else:
        if args.record_command:
            import shlex
            replay_argv = ["bazel", *rest]
            c.invocation["replay"] = {"argv": replay_argv, "cwd": os.getcwd(),
                                      "command": shlex.join(replay_argv),
                                      "scope": "argv only; environment, rc files and source state are not captured"}
        exit_code, size = run_bazel(rest, c, args.progress, args.fail_fast)
        # subprocess uses -N for signal N; expose the conventional shell status.
        if exit_code < 0:
            exit_code = 128 - exit_code
        status = c.invocation.setdefault("exit", {})
        if status.get("code") != exit_code:
            status.pop("name", None)
        status["code"] = exit_code
        c.invocation["overall_success"] = exit_code == 0

    result = c.finish(size)
    enrich(result, args.root or c.root)
    if not args.all_metrics:
        prune_metrics(result.get("invocation", {}))
    if bundle:
        bundle.finalize(result)

    if args.evidence != "inline":
        blobs = result.pop("evidence", {})
        if args.evidence == "ref" and blobs:
            import tempfile
            side = None
            try:
                if bundle:
                    if not bundle.evidence_saved:
                        raise OSError("bundle evidence file could not be saved")
                    side = str(bundle.root / "evidence.json")
                else:
                    fd, side = tempfile.mkstemp(prefix="bzlog-evidence-", suffix=".json")
                    with os.fdopen(fd, "w") as fh:
                        json.dump(blobs, fh)
                result["evidence_sidecar"] = side
            except OSError as exc:
                # Never leave dangling references when persistence fails.
                result["evidence"] = blobs
                print(f"bzlog: cannot save evidence; keeping it inline: {exc}", file=sys.stderr)
                if side:
                    try:
                        os.unlink(side)
                    except OSError:
                        pass
        for d in result.get("diagnostics", []):
            if args.evidence == "none":
                d.pop("evidence", None)

    if args.format == "text":
        print(render_text(result))
    else:
        json.dump(result, sys.stdout, indent=2 if args.pretty else None,
                  separators=None if args.pretty else (",", ":"))
        print()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
