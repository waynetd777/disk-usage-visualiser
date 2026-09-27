#!/usr/bin/env python3
"""Packs the release build into a DMG laid out like other Mac installers: the app on the left,
an arrow to Applications on the right, on a background with the app's icon, name and a hint.

    python3 tools/dmg/make_dmg.py        # after make app; writes dist-release/Disk-Usage.dmg

Finder lays out the window, so the first run asks to let the terminal control Finder.
"""
import os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
APP = ROOT / "src-tauri/target/release/bundle/macos/Disk Usage.app"
OUT = ROOT / "dist-release/Disk-Usage.dmg"
VOLUME = "Disk Usage"
W, H = 660, 400


def run(*cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout


def background(tmp: Path) -> Path:
    """background.tiff, at 1x and 2x so it's sharp on Retina screens."""
    page = HERE / "background.html"
    pngs = []
    for scale in (1, 2):
        png = tmp / f"background@{scale}x.png"
        run("swift", str(HERE / "snapshot.swift"), str(page), str(png), str(W), str(H), str(scale))
        pngs.append(str(png))
    tiff = tmp / "background.tiff"
    run("tiffutil", "-cathidpicheck", *pngs, "-out", str(tiff))
    return tiff


LAYOUT = """
tell application "Finder"
  tell disk "{volume}"
    open
    set w to container window
    set current view of w to icon view
    set toolbar visible of w to false
    set statusbar visible of w to false
    set pathbar visible of w to false
    set sidebar width of w to 0
    delay 1
    set the bounds of w to {{200, 120, {right}, {bottom}}}
    set o to the icon view options of w
    set arrangement of o to not arranged
    set icon size of o to 112
    set text size of o to 13
    set background picture of o to file ".background:background.tiff"
    set position of item "Disk Usage.app" of w to {{165, 230}}
    set position of item "Applications" of w to {{495, 230}}
    set extension hidden of item "Disk Usage.app" of w to true
    set the bounds of w to {{200, 120, {right}, {bottom}}}
    update without registering applications
    delay 2
    close
  end tell
end tell
"""


def main():
    if not APP.is_dir():
        sys.exit(f"no app at {APP.relative_to(ROOT)}: run make app first")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stage = tmp / "stage"
        stage.mkdir()
        run("ditto", str(APP), str(stage / APP.name))
        os.symlink("/Applications", stage / "Applications")
        (stage / ".background").mkdir()
        shutil.copy(background(tmp), stage / ".background/background.tiff")
        shutil.copy(APP / "Contents/Resources/icon.icns", stage / ".VolumeIcon.icns")
        rw = tmp / "rw.dmg"
        run("hdiutil", "create", "-volname", VOLUME, "-srcfolder", str(stage), "-fs", "HFS+", "-format", "UDRW", "-ov", str(rw))
        # Detach anything left mounted under the same name, or Finder lays out the wrong window.
        if Path(f"/Volumes/{VOLUME}").exists():
            subprocess.run(["hdiutil", "detach", f"/Volumes/{VOLUME}", "-force"], capture_output=True)
        dev = run("hdiutil", "attach", "-readwrite", "-noverify", "-noautoopen", str(rw)).split()[0]
        try:
            mount = Path(f"/Volumes/{VOLUME}")
            run("SetFile", "-a", "C", str(mount))
            time.sleep(1)
            run("osascript", "-e", LAYOUT.format(volume=VOLUME, right=200 + W, bottom=120 + H + 28))
            run("sync")
        finally:
            run("hdiutil", "detach", dev)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        run("hdiutil", "convert", str(rw), "-format", "UDZO", "-imagekey", "zlib-level=9", "-ov", "-o", str(OUT))
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
