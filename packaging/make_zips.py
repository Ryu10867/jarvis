"""Packs the two installer zips (called by build_package.ps1):

    dist/Jarvis-Installer-Windows.zip   wheel + install.bat / install.ps1 / uninstall.bat + README
    dist/Jarvis-Installer-macOS.zip     wheel + "Install Jarvis.command" / install.sh / "Uninstall Jarvis.command" + README

The Mac zip is written by hand rather than with Compress-Archive: shell scripts need Unix line endings and the
executable bit, and a zip made the ordinary way on Windows carries neither (double-clicking the .command would fail).
Also refreshes docs/install.ps1 and docs/install.sh, the copies the website's one-line installers download.
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG, DIST, DOCS = ROOT / "packaging", ROOT / "dist", ROOT / "docs"


def unix(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def add(z: zipfile.ZipFile, name: str, data: bytes, executable: bool = False):
    info = zipfile.ZipInfo(name)
    info.date_time = __import__("time").localtime()[:6]
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3                                           # Unix: so the permission bits below are honoured
    info.external_attr = ((0o755 if executable else 0o644) | 0o100000) << 16
    z.writestr(info, data)


def main(version: str):
    wheel = next(DIST.glob(f"jarvis_assistant-{version}-*.whl"))
    readme = (PKG / "INSTALL.md").read_bytes()

    win = DIST / "Jarvis-Installer-Windows.zip"
    with zipfile.ZipFile(win, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(wheel, wheel.name)
        for name in ("install.bat", "install.ps1", "uninstall.bat"):
            z.writestr(name, unix((PKG / name).read_bytes()).replace(b"\n", b"\r\n"))      # Windows line endings
        z.writestr("README.md", readme)

    mac = DIST / "Jarvis-Installer-macOS.zip"
    with zipfile.ZipFile(mac, "w", zipfile.ZIP_DEFLATED) as z:
        add(z, wheel.name, wheel.read_bytes())
        for name in ("Install Jarvis.command", "install.sh", "Uninstall Jarvis.command"):
            data = unix((PKG / name).read_bytes())
            assert data.startswith(b"#!/bin/bash\n"), name
            add(z, name, data, executable=True)
        add(z, "README.md", unix(readme))

    DOCS.mkdir(exist_ok=True)
    (DOCS / "install.sh").write_bytes(unix((PKG / "install.sh").read_bytes()))
    (DOCS / "install.ps1").write_bytes((PKG / "install.ps1").read_bytes())
    for p in (win, mac):
        print(f"  {p.name}  ({p.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1])
