import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deploy  # noqa: E402

CHANGELOG = """# Changelog

## 1.3.0

### Fixed

- A fix.

## 1.2.0

### Added

- A feature.
"""


class PureHelperTests(unittest.TestCase):
    def test_bump_zeroes_the_lower_parts(self) -> None:
        self.assertEqual(deploy.bump("1.2.3", "major"), "2.0.0")
        self.assertEqual(deploy.bump("1.2.3", "minor"), "1.3.0")
        self.assertEqual(deploy.bump("1.2.3", "patch"), "1.2.4")

    def test_read_and_replace_version(self) -> None:
        text = '[project]\nname = "x"\nversion = "1.2.3"\n'
        self.assertEqual(deploy.read_version(text), "1.2.3")
        self.assertIn('version = "1.3.0"', deploy.replace_version(text, "1.3.0"))
        with self.assertRaisesRegex(deploy.ReleaseError, "could not find"):
            deploy.read_version("[project]\n")

    def test_changelog_section_stops_at_the_next_version(self) -> None:
        self.assertEqual(
            deploy.changelog_section(CHANGELOG, "1.3.0"), "### Fixed\n\n- A fix."
        )
        self.assertEqual(
            deploy.changelog_section(CHANGELOG, "1.2.0"), "### Added\n\n- A feature."
        )

    def test_changelog_section_refuses_a_missing_or_empty_section(self) -> None:
        with self.assertRaisesRegex(deploy.ReleaseError, "no '## 9.9.9'"):
            deploy.changelog_section(CHANGELOG, "9.9.9")
        with self.assertRaisesRegex(deploy.ReleaseError, "is empty"):
            deploy.changelog_section("## 2.0.0\n\n## 1.0.0\n- x\n", "2.0.0")

    def test_tag_message_has_the_subject_then_the_section(self) -> None:
        self.assertEqual(
            deploy.tag_message("1.3.0", "### Fixed\n\n- A fix."),
            "Sustained 1.3.0\n\n### Fixed\n\n- A fix.\n",
        )

    def test_unexpected_changes_allows_only_the_release_notes(self) -> None:
        porcelain = (
            " M CHANGELOG.md\n M docs/changelog.md\n?? notes.txt\n M deploy.py\n"
        )
        self.assertEqual(
            deploy.unexpected_changes(porcelain), ["notes.txt", "deploy.py"]
        )
        self.assertEqual(deploy.unexpected_changes(""), [])

    def test_dist_problems(self) -> None:
        good = ["sustained-1.3.0-py3-none-any.whl", "sustained-1.3.0.tar.gz"]
        self.assertEqual(deploy.dist_problems(good, "1.3.0"), [])
        self.assertEqual(
            deploy.dist_problems(["sustained-1.2.0.tar.gz"], "1.3.0"),
            [
                "missing sustained-1.3.0.tar.gz",
                "missing a sustained-1.3.0-*.whl wheel",
                "unexpected files: sustained-1.2.0.tar.gz",
            ],
        )


class RunTests(unittest.TestCase):
    def test_run_returns_stdout_and_raises_on_failure(self) -> None:
        self.assertEqual(deploy.run([sys.executable, "-c", "print('hi')"]), "hi\n")
        with self.assertRaisesRegex(deploy.ReleaseError, "boom"):
            deploy.run(
                [sys.executable, "-c", "import sys; sys.exit('boom')"],
            )


