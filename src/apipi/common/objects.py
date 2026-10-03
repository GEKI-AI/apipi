"""Object ids and paths in the shared store layout."""

from pathlib import Path
from typing import Literal

Namespace = Literal["artifacts", "files", "skills", "templates"]

NS_ARTIFACTS: Namespace = "artifacts"
NS_FILES: Namespace = "files"
NS_SKILLS: Namespace = "skills"
NS_TEMPLATES: Namespace = "templates"


def local_object_path(root: Path, namespace: Namespace, object_id: str) -> Path:
    body = check_object_id(object_id)
    if namespace == NS_ARTIFACTS:
        return root / ".artifacts" / body
    return root / ".store" / namespace / body


def check_object_id(object_id: str) -> str:
    text = object_id.strip().strip("/")
    if not text:
        raise ValueError("object id is required")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid object id")
    return text
