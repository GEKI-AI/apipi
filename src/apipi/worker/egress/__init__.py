from collections.abc import Iterable

from apipi.common.netguard import split_allow_hosts
from apipi.config import Settings
from apipi.worker.egress.ca import WorkerCA, worker_ca
from apipi.worker.egress.dns import Upstream
from apipi.worker.egress.gateway import (
    GATEWAY_PORTS,
    EgressGateway,
    raise_nofile_limit,
    set_egress_metrics,
)
from apipi.worker.egress.intercept import (
    BodyFilter,
    BodyHook,
    EgressHooks,
    Reject,
    RequestHead,
    RequestHook,
    ResponseHead,
    ResponseHook,
    upstream_context,
)
from apipi.worker.egress.policy import EgressMode, EgressPolicy
from apipi.worker.egress.resolve import BLOCKED_EGRESS_CIDRS

__all__ = [
    "BLOCKED_EGRESS_CIDRS",
    "GATEWAY_PORTS",
    "BodyFilter",
    "BodyHook",
    "EgressGateway",
    "EgressHooks",
    "EgressMode",
    "EgressPolicy",
    "Reject",
    "RequestHead",
    "RequestHook",
    "ResponseHead",
    "ResponseHook",
    "WorkerCA",
    "raise_nofile_limit",
    "set_egress_metrics",
    "start_gateway",
    "upstream_context",
    "worker_ca",
]


async def start_gateway(
    settings: Settings,
    *,
    host: str,
    mode: EgressMode,
    allowed_hosts: Iterable[str] = (),
    session_id: str | None = None,
    intercept_hosts: Iterable[str] = (),
    hooks: EgressHooks | None = None,
    dns_upstreams: tuple[Upstream, ...] = (),
    freebind: bool = True,
) -> EgressGateway:
    policy = EgressPolicy.build(
        mode,
        allowed_hosts=allowed_hosts,
        private_hosts=split_allow_hosts(settings.microvm_egress_private_hosts),
        intercept_hosts=intercept_hosts,
    )
    gateway = EgressGateway(
        host=host,
        policy=policy,
        session_id=session_id,
        upstream_ca=settings.microvm_egress_upstream_ca,
        hooks=hooks,
        dns_upstreams=dns_upstreams,
        freebind=freebind,
    )
    await gateway.start()
    return gateway
