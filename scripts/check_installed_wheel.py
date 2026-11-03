"""Check installed modules and bundled resources from an isolated wheel environment."""

from __future__ import annotations

import importlib
import json
import sys
from importlib import resources
from pathlib import Path


def main() -> None:
    prefix = Path(sys.prefix).resolve()
    for name in (
        "core",
        "core.app",
        "plugins.xiaoqing_chat",
        "plugins.xiaoqing_chat.main",
        "plugins.xiaoqing_chat.life",
        "plugins.xiaoqing_chat.life.proactive",
        "plugins.xiaoqing_chat.life.daily_life",
    ):
        module = importlib.import_module(name)
        if module.__file__ is None:
            raise RuntimeError(f"Installed module has no origin: {name}")
        origin = Path(module.__file__).resolve()
        if not origin.is_relative_to(prefix):
            raise RuntimeError(f"Import escaped installation environment: {name}: {origin}")

    assets = {
        "plugins.xiaoqing_chat"       : ("plugin.json",),
        "plugins.xiaoqing_chat.config": ("xiaoqing_config.json", "persona.md", "agent.md"),
        "plugins.xiaoqing_chat.media" : ("qq_face_builtin_catalog.json",),
        "plugins.dict"                : (
            "assets/manifest.json",
            "assets/astrodict_ce.txt",
            "assets/astrodict_ec.txt",
        ),
        "plugins.pendo.web"         : ("static/index.html", "static/favicon.svg"),
        "plugins.pendo.web.services": ("assets/demo_bundle.pendo.zip",),
    }
    checked = 0
    for package, names in assets.items():
        for name in names:
            content = resources.files(package).joinpath(name).read_bytes()
            if not content:
                raise RuntimeError(f"Empty packaged resource: {package}/{name}")
            if name.endswith(".json"):
                json.loads(content)
            checked += 1
    print(f"Installed wheel imports and {checked} bundled resources passed")


if __name__ == "__main__":
    main()
