from contextlib import closing
"""Project CI holds use only isolated ledger state."""
import copy
import json
import unittest
from mahler import config, scheduler, tick
from mahler.ledger import Ledger


class CIHoldTests(unittest.TestCase):
    def test_only_blocker_builds_until_shipped(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["x"] = {"repo": "x/y", "path": "/unused"}
        with closing(Ledger(":memory:")) as led:
            ctx = scheduler.Ctx(cfg, led, dry_run=False)
            for n in (1, 289):
                led.upsert_item("x", n, state="ready")
            led.set_kv("ci_blocker:x", json.dumps({"number": 289}))
            work = tick._candidates(ctx, [ctx.policy("x")])
            self.assertEqual([it["number"] for _, _, it in work], [289])
            self.assertIn({"kind": "ci_blocker", "project": "x", "number": 1, "on": [289]}, ctx.holds)
            led.set_state("x", 289, "shipped")
            self.assertEqual([it["number"] for _, _, it in tick._candidates(ctx, [ctx.policy("x")])], [1])
            self.assertIsNone(led.get_kv("ci_blocker:x"))
