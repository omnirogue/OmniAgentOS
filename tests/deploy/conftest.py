"""Shared specs for the deploy suite."""

from __future__ import annotations

import pytest

from omniagentos.deploy.contracts import AppSpec, ServerSpec


@pytest.fixture
def server() -> ServerSpec:
    return ServerSpec(
        host="203.0.113.10",
        ssh_user="root",
        ssh_key_ref="vault://ssh/vultr-demo",
        runtime="python",
        packages=("sqlite3",),
    )


@pytest.fixture
def node_server() -> ServerSpec:
    return ServerSpec(
        host="203.0.113.11",
        ssh_user="root",
        ssh_key_ref="vault://ssh/vultr-demo",
        runtime="node",
    )


@pytest.fixture
def app() -> AppSpec:
    return AppSpec(
        repo_url_or_local_path="https://github.com/example-org/demo-app.git",
        domain="demo.example.com",
        service_name="demo-app",
        listen_port=8099,
        build_cmd="python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
        start_cmd=".venv/bin/python -m app --port $PORT",
        env_ref="vault://env/demo-app",
    )
