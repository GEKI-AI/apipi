import base64
import binascii
from dataclasses import dataclass
from typing import Any

from apipi.common.errors import ApiError
from apipi.config import Settings

_DEFAULT_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


@dataclass(frozen=True)
class ImagePart:
    mime: str = ""
    data: bytes = b""
    file_id: str = ""


@dataclass(frozen=True)
class UserContent:
    text: str
    images: tuple[ImagePart, ...]
    parts: tuple[dict[str, Any], ...]

    def wire_parts(self, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        index = 0
        for part in self.parts:
            if part.get("type") == "input_text":
                out.append({"type": "input_text", "text": str(part.get("text") or "")})
            else:
                out.append(refs[index])
                index += 1
        return out


def image_mimes(settings: Settings) -> frozenset[str]:
    raw = settings.image_mimes
    if not raw:
        return _DEFAULT_MIMES
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def _decode_data_url(value: str, *, settings: Settings) -> ImagePart:
    text = value.strip()
    if text.startswith("http://") or text.startswith("https://"):
        raise ApiError(
            "invalid_request",
            "input_image accepts data URLs only",
            code="invalid_request",
        )
    if not text.startswith("data:") or ";base64," not in text:
        raise ApiError(
            "invalid_request",
            "input_image image_url must be a data URL",
            code="invalid_request",
        )
    header, encoded = text.split(";base64,", 1)
    mime = header[5:].strip().lower()
    if mime not in image_mimes(settings):
        raise ApiError(
            "invalid_request",
            f"input_image mime {mime} is not allowed",
            code="invalid_request",
        )
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(
            "invalid_request",
            "input_image data is not valid base64",
            code="invalid_request",
        ) from exc
    if not data:
        raise ApiError(
            "invalid_request",
            "input_image data is empty",
            code="invalid_request",
        )
    if len(data) > settings.max_image_bytes:
        raise ApiError(
            "invalid_request",
            "input_image is too large",
            code="payload_too_large",
            status_code=413,
        )
    return ImagePart(mime=mime, data=data)


def _image_part(item: dict[str, Any], settings: Settings) -> ImagePart:
    url = item.get("image_url")
    file_id = item.get("file_id")
    has_url = isinstance(url, str) and bool(url.strip())
    has_file = isinstance(file_id, str) and bool(file_id.strip())
    if has_url == has_file:
        raise ApiError(
            "invalid_request",
            "input_image needs image_url or file_id",
            code="invalid_request",
        )
    if isinstance(file_id, str) and has_file:
        return ImagePart(file_id=file_id.strip())
    return _decode_data_url(str(url), settings=settings)


def _parts_from_content(content: object, *, settings: Settings) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ApiError(
            "invalid_request",
            "message content must be a string or a list",
            code="invalid_request",
        )
    parts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            raise ApiError(
                "invalid_request",
                "message content parts must be objects",
                code="invalid_request",
            )
        kind = item.get("type")
        if kind == "input_text":
            text = item.get("text")
            if not isinstance(text, str):
                raise ApiError(
                    "invalid_request",
                    "input_text needs text",
                    code="invalid_request",
                )
            parts.append({"type": "input_text", "text": text})
        elif kind == "input_image":
            parts.append({"type": "input_image", "image": _image_part(item, settings)})
        else:
            raise ApiError(
                "not_implemented",
                f"{kind} is not implemented",
                code=str(kind or "input"),
            )
    return parts


def require_image_model(
    settings: Settings, model: str | None, content: UserContent
) -> None:
    if not content.images:
        return
    from apipi.common.model_caps import model_accepts_image

    if model_accepts_image(settings.model_registry, model):
        return
    raise ApiError(
        "invalid_request",
        "model does not accept images",
        code="unsupported_input",
    )


def parse_user_content(value: object, *, settings: Settings) -> UserContent:
    messages: list[object]
    if value is None:
        messages = []
    elif isinstance(value, str):
        messages = [{"role": "user", "content": value}]
    elif isinstance(value, dict):
        if isinstance(value.get("content"), list) or value.get("role"):
            messages = [value]
        else:
            text = value.get("content")
            if not isinstance(text, str):
                text = value.get("text")
            messages = [
                {"role": "user", "content": text if isinstance(text, str) else ""}
            ]
    elif isinstance(value, list):
        messages = list(value)
    else:
        raise ApiError(
            "invalid_request",
            "input must be a string, message, or list of messages",
            code="invalid_request",
        )
    parts: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, str):
            parts.extend(_parts_from_content(message, settings=settings))
            continue
        if not isinstance(message, dict):
            raise ApiError(
                "invalid_request",
                "input messages must be objects",
                code="invalid_request",
            )
        parts.extend(_parts_from_content(message.get("content"), settings=settings))
    texts = [str(part["text"]) for part in parts if part.get("type") == "input_text"]
    images = tuple(
        part["image"] for part in parts if isinstance(part.get("image"), ImagePart)
    )
    if len(images) > settings.max_images:
        raise ApiError(
            "invalid_request",
            "too many images",
            code="invalid_request",
        )
    stored = tuple(
        {"type": "input_text", "text": str(part["text"])}
        if part.get("type") == "input_text"
        else {"type": "input_image"}
        for part in parts
    )
    return UserContent(text="\n".join(texts), images=images, parts=stored)
