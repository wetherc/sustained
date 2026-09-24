#!/usr/bin/env python3
"""Release a new version of the package.

The script bumps the version in pyproject.toml, commits it with the
changelog, and tags the commit. It then builds the sdist and wheel from a
`git archive` of the tag, so ignored or untracked files in the working tree
never reach the package. It runs `twine check`, asks for confirmation, and
uploads. It pushes the commit and the tag last.

PyPI never accepts a version twice, so the upload happens after the commit
and the tag exist. A pre-commit hook that fails stops the release before
anything is public.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parent

VERSION_PATTERN = re.compile(
    r'^(?P<prefix>version\s*=\s*")(?P<version>\d+\.\d+\.\d+)(?P<suffix>")',
    re.MULTILINE,
)

# Release notes for the new version go in the release commit, so these files
# may have changes when the script starts. Any other change stops it.
RELEASE_NOTE_FILES = ("CHANGELOG.md", "docs/changelog.md")


class ReleaseError(Exception):
    """A release step failed. The message says what to fix."""


def read_version(pyproject_text: str) -> str:
    match = VERSION_PATTERN.search(pyproject_text)
    if not match:
        raise ReleaseError("could not find a version in pyproject.toml.")
    return match.group("version")


def replace_version(pyproject_text: str, version: str) -> str:
    return VERSION_PATTERN.sub(rf"\g<prefix>{version}\g<suffix>", pyproject_text)


def bump(version: str, part: str) -> str:
    """Increment one part of a major.minor.patch version and zero the rest."""
    index = ("major", "minor", "patch").index(part)
    parts = [int(p) for p in version.split(".")]
    parts[index] += 1
    parts[index + 1 :] = [0] * (len(parts) - index - 1)
    return ".".join(map(str, parts))


def changelog_section(changelog_text: str, version: str) -> str:
    """Return the body under `## <version>`, without the heading line."""
    lines = changelog_text.splitlines()
    heading = f"## {version}"
    if heading not in lines:
        raise ReleaseError(f"CHANGELOG.md has no '{heading}' section.")
    start = lines.index(heading) + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise ReleaseError(f"the '{heading}' section in CHANGELOG.md is empty.")
    return body


def tag_message(version: str, section: str) -> str:
    return f"Sustained {version}\n\n{section}\n"


def unexpected_changes(porcelain: str) -> list[str]:
    """Paths in `git status --porcelain` output outside the release notes."""
    paths = [line[3:] for line in porcelain.splitlines() if line.strip()]
    return [path for path in paths if path not in RELEASE_NOTE_FILES]


def dist_problems(names: Sequence[str], version: str) -> list[str]:
    """Reasons the built files do not match one sdist and one wheel."""
    sdist = f"sustained-{version}.tar.gz"
    wheel_prefix = f"sustained-{version}-"
    problems = []
    if sdist not in names:
        problems.append(f"missing {sdist}")
    if not any(n.startswith(wheel_prefix) and n.endswith(".whl") for n in names):
        problems.append(f"missing a {wheel_prefix}*.whl wheel")
    extra = [n for n in names if n != sdist and not n.startswith(wheel_prefix)]
    if extra:
        problems.append(f"unexpected files: {', '.join(sorted(extra))}")
    return problems


def run(args: Sequence[str], cwd: Path = PROJECT_ROOT) -> str:
    """Run a command, return its stdout, and raise ReleaseError on failure."""
    result = subprocess.run(list(args), capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise ReleaseError(f"'{' '.join(args)}' failed:\n{output}")
    return result.stdout


def commit(version: str) -> None:
    """Commit the version and the release notes."""
    run([sys.executable, "sync_changelog.py"])
    run(["git", "add", "pyproject.toml", *RELEASE_NOTE_FILES])
    run(["git", "commit", "-m", f"Release {version}"])


def tag(version: str, section: str) -> None:
    """Tag HEAD with the changelog section as the annotation."""
    with tempfile.TemporaryDirectory() as tmp:
        message = Path(tmp) / "tag-message.txt"
        message.write_text(tag_message(version, section))
        run(
            [
                "git",
                "tag",
                "-a",
                "--cleanup=verbatim",
                "-F",
                str(message),
                f"v{version}",
            ]
        )


def build_from_tag(version: str, work: Path) -> Path:
    """Build the sdist and wheel from `git archive` of the tag."""
    source = work / "source"
    source.mkdir()
    archive = work / "source.tar"
    run(["git", "archive", "--format=tar", "-o", str(archive), f"v{version}"])
    run(["tar", "-xf", str(archive), "-C", str(source)])
    dist = work / "dist"
    run([sys.executable, "-m", "build", "--outdir", str(dist), str(source)])
    names = sorted(p.name for p in dist.iterdir())
    problems = dist_problems(names, version)
    if problems:
        raise ReleaseError("the build is wrong: " + "; ".join(problems))
    run([sys.executable, "-m", "twine", "check", "--strict", *map(str, dist.iterdir())])
    return dist


def undo_release(version: str, old_pyproject: str) -> None:
    """Delete the local tag and commit, and keep the release notes edits."""
    run(["git", "tag", "-d", f"v{version}"])
    run(["git", "reset", "--mixed", "HEAD~1"])
    (PROJECT_ROOT / "pyproject.toml").write_text(old_pyproject)
    print(f"Removed the local v{version} commit and tag.")


def release(part: str) -> None:
    changes = unexpected_changes(run(["git", "status", "--porcelain"]))
    if changes:
        raise ReleaseError(
            "the working tree has changes outside the release notes: "
            + ", ".join(changes)
        )

    pyproject = PROJECT_ROOT / "pyproject.toml"
    old_pyproject = pyproject.read_text()
    version = bump(read_version(old_pyproject), part)
    section = changelog_section((PROJECT_ROOT / "CHANGELOG.md").read_text(), version)

    pyproject.write_text(replace_version(old_pyproject, version))
    try:
        commit(version)
    except ReleaseError:
        run(["git", "reset", "--mixed", "HEAD"])
        pyproject.write_text(old_pyproject)
        raise
    try:
        tag(version, section)
    except ReleaseError:
        run(["git", "reset", "--mixed", "HEAD~1"])
        pyproject.write_text(old_pyproject)
        raise
    print(f"Committed and tagged v{version}.")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            dist = build_from_tag(version, Path(tmp))
        except ReleaseError:
            undo_release(version, old_pyproject)
            raise
        files = sorted(str(p) for p in dist.iterdir())
        print("Built and checked:", ", ".join(Path(f).name for f in files))

        confirm = input(f"Publish version {version} to PyPI? (y/N): ").lower()
        if confirm not in ("y", "yes"):
            undo_release(version, old_pyproject)
            print("Aborted.")
            return

        # One file can reach PyPI before another fails, and PyPI then refuses
        # the version forever, so a failed upload keeps the commit and tag.
        try:
            run([sys.executable, "-m", "twine", "upload", "--non-interactive", *files])
        except ReleaseError as error:
            raise ReleaseError(
                f"{error}\nThe v{version} commit and tag stay local. Check PyPI, "
                "then upload the missing files with 'twine upload --skip-existing' "
                f"from a build of v{version}, and push the commit and tag."
            ) from None
    print(f"Uploaded {version} to PyPI.")

    run(["git", "push", "origin", "HEAD"])
    run(["git", "push", "origin", f"v{version}"])
    print("Pushed the release commit and tag.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Release the package.")
    parser.add_argument(
        "--version",
        required=True,
        choices=["major", "minor", "patch"],
        help="The part of the version to increment.",
    )
    args = parser.parse_args()
    try:
        release(args.version)
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
