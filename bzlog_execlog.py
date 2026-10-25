"""Compact execution-log inspection using Bazel's authoritative decoder.

The converter reconstructs input sets, directories, symlinks and runfiles.
This module consumes SpawnExec JSON and reports observations, not inferred
remote-cache lookup outcomes or hermeticity claims.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

SCHEMA = "bzlog/exec-log-v1"
MAX_RECORD_BYTES = 16 * 1024 * 1024
DETAIL_LIMIT = 10


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def json_records(stream, max_record_bytes=MAX_RECORD_BYTES, byte_limit=None):
    """Frame NDJSON or concatenated pretty objects in one pass over the bytes."""
    record = bytearray()
    depth = 0
    quoted = escaped = False
    remaining = byte_limit
    while remaining is None or remaining > 0:
        chunk = stream.read(65536 if remaining is None else min(65536, remaining))
        if not chunk:
            break
        if remaining is not None:
            remaining -= len(chunk)
        for char in chunk:
            if not record:
                if char in b" \t\r\n":
                    continue
                if char != ord("{"):
                    raise ValueError("Expected a SpawnExec JSON object.")
            record.append(char)
            if len(record) > max_record_bytes:
                raise ValueError(f"Execution-log record exceeds {max_record_bytes} bytes.")
            if quoted:
                if escaped:
                    escaped = False
                elif char == ord("\\"):
                    escaped = True
                elif char == ord('"'):
                    quoted = False
            elif char == ord('"'):
                quoted = True
            elif char in b"{[":
                depth += 1
            elif char in b"}]":
                depth -= 1
            if depth == 0:
                # Decode explicitly: the interchange format is UTF-8.
                yield json.loads(record.decode("utf-8"), object_pairs_hook=_object)
                record.clear()
    if record:
        raise ValueError("Truncated execution-log JSON record.")


def _field(obj, name, default=None):
    if not isinstance(obj, dict):
        raise ValueError("Expected a protobuf JSON object.")
    snake = re.sub(r"([A-Z])", r"_\1", name).lower()
    if snake != name and name in obj and snake in obj:
        raise ValueError(f"Duplicate protobuf field: {name}/{snake}")
    return obj.get(name, obj.get(snake, default))


def _text(value):
    if not isinstance(value, str):
        raise ValueError("Expected a string in SpawnExec.")
    return value


def _array(value):
    if not isinstance(value, list):
        raise ValueError("Expected an array in SpawnExec.")
    return value


def _bool(value):
    if type(value) is not bool:
        raise ValueError("Expected a boolean in SpawnExec.")
    return value


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    raise ValueError("Expected an integer in SpawnExec.")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _digest(obj):
    if obj is None:
        return None
    value = _text(_field(obj, "hash", ""))
    size = _integer(_field(obj, "sizeBytes", 0))
    if size < 0:
        raise ValueError("Digest size must be nonnegative.")
    return (_text(_field(obj, "hashFunctionName", "")).upper(), value, size) if value else None


def _named_values(values, repeatable=False):
    result = defaultdict(list)
    for item in _array(values):
        name = _text(_field(item, "name", ""))
        if name in result and not repeatable:
            raise ValueError(f"Duplicate named value in SpawnExec: {name}")
        # Values can contain credentials; retain only fingerprints for comparison.
        result[name].append(_hash(_text(_field(item, "value", ""))))
    # REAPI permits repeated platform property names; preserve multiplicity
    # while ignoring serialization order.
    return {name: _hash(sorted(fingerprints)) for name, fingerprints in result.items()}


@dataclass
class Action:
    label: str
    mnemonic: str
    outputs: tuple[str, ...]
    runner: str
    cache_hit: bool
    cacheable: bool
    remote_cacheable: bool
    remotable: bool
    exit_code: int
    status: str
    digest: tuple | None
    args: str
    environment: dict
    platform: dict
    inputs: dict
    inputs_without_digest: int
    timeout: int

    @property
    def key(self):
        # Output paths retain Bazel configuration directories. Never match by
        # label alone or pick arbitrarily between retries with the same output.
        return (self.label, self.mnemonic, self.outputs[0]) if self.outputs else None

    def identity(self):
        return {"target": self.label, "mnemonic": self.mnemonic,
                "primary_output": self.outputs[0] if self.outputs else None}

    def compact(self):
        return {**self.identity(), "runner": self.runner, "cache_hit": self.cache_hit,
                "cacheable": self.cacheable, "remote_cacheable": self.remote_cacheable,
                "exit_code": self.exit_code, "status": self.status}


def normalize(record):
    mnemonic = _text(_field(record, "mnemonic", ""))
    if not mnemonic:
        raise ValueError("Not a SpawnExec record: mnemonic is missing.")
    inputs, missing = {}, 0
    for item in _array(_field(record, "inputs", [])):
        path = _text(_field(item, "path", ""))
        if path in inputs:
            raise ValueError(f"Duplicate input path in SpawnExec: {path}")
        digest = _digest(_field(item, "digest"))
        symlink = _text(_field(item, "symlinkTargetPath", ""))
        missing += digest is None and not symlink
        inputs[path] = _hash((digest, symlink, _bool(_field(item, "isTool", False))))
    outputs = tuple(sorted(_text(p) for p in _array(_field(record, "listedOutputs", []))))
    args = [_text(a) for a in _array(_field(record, "commandArgs", []))]
    return Action(
        label=_text(_field(record, "targetLabel", "")), mnemonic=mnemonic, outputs=outputs,
        runner=_text(_field(record, "runner", "")),
        cache_hit=_bool(_field(record, "cacheHit", False)),
        cacheable=_bool(_field(record, "cacheable", False)),
        remote_cacheable=_bool(_field(record, "remoteCacheable", False)),
        remotable=_bool(_field(record, "remotable", False)),
        exit_code=_integer(_field(record, "exitCode", 0)),
        status=_text(_field(record, "status", "")), digest=_digest(_field(record, "digest")),
        args=_hash(args), environment=_named_values(_field(record, "environmentVariables", [])),
        platform=_named_values(_field(_field(record, "platform", {}), "properties", []), repeatable=True),
        inputs=inputs, inputs_without_digest=missing,
        timeout=_integer(_field(record, "timeoutMillis", 0)))


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_json(path, max_record_bytes):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Execution log must be a regular file.")
        actions = [normalize(record) for record in json_records(stream, max_record_bytes, before.st_size)]
        if (_signature(before) != _signature(os.fstat(stream.fileno()))
                or _signature(before) != _signature(os.stat(path))):
            raise ValueError("Execution log changed while it was read.")
    return actions


def load_log(path, input_format, converter, timeout, max_record_bytes):
    path = Path(path).absolute()
    if input_format == "json":
        return read_json(path, max_record_bytes)
    if not converter:
        raise ValueError("Compact/binary input requires --converter PATH to Bazel's "
                         "//src/tools/execlog:converter; use --input-format json for SpawnExec JSON.")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Execution log must be a regular file.")
    with tempfile.TemporaryDirectory(prefix="bzlog-execlog-") as temp:
        converted = Path(temp) / "spawn.json"
        with tempfile.TemporaryFile() as console:
            try:
                proc = subprocess.run([converter, "--input", f"{input_format}:{path}",
                                       "--output", f"json:{converted}"],
                                      stdout=console, stderr=console, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise ValueError(f"Execution-log conversion exceeded {timeout:g} seconds.") from exc
            if proc.returncode:
                console.seek(0, os.SEEK_END)
                console.seek(max(0, console.tell() - 2048))
                message = console.read().decode("utf-8", "replace").strip()
                raise ValueError(f"Execution-log converter exited {proc.returncode}: {message}")
        if _signature(before) != _signature(path.stat()):
            raise ValueError("Execution log changed during conversion.")
        return read_json(converted, max_record_bytes)


def _bounded(rows, limit):
    return {"total": len(rows), "items": rows[:limit], "omitted": max(0, len(rows) - limit)}


def summarize(actions, limit):
    hits = Counter(a.runner or "unknown" for a in actions if a.cache_hit)
    mnemonics = defaultdict(Counter)
    for action in actions:
        row = mnemonics[action.mnemonic]
        row["spawns"] += 1
        row["cache_hits"] += action.cache_hit
        row["without_cache_hit"] += not action.cache_hit
        row["not_cacheable"] += not action.cacheable
    candidates = [a.compact() for a in sorted(actions, key=lambda a: (
        a.cacheable, a.label, a.mnemonic, a.outputs, a.runner)) if not a.cache_hit]
    return {"spawns": len(actions), "cache_hits": sum(hits.values()),
            "without_cache_hit": len(actions) - sum(hits.values()),
            "not_cacheable": sum(not a.cacheable for a in actions),
            "not_remote_cacheable": sum(not a.remote_cacheable for a in actions),
            "cache_hit_runners": dict(sorted(hits.items())),
            "runners": dict(sorted(Counter(a.runner or "unknown" for a in actions).items())),
            "by_mnemonic": _bounded([{"mnemonic": name, **counts} for name, counts in
                sorted(mnemonics.items(), key=lambda item: (-item[1]["without_cache_hit"], item[0]))], limit),
            "actions_without_cache_hit": _bounded(candidates, limit)}


def _map_changes(before, after):
    return _bounded(sorted(key for key in before.keys() | after.keys()
                           if before.get(key) != after.get(key)), DETAIL_LIMIT)


def compare(before, after, limit):
    def index(actions):
        entries = defaultdict(list)
        for action in actions:
            if action.key is not None:
                entries[action.key].append(action)
        return entries

    old, new = index(before), index(after)
    changes, ambiguous, only_before, only_after = [], [], [], []
    matched = no_changes = 0
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key, []), new.get(key, [])
        identity = (left or right)[0].identity()
        if len(left) > 1 or len(right) > 1:
            ambiguous.append({**identity, "baseline_records": len(left), "current_records": len(right)})
            continue
        if not left:
            only_after.append(identity)
            continue
        if not right:
            only_before.append(identity)
            continue
        a, b = left[0], right[0]
        matched += 1
        fields = []
        for name, previous, current in (
                ("arguments", a.args, b.args), ("environment", a.environment, b.environment),
                ("platform", a.platform, b.platform), ("inputs", a.inputs, b.inputs),
                ("declared_outputs", a.outputs, b.outputs), ("timeout", a.timeout, b.timeout),
                ("cache_policy", (a.cacheable, a.remote_cacheable, a.remotable),
                 (b.cacheable, b.remote_cacheable, b.remotable))):
            if previous != current:
                fields.append(name)
        digest_changed = a.digest != b.digest if a.digest is not None and b.digest is not None else None
        if fields or digest_changed:
            changes.append({**identity, "fields": fields, "action_digest_changed": digest_changed,
                            "cache_hit_before": a.cache_hit, "cache_hit_after": b.cache_hit,
                            "input_paths": _map_changes(a.inputs, b.inputs),
                            "environment_names": _map_changes(a.environment, b.environment),
                            "platform_names": _map_changes(a.platform, b.platform),
                            "inputs_without_digest": {"baseline": a.inputs_without_digest,
                                                      "current": b.inputs_without_digest}})
        else:
            no_changes += 1
    return {"match_key": ["target_label", "mnemonic", "first_sorted_output"],
            "matched": matched, "no_recorded_changes": no_changes,
            "changed": _bounded(changes, limit), "ambiguous": _bounded(ambiguous, limit),
            "only_in_baseline": _bounded(only_before, limit),
            "only_in_current": _bounded(only_after, limit),
            "without_outputs": {"baseline": sum(a.key is None for a in before),
                                "current": sum(a.key is None for a in after)}}


def render_text(report):
    summary = report["summary"]
    out = [f"execution log: {summary['spawns']} spawns; {summary['cache_hits']} cache hits; "
           f"{summary['without_cache_hit']} without a recorded cache hit",
           f"  {summary['not_cacheable']} not cacheable; "
           f"{summary['not_remote_cacheable']} not remote-cacheable"]
    for row in summary["actions_without_cache_hit"]["items"]:
        out.append(f"  {row['target']} {row['mnemonic']} ({row['runner'] or 'unknown runner'}): "
                   f"{row['primary_output'] or 'no declared output'}")
    if summary["actions_without_cache_hit"]["omitted"]:
        out.append(f"  {summary['actions_without_cache_hit']['omitted']} additional spawns omitted")
    if "comparison" in report:
        comparison = report["comparison"]
        out.append(f"comparison: {comparison['matched']} matched; {comparison['changed']['total']} changed; "
                   f"{comparison['no_recorded_changes']} without recorded changes")
        for row in comparison["changed"]["items"]:
            out.append(f"  {row['target']} {row['mnemonic']}: " +
                       (", ".join(row["fields"]) or "action digest only"))
            for field in ("input_paths", "environment_names", "platform_names"):
                if row[field]["total"]:
                    out.append(f"    {field}: " + ", ".join(row[field]["items"]) +
                               (f" (+{row[field]['omitted']} omitted)" if row[field]["omitted"] else ""))
        if comparison["changed"]["omitted"]:
            out.append(f"  {comparison['changed']['omitted']} additional changes omitted")
        out.append(f"  unmatched: {comparison['only_in_baseline']['total']} baseline-only, "
                   f"{comparison['only_in_current']['total']} current-only; "
                   f"{comparison['ambiguous']['total']} ambiguous keys; "
                   f"{sum(comparison['without_outputs'].values())} records without outputs")
    out.extend(report["notices"])
    return "\n".join(out)


def main(argv):
    ap = argparse.ArgumentParser(prog="bzlog exec-log", description="Inspect spawn cache evidence and compare execution logs.")
    ap.add_argument("log", help="current execution log")
    ap.add_argument("--baseline", help="earlier execution log to compare")
    ap.add_argument("--input-format", choices=["compact", "binary", "json"], default="compact")
    ap.add_argument("--baseline-format", choices=["compact", "binary", "json"])
    ap.add_argument("--converter", help="executable path to Bazel's //src/tools/execlog:converter")
    ap.add_argument("--timeout", type=float, default=120, help="conversion timeout per file, in seconds")
    ap.add_argument("--max-record-bytes", type=int, default=MAX_RECORD_BYTES)
    ap.add_argument("--max-actions", type=int, default=25, help="maximum rows per output section")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    args = ap.parse_args(argv)
    import math
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.max_record_bytes < 1 or args.max_actions < 1:
        ap.error("timeout and output/record limits must be positive and finite")
    if args.baseline_format and not args.baseline:
        ap.error("--baseline-format requires --baseline")
    try:
        current = load_log(args.log, args.input_format, args.converter, args.timeout, args.max_record_bytes)
        report = {"schema": SCHEMA, "input": {"path": str(Path(args.log).absolute()), "format": args.input_format},
                  "summary": summarize(current, args.max_actions), "notices": [
                      "Scope: recorded spawns only; actions reused by Bazel's local action cache are absent.",
                      "No cache hit recorded does not prove a cache lookup or miss. Check cache policy and invocation flags.",
                      "A parsed log has no terminal completeness marker; missing records are not proof an action disappeared."]}
        if args.baseline:
            baseline_format = args.baseline_format or args.input_format
            previous = load_log(args.baseline, baseline_format, args.converter, args.timeout, args.max_record_bytes)
            report["baseline"] = {"path": str(Path(args.baseline).absolute()), "format": baseline_format,
                                  "spawns": len(previous)}
            report["comparison"] = compare(previous, current, args.max_actions)
            report["notices"].append("Changes are recorded differences, not proven cache-miss causes. "
                                     "No recorded change does not establish identical actions or hermeticity; "
                                     "digests may be unavailable, including for empty inputs.")
        print(render_text(report) if args.format == "text" else json.dumps(report, separators=(",", ":")))
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f"bzlog exec-log: {exc}", file=sys.stderr)
        return 2
    return 0
