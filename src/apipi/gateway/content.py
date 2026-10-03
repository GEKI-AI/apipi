import base64
import binascii
from dataclasses import dataclass
from typing import Any

from apipi.common.errors import ApiError
from apipi.config import Settings

_DEFAULT_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


@dataclass(frozen=True)
class ImagePart:
    mime: str
    data: bytes

    def rpc(self) -> dict[str, str]:
        return {
            "type": "image",
            "data": base64.b64encode(self.data).decode(),
            "mimeType": self.mime,
        }


@dataclass(frozen=True)
class UserContent:
    text: str
    images: tuple[ImagePart, ...]
    parts: tuple[dict[str, Any], ...]

    def wire_parts(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        index = 0
        for part in self.parts:
            if part.get("type") == "input_text":
                out.append({"type": "input_text", "text": str(part.get("text") or "")})
            else:
                out.append(self.images[index].rpc())
                index += 1
        return out

    def item_content(self, file_ids: list[str]) -> str | list[dict[str, Any]]:
        if not self.images and len(self.parts) <= 1 and self.text:
            return self.text
        if not self.images and not self.parts:
            return self.text
        out: list[dict[str, Any]] = []
        image_at = 0
        for part in self.parts:
            if part.get("type") == "input_image":
                out.append({"type": "input_image", "file_id": file_ids[image_at]})
                image_at += 1
            else:
                out.append(dict(part))
        if not out and self.text:
            return self.text
        return out


def _mimes(settings: Settings) -> frozenset[str]:
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
    if mime not in _mimes(settings):
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
            url = item.get("image_url")
            if not isinstance(url, str) or not url.strip():
                raise ApiError(
                    "invalid_request",
                    "input_image needs image_url",
                    code="invalid_request",
                )
            image = _decode_data_url(url, settings=settings)
            parts.append(
                {
                    "type": "input_image",
                    "mime": image.mime,
                    "data": image.data,
                }
            )
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
        ImagePart(mime=str(part["mime"]), data=part["data"])
        for part in parts
        if part.get("type") == "input_image" and isinstance(part.get("data"), bytes)
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
