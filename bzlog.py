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
from typing import Any, Iterator, Optional
from urllib.parse import urlparse, unquote

SCHEMA = "bzlog/v1"
MAX_EVIDENCE_LINES = 40
MAX_EVIDENCE_BYTES = 4096
# Framework-extracted logs are already distilled, so they get a higher cap
# than the blind tail: cutting a distilled log loses signal, cutting a raw
# one loses noise.
MAX_EXTRACTED_LINES = 160
MAX_EXTRACTED_BYTES = 16384
MAX_DIAGNOSTICS = 25


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

_PARSERS = [
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
                d["file"] = g["file"].lstrip("./")
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
    if not uri:
        return None
    if uri.startswith("file://"):
        return unquote(urlparse(uri).path)
    if uri.startswith("bytestream://"):
        return None  # remote-only blob; would need a CAS fetch
    return uri if os.path.exists(uri) else None


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
        if framework:
            # already distilled by a framework extractor: generous cap only
            body, truncated = tail(text, MAX_EXTRACTED_LINES, MAX_EXTRACTED_BYTES)
        else:
            body, truncated = tail(text)
        h = hashlib.sha256(body.encode()).hexdigest()[:12]
        if h in self._by_hash:
            return self._by_hash[h]
        ref = f"e{len(self.store) + 1}"
        self._by_hash[h] = ref
        entry: dict[str, Any] = {"kind": kind, "truncated": truncated, "text": body}
        if framework:
            entry["framework"] = framework
            entry["kept_lines"] = len(body.splitlines())
            if total_lines is not None:
                entry["total_lines"] = total_lines
        self.store[ref] = entry
        return ref


class Container:
    def __init__(self, root: Optional[str] = None):
        self.root = root
        self.ev = Evidence()
        self.invocation: dict[str, Any] = {}
        self.counts = {
            "actions_failed": 0,
            "targets_failed": 0,
            "tests_total": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "tests_flaky": 0,
        }
        self._diags: dict[str, dict[str, Any]] = {}  # fingerprint -> diagnostic
        self._order: list[str] = []

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

    def add_diag(self, diag: dict[str, Any], target: Optional[str]) -> None:
        fp = self.fingerprint(
            diag["kind"], diag.get("action_type", ""), diag.get("errors", []), diag.get("evidence")
        )
        existing = self._diags.get(fp)
        if existing:
            if target and target not in existing["_targets"]:
                existing["_targets"].append(target)
            return
        diag["_targets"] = [target] if target else []
        self._diags[fp] = diag
        self._order.append(fp)

    # -- event handlers ---------------------------------------------------
    def handle(self, ev: dict[str, Any]) -> None:
        eid = ev.get("id", {})

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

        elif "pattern" in eid:
            self.invocation["patterns"] = eid["pattern"].get("pattern", [])

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
            }
            fd = a.get("failureDetail")
            if fd:
                cat = next((k for k in fd if k != "message"), None)
                diag["failure_category"] = cat
                if fd.get("message"):
                    diag["failure_message"] = fd["message"][:300]
            if a.get("primaryOutput", {}).get("name"):
                diag["primary_output"] = a["primaryOutput"]["name"]
            if ref:
                diag["evidence"] = ref
            self.add_diag(diag, label)

        elif "testResult" in ev:
            r = ev["testResult"]
            status = r.get("status", "NO_STATUS")
            tid = eid.get("testResult", {})
            label = tid.get("label")
            if status in ("PASSED",):
                return
            if status == "FLAKY":
                self.counts["tests_flaky"] += 1
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
            diag = {
                "kind": "test_failure",
                "status": status,
                "attempt": int(tid.get("attempt", 1)),
                "shard": int(tid.get("shard", 0)),
                "cases": cases,
                "errors": [],
            }
            ei = r.get("executionInfo", {})
            if ei.get("exitCode") is not None:
                diag["exit_code"] = ei["exitCode"]
            if ei.get("strategy"):
                diag["strategy"] = ei["strategy"]
            if ref:
                diag["evidence"] = ref
            self.add_diag(diag, label)

        elif "testSummary" in ev:
            s = ev["testSummary"]
            self.counts["tests_total"] += 1
            if s.get("overallStatus") == "PASSED":
                self.counts["tests_passed"] += 1
            else:
                self.counts["tests_failed"] += 1

        elif "aborted" in ev:
            a = ev["aborted"]
            label = eid.get("targetCompleted", {}).get("label") or eid.get("pattern", {})
            diag = {
                "kind": "analysis_error" if a.get("reason") in
                        ("ANALYSIS_FAILURE", "LOADING_FAILURE") else "aborted",
                "reason": a.get("reason"),
                "errors": [],
                "description": (a.get("description") or "")[:500],
            }
            self.add_diag(diag, label if isinstance(label, str) else None)

        elif "completed" in ev:
            if not ev["completed"].get("success", True):
                self.counts["targets_failed"] += 1

        elif "finished" in ev:
            f = ev["finished"]
            self.invocation["exit"] = {
                "code": f.get("exitCode", {}).get("code"),
                "name": f.get("exitCode", {}).get("name"),
            }
            self.invocation["overall_success"] = f.get("overallSuccess", False)

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

        # execution strategies; `remote cache hit` is where the RBE hit rate lives.
        # NB: ActionSummary.remote_cache_hits is deprecated -- do not use it.
        runners = {r.get("name", "?"): int(r.get("count", 0))
                   for r in asm.get("runnerCount", []) if int(r.get("count", 0))}
        if runners:
            out["runners"] = dict(sorted(runners.items(), key=lambda kv: -kv[1]))
            hits = sum(v for k, v in runners.items() if "cache hit" in k.lower())
            total = sum(runners.values())
            if total:
                out["remote_cache_hit_rate"] = round(hits / total, 3)

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
        diags = []
        for fp in self._order[:MAX_DIAGNOSTICS]:
            d = self._diags[fp]
            targets = d.pop("_targets", [])
            if targets:
                d["target"] = targets[0]
                if len(targets) > 1:
                    d["also_affects"] = targets[1:11]
                    d["affected_count"] = len(targets)
            d = {k: v for k, v in d.items() if v not in ([], "", None, {})}
            d["id"] = f"d{len(diags) + 1}"
            diags.append(d)

        omitted = max(0, len(self._order) - MAX_DIAGNOSTICS)
        # evidence for dropped diagnostics is dead weight
        live = {d["evidence"] for d in diags if "evidence" in d}
        evidence = {k: v for k, v in self.ev.store.items() if k in live}

        out = {
            "schema": SCHEMA,
            "invocation": self.invocation,
            "summary": {k: v for k, v in self.counts.items() if v},
            "diagnostics": diags,
            "evidence": evidence,
        }
        if omitted:
            out["summary"]["diagnostics_omitted"] = omitted
        blob = json.dumps(out, separators=(",", ":"))
        out["stats"] = {
            "input_bytes": input_bytes,
            "output_bytes": len(blob),
            "ratio": round(len(blob) / input_bytes, 4) if input_bytes else None,
        }
        return out


