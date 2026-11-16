import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from PyInstaller.archive.readers import CArchiveReader


ROOT = Path(__file__).resolve().parent
NAME = "SteamLaunchHelper_v3_6_1"
LAUNCHER_HASH = "f73d2b4e7299d94454214bffacc4ed8a0e5159124d95d3c6ee607831de7633b6"
RUNTIME_FILES = {
    "launcher.exe",
    "_internal/_bz2.pyd",
    "_internal/_decimal.pyd",
    "_internal/_hashlib.pyd",
    "_internal/_lzma.pyd",
    "_internal/_socket.pyd",
    "_internal/_ssl.pyd",
    "_internal/_zstd.pyd",
    "_internal/base_library.zip",
    "_internal/libcrypto-3.dll",
    "_internal/libssl-3.dll",
    "_internal/python314.dll",
    "_internal/select.pyd",
    "_internal/unicodedata.pyd",
    "_internal/VCRUNTIME140.dll",
}


def sha256(file):
    return hashlib.file_digest(file, "sha256").hexdigest()


def stage_launcher(destination, release_zip):
    if release_zip is None:
        shutil.copytree(ROOT / "launcher", destination)
    else:
        # Read only the known launcher files from the prior release.
        with ZipFile(release_zip) as archive:
            for name in sorted(RUNTIME_FILES):
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(f"launcher/{name}") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

    missing = [name for name in sorted(RUNTIME_FILES)
               if not (destination / name).is_file() or not (destination / name).stat().st_size]
    if missing:
        raise ValueError(f"Incomplete launcher template: {', '.join(missing)}")
    with (destination / "launcher.exe").open("rb") as file:
        if sha256(file) != LAUNCHER_HASH:
            raise ValueError("launcher.exe differs from the working v3.6 release template")


def build(release_zip):
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{NAME}-", dir=dist) as temp:
        work = Path(temp)
        package = work / "package"
        package.mkdir()
        stage_launcher(package / "launcher", release_zip)
        subprocess.run([
            sys.executable, "-m", "PyInstaller",
            "--noconsole", "--onefile", "--clean", "--noconfirm",
            "--collect-all", "customtkinter", "--collect-data", "certifi",
            "--name", NAME, "--icon", str(ROOT / "steamlaunchhelper.ico"),
            "--add-data", f"{ROOT / 'steamlaunchhelper.ico'}:.", "--add-data", f"{ROOT / 'steamlaunchhelper.png'}:.",
            "--workpath", str(work / "build"), "--specpath", str(work),
            "--distpath", str(package), str(ROOT / f"{NAME}.py"),
        ], cwd=ROOT, check=True)
        executable = package / f"{NAME}.exe"
        bundle = CArchiveReader(str(executable))
        ca_entry = next((name for name in bundle.toc
                         if name.replace("\\", "/") == "certifi/cacert.pem"), None)
        if ca_entry is None or b"-----BEGIN CERTIFICATE-----" not in bundle.extract(ca_entry):
            raise ValueError("The frozen application is missing its certifi certificate bundle")
        result = subprocess.run([str(executable), "--check-tls"], cwd=package, timeout=60)
        if result.returncode:
            raise ValueError(
                "The frozen application failed its TLS check "
                f"(exit {result.returncode}). See %LOCALAPPDATA%/SteamLaunchHelper/logs/steamgriddb.log."
            )
        shutil.copyfile(ROOT / "RELEASE_v3_6_1.md", package / "README.md")

        files = {p.relative_to(package).as_posix(): p for p in package.rglob("*") if p.is_file()}
        archive_path = work / f"{NAME}.zip"
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            for name, path in sorted(files.items()):
                archive.write(path, name)
        with ZipFile(archive_path) as archive:
            if set(archive.namelist()) != files.keys():
                raise ValueError("Release ZIP contents differ from the staged package")
            for name, path in files.items():
                with archive.open(name) as member, path.open("rb") as source:
                    if sha256(member) != sha256(source):
                        raise ValueError(f"Release ZIP validation failed for {name}")

        target = dist / archive_path.name
        os.replace(archive_path, target)
        print(f"Validated release: {target}")


def main():
    parser = argparse.ArgumentParser(description="Build and validate the Windows release ZIP.")
    parser.add_argument("--launcher-zip", type=Path,
                        help="v3.6 Windows release ZIP supplying the existing launcher template")
    args = parser.parse_args()
    try:
        build(args.launcher_zip)
    except (OSError, KeyError, ValueError, BadZipFile, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Build failed: {exc}\n")


if __name__ == "__main__":
    main()
