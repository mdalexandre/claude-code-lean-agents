#!/usr/bin/env python3
"""Negative control: break one ARI gate at a time in a scratch copy; the tests must fail.

An unmutated baseline runs first; when it does not pass, the harness stops as broken.
Exit 1 when every mutation was caught (the control did its job: the suite can fail).
Exit 0 when any mutation survived, or the baseline failed (the controls prove nothing).
The working tree is never modified.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MUTATIONS = [
    ("dedup disabled", "scripts/ari/evidence.py", "    if prior is not None:\n",
     "    if False:\n", "tests/test_ari_evidence.py"),
    ("symlink escape allowed", "scripts/ari/evidence.py",
     "                if not _within(real, root) or not real.is_file():",
     "                if not real.is_file():", "tests/test_ari_evidence.py"),
    ("symlink escape allowed for unread files", "scripts/ari/evidence.py",
     "    if not _within(real, root) or not real.is_file():\n        cov.files_outside_root",
     "    if not real.is_file():\n        cov.files_outside_root",
     "tests/test_ari2_evidence.py"),
    ("backtest leaks the future", "scripts/ari/forecast.py", "prior = values[:i]  #",
     "prior = values[:i + horizon]  #", "tests/test_ari_forecast.py"),
    ("removal validator disabled", "scripts/ari/audit.py",
     "    if rec.action not in CONSEQUENTIAL:\n        return None\n",
     "    return None\n", "tests/test_ari_audit.py"),
    ("share screen disabled", "scripts/ari/report.py",
     '    problems: list[str] = []\n    if set(obj) != _ALLOWED_TOP:',
     '    return []\n    problems: list[str] = []\n    if set(obj) != _ALLOWED_TOP:',
     "tests/test_ari_report.py"),
    ("MCP display names not joined", "scripts/ari/evidence.py",
     '    return _MCP_NAME_UNSAFE.sub("_", name)', "    return name", "tests/test_ari_audit.py"),
    ("workflow subagents not discovered", "scripts/ari/evidence.py",
     '            patterns.append(("*/*/subagents/workflows/*/agent-*.jsonl", "subagent"))\n',
     "", "tests/test_ari_review_repairs.py"),
    ("unknown names reach skill events", "scripts/ari/evidence.py",
     "            if t.skill and t.skill not in known:\n                t.skill = None",
     "            pass", "tests/test_ari_review_repairs_r5.py"),
    ("validation contract skipped", "scripts/ari/learn.py",
     'if last["outcome"] != "supports":', "if False:", "tests/test_ari_learn.py"),
    # ARI 2.0 gates
    ("finding contract removal gate off", "scripts/ari/interpret.py",
     "    if f.decision in REMOVAL_CLASS:\n", "    if False:\n",
     "tests/test_ari2_interpret.py"),
    ("ambiguous band collapsed to the total", "scripts/ari/evidence.py",
     "lo += min(t, e.copy_max_total)", "lo += t", "tests/test_ari2_evidence.py"),
    ("journal agents not checked", "scripts/ari/evidence.py",
     "cov.journal_agents_missing += sum(", "cov.journal_agents_missing += 0 * sum(",
     "tests/test_ari2_evidence.py"),
    ("marked unread files counted as foreign", "scripts/ari/evidence.py",
     "    if marked:\n        cov.files_unrecognized += 1", "    if False:\n        "
     "cov.files_unrecognized += 1", "tests/test_ari2_evidence.py"),
    ("hostile journal line crashes ingest", "scripts/ari/evidence.py",
     "                    if not isinstance(rec, dict):  # a list or string line is not a "
     "record\n                        continue\n", "", "tests/test_ari2_evidence.py"),
    ("failed replay not quarantined", "scripts/ari/learn.py",
     '    if problems and lesson["state"] not in TERMINAL:', "    if False:",
     "tests/test_ari2_learn.py"),
    ("approval not bound to content", "scripts/ari/learn.py",
     '    if data.get("content_hash") != _content_hash(lesson):', "    if False:",
     "tests/test_ari2_learn.py"),
    ("code change after replay ignored", "scripts/ari/learn.py",
     'rp.get("code_sha256") != code_sha256() or ', "", "tests/test_ari2_learn.py"),
    ("lessons drawn from unstable days", "scripts/ari/learn.py",
     "    for d in sorted(d for d in snaps if d not in unstable):", "    for d in sorted(snaps):",
     "tests/test_ari2_learn.py"),
    ("SUPPORTED while rivals are open", "scripts/ari/discovery.py",
     "            if open_rivals:", "            if False:", "tests/test_ari2_discovery.py"),
    ("escaped text read as a live tag", "scripts/ari_discover.py",
     'for t in ("<script", "<img")', 'for t in ("<script", "<img", "onerror=")',
     "tests/test_ari2_discovery.py"),
    ("routine 2.1 record types reported unknown", "scripts/ari/evidence.py",
     '"file-history-snapshot", "file-history-delta", "queue-operation", "api-request",',
     '"file-history-snapshot", "file-history-delta", "queue-operation",',
     "tests/test_ari2_evidence.py"),
    ("CONSOLIDATE first gate off", "scripts/ari/audit.py",
     '    if rec.action == "CONSOLIDATE" and coverage_gaps:', "    if False:",
     "tests/test_ari_audit.py"),
    ("CONSOLIDATE finding not built", "scripts/ari/audit.py",
     "        if r.reason == DUPLICATE_REASON:", "        if False:",
     "tests/test_ari_audit.py"),
    ("forbidden holon authority unchecked", "scripts/ari/holons.py",
     '                errs.append(f"{hid}: authority includes forbidden action {word}")',
     "                pass", "tests/test_ari2_contracts.py"),
    # D1 hostile records
    ("deep nesting escapes the bounded parser", "scripts/ari/evidence.py",
     "    except (ValueError, UnicodeDecodeError, RecursionError):\n        return _UNPARSED",
     "    except (ValueError, UnicodeDecodeError):\n        return _UNPARSED",
     "tests/test_ari2_hostile.py"),
    ("timestamp overflow not guarded", "scripts/ari/evidence.py",
     "    except OverflowError:  # an offset", "    except ZeroDivisionError:  # an offset",
     "tests/test_ari2_hostile.py"),
    ("journal agent id not validated", "scripts/ari/evidence.py",
     "if isinstance(aid, str) and AGENT_ID.fullmatch(aid):", "if isinstance(aid, str):",
     "tests/test_ari2_hostile.py"),
    # D2 share screen
    ("share note free text allowed", "scripts/ari/report.py",
     '    if obj.get("note") != SHARE_NOTE:', "    if False:", "tests/test_ari2_interpret.py"),
    ("share row type unchecked", "scripts/ari/report.py",
     '        if not isinstance(row, dict):\n            problems.append("agent_types row is '
     'not an object")\n            continue\n', "", "tests/test_ari2_interpret.py"),
    ("share value types unchecked", "scripts/ari/report.py",
     '        if not (_count(row.get("median_start_k")) and _count(row.get("p90_start_k"))):',
     "        if False:", "tests/test_ari2_interpret.py"),
    # D3 hashes recomputed
    ("approval trusts the stored lesson hash", "scripts/ari/learn.py",
     'if data.get("content_hash") != _content_hash(lesson):',
     'if data.get("content_hash") != lesson["content_hash"]:', "tests/test_ari2_learn.py"),
    ("replay trusts stored snapshot hashes", "scripts/ari/learn.py",
     "        elif _snapshot_hash(snaps[day]) != h:",
     '        elif snaps[day].get("content_hash") != h:', "tests/test_ari2_learn.py"),
    # D4 discovery bounds and re-settling
    ("capped rival lost", "scripts/ari/discovery.py",
     "                self.backlog.append(entry)", "                pass",
     "tests/test_ari2_discovery.py"),
    ("new rival does not reopen", "scripts/ari/discovery.py",
     "        self._settle(qid)  # a new open rival", "        pass  # a new open rival",
     "tests/test_ari2_discovery.py"),
    # D5 behaviours no test pinned down
    ("marker scan reads only the first line", "scripts/ari/evidence.py",
     "for m in TRANSCRIPT_MARKERS) for raw in fh)",
     "for m in TRANSCRIPT_MARKERS) for raw in [fh.readline()])", "tests/test_ari2_behaviours.py"),
    ("zeroed first copy read as ambiguous", "scripts/ari/evidence.py",
     "    if not any(a) or not any(b):", "    if not any(b):", "tests/test_ari2_behaviours.py"),
    ("growth checked before a zeroed copy", "scripts/ari/evidence.py",
     '    if not any(a) or not any(b):\n        return "ZEROED_COPY"\n    if a[:4] == b[:4]:\n'
     '        return "STREAM_GROWTH"\n',
     '    if a[:4] == b[:4]:\n        return "STREAM_GROWTH"\n'
     '    if not any(a) or not any(b):\n        return "ZEROED_COPY"\n',
     "tests/test_ari2_behaviours.py"),
    ("growth compares three fields", "scripts/ari/evidence.py",
     "    if a[:4] == b[:4]:", "    if a[:3] == b[:3]:", "tests/test_ari2_behaviours.py"),
    ("checkpoint window ignored", "scripts/ari/evidence.py",
     '\n            or now["window_sha256"] != prev.get("window_sha256")):', "):",
     "tests/test_ari2_behaviours.py"),
    ("unreadable journal not counted", "scripts/ari/evidence.py",
     '        except OSError:\n            cov.files_unreadable += 1\n'
     '            return "unreadable"\n',
     '        except OSError:\n            return "unreadable"\n', "tests/test_ari2_behaviours.py"),
    ("parser change after replay ignored", "scripts/ari/learn.py",
     ' or rp.get("parser") != PARSER_VERSION', "", "tests/test_ari2_behaviours.py"),
    ("run day snapshotted", "scripts/ari/learn.py",
     "for i in range(days, 0, -1)]", "for i in range(days, -1, -1)]",
     "tests/test_ari2_behaviours.py"),
    ("unstable day used as a baseline", "scripts/ari/learn.py",
     'if k < day and not snaps[k].get("unstable")]', "if k < day]",
     "tests/test_ari2_behaviours.py"),
    # D7 low items
    ("bad quiet window accepted", "scripts/ari_nightly.py",
     '        ap.error("--quiet-seconds must be a finite number of at least 0")', "        pass",
     "tests/test_ari2_learn.py"),
    ("combined forbidden word matched whole", "scripts/ari/holons.py",
     'for alt in word.split("_or_")', "for alt in [word]", "tests/test_ari2_contracts.py"),
    ("holon parent cycle unchecked", "scripts/ari/holons.py",
     '            errs.append(f"{start}: parent cycle through {cur}")', "            pass",
     "tests/test_ari2_contracts.py"),
    ("finding decision vocabulary open", "scripts/ari/interpret.py",
     "    elif f.decision not in DECISIONS:", "    elif False:", "tests/test_ari2_interpret.py"),
    ("removal without an exposure reading", "scripts/ari/interpret.py",
     '"RESTRICT_EXPOSURE") and "not_exposed" not in by:',
     '"RESTRICT_EXPOSURE") and False:', "tests/test_ari2_interpret.py"),
    ("coverage reading may deny counted gaps", "scripts/ari/interpret.py",
     '        if unread and gap_r is not None and gap_r.status == "WEAKENED":',
     "        if False:", "tests/test_ari2_interpret.py"),
    ("directory link out of the root silent", "scripts/ari/evidence.py",
     "                        cov.files_outside_root += 1  # a directory link",
     "                        pass  # a directory link", "tests/test_ari2_behaviours.py"),
    ("band line mislabelled under another metric", "scripts/ari/forecast.py",
     '        label = ("Total tokens" if r.get("metric", "total") == "total" else',
     '        label = ("Total tokens" if True else', "tests/test_ari2_evidence.py"),
    # D8 found in the real data check
    ("tool and exposure records ignore the window", "scripts/ari/evidence.py",
     "            in_window = ts is None or not (", "            in_window = True or not (",
     "tests/test_ari2_behaviours.py"),
]


def _copy(dst: Path) -> None:
    for part in ("scripts", "tests"):
        shutil.copytree(REPO / part, dst / part, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "docs", dst / "docs")


def baseline() -> bool:
    """Unmutated run of every test file the controls use. A control only means something
    when the same tests pass on the unbroken code: a file that already fails would read
    every mutation of it as caught."""
    files = sorted({m[4] for m in MUTATIONS})
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "repo"
        _copy(dst)
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               *files], cwd=dst, capture_output=True, text=True)
    ok = proc.returncode == 0 and " passed" in proc.stdout and " failed" not in proc.stdout
    tail = proc.stdout.strip().splitlines()[-1:] or ["no output"]
    print(f"{'baseline' if ok else 'BROKEN BASELINE'} unmutated {len(files)} files: {tail[0]}")
    return ok


def main() -> int:
    if not baseline():
        print("BROKEN CONTROL: the unmutated tests do not pass")
        return 0
    survived = []
    for name, rel, old, new, test in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "repo"
            _copy(dst)
            target = dst / rel
            text = target.read_text()
            if text.count(old) != 1:
                print(f"BROKEN CONTROL {name}: anchor found {text.count(old)} times")
                return 0
            target.write_text(text.replace(old, new))
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x",
                                   "-p", "no:cacheprovider", test], cwd=dst,
                                  capture_output=True, text=True)
            # Caught only when pytest ran and a test FAILED (exit 1 plus a failed
            # summary). Collection or usage errors (exit 2 to 5) prove nothing.
            caught = proc.returncode == 1 and " failed" in proc.stdout
            print(f"{'caught' if caught else 'SURVIVED':8} {name} (pytest exit {proc.returncode})")
            if not caught:
                survived.append(name)
    if survived:
        print("mutations survived: " + ", ".join(survived))
        return 0
    print(f"all {len(MUTATIONS)} mutations caught")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # a crashed control must not read as "all caught"
        print(f"BROKEN CONTROL: {type(exc).__name__}: {exc}")
        sys.exit(0)