def iter_events(path: str) -> Iterator[dict[str, Any]]:
    """Bazel writes one JSON object per line, but older versions pretty-print."""
    with open(path, "r", errors="replace") as fh:
        buf = ""
        depth = 0
        for line in fh:
            s = line.strip()
            if not s:
                continue
            if depth == 0 and s.startswith("{") and s.endswith("}"):
                try:
                    yield json.loads(s)
                    continue
                except json.JSONDecodeError:
                    pass
            buf += line
            depth += line.count("{") - line.count("}")
            if depth <= 0 and buf.strip():
                try:
                    yield json.loads(buf)
                except json.JSONDecodeError:
                    pass
                buf, depth = "", 0


def tail_events(path: str, is_alive) -> Iterator[dict[str, Any]]:
    """Follow a BEP file while Bazel writes it.

    FileTransport flushes a BufferedOutputStream every 250ms
    (EXPERIMENTAL_BEP_FILE_FLUSH_MILLIS), so a flush can land mid-line: the
    buffer is byte-oriented, not line-oriented. We therefore only parse on a
    trailing newline and carry the remainder forward.
    """
    import time
    pending = ""
    with open(path, "r", errors="replace") as fh:
        while True:
            chunk = fh.read()
            if chunk:
                pending += chunk
                *complete, pending = pending.split("\n")
                for line in complete:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass  # pretty-printed BEP: not supported in follow mode
                continue
            if not is_alive():
                # drain whatever landed between the last read and process exit
                rest = fh.read()
                for line in (pending + rest).split("\n"):
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            pass
                return
            time.sleep(0.05)


