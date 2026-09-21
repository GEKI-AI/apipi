from apipi.worker.pi.isolation.none import NoneIsolation


class ChatIsolation(NoneIsolation):
    name = "chat"
    warn_not_production = False
