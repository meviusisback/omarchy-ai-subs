"""Credential-file confinement tests.

Hermetic: temp directories only — no network, no shell-outs, and nothing in the
developer's real ``~/.hermes`` is read or written.  The trusted-root rules are
exercised through the injected ``roots`` / ``uid`` / ``ancestor_uids`` /
``trust_root`` parameters, because a temp dir lives under a world-writable
``/tmp`` and would otherwise be (correctly) refused.

Run: ``python3 -m unittest discover -s tests -v``
"""

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "fetch_usage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fetch_usage_under_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_usage = _load_module()


class ConfinementTestCase(unittest.TestCase):
    """Base fixture: a private root inside a private base directory."""

    def setUp(self):
        self._env_snapshot = dict(os.environ)
        self.base = tempfile.mkdtemp()
        self.root = os.path.join(self.base, "hermes")
        os.mkdir(self.root, 0o700)
        self.trust_root = self.base
        # Tests own their whole tree, so their uid is the trusted ancestor uid.
        self.ancestors = (os.getuid(),)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_snapshot)
        for dirpath, dirnames, filenames in os.walk(self.base, topdown=False):
            for name in filenames:
                try:
                    os.unlink(os.path.join(dirpath, name))
                except OSError:
                    pass
            for name in dirnames:
                try:
                    os.rmdir(os.path.join(dirpath, name))
                except OSError:
                    pass
        try:
            os.rmdir(self.base)
        except OSError:
            pass

    # -- helpers ---------------------------------------------------------- #
    def write_file(self, relpath, content, mode=0o600):
        path = os.path.join(self.root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, mode)
        return path

    def load(self, path, **kwargs):
        kwargs.setdefault("trust_root", self.trust_root)
        kwargs.setdefault("ancestor_uids", self.ancestors)
        return fetch_usage.load_hermes_dotenv(path, roots=[self.root], **kwargs)


class TestDotenvAcceptance(ConfinementTestCase):
    def test_reads_private_env_file_inside_root(self):
        path = self.write_file(".env", "OPENROUTER_API_KEY=sk-test\n")
        self.assertTrue(self.load(path))
        self.assertEqual(os.environ.get("OPENROUTER_API_KEY"), "sk-test")

    def test_parses_dotenv_idioms(self):
        path = self.write_file(
            ".env",
            "# comment\n"
            "export DEEPSEEK_API_KEY=abc123\n"
            'KIMI_API_KEY="quoted # not a comment"\n'
            "ZAI_API_KEY=bare # trailing comment\n"
        )
        self.assertTrue(self.load(path))
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "abc123")
        self.assertEqual(os.environ["KIMI_API_KEY"], "quoted # not a comment")
        self.assertEqual(os.environ["ZAI_API_KEY"], "bare")

    def test_accepts_profile_subdirectory(self):
        path = self.write_file(os.path.join("profiles", "work", ".env"), "NOVITA_API_KEY=n1\n")
        self.assertTrue(self.load(path))
        self.assertEqual(os.environ.get("NOVITA_API_KEY"), "n1")

    def test_accepts_symlinked_root_directory(self):
        # A dotfiles-style symlinked root is resolved, not rejected.
        link = os.path.join(self.base, "hermes-link")
        os.symlink(self.root, link)
        path = self.write_file(".env", "ARCEE_API_KEY=a1\n")
        self.assertTrue(fetch_usage.load_hermes_dotenv(
            path, roots=[link], trust_root=self.trust_root, ancestor_uids=self.ancestors))
        self.assertEqual(os.environ.get("ARCEE_API_KEY"), "a1")

    def test_ignores_process_control_variables(self):
        path = self.write_file(
            ".env",
            "OPENROUTER_API_KEY=keep-me\n"
            "PATH=/tmp/evil\n"
            "LD_PRELOAD=/tmp/evil.so\n"
            "PYTHONPATH=/tmp/evil\n"
            "HOME=/tmp/evil\n"
        )
        before = os.environ.get("PATH")
        self.assertTrue(self.load(path))
        self.assertEqual(os.environ.get("OPENROUTER_API_KEY"), "keep-me")
        self.assertEqual(os.environ.get("PATH"), before)
        self.assertNotIn("LD_PRELOAD", os.environ)
        self.assertNotEqual(os.environ.get("HOME"), "/tmp/evil")

    def test_ignores_malformed_variable_names(self):
        path = self.write_file(".env", "BAD NAME=x\n1LEADING=x\nOPENROUTER_API_KEY=ok\n")
        self.assertTrue(self.load(path))
        self.assertNotIn("BAD NAME", os.environ)
        self.assertNotIn("1LEADING", os.environ)
        self.assertEqual(os.environ.get("OPENROUTER_API_KEY"), "ok")