def run_bazel(argv: list[str], c: "Container", emit_progress: bool,
              fail_fast: int) -> tuple[int, int]:
    """Spawn Bazel and consume its event stream live. Returns (exit_code, bep_bytes)."""
    import subprocess
    import tempfile

    # Respect a BEP path the user already asked for; otherwise use our own.
    user_bep = next((a.split("=", 1)[1] for a in argv
                     if a.startswith("--build_event_json_file=")), None)
    own_flags: list[str] = []
    if user_bep:
        bep, ephemeral = user_bep, False
    else:
        fd, bep = tempfile.mkstemp(prefix="bzlog-", suffix=".json")
        os.close(fd)
        ephemeral = True
        own_flags.append(f"--build_event_json_file={bep}")
    if not any(a.startswith("--build_event_json_file_path_conversion") for a in argv):
        # keep file:// URIs so stderr and test.log stay readable from disk
        own_flags.append("--build_event_json_file_path_conversion=false")

    # `bazel run //x -- args` : our flags must land before the separator,
    # otherwise they are handed to the target binary.
    if "--" in argv:
        cut = argv.index("--")
        cmd = ["bazel", *argv[:cut], *own_flags, *argv[cut:]]
    else:
        cmd = ["bazel", *argv, *own_flags]

    # Bazel's own console output is the thing we are replacing; keep it out of
    # the agent's stdout but preserve it for humans.
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # wait for the file to appear before tailing
    import time
    for _ in range(200):
        if os.path.exists(bep) and os.path.getsize(bep) > 0:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    seen = 0
    killed = False
    try:
        for ev in tail_events(bep, lambda: proc.poll() is None):
            try:
                c.handle(ev)
            except Exception as exc:
                print(f"bzlog: skipped event: {exc}", file=sys.stderr)
            if emit_progress and len(c._order) > seen:
                for fp in c._order[seen:]:
                    d = c._diags[fp]
                    print(json.dumps({
                        "event": "diagnostic",
                        "kind": d.get("kind"),
                        "target": (d.get("_targets") or [None])[0],
                        "first_error": (d.get("errors") or [{}])[0].get("message"),
                    }, separators=(",", ":")), file=sys.stderr, flush=True)
                seen = len(c._order)
            if fail_fast and len(c._order) >= fail_fast and not killed:
                # enough distinct root causes; stop burning CI time.
                # SIGINT lets Bazel finish writing BuildMetrics and Aborted events.
                proc.send_signal(2)
                killed = True
                c.invocation["stopped_early"] = {"reason": "fail_fast",
                                                 "distinct_causes": len(c._order)}
    finally:
        code = proc.wait()
        size = os.path.getsize(bep) if os.path.exists(bep) else 0
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

_OWN_BOOL = {"--pretty", "--progress", "--all-metrics"}
_OWN_VALUE = {"--root", "--fail-fast", "--max-diagnostics", "--format", "--evidence"}


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

        # 2. the exact command to reproduce one failure in isolation
        tgt = d.get("target")
        if not tgt:
            continue
        if d.get("kind") == "test_failure":
            cases = d.get("cases") or []
            cmd = f"bazel test {tgt}"
            if cases:
                cmd += f" --test_filter={cases[0].get('class','')}.{cases[0]['case']}".rstrip(".")
            d["repro"] = cmd + " --test_output=streamed"
        elif d.get("kind") in ("compile_error", "action_failure"):
            d["repro"] = f"bazel build {tgt}"


