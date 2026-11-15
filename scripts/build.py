"""Build an install archive with no development files or private data."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent.parent
component = root / "custom_components/appliance_meter"
version = json.loads((component / "manifest.json").read_text())["version"]
destination = root / "dist" / f"device-proxy-{version}.zip"
destination.parent.mkdir(exist_ok=True)
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for file in sorted(component.rglob("*")):
        if file.is_file() and file.suffix in {".py", ".json", ".png", ".svg"}:
            archive.write(file, file.relative_to(root))
    archive.write(root / "README.md", "README.md")
    archive.write(root / "LICENSE", "LICENSE")
    archive.write(root / "docs/behavior.md", "docs/behavior.md")
print(destination)
