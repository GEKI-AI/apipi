import sys
import zipfile
from pathlib import Path

from apipi import __version__


def main() -> None:
    dist = Path("dist")
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"expected one wheel and one sdist in dist/, got {wheels} {sdists}"
        )
    wheel = wheels[0]
    if __version__ not in wheel.name:
        raise SystemExit(f"wheel name {wheel.name} does not contain {__version__}")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        required = (
            "apipi/worker/pi/guest.sh",
            "apipi/worker/pi/images/build.sh",
            "apipi/worker/pi/images/default/image.env",
            "apipi/worker/pi/images/browser/image.env",
        )
        missing = [name for name in required if name not in names]
        if missing:
            raise SystemExit(f"wheel is missing {', '.join(missing)}")
        meta_name = next(
            name
            for name in names
            if name.endswith(".dist-info/METADATA") and name.startswith("geki_apipi-")
        )
        text = archive.read(meta_name).decode()
        if "Name: geki-apipi" not in text:
            raise SystemExit("METADATA missing Name: geki-apipi")
        if "Provides-Extra: s3" not in text:
            raise SystemExit("METADATA missing Provides-Extra: s3")
        entry_name = next(
            name
            for name in names
            if name.endswith(".dist-info/entry_points.txt")
            and name.startswith("geki_apipi-")
        )
        entries = archive.read(entry_name).decode()
        if "apipi = apipi.cli:main" not in entries:
            raise SystemExit("wheel missing apipi console script")
    print(wheel.name, file=sys.stderr)


if __name__ == "__main__":
    main()