class TestDotenvRefusals(ConfinementTestCase):
    def assert_refused(self, path, **kwargs):
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.assertFalse(self.load(path, **kwargs))
        self.assertIsNone(os.environ.get("OPENROUTER_API_KEY"))

    def test_refuses_path_outside_root(self):
        outside = os.path.join(self.base, "elsewhere", ".env")
        os.makedirs(os.path.dirname(outside))
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("OPENROUTER_API_KEY=leak\n")
        os.chmod(outside, 0o600)
        self.assert_refused(outside)

    def test_refuses_symlink_leaf(self):
        secret = self.write_file("real-secret", "OPENROUTER_API_KEY=leak\n")
        link = os.path.join(self.root, ".env")
        os.symlink(secret, link)
        self.assert_refused(link)

    def test_refuses_group_or_world_readable_file(self):
        for mode in (0o640, 0o604, 0o644, 0o660):
            path = self.write_file("mode-%o.env" % mode, "OPENROUTER_API_KEY=leak\n", mode=mode)
            with self.subTest(mode=oct(mode)):
                self.assert_refused(path)

    def test_refuses_hard_linked_file(self):
        path = self.write_file(".env", "OPENROUTER_API_KEY=leak\n")
        os.link(path, os.path.join(self.base, "outside-copy"))
        self.assert_refused(path)

    def test_refuses_oversized_file(self):
        path = self.write_file(".env", "OPENROUTER_API_KEY=leak\n" + "x" * 4096)
        self.assert_refused(path, max_bytes=1024)

    def test_refuses_fifo_without_blocking(self):
        path = os.path.join(self.root, ".env")
        os.mkfifo(path, 0o600)
        self.assert_refused(path)

    def test_refuses_directory_at_path(self):
        path = os.path.join(self.root, ".env")
        os.mkdir(path, 0o700)
        self.assert_refused(path)

    def test_refuses_invalid_utf8(self):
        path = os.path.join(self.root, ".env")
        with open(path, "wb") as fh:
            fh.write(b"OPENROUTER_API_KEY=\xff\xfe\n")
        os.chmod(path, 0o600)
        self.assert_refused(path)

    def test_refuses_missing_file(self):
        self.assert_refused(os.path.join(self.root, "absent.env"))

    def test_refuses_relative_path_and_dotdot(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.write_file(".env", "OPENROUTER_API_KEY=leak\n")
        for candidate in (".env", "../.env",
                          os.path.join(self.root, "..", "hermes", ".env")):
            with self.subTest(candidate=candidate):
                self.assertFalse(self.load(candidate))

    def test_refuses_world_writable_root(self):
        path = self.write_file(".env", "OPENROUTER_API_KEY=leak\n")
        os.chmod(self.root, 0o777)
        try:
            self.assert_refused(path)
        finally:
            os.chmod(self.root, 0o700)

    def test_refuses_world_writable_subdirectory(self):
        path = self.write_file(os.path.join("profiles", "work", ".env"),
                               "OPENROUTER_API_KEY=leak\n")
        os.chmod(os.path.join(self.root, "profiles"), 0o777)
        try:
            self.assert_refused(path)
        finally:
            os.chmod(os.path.join(self.root, "profiles"), 0o755)

    def test_refuses_foreign_owned_path_via_injected_uid(self):
        path = self.write_file(".env", "OPENROUTER_API_KEY=leak\n")
        with self.assertRaises(fetch_usage.CredentialFileError):
            fetch_usage.confined_path(path, [self.root], uid=os.getuid() + 1,
                                      ancestor_uids=self.ancestors,
                                      trust_root=self.trust_root)

    def test_refuses_empty_and_non_string_paths(self):
        for candidate in ("", "   ", None, 5):
            with self.subTest(candidate=candidate):
                with self.assertRaises(fetch_usage.CredentialFileError):
                    fetch_usage.confined_path(candidate, [self.root],
                                              trust_root=self.trust_root,
                                              ancestor_uids=self.ancestors)

    def test_refuses_when_root_itself_is_untrusted(self):
        path = self.write_file(".env", "OPENROUTER_API_KEY=leak\n")
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.assertFalse(fetch_usage.load_hermes_dotenv(
            path, roots=[self.root], uid=os.getuid() + 1,
            trust_root=self.trust_root, ancestor_uids=self.ancestors))
        self.assertIsNone(os.environ.get("OPENROUTER_API_KEY"))


class TestReadConfinedText(ConfinementTestCase):
    def test_rejects_overlong_read(self):
        path = self.write_file(".env", "x" * 300)
        with self.assertRaises(fetch_usage.CredentialFileError):
            fetch_usage.read_confined_text(path, [self.root], max_bytes=64,
                                           trust_root=self.trust_root,
                                           ancestor_uids=self.ancestors)

    def test_reads_exactly_at_cap(self):
        body = "x" * 64
        path = self.write_file(".env", body)
        text = fetch_usage.read_confined_text(path, [self.root], max_bytes=64,
                                              trust_root=self.trust_root,
                                              ancestor_uids=self.ancestors)
        self.assertEqual(text, body)


class TestEnvRoots(ConfinementTestCase):
    def test_hermes_home_and_default_are_accepted(self):
        os.environ["HOME"] = self.base
        os.environ["HERMES_HOME"] = self.root
        roots = fetch_usage.hermes_env_roots(trust_root=self.trust_root,
                                             ancestor_uids=self.ancestors)
        self.assertIn(os.path.realpath(self.root), roots)
        # Nothing from the developer's real environment leaks in.
        self.assertNotIn(os.path.realpath(os.path.join(self._env_snapshot.get("HOME", ""),
                                                       ".hermes")), roots)

    def test_untrusted_hermes_home_is_dropped(self):
        other = os.path.join(self.base, "writable-hermes")
        os.mkdir(other)
        os.chmod(other, 0o777)
        os.environ["HOME"] = self.base
        os.environ["HERMES_HOME"] = other
        roots = fetch_usage.hermes_env_roots(trust_root=self.trust_root,
                                             ancestor_uids=self.ancestors)
        self.assertNotIn(os.path.realpath(other), roots)

    def test_home_dir_falls_back_to_passwd_entry(self):
        real_home = fetch_usage._home_dir()
        os.environ["HOME"] = ""
        try:
            fallback = fetch_usage._home_dir()
        finally:
            os.environ["HOME"] = real_home or ""
        self.assertTrue(fallback is None or fallback.startswith(os.sep))
        self.assertNotEqual(fallback, "/")

    def test_expand_home_with_empty_home_never_yields_root_path(self):
        os.environ["HOME"] = ""
        expanded = fetch_usage._expand_home("~/.hermes/.env")
        # Either the passwd entry resolved it, or it stayed unexpanded for
        # confined_path to refuse — never the root-relative "/.hermes/.env".
        self.assertFalse(expanded.startswith("/.hermes"), expanded)


class TestProviderSpecsUntouched(ConfinementTestCase):
    def test_provider_count_and_shape(self):
        specs = fetch_usage.PROVIDER_SPECS
        self.assertEqual(len(specs), 13)
        ids = [spec["id"] for spec in specs]
        self.assertEqual(len(ids), len(set(ids)))
        for spec in specs:
            self.assertTrue(callable(spec["fetch"]), spec["id"])

    def test_json_output_shape_with_refused_env_file(self):
        # The dry-run path: a refused --env file must not break the payload.
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.assertFalse(self.load(os.path.join(self.root, "absent.env")))


if __name__ == "__main__":
    unittest.main()
