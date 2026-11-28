"""Contract tests for the daemon's launchd plist template."""

import plistlib
import tempfile
import unittest
from xml.parsers.expat import ExpatError
from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "launcher" / "local.mahler.plist"
CONSOLE_TEMPLATE = Path(__file__).parents[1] / "launcher" / "local.mahler.serve.plist"


def _render_template(home: Path) -> bytes:
    return TEMPLATE.read_bytes().replace(b"__HOME__", str(home).encode())


def _render_console_template(home: Path) -> bytes:
    return CONSOLE_TEMPLATE.read_bytes().replace(b"__HOME__", str(home).encode())


def _assert_daemon_contract(test_case: unittest.TestCase, plist: dict, home: Path) -> None:
    test_case.assertEqual(plist["Label"], "local.mahler")
    test_case.assertEqual(
        plist["ProgramArguments"],
        ["/bin/sh", f"{home}/.mahler/bin/mahler-launcher"],
    )
    test_case.assertEqual(plist["StartInterval"], 60)
    test_case.assertTrue(plist["RunAtLoad"])
    test_case.assertEqual(
        plist["EnvironmentVariables"],
        {
            "PATH": (
                f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
                "/usr/sbin:/sbin"
            )
        },
    )
    test_case.assertEqual(
        plist["StandardOutPath"], f"{home}/.mahler/logs/launchd.out.log"
    )
    test_case.assertEqual(
        plist["StandardErrorPath"], f"{home}/.mahler/logs/launchd.err.log"
    )


def _assert_console_contract(test_case: unittest.TestCase, plist: dict, home: Path) -> None:
    test_case.assertEqual(plist["Label"], "local.mahler.serve")
    test_case.assertEqual(
        plist["ProgramArguments"],
        [f"{home}/.mahler/app/bin/mahler", "serve", "--port", "8787"],
    )
    test_case.assertTrue(plist["RunAtLoad"])
    test_case.assertTrue(plist["KeepAlive"])
    test_case.assertEqual(
        plist["EnvironmentVariables"],
        {
            "PATH": (
                f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
                "/usr/sbin:/sbin"
            )
        },
    )
    test_case.assertEqual(
        plist["StandardOutPath"], f"{home}/.mahler/logs/serve.out.log"
    )
    test_case.assertEqual(
        plist["StandardErrorPath"], f"{home}/.mahler/logs/serve.err.log"
    )


class LaunchdTemplateTests(unittest.TestCase):
    def test_console_template_renders_expected_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="test path with spaces ") as home_text:
            home = Path(home_text)
            rendered = _render_console_template(home)

            self.assertNotIn(b"__HOME__", rendered)
            plist = plistlib.loads(rendered)
            _assert_console_contract(self, plist, home)

    def test_daemon_template_renders_expected_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="test path with spaces ") as home_text:
            home = Path(home_text)
            rendered = _render_template(home)

            self.assertNotIn(b"__HOME__", rendered)
            plist = plistlib.loads(rendered)
            _assert_daemon_contract(self, plist, home)

    def test_malformed_console_template_is_rejected(self) -> None:
        with self.assertRaises(ExpatError):
            plistlib.loads(b"<plist><dict></plist>")

    def test_incorrect_console_command_fails_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="test path with spaces ") as home_text:
            home = Path(home_text)
            plist = plistlib.loads(_render_console_template(home))
            plist["ProgramArguments"][2] = "--wrong-port"

            with self.assertRaises(AssertionError):
                _assert_console_contract(self, plist, home)

    def test_malformed_rendered_template_is_rejected(self) -> None:
        with self.assertRaises(ExpatError):
            plistlib.loads(b"<plist><dict></plist>")

    def test_incorrect_executable_argument_fails_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="test path with spaces ") as home_text:
            home = Path(home_text)
            plist = plistlib.loads(_render_template(home))
            plist["ProgramArguments"][1] = f"{home}/wrong-launcher"

            with self.assertRaises(AssertionError):
                _assert_daemon_contract(self, plist, home)

    def test_missing_contract_key_fails_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="test path with spaces ") as home_text:
            home = Path(home_text)
            plist = plistlib.loads(_render_template(home))
            del plist["StandardErrorPath"]

            with self.assertRaises(KeyError):
                _assert_daemon_contract(self, plist, home)


if __name__ == "__main__":
    unittest.main()
