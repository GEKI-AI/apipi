import shutil
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory

from apipi.store.migrate import alembic_config


class SqliteRevisions:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, Path] = {}
        script = ScriptDirectory.from_config(alembic_config("sqlite://"))
        self.order = [item.revision for item in reversed(list(script.walk_revisions()))]

    def copy_at(self, revision: str, path: Path) -> str:
        if revision not in self.files:
            target = self.root / f"{revision}.db"
            earlier = [
                item
                for item in self.order[: self.order.index(revision)]
                if item in self.files
            ]
            if earlier:
                shutil.copyfile(self.files[earlier[-1]], target)
            command.upgrade(alembic_config(f"sqlite:///{target}"), revision)
            self.files[revision] = target
        shutil.copyfile(self.files[revision], path)
        return f"sqlite:///{path}"
