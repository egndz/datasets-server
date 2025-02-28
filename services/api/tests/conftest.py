# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

from collections.abc import Iterator

import pytest
from libapi.config import UvicornConfig
from libcommon.processing_graph import processing_graph
from libcommon.queue import _clean_queue_database
from libcommon.resources import CacheMongoResource, QueueMongoResource
from libcommon.simple_cache import _clean_cache_database
from pytest import MonkeyPatch, fixture

from api.config import AppConfig, EndpointConfig
from api.routes.endpoint import EndpointsDefinition, StepsByInputTypeAndEndpoint


# see https://github.com/pytest-dev/pytest/issues/363#issuecomment-406536200
@fixture(scope="session")
def monkeypatch_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MonkeyPatch]:
    monkeypatch_session = MonkeyPatch()
    assets_root = str(tmp_path_factory.mktemp("assets_root"))
    monkeypatch_session.setenv("CACHED_ASSETS_STORAGE_ROOT", assets_root)
    monkeypatch_session.setenv("ASSETS_STORAGE_ROOT", assets_root)
    monkeypatch_session.setenv("CACHE_MONGO_DATABASE", "datasets_server_cache_test")
    monkeypatch_session.setenv("QUEUE_MONGO_DATABASE", "datasets_server_queue_test")
    hostname = "localhost"
    port = "8888"
    monkeypatch_session.setenv("API_HF_TIMEOUT_SECONDS", "10")
    monkeypatch_session.setenv("API_UVICORN_HOSTNAME", hostname)
    monkeypatch_session.setenv("API_UVICORN_PORT", port)
    monkeypatch_session.setenv("COMMON_HF_ENDPOINT", f"http://{hostname}:{port}")
    yield monkeypatch_session
    monkeypatch_session.undo()


@fixture(scope="session")
def app_config(monkeypatch_session: MonkeyPatch) -> AppConfig:
    app_config = AppConfig.from_env()
    if "test" not in app_config.cache.mongo_database or "test" not in app_config.queue.mongo_database:
        raise ValueError("Test must be launched on a test mongo database")
    return app_config


@fixture(scope="session")
def endpoint_config(monkeypatch_session: MonkeyPatch) -> EndpointConfig:
    return EndpointConfig(
        processing_step_names_by_input_type_and_endpoint={
            "/splits": {
                "dataset": ["dataset-split-names"],
                "config": ["config-split-names-from-streaming"],
            },
            "/first-rows": {"split": ["split-first-rows-from-streaming"]},
            "/parquet": {"config": ["config-parquet"]},
        }
    )


@fixture(scope="session")
def endpoint_definition(endpoint_config: EndpointConfig) -> StepsByInputTypeAndEndpoint:
    return EndpointsDefinition(processing_graph, endpoint_config=endpoint_config).steps_by_input_type_and_endpoint


@fixture(scope="session")
def first_dataset_endpoint(endpoint_definition: StepsByInputTypeAndEndpoint) -> str:
    return next(
        endpoint
        for endpoint, input_types in endpoint_definition.items()
        if next((endpoint for input_type, _ in input_types.items() if input_type == "dataset"), None)
    )


@fixture(scope="session")
def first_config_endpoint(endpoint_definition: StepsByInputTypeAndEndpoint) -> str:
    return next(
        endpoint
        for endpoint, input_types in endpoint_definition.items()
        if next((endpoint for input_type, _ in input_types.items() if input_type == "config"), None)
    )


@fixture(scope="session")
def first_split_endpoint(endpoint_definition: StepsByInputTypeAndEndpoint) -> str:
    return next(
        endpoint
        for endpoint, input_types in endpoint_definition.items()
        if next((endpoint for input_type, _ in input_types.items() if input_type == "split"), None)
    )


@fixture(autouse=True)
def cache_mongo_resource(app_config: AppConfig) -> Iterator[CacheMongoResource]:
    with CacheMongoResource(database=app_config.cache.mongo_database, host=app_config.cache.mongo_url) as resource:
        yield resource
        _clean_cache_database()


@fixture(autouse=True)
def queue_mongo_resource(app_config: AppConfig) -> Iterator[QueueMongoResource]:
    with QueueMongoResource(database=app_config.queue.mongo_database, host=app_config.queue.mongo_url) as resource:
        yield resource
        _clean_queue_database()


@fixture(scope="session")
def uvicorn_config(monkeypatch_session: MonkeyPatch) -> UvicornConfig:
    return UvicornConfig.from_env()


@fixture(scope="session")
def httpserver_listen_address(uvicorn_config: UvicornConfig) -> tuple[str, int]:
    return (uvicorn_config.hostname, uvicorn_config.port)


@fixture(scope="session")
def hf_endpoint(app_config: AppConfig) -> str:
    return app_config.common.hf_endpoint


@fixture(scope="session")
def hf_auth_path(app_config: AppConfig) -> str:
    return app_config.api.hf_auth_path
