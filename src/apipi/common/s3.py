"""S3 client construction shared by the object store and the image store."""

from apipi.config import ConfigError, Settings


def s3_addressing(settings: Settings) -> str:
    if settings.s3_addressing == "path":
        return "path"
    return "virtual"


def s3_client_kwargs(settings: Settings) -> dict[str, object]:
    style = s3_addressing(settings)
    config_kwargs: dict[str, object] = {
        "signature_version": "s3v4",
        "s3": {
            "addressing_style": style,
            "payload_signing_enabled": False,
        },
        "request_checksum_calculation": "when_required",
        "response_checksum_validation": "when_required",
    }
    kwargs: dict[str, object] = {
        "service_name": "s3",
        "region_name": settings.s3_region,
        "config_kwargs": config_kwargs,
    }
    if settings.s3_endpoint:
        kwargs["endpoint_url"] = settings.s3_endpoint
    return kwargs


def make_s3_client(
    settings: Settings,
    *,
    missing: str,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    profile_name: str | None = None,
) -> object:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ConfigError(missing) from exc
    kwargs = s3_client_kwargs(settings)
    config_kwargs = kwargs.pop("config_kwargs")
    if not isinstance(config_kwargs, dict):
        config_kwargs = {}
    try:
        config = Config(**config_kwargs)
    except TypeError:
        config = Config(s3=config_kwargs.get("s3", {"addressing_style": "path"}))
    client_kwargs: dict[str, object] = {
        "region_name": kwargs.get("region_name"),
        "config": config,
    }
    endpoint = kwargs.get("endpoint_url")
    if endpoint is not None:
        client_kwargs["endpoint_url"] = endpoint
    if aws_access_key_id is not None:
        client_kwargs["aws_access_key_id"] = aws_access_key_id
    if aws_secret_access_key is not None:
        client_kwargs["aws_secret_access_key"] = aws_secret_access_key
    if profile_name:
        return boto3.Session(profile_name=profile_name).client("s3", **client_kwargs)
    return boto3.client("s3", **client_kwargs)
