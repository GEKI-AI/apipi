import tomllib
from pathlib import Path

from apipi import __version__


def test_version_is_first_public_release() -> None:
    assert __version__ == "0.3.2"


def test_pyproject_ships_cli_and_s3_extra() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    assert project["name"] == "geki-apipi"
    assert project["scripts"]["apipi"] == "apipi.cli:main"
    assert "s3" in project["optional-dependencies"]
    assert project["urls"]["Documentation"] == "https://geki-ai.github.io/apipi/"


def test_alembic_revisions_chain() -> None:
    versions = Path("src/apipi/store/migrations/versions")
    files = sorted(path.name for path in versions.glob("*.py"))
    assert files == [
        "0001_initial.py",
        "0002_workers.py",
        "0003_pi_session.py",
        "0004_vaults.py",
        "0005_leases.py",
        "0006_worker_memory.py",
        "0007_files.py",
        "0008_skills.py",
        "0009_pi_session_uri.py",
        "0010_uploads.py",
    ]
