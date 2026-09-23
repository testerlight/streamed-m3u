"""Offline checks for tools/reorder_channels.py. Not shipped.

The planner is pure, so the numbering can be proven without Dispatcharr:

    python3 tools/check_reorder.py

Needs only `requests` importable, because the script under test imports it at
module level. Runs in a second; touches no network.
"""

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "reorder_channels", os.path.join(HERE, "reorder_channels.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  [%s]" % (detail,)) if detail != "" else ""))
    if not cond:
        fails.append(name)


def channel(i, name, tvg, number):
    return {"id": i, "name": name, "tvg_id": tvg, "channel_number": number}


# A full league roster, as it stands on this box: every MLB and NFL team, both
# feeds, and a tail of everything else - numbered with gaps and stragglers at
# the end, the way Dispatcharr leaves things between reorders.
blocks = r.expand_blocks(r.DEFAULT_CONFIG["blocks"])
old_blocks = [b for b in blocks if b[0] != "Multi-view"]
chans, n = [], 0
for label, ids in old_blocks:
    for tvg in ids:
        n += 1
        chans.append(channel(n, tvg.rsplit(".", 1)[-1].replace("-", " ").title(), tvg, n))
LEAGUE = n
for k, num in enumerate([66, 68, 69, 70, 400, 1460, 1461]):
    chans.append(channel(1000 + k, "Other %d" % k, "streamed.team.other-%d" % k, num))
MULTI = [channel(-1, "Multi-Player 1", "streamed.feed.multi-player-1", 1462),
         channel(-2, "Multi-Player 2", "streamed.feed.multi-player-2", 1463)]


def numbers(chanlist, blks):
    ordered, _, _ = r.plan_order(chanlist, blks)
    return {c["id"]: i + 1 for i, c in enumerate(ordered)}


print("=== the block ===")
check("the built-in order has a Multi-view block",
      [b["label"] for b in r.DEFAULT_CONFIG["blocks"]][-1] == "Multi-view")
check("it sits immediately after NFL Network",
      [b["label"] for b in r.DEFAULT_CONFIG["blocks"]][-2] == "NFL Network")
check("the league blocks fill 1-64", LEAGUE == 64, LEAGUE)

print("\n=== inert until the channels exist ===")
check("with no multi channels the block changes nothing",
      numbers(chans, old_blocks) == numbers(chans, blocks))
_, _, found = r.plan_order(chans, blocks)
check("and reports them as not present yet",
      ("Multi-view", 0, 2) in found, found)

print("\n=== once they do ===")
without = numbers(chans + MULTI, old_blocks)
with_ = numbers(chans + MULTI, blocks)
check("Multi-Player 1 is 65", with_[-1] == 65, with_[-1])
check("Multi-Player 2 is 66", with_[-2] == 66, with_[-2])
check("ordered by name, so slot 1 precedes slot 2", with_[-1] < with_[-2])
check("nothing in 1-64 moves",
      all(with_[c["id"]] == without[c["id"]] for c in chans[:LEAGUE]))
rest = chans[LEAGUE:]
check("everything else moves down exactly two",
      all(with_[c["id"]] == without[c["id"]] + 2 for c in rest),
      [(c["name"], without[c["id"]], with_[c["id"]]) for c in rest])
check("and keeps its relative order",
      sorted(rest, key=lambda c: without[c["id"]])
      == sorted(rest, key=lambda c: with_[c["id"]]))
check("gaps are compacted, as every reorder does",
      [with_[c["id"]] for c in rest] == list(range(67, 67 + len(rest))),
      [with_[c["id"]] for c in rest])

only_one = chans + MULTI[:1]
check("with only one slot present it still lands on 65",
      numbers(only_one, blocks)[-1] == 65)

print("\n=== the example file is a dump, not a hand edit ===")
example = json.load(open(os.path.join(HERE, "channel_order.example.json")))
check("channel_order.example.json equals --dump-config", example == r.DEFAULT_CONFIG)

print("\n%s" % ("ALL CHECKS PASSED" if not fails else "FAILURES: " + "; ".join(fails)))
sys.exit(1 if fails else 0)
