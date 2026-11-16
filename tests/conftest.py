"""Shared fixtures: every simulator runs in-process on an ephemeral port; nothing touches the network."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import create_app
from sim import fake_anthropic, fake_ha, fake_ingress
from sim.server import ServerThread

INGRESS_PREFIX = "/api/hassio_ingress/simtoken"


@pytest.fixture
def ha_state():
    return fake_ha.HAState.default()


@pytest.fixture
def ha_server(ha_state):
    with ServerThread(fake_ha.create_app(ha_state)) as server:
        yield server


@pytest.fixture
def claude_sim():
    """The orchestrator's model in tests (D40: there is no local one): a fake Claude Messages API."""
    return fake_anthropic.AnthropicSim()


@pytest.fixture
def claude_server(claude_sim):
    with ServerThread(fake_anthropic.create_app(claude_sim)) as server:
        server.sim = claude_sim
        yield server


@pytest.fixture
def settings(tmp_path, ha_server, ha_state, claude_server, claude_sim):
    return Settings(
        data_dir=tmp_path,
        ha_url=ha_server.url,
        ha_token=ha_state.token,
        allowed_peers=["127.0.0.1"],
        jetson_token="jetson-sim",
        anthropic_api_key=claude_sim.api_key,
        anthropic_base_url=claude_server.url,
    )


@pytest.fixture
def app_server(settings):
    """The real app, served over HTTP, with its lifespan (HA connection, council, learner link) running."""
    app = create_app(settings)
    with ServerThread(app) as server:
        server.app = app
        yield server


@pytest.fixture
def ingress(app_server):
    """The app as the browser sees it inside Home Assistant: behind the ingress proxy, under a prefix."""
    with ServerThread(fake_ingress.create_app(app_server.url)) as server:
        server.base = server.url + INGRESS_PREFIX
        server.app_server = app_server
        yield server
