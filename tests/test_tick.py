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

    def test_fact_based_hold_without_item_holds_every_build(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["x"] = {"repo": "x/y", "path": "/unused"}
        with closing(Ledger(":memory:")) as led:
            ctx = scheduler.Ctx(cfg, led, dry_run=False)
            led.upsert_item("x", 1, state="ready")
            led.set_kv("ci_blocker:x", json.dumps({"number": None, "bot_pr": 292, "check": "checks"}))
            self.assertEqual(tick._candidates(ctx, [ctx.policy("x")]), [])
            self.assertIn({"kind": "ci_blocker", "project": "x", "number": 1, "on": [292]}, ctx.holds)


class PassBudgetTests(unittest.TestCase):
    def test_stalled_pass_yields_and_reaps_exited_run(self):
        import time
        from unittest.mock import patch
        with closing(Ledger(':memory:')) as led:
            ctx = scheduler.Ctx(copy.deepcopy(config.DEFAULTS), led)
            run = led.create_run(project='x', number=1, role='build',
                                 platform='test', epoch=0, pid=123,
                                 log_path='/unused/log', status_path='/unused/status')
            def finalize(ctx, row):
                ctx.led.update_run(row['id'], status='done')
            with patch('mahler.runner.alive', return_value=False), \
                 patch('mahler.runner.kill'), \
                 patch('mahler.watchdog.finalize', side_effect=finalize), \
                 patch('builtins.print') as output:
                scheduler.run_pass(ctx, 'sync:x', lambda: time.sleep(1), budget=.02)
            self.assertEqual(led.run(run)['status'], 'done')
            warning = next(c for c in output.call_args_list if 'stalled' in str(c))
            self.assertIn('sync:x', str(warning))
            self.assertIn('operation=', str(warning))
            self.assertTrue(warning.kwargs['flush'])

    def test_timeout_error_reaps_and_allows_next_pass(self):
        from unittest.mock import patch
        import subprocess
        with closing(Ledger(':memory:')) as led:
            ctx = scheduler.Ctx(copy.deepcopy(config.DEFAULTS), led)
            def slow():
                raise subprocess.TimeoutExpired('gh', 90)
            with patch('mahler.scheduler.watchdog') as reap:
                scheduler.run_pass(ctx, 'sync', slow)
                reap.assert_called_once_with(ctx)
            self.assertEqual(scheduler.run_pass(ctx, 'next', lambda: 42), 42)
