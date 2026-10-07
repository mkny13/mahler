"""Launch availability must not consume an item's implementation budget."""

import unittest
from datetime import timedelta
from unittest import mock

from mahler import scheduler, tick
from mahler.ledger import Ledger
from tests.test_schedule import NOW, item, mk_cfg, proj


class LaunchCapacityTests(unittest.TestCase):
    def test_resume_fix_gets_new_run_lease_and_current_rendered_prompt(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        ctx = scheduler.Ctx(mk_cfg({"a": proj(max_parallel=1)}), led)
        ctx.ping = mock.Mock()
        item(led, "a", 1)
        led.upsert_item("a", 1, branch="mahler/existing-pr", pr=88)
        source = led.create_run(project="a", number=1, role="build", platform="claude",
                                epoch=1, status="ended", session_id="saved-id")
        old, _ = led.claim("a", 1, "conductor", "auto", 30, capacity=False)
        prep = dict(worktree="/new/worktree", run_dir="/new/run",
                    branch="mahler/existing-pr", base_ref="origin/mahler/existing-pr")
        current_prompt = "Full current fix recipe, latest CI log and review findings"
        with mock.patch("mahler.tick.runner.prepare", return_value=prep) as prepare, \
                mock.patch("mahler.tick.prompt.build", return_value=current_prompt) as render, \
                mock.patch("mahler.tick.runner.launch", return_value={
                    "pid": 123, "worktree": prep["worktree"], "branch": prep["branch"],
                    "base_ref": prep["base_ref"], "log_path": "/new/run/agent.log",
                    "status_path": "/new/run/exit"}) as launch, \
                mock.patch("mahler.launch_health.succeeded"), \
                mock.patch.object(ctx, "gh", return_value=mock.Mock()):
            self.assertTrue(tick.start(ctx, "a", led.item("a", 1), "fix", "claude",
                handoff_from=("conductor", old["epoch"]), context="new CI log",
                resume_from=source))
        lease = led.lease("a", 1)
        self.assertNotEqual(lease["run_id"], source)
        self.assertGreater(lease["epoch"], old["epoch"])
        new = led.run(lease["run_id"])
        self.assertEqual(new["role"], "fix")
        self.assertEqual(new["branch"], "mahler/existing-pr")
        self.assertEqual(new["log_path"], "/new/run/agent.log")
        self.assertEqual(prepare.call_args.args[-1], new["id"])
        self.assertEqual(render.call_args.kwargs["context"], "new CI log")
        self.assertEqual(launch.call_args.args[5:],
                         (new["id"], lease["epoch"], current_prompt, prep))
        self.assertEqual(launch.call_args.kwargs, {"resume_from": source})

    def test_three_launch_failures_preserve_budget_and_retry_after_backoff(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        cfg = mk_cfg({"a": proj(max_parallel=1)}, total=1)
        ctx = scheduler.Ctx(cfg, led)
        ctx.ping = mock.Mock()
        item(led, "a", 1)
        led.upsert_item("a", 1, attempts=2, esc_tier=1, esc_fails=1)
        with mock.patch("mahler.tick.runner.prepare",
                        side_effect=OSError("temporarily unavailable")) as prepare, \
             mock.patch("mahler.launch_health.version._short_head", return_value="abc1234"):
            for _ in range(3):
                self.assertFalse(tick.start(ctx, "a", led.item("a", 1), "build", "claude"))
            current = led.item("a", 1)
            self.assertEqual((current["state"], current["attempts"], current["esc_tier"],
                              current["esc_fails"]), ("ready", 2, 1, 1))
            self.assertIsNone(led.lease("a", 1))
            self.assertFalse(tick.start(ctx, "a", current, "build", "claude"))
            self.assertEqual(prepare.call_count, 3)
            self.assertIsNotNone(led.get_kv("launch_broken:a"))
            led.now = lambda: NOW + timedelta(minutes=30)
            self.assertFalse(tick.start(ctx, "a", current, "build", "claude"))
            self.assertEqual(prepare.call_count, 4)
            self.assertEqual(led.item("a", 1)["attempts"], 2)
