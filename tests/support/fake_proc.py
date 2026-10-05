class FakeProc:
    def __init__(self) -> None:
        self.alive = True
        self.image = None
        self.vm_id = None

    async def terminate(self) -> None:
        self.alive = False
