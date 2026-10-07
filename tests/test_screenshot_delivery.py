"""Privacy-gated screenshot delivery: temporary state, in-memory ledger, mocked GitHub."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mahler import config, screenshot_delivery as sd, screenshots, scheduler
from mahler.gh import GHError
from mahler.ledger import Ledger

SHA = "a" * 40
NEW = "b" * 40


class FakeGH:
    def __init__(self):
        self.head = SHA
        self.published, self.created, self.edited = [], [], []
        self.fail_publish = self.fail_comment = False
        self.existing = None

    def pr_view(self, pr):
        return {"headRefOid": self.head}

    def publish_artifacts(self, branch, directory, files, message):
        if self.fail_publish:
            raise GHError("denied")
        self.published.append((branch, directory, sorted(files)))
        return "c" * 40

    def artifact_url(self, commit, path):
        return f"https://github.com/x/y/blob/{commit}/{path}"

    def find_comment(self, number, marker):
        return self.existing

    def comment_create(self, number, body):
        if self.fail_comment:
            raise GHError("nope")
        self.created.append(body)
        return 77

    def comment_edit(self, cid, body):
        if self.fail_comment:
            raise GHError("nope")
        self.edited.append((cid, body))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patcher = mock.patch("mahler.config.STATE", str(self.base))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y", "screenshot": "/bin/true",
                                     "screenshot_environment": "Preview",
                                     "screenshot_preview_non_personal": True}
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = FakeGH()
        patch = mock.patch.object(self.ctx, "gh", return_value=self.gh)
        patch.start()
        self.addCleanup(patch.stop)
        self.capture(SHA)

    def capture(self, sha, state="success"):
        if state == "success":
            src = self.base / f"src-{sha[:1]}"
            src.mkdir()
            (src / "home.png").write_bytes(screenshots.PNG_SIGNATURE + b"x")
            (src / "manifest.json").write_text(json.dumps({"version": 1, "sha": sha, "screenshots": [
                {"route": "/home", "file": "home.png"}]}))
            screenshots.store(src, "x", 9, sha)
        self.led.set_kv(f"screenshot:x:9:{sha}", json.dumps(
            {"sha": sha, "pr": 9, "state": state, "reason": "captured"}))

    def private(self, value):
        self.cfg["projects"]["x"]["screenshot_preview_non_personal"] = value

    def deliver(self, sha=SHA):
        sd.deliver(self.ctx, "x", {"number": 5}, 9, {"headRefOid": sha})

    def test_privacy_not_exactly_true_leaks_nothing(self):
        # Visibility is deliberately not consulted: public, private and unknown behave alike.
        for value in (False, "true", 1, None):
            for visibility in ("public", "private", "unknown"):
                with self.subTest(value=value, visibility=visibility):
                    self.private(value)
                    self.led.set_kv(f"screenshot-delivery:x:9:{SHA}", "")
                    self.led.set_kv("screenshot-comment:x:9", "")
                    self.gh.created.clear()
                    self.deliver()
                    self.assertEqual(self.gh.published, [])
                    body = self.gh.created[0]
                    self.assertTrue(body.startswith("<!-- mahler:agent -->"))
                    for leak in ("/home", ".png", "github.com/x/y", str(self.base)):
                        self.assertNotIn(leak, body)
                    context = sd.review_context(self.ctx, "x", 9, SHA)
                    self.assertIn("local-only", context)
                    for leak in ("/home", ".png", str(self.base), "blob"):
                        self.assertNotIn(leak, context)

    def test_opted_in_publishes_immutable_links_and_reviewer_context(self):
        self.deliver()
        self.assertEqual(self.gh.published, [("mahler-screenshots", f"pr-9/{SHA}",
                                              ["home.png", "manifest.json"])])
        body = self.gh.created[0]
        self.assertIn("<!-- mahler:screenshots -->", body)
        self.assertIn(f"blob/{'c' * 40}/pr-9/{SHA}/home.png", body)
        self.assertIn(SHA[:12], body)
        context = sd.review_context(self.ctx, "x", 9, SHA)
        self.assertIn("not proof of functional correctness", context)
        self.assertIn(str(self.base / "screenshots" / "x" / "9" / SHA / "home.png"), context)
        self.assertIn(f"blob/{'c' * 40}", context)
        self.assertEqual(sd.review_context(self.ctx, "x", 9, NEW), "")

    def test_repeat_ticks_and_restart_reuse_records(self):
        self.deliver()
        self.deliver()
        self.assertEqual((len(self.gh.published), len(self.gh.created), self.gh.edited), (1, 1, []))

    def test_lost_comment_record_recovers_by_marker(self):
        self.gh.existing = 55
        self.deliver()
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.edited[0][0], 55)

    def test_new_head_edits_same_comment_and_old_evidence_not_current(self):
        self.deliver()
        self.capture(NEW)
        self.gh.head = NEW
        self.deliver(NEW)
        self.assertEqual(len(self.gh.created), 1)
        cid, body = self.gh.edited[0]
        self.assertEqual(cid, 77)
        self.assertIn(NEW[:12], body)
        self.assertNotIn(SHA[:12], body)

    def test_wrong_head_never_publishes_or_comments(self):
        self.capture(NEW)
        self.gh.head = SHA          # PR moved on after the view was read
        self.deliver(NEW)
        self.assertEqual((self.gh.published, self.gh.created, self.gh.edited), ([], [], []))

    def test_head_drift_after_publication_skips_comment(self):
        original = self.gh.publish_artifacts

        def drift(*a):
            result = original(*a)
            self.gh.head = NEW
            return result
        self.gh.publish_artifacts = drift
        self.deliver()
        self.assertEqual(self.gh.created, [])

    def test_upload_failure_is_advisory_generic_and_keeps_local_images(self):
        self.gh.fail_publish = True
        self.deliver()
        body = self.gh.created[0]
        self.assertIn("publication unavailable", body)
        self.assertNotIn("denied", body)
        self.assertNotIn("github.com/x/y", body)
        self.assertIn("local image", sd.review_context(self.ctx, "x", 9, SHA))
        self.assertTrue(screenshots.lookup("x", 9, SHA))

    def test_comment_failure_is_advisory_and_retries_without_reupload(self):
        self.gh.fail_comment = True
        self.deliver()
        self.assertEqual(len(self.gh.published), 1)
        self.gh.fail_comment = False
        self.deliver()
        self.assertEqual((len(self.gh.published), len(self.gh.created)), (1, 1))

    def test_attempts_are_bounded(self):
        self.gh.fail_comment = True
        for _ in range(6):
            self.deliver()
        record = json.loads(self.led.get_kv(f"screenshot-delivery:x:9:{SHA}"))
        self.assertEqual(record["attempts"], sd.MAX_ATTEMPTS)

    def test_failed_capture_gets_generic_note_and_no_context(self):
        self.led.set_kv(f"screenshot:x:9:{SHA}", json.dumps(
            {"sha": SHA, "pr": 9, "state": "failed", "reason": "command_failed"}))
        self.deliver()
        self.assertEqual(self.gh.published, [])
        self.assertIn("capture failed", self.gh.created[0])
        self.assertNotIn("command_failed", self.gh.created[0])
        self.assertEqual(sd.review_context(self.ctx, "x", 9, SHA), "")

    def test_disabled_or_dry_run_does_nothing(self):
        self.cfg["projects"]["x"]["screenshot"] = ""
        self.deliver()
        self.ctx.dry_run = True
        self.cfg["projects"]["x"]["screenshot"] = "/bin/true"
        self.deliver()
        self.assertEqual((self.gh.published, self.gh.created), ([], []))

    def test_record_merge_keeps_head_and_merge_sha_separate(self):
        self.deliver()
        sd.record_merge(self.ctx, "x", 5, 9, {"headRefOid": SHA, "mergeCommit": {"oid": "d" * 40}})
        final = json.loads(self.led.get_kv("screenshot-final:x#5"))
        self.assertEqual((final["head"], final["merge_sha"]), (SHA, "d" * 40))
        sd.record_merge(self.ctx, "x", 6, 9, {"headRefOid": NEW})
        self.assertFalse(self.led.get_kv("screenshot-final:x#6"))


if __name__ == "__main__":
    unittest.main()
