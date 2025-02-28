# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import pytest
from libcommon.dtos import Priority
from libcommon.exceptions import PreviousStepFormatError
from libcommon.resources import CacheMongoResource, QueueMongoResource
from libcommon.simple_cache import (
    CachedArtifactError,
    CachedArtifactNotFoundError,
    upsert_response,
)

from worker.config import AppConfig
from worker.job_runners.config.split_names_from_info import (
    ConfigSplitNamesFromInfoJobRunner,
)

from ..utils import REVISION_NAME

GetJobRunner = Callable[[str, str, AppConfig], ConfigSplitNamesFromInfoJobRunner]


@pytest.fixture
def get_job_runner(
    cache_mongo_resource: CacheMongoResource,
    queue_mongo_resource: QueueMongoResource,
) -> GetJobRunner:
    def _get_job_runner(
        dataset: str,
        config: str,
        app_config: AppConfig,
    ) -> ConfigSplitNamesFromInfoJobRunner:
        upsert_response(
            kind="dataset-config-names",
            dataset=dataset,
            dataset_git_revision=REVISION_NAME,
            content={"config_names": [{"dataset": dataset, "config": config}]},
            http_status=HTTPStatus.OK,
        )

        return ConfigSplitNamesFromInfoJobRunner(
            job_info={
                "type": ConfigSplitNamesFromInfoJobRunner.get_job_type(),
                "params": {
                    "dataset": dataset,
                    "revision": REVISION_NAME,
                    "config": config,
                    "split": None,
                },
                "job_id": "job_id",
                "priority": Priority.NORMAL,
                "difficulty": 50,
            },
            app_config=app_config,
        )

    return _get_job_runner


@pytest.mark.parametrize(
    "dataset,upstream_status,upstream_content,error_code,content",
    [
        (
            "ok",
            HTTPStatus.OK,
            {
                "dataset_info": {
                    "splits": {
                        "train": {"name": "train", "dataset_name": "ok"},
                        "validation": {"name": "validation", "dataset_name": "ok"},
                        "test": {"name": "test", "dataset_name": "ok"},
                    },
                }
            },
            None,
            {
                "splits": [
                    {"dataset": "ok", "config": "config_name", "split": "train"},
                    {"dataset": "ok", "config": "config_name", "split": "validation"},
                    {"dataset": "ok", "config": "config_name", "split": "test"},
                ]
            },
        ),
        (
            "upstream_fail",
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": "error"},
            CachedArtifactError.__name__,
            None,
        ),
        (
            "without_dataset_info",
            HTTPStatus.OK,
            {"some_column": "wrong_format"},
            PreviousStepFormatError.__name__,
            None,
        ),
        (
            "without_config_name",
            HTTPStatus.OK,
            {"dataset_info": "wrong_format"},
            PreviousStepFormatError.__name__,
            None,
        ),
        (
            "without_splits",
            HTTPStatus.OK,
            {"dataset_info": {"config_name": "wrong_format"}},
            PreviousStepFormatError.__name__,
            None,
        ),
    ],
)
def test_compute(
    app_config: AppConfig,
    get_job_runner: GetJobRunner,
    dataset: str,
    upstream_status: HTTPStatus,
    upstream_content: Any,
    error_code: str,
    content: Any,
) -> None:
    config = "config_name"
    upsert_response(
        kind="config-info",
        dataset=dataset,
        dataset_git_revision=REVISION_NAME,
        config=config,
        content=upstream_content,
        http_status=upstream_status,
    )
    job_runner = get_job_runner(dataset, config, app_config)

    if error_code:
        with pytest.raises(Exception) as e:
            job_runner.compute()
        assert e.typename == error_code
    else:
        assert job_runner.compute().content == content


def test_doesnotexist(app_config: AppConfig, get_job_runner: GetJobRunner) -> None:
    dataset = "non_existent"
    config = "non_existent"
    worker = get_job_runner(dataset, config, app_config)
    with pytest.raises(CachedArtifactNotFoundError):
        worker.compute()
