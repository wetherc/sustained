#!/usr/bin/env python3

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

VERSION_PATTERN = re.compile(
    r'^(?P<prefix>version\s*=\s*")(?P<version>\d+\.\d+\.\d+)(?P<suffix>")',
    re.MULTILINE,
)


def read_version(pyproject_text: str) -> str:
    match = VERSION_PATTERN.search(pyproject_text)
    if not match:
        print("Error: could not find a version in pyproject.toml.", file=sys.stderr)
        sys.exit(1)
    return match.group("version")


def write_version(pyproject_path: Path, pyproject_text: str, version: str) -> None:
    updated = VERSION_PATTERN.sub(rf"\g<prefix>{version}\g<suffix>", pyproject_text)
    pyproject_path.write_text(updated)


def main() -> None:
    """
    Automates the package deployment process:
    1. Updates the version in pyproject.toml.
    2. Cleans the dist/ directory.
    3. Builds the package using 'python3 -m build'.
    4. Uploads the package to PyPI using 'python3 -m twine upload dist/*'.
    5. Commits, tags, and pushes the release.

    The version bump is rolled back if the build fails, if the upload
    fails, or if the user aborts at the confirmation prompt.
    """
    parser = argparse.ArgumentParser(description="Deploy the package.")
    parser.add_argument(
        "--version",
        type=str,
        required=True,
        choices=["major", "minor", "patch"],
        help="The type of version increment (major, minor, or patch).",
    )
    project_root = Path(__file__).resolve().parent
    args = parser.parse_args()

    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        print(f"Error: {pyproject_path} not found.", file=sys.stderr)
        sys.exit(1)

    pyproject_text = pyproject_path.read_text()
    current_version = read_version(pyproject_text)
    version_parts = list(map(int, current_version.split(".")))

    increment_map = {"major": 0, "minor": 1, "patch": 2}
    increment_index = increment_map[args.version]

    version_parts[increment_index] += 1
    for i in range(increment_index + 1, len(version_parts)):
        version_parts[i] = 0

    new_version = ".".join(map(str, version_parts))
    write_version(pyproject_path, pyproject_text, new_version)
    print(f"Updated version to {new_version}")

    def rollback() -> None:
        write_version(pyproject_path, pyproject_text, current_version)
        print(f"Version rolled back to {current_version}.")

    dist_dir = project_root / "dist"
    if dist_dir.exists():
        print(f"Removing existing '{dist_dir}' directory...")
        shutil.rmtree(dist_dir)

    print("Building the package...")
    build_result = subprocess.run(
        ["python3", "-m", "build"], capture_output=True, text=True, cwd=project_root
    )
    if build_result.returncode != 0:
        print("Error during build:", file=sys.stderr)
        print(build_result.stdout)
        print(build_result.stderr, file=sys.stderr)
        rollback()
        sys.exit(1)
    print(build_result.stdout)

    confirm = input(f"Publish version {new_version} to PyPI? (y/N): ").lower()
    if confirm not in ["y", "yes"]:
        print("Aborting.")
        rollback()
        sys.exit(0)

    print("Uploading to PyPI...")
    upload_result = subprocess.run(
        ["python3", "-m", "twine", "upload", "dist/*"],
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    if upload_result.returncode != 0:
        print("Error during upload:", file=sys.stderr)
        print(upload_result.stdout)
        print(upload_result.stderr, file=sys.stderr)
        rollback()
        print("Upload failed.")
        sys.exit(1)
    print(upload_result.stdout)

    print("Committing version change...")
    subprocess.run(
        ["git", "add", str(pyproject_path)],
        cwd=project_root,
        check=True,
        capture_output=True,
    )
    commit_message = f"chore(release): v{new_version}"
    subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=project_root,
        check=True,
        capture_output=True,
    )

    print(f"Tagging version {new_version}...")
    tag_name = f"v{new_version}"
    tag_message = f"Sustained v{new_version}"
    subprocess.run(
        ["git", "tag", "-a", tag_name, "-m", tag_message],
        cwd=project_root,
        check=True,
        capture_output=True,
    )

    print("Pushing release commit and tags to remote...")
    subprocess.run(["git", "push"], cwd=project_root, check=True)
    subprocess.run(["git", "push", "--tags"], cwd=project_root, check=True)

    print("Deployment successful!")


if __name__ == "__main__":
    main()
