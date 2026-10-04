import re

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

GUEST_ENV_NEVER = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY_OVERWRITE",
        "DATABASE_URL",
        "PI_CODING_AGENT_DIR",
        "NODE_OPTIONS",
        "APIPI_SEARCH_URL",
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    }
)

GUEST_ENV_RESERVED_PREFIXES = ("OPENAI_", "APIPI_", "PI_", "CODEX_")

SECRET_NAME_RESERVED = GUEST_ENV_NEVER | frozenset(
    {
        "USER",
        "SHELL",
        "PWD",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
        "CURL_CA_BUNDLE",
        "NPM_CONFIG_CACHE",
        "npm_config_cache",
        "UV_CACHE_DIR",
        "GIT_CONFIG_COUNT",
        "WS",
        "CA_BUNDLE",
        "CA_DIR",
        "cmd",
    }
)
SECRET_NAME_RESERVED_PATTERN = re.compile(r"GIT_CONFIG_(KEY|VALUE)_[0-9]+")


def reserved_secret_name(name: str) -> bool:
    if name in SECRET_NAME_RESERVED:
        return True
    if SECRET_NAME_RESERVED_PATTERN.fullmatch(name):
        return True
    return name.startswith(GUEST_ENV_RESERVED_PREFIXES)
