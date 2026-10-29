#!/usr/bin/env python3
"""Export OpenAPI specification from the FastAPI app to openapi.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure src/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from urja.api.app import app


def export_openapi() -> None:
    output_path = Path("openapi.json")
    spec = app.openapi()
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)
    print(f"Exported OpenAPI spec to {output_path.resolve()} ({len(spec['paths'])} endpoints)")


if __name__ == "__main__":
    export_openapi()
