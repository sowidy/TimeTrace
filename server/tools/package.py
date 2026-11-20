"""Build a server-only ZIP, excluding real configuration and local artifacts."""
import argparse
from pathlib import Path
import zipfile
import tempfile

from collect_licenses import stage

SERVER = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    required = ("app.py", "api/tracking.py", "api/tracking_schemas.py", "db/tracking.py",
                "mail/tracking_report.py", "mail/charts.py", "mail/chart_theme.py", "mail/colors.py", "mail/timeline.py",
                "mail/site_categories.py", "mail/site_names.py",
                "db/schema.sql", "run.py", "check.py")
    for name in required:
        if not (SERVER / name).is_file():
            raise RuntimeError("Missing runtime file: " + name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="timetrace-legal-") as temp, \
            zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        stage(temp, ("fastapi", "uvicorn", "pydantic", "pymysql", "cryptography", "Pillow"))
        for path in sorted(SERVER.rglob("*")):
            relative = path.relative_to(SERVER)
            if not path.is_file() or any(p in ("__pycache__", ".venv", ".git") for p in relative.parts):
                continue
            if path.name.startswith(".env") and relative.as_posix() != ".env.example":
                continue
            if relative.parts[0] == "licenses" or relative.as_posix() in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
                continue  # Legal files come from the checked staging directory below.
            allowed = (path.suffix in (".py", ".sql", ".md", ".yaml", ".example")
                       or (relative.parts[:2] == ("docs", "images") and path.suffix.lower() in (".png", ".jpg", ".webp"))
                       or relative.as_posix() in ("requirements.txt", "Dockerfile", ".dockerignore"))
            if allowed:
                archive.write(path, "server/" + relative.as_posix())
        for path in sorted(Path(temp).rglob("*")):
            if path.is_file():
                archive.write(path, "server/" + path.relative_to(temp).as_posix())
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
