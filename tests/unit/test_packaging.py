import tomllib
from pathlib import Path

from apipi import __version__


def test_version_is_first_public_release() -> None:
    assert __version__ == "0.13.0"


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
        "0011_idle_ttl.py",
        "0012_session_user.py",
        "0013_agent_session_defaults.py",
        "0014_templates.py",
        "0015_session_org.py",
        "0016_turn_failure.py",
        "0017_upstream_attempts.py",
        "0018_sandbox_status.py",
        "0019_agent_versions.py",
        "0020_mcp_list_tools.py",
        "0021_agent_revision.py",
        "0022_drop_agent_versions.py",
        "0023_session_tools.py",
        "0024_drop_pi_session_uri.py",
        "0025_worker_tokens.py",
        "0026_worker_ingest.py",
        "0027_artifact_uploads.py",
        "0028_search_usage.py",
    ]
