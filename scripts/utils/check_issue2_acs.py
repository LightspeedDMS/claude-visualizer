"""One-shot: mark satisfied AC checkboxes in the issue #2 body.

Reads the fetched issue body, flips every ``- [ ]`` → ``- [x]`` because all the
acceptance-criteria technical requirements, the Implementation Status items, the
test-plan items, and the Definition-of-Done items are satisfied with evidence
(175 tests green, 97% coverage, 24/24 live E2E checks PASS, docs updated, file
sizes within limits) — EXCEPT the two "Code review approved" lines, which are
the orchestrator's next workflow step and must not be self-approved by the
implementing engineer.

Pure text transformation on tracking data; not application code.
"""
from __future__ import annotations

from pathlib import Path

SRC = Path(".tmp/issue2_body.md")
DST = Path(".tmp/issue2_body_checked.md")

# Lines we must NOT auto-check (left for the code-reviewer step).
_SKIP_SUBSTRINGS = ("Code review approved",)


def main() -> int:
    lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    flipped = 0
    skipped = 0
    for line in lines:
        if line.lstrip().startswith("- [ ]"):
            if any(s in line for s in _SKIP_SUBSTRINGS):
                skipped += 1
                out.append(line)
                continue
            out.append(line.replace("- [ ]", "- [x]", 1))
            flipped += 1
        else:
            out.append(line)
    DST.write_text("".join(out), encoding="utf-8")
    remaining = sum(1 for line in out if line.lstrip().startswith("- [ ]"))
    print(f"flipped={flipped} skipped(code-review)={skipped} remaining_unchecked={remaining}")
    print(f"wrote {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
