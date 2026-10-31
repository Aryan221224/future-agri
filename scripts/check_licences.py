#!/usr/bin/env python3
"""Fail the build if a copyleft package enters the runtime dependency tree.

This is enforcement rather than documentation because the real risk in this
ecosystem is transitive and non-obvious. The concrete cases that motivated it:

* CODS -- the only correct conformal-object-detection library, MIT -- hard-
  depends on `ultralytics` (AGPL-3.0) purely for YOLO wrappers we never call.
* TorchCP, the most-cited conformal library, is LGPL-3.0/GPL-3.0.

Deliberate exceptions are listed with a reason, not silently skipped. An
exception that cannot be justified in one sentence is not an exception.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from importlib import metadata

BLOCKED = re.compile(r"\b(AGPL|GNU Affero|GPL-?[23]|GNU General Public)", re.I)
ALLOWED_COPYLEFT = re.compile(r"\bLGPL\b", re.I)
# Weak copyleft that we accept but want to keep visible in every CI run, so it
# never quietly becomes load-bearing without anyone noticing.
WATCH = re.compile(r"\b(EUPL|MPL-?2|Mozilla Public|EPL-?2|Eclipse Public)", re.I)

# Package -> why it is permitted despite a copyleft licence.
EXCEPTIONS = {
    "pcse": "EUPL-1.2 (weak copyleft, network trigger). No permissively-licensed "
            "crop model of comparable quality exists. Watch-item, see ADR 0005.",
    "pymavlink": "LGPL-3.0, imported as a library only, never statically linked.",
}

# Never installed here; run as isolated services or not at all.
PROCESS_ISOLATED = {
    "odm": "AGPL-3.0, runs as a separate NodeODM container behind an HTTP API; "
           "we talk to it via pyodm (BSD). See ADR 0009.",
}


def runtime_tree(req_path: str) -> set[str]:
    """Package names reachable from a requirements file.

    Scanning every installed distribution is wrong: it flags the developer's
    own toolchain (pglast is GPL-3.0 and is a CI-only SQL parser) and says
    nothing about what ships. Walk the actual declared runtime tree instead,
    including the extras a line asks for (``rfdetr[train,onnx]`` pulls in the
    training stack, and that stack is what trains the shipped model).
    """
    from packaging.requirements import Requirement

    stack: list[tuple[str, frozenset[str]]] = []
    for line in pathlib.Path(req_path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            r = Requirement(line)
            stack.append((r.name.lower(), frozenset(r.extras)))
        except Exception:
            continue

    seen: set[tuple[str, frozenset[str]]] = set()
    names: set[str] = set()
    while stack:
        name, extras = stack.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        names.add(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for raw in dist.metadata.get_all("Requires-Dist") or []:
            try:
                r = Requirement(raw)
            except Exception:
                continue
            if r.marker is not None:
                if "extra ==" in str(r.marker):
                    # Optional extras count only when this package was asked for with them.
                    if not any(r.marker.evaluate({"extra": e}) for e in extras):
                        continue
                elif not r.marker.evaluate():
                    continue
            stack.append((r.name.lower(), frozenset(r.extras)))
    return names


def _licence_blob(dist) -> str:
    """Licence signal only -- never the full licence text.

    Several packages dump their entire licence file into the License field, and
    a BSD text that happens to mention the GPL in a comparison clause is not a
    GPL dependency. Classifiers and License-Expression are the reliable signals.
    """
    expr = dist.metadata.get("License-Expression") or ""
    classifiers = " ".join(c for c in (dist.metadata.get_all("Classifier") or [])
                           if c.startswith("License ::"))
    lic = (dist.metadata.get("License") or "").strip()
    if "\n" in lic or len(lic) > 60:      # a licence *text*, not an identifier
        lic = lic.splitlines()[0][:60] if lic else ""
    return f"{expr} {classifiers} {lic}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true",
                    help="also fail on LGPL")
    ap.add_argument("--requirements", default="requirements.txt")
    a = ap.parse_args()

    tree = runtime_tree(a.requirements)
    problems, noted = [], []
    for name in sorted(tree):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        blob = _licence_blob(dist)
        if WATCH.search(blob) and not BLOCKED.search(blob):
            noted.append((name, blob.strip()[:30],
                          EXCEPTIONS.get(name, "weak copyleft; acceptable as an "
                                               "unmodified library dependency")))
            continue
        if not BLOCKED.search(blob):
            continue
        name = name.lower()
        if ALLOWED_COPYLEFT.search(blob) and not BLOCKED.search(
                BLOCKED.sub("", blob)) and not a.strict:
            noted.append((name, "LGPL", "permitted: library use only"))
            continue
        if name in EXCEPTIONS:
            noted.append((name, blob.strip()[:40], EXCEPTIONS[name]))
            continue
        problems.append((name, dist.version, blob.strip()[:70]))

    for n, why, reason in noted:
        print(f"  note   {n:24s} {why:14s} {reason}")
    for n, v, lic in problems:
        print(f"  BLOCK  {n:24s} {v:12s} {lic}", file=sys.stderr)

    if problems:
        print(f"\n{len(problems)} copyleft package(s) in the runtime tree. "
              "Remove them, isolate them behind a process boundary, or add a "
              "justified exception to scripts/check_licences.py.", file=sys.stderr)
        return 1
    print(f"\nlicence check passed: {len(tree)} packages in the runtime tree, "
          f"{len(noted)} documented exception(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