class ReleaseFlowTests(unittest.TestCase):
    """The order of the release steps, with every command recorded."""

    def setUp(self) -> None:
        self.commands: List[List[str]] = []
        self.fail_on: Optional[str] = None
        self.pyproject = '[project]\nversion = "1.2.0"\n'
        self.written: List[str] = []

        def fake_run(args: List[str], cwd: Path = deploy.PROJECT_ROOT) -> str:
            self.commands.append(list(args))
            joined = " ".join(args)
            if self.fail_on and self.fail_on in joined:
                raise deploy.ReleaseError(f"{self.fail_on} failed")
            return ""

        def fake_read_text(path: Path) -> str:
            return CHANGELOG if path.name == "CHANGELOG.md" else self.pyproject

        def fake_write_text(path: Path, text: str) -> int:
            if path.name == "pyproject.toml":
                self.written.append(text)
            return len(text)

        def fake_build(version: str, work: Path) -> Path:
            fake_run(["build", version])
            return work

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(deploy, "run", side_effect=fake_run))
        stack.enter_context(
            mock.patch.object(deploy, "build_from_tag", side_effect=fake_build)
        )
        stack.enter_context(mock.patch.object(Path, "read_text", fake_read_text))
        stack.enter_context(mock.patch.object(Path, "write_text", fake_write_text))
        stack.enter_context(mock.patch.object(Path, "iterdir", lambda self: iter([])))

    def test_upload_comes_after_commit_and_tag(self) -> None:
        with mock.patch("builtins.input", return_value="y"):
            deploy.release("minor")
        joined = [" ".join(c) for c in self.commands]
        commit = next(i for i, c in enumerate(joined) if c.startswith("git commit"))
        tag = next(i for i, c in enumerate(joined) if c.startswith("git tag"))
        build = next(i for i, c in enumerate(joined) if c.startswith("build"))
        upload = next(i for i, c in enumerate(joined) if "twine upload" in c)
        push = next(i for i, c in enumerate(joined) if c.startswith("git push"))
        self.assertLess(commit, tag)
        self.assertLess(tag, build)
        self.assertLess(build, upload)
        self.assertLess(upload, push)
        self.assertEqual(self.written, ['[project]\nversion = "1.3.0"\n'])

    def test_a_dirty_tree_stops_before_any_change(self) -> None:
        with mock.patch.object(deploy, "unexpected_changes", return_value=["x.py"]):
            with self.assertRaisesRegex(deploy.ReleaseError, "x.py"):
                deploy.release("minor")
        self.assertEqual(self.written, [])

    def test_a_failed_commit_restores_the_version(self) -> None:
        self.fail_on = "git commit"
        with self.assertRaisesRegex(deploy.ReleaseError, "git commit failed"):
            deploy.release("minor")
        self.assertEqual(self.written[-1], self.pyproject)
        self.assertIn(["git", "reset", "--mixed", "HEAD"], self.commands)
        self.assertFalse(any("twine" in " ".join(c) for c in self.commands))

    def test_a_failed_tag_removes_the_commit(self) -> None:
        self.fail_on = "git tag -a"
        with self.assertRaises(deploy.ReleaseError):
            deploy.release("minor")
        self.assertIn(["git", "reset", "--mixed", "HEAD~1"], self.commands)
        self.assertEqual(self.written[-1], self.pyproject)

    def test_a_failed_build_removes_the_commit_and_tag(self) -> None:
        self.fail_on = "build"
        with self.assertRaises(deploy.ReleaseError):
            deploy.release("minor")
        self.assertIn(["git", "tag", "-d", "v1.3.0"], self.commands)
        self.assertIn(["git", "reset", "--mixed", "HEAD~1"], self.commands)
        self.assertEqual(self.written[-1], self.pyproject)

    def test_declining_removes_the_commit_and_tag(self) -> None:
        with mock.patch("builtins.input", return_value="n"):
            deploy.release("minor")
        self.assertIn(["git", "tag", "-d", "v1.3.0"], self.commands)
        self.assertFalse(any("twine upload" in " ".join(c) for c in self.commands))

    def test_a_failed_upload_keeps_the_commit_and_tag(self) -> None:
        self.fail_on = "twine upload"
        with mock.patch("builtins.input", return_value="y"):
            with self.assertRaisesRegex(deploy.ReleaseError, "stay local"):
                deploy.release("minor")
        self.assertNotIn(["git", "tag", "-d", "v1.3.0"], self.commands)
        self.assertFalse(any(c[:2] == ["git", "push"] for c in self.commands))


class MainTests(unittest.TestCase):
    def test_main_prints_the_error_and_exits_1(self) -> None:
        argv = ["deploy.py", "--version", "patch"]
        error = deploy.ReleaseError("bad")
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch("sys.stderr") as stderr,
            mock.patch.object(deploy, "release", side_effect=error),
        ):
            with self.assertRaises(SystemExit) as raised:
                deploy.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("error: bad", "".join(c.args[0] for c in stderr.write.mock_calls))


if __name__ == "__main__":
    unittest.main()
