#!/usr/bin/env python3
"""Compare a results directory against the earlier full run.

Usage: python3 compare.py results-<timestamp> <baseline-dir>

Only files present in the new results directory are compared.
"""
import collections
import glob
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
STATES = ["passed", "failure", "error", "skipped"]
LABELS = {"passed": "pass", "failure": "fail", "error": "error", "skipped": "skip"}


def load_file(path):
    """Return {test: state} for one JUnit file, one state per test.

    A test that fails in the call and errors in teardown appears twice in JUnit; the call result wins.
    """
    tests = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        name = f"{case.get('classname')}::{case.get('name')}"
        state = next((child.tag for child in case if child.tag in STATES), "passed")
        old = tests.get(name)
        if old is None or old == "passed" or (old == "error" and state == "failure"):
            tests[name] = state
    return tests


def load_dir(directory):
    return {os.path.basename(p)[:-len(".xml")]: load_file(p) for p in sorted(glob.glob(os.path.join(directory, "*.xml")))}


def counts(tests):
    counter = collections.Counter(tests.values())
    return [counter[s] for s in STATES]


def format_counts(values):
    return " ".join(f"{LABELS[s]}={v}" for s, v in zip(STATES, values))


def display_name(key):
    return key.replace("__", "/")


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    new_dir, base_dir = sys.argv[1], sys.argv[2]
    new, base = load_dir(new_dir), load_dir(base_dir)
    if not new:
        sys.exit(f"no JUnit files in {new_dir}")
    print(f"new:      {new_dir}\nbaseline: {base_dir}\n")

    by_dir = collections.defaultdict(lambda: [[0] * 4, [0] * 4])
    changed = []
    print("Per file (baseline -> new, only files whose counts differ):")
    for key, tests in new.items():
        old = base.get(key)
        if old is None:
            print(f"  {display_name(key)}: not in baseline, new {format_counts(counts(tests))}")
            continue
        old_counts, new_counts = counts(old), counts(tests)
        directory = display_name(key).split("/")[2]
        for i in range(4):
            by_dir[directory][0][i] += old_counts[i]
            by_dir[directory][1][i] += new_counts[i]
        if old_counts != new_counts:
            delta = " ".join(f"{LABELS[s]}{n - o:+d}" for s, o, n in zip(STATES, old_counts, new_counts) if n != o)
            print(f"  {display_name(key)}: {format_counts(old_counts)} -> {format_counts(new_counts)} ({delta})")
        for test in sorted(set(old) | set(tests)):
            if old.get(test) != tests.get(test):
                changed.append((test, old.get(test, "not run"), tests.get(test, "not run")))

    print("\nPer directory (baseline -> new):")
    total = [[0] * 4, [0] * 4]
    for directory, (old_counts, new_counts) in sorted(by_dir.items()):
        delta = " ".join(f"{LABELS[s]}{n - o:+d}" for s, o, n in zip(STATES, old_counts, new_counts))
        print(f"  {directory}: {format_counts(old_counts)} -> {format_counts(new_counts)} ({delta})")
        for i in range(4):
            total[0][i] += old_counts[i]
            total[1][i] += new_counts[i]
    delta = " ".join(f"{LABELS[s]}{n - o:+d}" for s, o, n in zip(STATES, *total))
    print(f"  total: {format_counts(total[0])} -> {format_counts(total[1])} ({delta})")

    print(f"\nTests whose result changed: {len(changed)}")
    for test, old_state, new_state in changed:
        print(f"  {LABELS.get(old_state, old_state)} -> {LABELS.get(new_state, new_state)}  {test.replace('webdriver.tests.bidi.', '')}")


if __name__ == "__main__":
    main()
