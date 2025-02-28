# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

from collections.abc import Iterator

from libcommon.processing_graph import processing_graph
from pytest import MonkeyPatch, fixture, mark
from starlette.testclient import TestClient

from admin.app import create_app
from admin.config import AppConfig


# see https://github.com/pytest-dev/pytest/issues/363#issuecomment-406536200
@fixture(scope="module")
def real_monkeypatch() -> Iterator[MonkeyPatch]:
    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("CACHE_MONGO_DATABASE", "datasets_server_cache_test")
    monkeypatch.setenv("QUEUE_MONGO_DATABASE", "datasets_server_queue_test")
    monkeypatch.setenv("COMMON_HF_ENDPOINT", "https://huggingface.co")
    monkeypatch.setenv("COMMON_HF_TOKEN", "")
    yield monkeypatch
    monkeypatch.undo()


@fixture(scope="module")
def real_client(real_monkeypatch: MonkeyPatch) -> TestClient:
    return TestClient(create_app())


@fixture(scope="module")
def real_app_config(real_monkeypatch: MonkeyPatch) -> AppConfig:
    app_config = AppConfig.from_env()
    if "test" not in app_config.cache.mongo_database or "test" not in app_config.queue.mongo_database:
        raise ValueError("Test must be launched on a test mongo database")
    if app_config.common.hf_endpoint != "https://huggingface.co":
        raise ValueError("Test must be launched on the production hub")
    return app_config


@mark.real_dataset
def test_force_refresh(
    real_app_config: AppConfig,
    real_client: TestClient,
) -> None:
    dataset = "glue"
    first_step = processing_graph.get_processing_steps(order="topological")[0]
    path = first_step.job_type
    response = real_client.request("post", f"/force-refresh/{path}?dataset={dataset}")
    assert response.status_code == 200, response.text
