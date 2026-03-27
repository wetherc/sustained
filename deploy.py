#!/usr/bin/env python3

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import toml


def main():
    """
    Automates the package deployment process:
    1. Updates the version in pyproject.toml.
    2. Cleans the dist/ directory.
    3. Builds the package using 'python3 -m build'.
    4. Uploads the package to PyPI using 'python3 -m twine upload dist/*'.
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

    pyproject_data = toml.load(pyproject_path)

    try:
        current_version = pyproject_data["project"]["version"]
        version_parts = list(map(int, current_version.split(".")))
    except (KeyError, ValueError) as e:
        print(f"Error parsing version from {pyproject_path}: {e}", file=sys.stderr)
        sys.exit(1)

    increment_map = {"major": 0, "minor": 1, "patch": 2}
    increment_index = increment_map[args.version]

    version_parts[increment_index] += 1

    for i in range(increment_index + 1, len(version_parts)):
        version_parts[i] = 0

    new_version = ".".join(map(str, version_parts))
    pyproject_data["project"]["version"] = new_version

    with open(pyproject_path, "w") as f:
        toml.dump(pyproject_data, f)

    print(f"Updated version to {new_version}")

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
        sys.exit(1)
    print(build_result.stdout)

    confirm = input(f"Publish version {new_version} to PyPI? (y/N): ").lower()
    if confirm.lower() in ["y", "yes"]:
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
            # Rollback on upload failure
            pyproject_data["project"]["version"] = current_version
            with open(pyproject_path, "w") as f:
                toml.dump(pyproject_data, f)
            print(f"Upload failed. Version rolled back to {current_version}.")
            sys.exit(1)
        print(upload_result.stdout)
    else:
        print("Aborting. Rolling back version change.")
        pyproject_data["project"]["version"] = current_version
        with open(pyproject_path, "w") as f:
            toml.dump(pyproject_data, f)
        print(f"Version rolled back to {current_version}.")
        sys.exit(0)

    print("Deployment successful!")


if __name__ == "__main__":
    main()
