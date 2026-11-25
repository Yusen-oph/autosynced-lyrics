r"""Publish a new version on GitHub:  .venv\Scripts\python release.py "what changed"

1. bump VERSION in installer.py first (e.g. 2.1 -> 2.2)
2. this builds the app (build.py), then creates release vX.Y on GitHub with the three files
   the app's updater looks for. Needs the GitHub CLI (gh), logged in.
Everyone's app sees the new version within 6 hours (or at once via Settings -> About -> Check now).
"""
import subprocess
import sys

import build
import installer
import updater


HOW_TO_GET = ("**New here? Download AutosyncedLyrics-Windows.zip**, unzip it and run Autosynced Lyrics.exe.\n"
              "Already have the app? Settings -> About -> Check now. (AutosyncedLyrics-Update.zip is "
              "only for updating an installed copy: it can't run on its own.)\n\n")


def main():
    notes = " ".join(sys.argv[1:]).strip() or f"Version {installer.VERSION}"
    notes = HOW_TO_GET + notes
    tag = f"v{installer.VERSION}"
    exists = subprocess.run(["gh", "release", "view", tag, "--repo", updater.REPO],
                            capture_output=True).returncode == 0
    if exists:
        sys.exit(f"Release {tag} already exists: bump VERSION in installer.py first.")
    build.main()
    manifest = build.DIST / updater.MANIFEST
    with __import__("zipfile").ZipFile(build.UPDATE_ZIP) as z:
        manifest.write_bytes(z.read(updater.MANIFEST))
    subprocess.run(["gh", "release", "create", tag, "--repo", updater.REPO,
                    "--title", f"Autosynced Lyrics {installer.VERSION}", "--notes", notes,
                    str(manifest), str(build.UPDATE_ZIP), str(build.ZIP)], check=True)
    print(f"Published {tag}: https://github.com/{updater.REPO}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
