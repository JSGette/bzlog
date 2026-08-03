#!/usr/bin/env python3
"""Golden tests for framework-aware test log truncation.

    python3 test_truncate.py            # compare against testdata/golden/
    python3 test_truncate.py --update   # regenerate the golden files

Also collectable by pytest.
"""

import os
import sys

import bzlog

HERE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(HERE, "testdata")
GOLDEN = os.path.join(TESTDATA, "golden")
# Fixture logs use POSIX paths; the workspace root must match them.
WORKSPACE = "/home/user/ws"

# fixture -> framework the registry must detect
CASES = {
    "gtest.log": "gtest",
    "go.log": "go",
    "go_panic.log": "go",
    "pytest.log": "pytest",
    "python_traceback.log": "python",
    "junit.log": "junit",
}


def _raw(name: str) -> str:
    with open(os.path.join(TESTDATA, name), encoding="utf-8") as fh:
        return fh.read()


def _run(name: str):
    return bzlog.truncate_test_log(_raw(name), WORKSPACE)


def test_detection_and_golden():
    for name, want_fw in CASES.items():
        got = _run(name)
        assert got is not None, f"{name}: no framework detected"
        text, kept, fw = got
        assert fw == want_fw, f"{name}: detected {fw!r}, want {want_fw!r}"
        assert kept == len(text.splitlines()), f"{name}: kept_lines mismatch"
        golden_path = os.path.join(GOLDEN, name + ".golden")
        with open(golden_path, encoding="utf-8") as fh:
            want = fh.read().rstrip("\n")
        assert text == want, (
            f"{name}: output differs from golden "
            f"(python3 test_truncate.py --update to regenerate)"
        )


def test_extraction_shrinks():
    for name in CASES:
        text, _, _ = _run(name)
        assert len(text) < len(_raw(name)), f"{name}: extraction did not shrink"


def test_unknown_falls_back():
    assert _run("unknown.log") is None
    assert bzlog.truncate_test_log("", WORKSPACE) is None


def test_workspace_frames_filtered():
    text, _, _ = _run("python_traceback.log")
    assert "/home/user/ws/pkg/conn_test.py" in text
    assert "site-packages" not in text and "/usr/lib/python" not in text
    assert "frames outside workspace" in text


def _update() -> None:
    os.makedirs(GOLDEN, exist_ok=True)
    for name in CASES:
        got = _run(name)
        assert got is not None, f"{name}: no framework detected"
        text, _, fw = got
        path = os.path.join(GOLDEN, name + ".golden")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        print(f"wrote {path} ({fw}, {len(text.splitlines())} lines)")


def main() -> int:
    if "--update" in sys.argv:
        _update()
        return 0
    failed = 0
    for fn in (test_detection_and_golden, test_extraction_shrinks,
               test_unknown_falls_back, test_workspace_frames_filtered):
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
