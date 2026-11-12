"""Shared fixtures: every simulator runs in-process on an ephemeral port; nothing touches the network."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import create_app
from sim import fake_ha, fake_ingress, fake_jetson, fake_llm
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
def llm_sim():
    return fake_llm.LLMSim()


@pytest.fixture
def llm_server(llm_sim):
    with ServerThread(fake_llm.create_app(llm_sim)) as server:
        yield server


@pytest.fixture
def jetson_sim():
    return fake_jetson.JetsonSim()


@pytest.fixture
def jetson_server(jetson_sim):
    with ServerThread(fake_jetson.create_app(jetson_sim)) as server:
        yield server


@pytest.fixture
def settings(tmp_path, ha_server, ha_state, llm_server, jetson_server):
    return Settings(
        data_dir=tmp_path,
        ha_url=ha_server.url,
        ha_token=ha_state.token,
        allowed_peers=["127.0.0.1"],
        jetson_llm_url=llm_server.url,
        jetson_metrics_url=jetson_server.url,
        jetson_token="jetson-sim",
    )


@pytest.fixture
def app_server(settings):
    """The real app, served over HTTP, with its lifespan (HA connection, monitors) running."""
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
