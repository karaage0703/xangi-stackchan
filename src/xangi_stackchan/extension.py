"""xangi managed Extension entrypoint."""

import argparse
import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

from .app import build_parser, config_from_args, run_bridge
from .settings import (
    DEFAULT_CONFIG_PATH,
    RuntimeState,
    load_instance_dict,
    merge_config,
)
from .settings_server import start_settings_server


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required for managed Extension mode")
    return value


def _xangi_url(events_url: str) -> str:
    suffix = "/api/events/stream"
    return events_url[: -len(suffix)] if events_url.endswith(suffix) else events_url


def build_state(workspace: Path) -> RuntimeState:
    events_url = _required_env("XANGI_EXTENSION_EVENTS_URL")
    instance_id = os.environ.get("XANGI_EXTENSION_INSTANCE_ID", "default").strip()
    parser = build_parser()
    args = parser.parse_args(
        [
            "--xangi-url",
            _xangi_url(events_url),
            "--instance-id",
            instance_id,
            "--config",
            str(DEFAULT_CONFIG_PATH),
        ]
    )
    config_path = Path(args.config).expanduser()
    defaults = replace(
        config_from_args(args),
        speak_responses=False,
        completion_notifications=True,
        lcd_mic_voice=False,
        firmware_head_pet_sound=False,
    )
    config = merge_config(defaults, load_instance_dict(config_path, instance_id))
    return RuntimeState(config, config_path, instance_id=instance_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="xangi-stackchan-extension")
    parser.add_argument("action", choices=["serve"])
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args(argv)

    auth_token = _required_env("XANGI_EXTENSION_AUTH_TOKEN")
    workspace = Path(args.workspace).expanduser().resolve()
    state = build_state(workspace)
    server, port = start_settings_server(state, "127.0.0.1", 0, auth_token=auth_token)
    threading.Thread(target=run_bridge, args=(state,), daemon=True).start()
    ready = {
        "schemaVersion": 2,
        "event": "ready",
        "id": "xangi-stackchan",
        "baseUrl": f"http://127.0.0.1:{port}",
        "workspace": str(workspace),
        "pid": os.getpid(),
    }
    print(json.dumps(ready), flush=True)
    try:
        sys.stdin.buffer.read()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
