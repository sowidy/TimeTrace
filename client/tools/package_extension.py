"""Package the extension with source acknowledgements, licenses and privacy notice."""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

CLIENT = Path(__file__).resolve().parents[1]
SOURCE = CLIENT / "browser_extension/chromium"
FILES = ("manifest.json", "options.html", "options.js", "protocol.js", "service-worker.js", "README.md",
         "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "PRIVACY.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=CLIENT / "dist/extension")
    args = parser.parse_args()
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / f"TimeTrace-chromium-{manifest['version']}.zip"
    files = [SOURCE / name for name in FILES]
    files.extend(p for p in (SOURCE / "licenses").rglob("*") if p.is_file())
    with ZipFile(archive, "w", ZIP_DEFLATED) as package:
        for path in files:
            package.write(path, path.relative_to(SOURCE).as_posix())
    with ZipFile(archive) as package:
        assert package.testzip() is None
        assert set(package.namelist()) == {p.relative_to(SOURCE).as_posix() for p in files}
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(archive), "sha256": digest, "files": len(files)}))


if __name__ == "__main__":
    main()