def prune_metrics(inv: dict[str, Any]) -> None:
    """Report metrics by exception. A healthy 94% cache hit rate is not news;
    a 40% one is. Emitting the full block every run is pure token tax."""
    m = inv.get("metrics")
    if not m:
        return
    keep: dict[str, Any] = {}
    hit = m.get("remote_cache_hit_rate")
    if hit is not None and hit < 0.7:
        keep["remote_cache_hit_rate"] = hit
        keep["runners"] = m.get("runners")
    ac = m.get("action_cache", {})
    reasons = ac.get("miss_reasons", {})
    # DIGEST_MISMATCH at scale means non-hermeticity, which is actionable
    if reasons.get("DIGEST_MISMATCH", 0) > max(50, 0.1 * (ac.get("misses") or 0)):
        keep["action_cache"] = {"misses": ac.get("misses"),
                                "miss_reasons": {"DIGEST_MISMATCH": reasons["DIGEST_MISMATCH"]}}
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

    for d in result.get("diagnostics", []):
        n = d.get("affected_count")
        tag = f"[{d['id']}] {d.get('kind')}"
        if n:
            tag += f" x{n} targets"
        out.append(tag + f"  {d.get('target','')}")
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
            out.append(f"  category: {d['failure_category']}")
        if d.get("repro"):
            out.append(f"  repro: {d['repro']}")
        if d.get("evidence"):
            out.append(f"  log: bzlog-show {d['evidence']}")

    m = inv.get("metrics")
    if m:
        out.append("metrics: " + json.dumps(m, separators=(",", ":")))
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    own, rest = split_argv(argv)

    ap = argparse.ArgumentParser(
        prog="bzlog",
        description="Compact a Bazel BEP stream for an LLM agent.",
        epilog="bzlog [flags] <bazel command...>   |   bzlog [flags] <bep.json>",
    )
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
                    help="inline: full logs in the container; ref: keep them in a "
                         "sidecar for on-demand fetch (default); none: drop")
    ap.add_argument("--all-metrics", action="store_true",
                    help="always emit build metrics instead of only anomalies")
    args = ap.parse_args(own)
    MAX_DIAGNOSTICS = args.max_diagnostics

    if not rest:
        ap.error("need a Bazel command (e.g. `bzlog test //...`) or a BEP file")

    c = Container(root=args.root)
    exit_code = 0

    # A lone existing file means post-hoc analysis; anything else is a build.
    if len(rest) == 1 and os.path.isfile(rest[0]) and rest[0] not in BAZEL_VERBS:
        for ev in iter_events(rest[0]):
            try:
                c.handle(ev)
            except Exception as exc:  # never let one bad event kill the run
                print(f"bzlog: skipped event: {exc}", file=sys.stderr)
        size = os.path.getsize(rest[0])
    else:
        if rest[0] not in BAZEL_VERBS and not rest[0].startswith("-"):
            print(f"bzlog: '{rest[0]}' is not a Bazel command or an existing file",
                  file=sys.stderr)
            return 2
        exit_code, size = run_bazel(rest, c, args.progress, args.fail_fast)
        # subprocess uses -N for signal N; expose the conventional shell status.
        if exit_code < 0:
            exit_code = 128 - exit_code
        c.invocation.setdefault("exit", {})["code"] = exit_code

    result = c.finish(size)
    enrich(result, args.root or c.root)
    if not args.all_metrics:
        prune_metrics(result.get("invocation", {}))

    if args.evidence != "inline":
        blobs = result.pop("evidence", {})
        if args.evidence == "ref" and blobs:
            side = os.path.join(os.environ.get("TMPDIR", "/tmp"), "bzlog-evidence.json")
            try:
                with open(side, "w") as fh:
                    json.dump(blobs, fh)
                result["evidence_sidecar"] = side
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
