# Pendo Plugin Dedupe Notes

**Date:** 2026-03-26

## Done

- Removed plugin-side `default_view` from settings command/help because it was configurable but not consumed by runtime behavior.
- Centralized plugin settings help lines, settings summary formatting, and `on/off` toggle parsing in `plugins/pendo/utils/settings_utils.py`.
- Updated `plugins/pendo/commands/settings.py` and `plugins/pendo/main.py` to reuse the shared settings helpers.
- Extracted event reminder/message helper logic from `plugins/pendo/handlers/event.py` into `plugins/pendo/handlers/event_support.py`.
- Kept `note` and `task` creation flows separate as requested; no behavioral merge there.

## Verified

- `python -m py_compile` passes for the touched plugin files.
- `pytest tests/plugins/test_pendo_web_settings.py -q` passes.

## Remaining Low-Priority Cleanup

- `note` and `task` still have similar parse/create scaffolding, but remain intentionally separate.
- `EventHandler` is still large; the next reasonable split would be list/detail rendering helpers.
- Plugin help text outside settings is still centralized in `main.py`; if command modules continue to grow, that help map may deserve its own module.
