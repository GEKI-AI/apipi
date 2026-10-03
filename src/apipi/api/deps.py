from fastapi import Request

from apipi.gateway.auth import identity_of, unauthorized


async def model_key(request: Request) -> str:
    identity = identity_of(request)
    if identity is None:
        unauthorized()
    bearer = getattr(request.state, "bearer", None)
    return await request.app.state.model_credentials.resolve(
        identity, bearer if isinstance(bearer, str) else None
    )
