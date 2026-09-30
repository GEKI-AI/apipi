from pathlib import Path

from apipi.config import ConfigError

_BROWSER = Path(__file__).resolve().parent / "skills" / "browser"


def browser_skill_dir() -> Path:
    if not (_BROWSER / "SKILL.md").is_file():
        raise ConfigError("browser skill is missing")
    return _BROWSER
