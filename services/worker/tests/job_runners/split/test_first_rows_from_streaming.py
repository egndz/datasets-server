# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import pytest
from datasets.packaged_modules import csv
from libcommon.dtos import Priority
from libcommon.exceptions import CustomError
from libcommon.resources import CacheMongoResource, QueueMongoResource
from libcommon.simple_cache import upsert_response
from libcommon.storage_client import StorageClient
from libcommon.utils import get_json_size

from worker.config import AppConfig
from worker.job_runners.split.first_rows_from_streaming import (
    SplitFirstRowsFromStreamingJobRunner,
)
from worker.resources import LibrariesResource

from ...constants import ASSETS_BASE_URL
from ...fixtures.hub import HubDatasetTest, get_default_config_split
from ..utils import REVISION_NAME

GetJobRunner = Callable[[str, str, str, AppConfig], SplitFirstRowsFromStreamingJobRunner]


@pytest.fixture
def get_job_runner(
    libraries_resource: LibrariesResource,
    cache_mongo_resource: CacheMongoResource,
    queue_mongo_resource: QueueMongoResource,
    tmp_path: Path,
) -> GetJobRunner:
    def _get_job_runner(
        dataset: str,
        config: str,
        split: str,
        app_config: AppConfig,
    ) -> SplitFirstRowsFromStreamingJobRunner:
        upsert_response(
            kind="dataset-config-names",
            dataset=dataset,
            dataset_git_revision=REVISION_NAME,
            content={"config_names": [{"dataset": dataset, "config": config}]},
            http_status=HTTPStatus.OK,
        )

        upsert_response(
            kind="config-split-names-from-streaming",
            dataset=dataset,
            dataset_git_revision=REVISION_NAME,
            config=config,
            content={"splits": [{"dataset": dataset, "config": config, "split": split}]},
            http_status=HTTPStatus.OK,
        )

        return SplitFirstRowsFromStreamingJobRunner(
            job_info={
                "type": SplitFirstRowsFromStreamingJobRunner.get_job_type(),
                "params": {
                    "dataset": dataset,
                    "revision": REVISION_NAME,
                    "config": config,
                    "split": split,
                },
                "job_id": "job_id",
                "priority": Priority.NORMAL,
                "difficulty": 50,
            },
            app_config=app_config,
            hf_datasets_cache=libraries_resource.hf_datasets_cache,
            storage_client=StorageClient(
                protocol="file",
                storage_root=str(tmp_path / "assets"),
                base_url=ASSETS_BASE_URL,
                overwrite=True,  # all the job runners will overwrite the files
            ),
        )

    return _get_job_runner


def test_compute(app_config: AppConfig, get_job_runner: GetJobRunner, hub_public_csv: str) -> None:
    dataset = hub_public_csv
    config, split = get_default_config_split()
    job_runner = get_job_runner(dataset, config, split, app_config)
    response = job_runner.compute()
    assert response
    content = response.content
    assert content
    assert content["features"][0]["feature_idx"] == 0
    assert content["features"][0]["name"] == "col_1"
    assert content["features"][0]["type"]["_type"] == "Value"
    assert content["features"][0]["type"]["dtype"] == "int64"  # <---|
    assert content["features"][1]["type"]["dtype"] == "int64"  # <---|- auto-detected by the datasets library
    assert content["features"][2]["type"]["dtype"] == "float64"  # <-|


@pytest.mark.parametrize(
    "name,use_token,exception_name,cause",
    [
        ("public", False, None, None),
        ("audio", False, None, None),
        ("image", False, None, None),
        ("images_list", False, None, None),
        ("jsonl", False, None, None),
        ("gated", True, None, None),
        ("private", True, None, None),
        # should we really test the following cases?
        # The assumption is that the dataset exists and is accessible with the token
        ("gated", False, "InfoError", "FileNotFoundError"),
        ("private", False, "InfoError", "FileNotFoundError"),
    ],
)
def test_number_rows(
    hub_responses_public: HubDatasetTest,
    hub_responses_audio: HubDatasetTest,
    hub_responses_image: HubDatasetTest,
    hub_responses_images_list: HubDatasetTest,
    hub_reponses_jsonl: HubDatasetTest,
    hub_responses_gated: HubDatasetTest,
    hub_responses_private: HubDatasetTest,
    hub_responses_empty: HubDatasetTest,
    hub_responses_does_not_exist_config: HubDatasetTest,
    hub_responses_does_not_exist_split: HubDatasetTest,
    get_job_runner: GetJobRunner,
    name: str,
    use_token: bool,
    exception_name: str,
    cause: str,
    app_config: AppConfig,
) -> None:
    # temporary patch to remove the effect of
    # https://github.com/huggingface/datasets/issues/4875#issuecomment-1280744233
    # note: it fixes the tests, but it does not fix the bug in the "real world"
    if hasattr(csv, "_patched_for_streaming") and csv._patched_for_streaming:
        csv._patched_for_streaming = False

    hub_datasets = {
        "public": hub_responses_public,
        "audio": hub_responses_audio,
        "image": hub_responses_image,
        "images_list": hub_responses_images_list,
        "jsonl": hub_reponses_jsonl,
        "gated": hub_responses_gated,
        "private": hub_responses_private,
        "empty": hub_responses_empty,
        "does_not_exist_config": hub_responses_does_not_exist_config,
        "does_not_exist_split": hub_responses_does_not_exist_split,
    }
    dataset = hub_datasets[name]["name"]
    expected_first_rows_response = hub_datasets[name]["first_rows_response"]
    config, split = get_default_config_split()
    job_runner = get_job_runner(
        dataset,
        config,
        split,
        app_config if use_token else replace(app_config, common=replace(app_config.common, hf_token=None)),
    )

    if exception_name is None:
        job_runner.validate()
        result = job_runner.compute().content
        assert result == expected_first_rows_response
    else:
        with pytest.raises(Exception) as exc_info:
            job_runner.validate()
            job_runner.compute()
        assert exc_info.typename == exception_name


@pytest.mark.parametrize(
    "name,rows_max_bytes,columns_max_number,error_code,truncated",
    [
        # not-truncated public response is 687 bytes
        ("public", 10, 1_000, "TooBigContentError", False),  # too small limit, even with truncation
        ("public", 1_000, 1_000, None, False),  # not truncated
        ("public", 1_000, 1, "TooManyColumnsError", False),  # too small columns limit
        # not-truncated big response is 5_885_989 bytes
        ("big", 10, 1_000, "TooBigContentError", False),  # too small limit, even with truncation
        ("big", 1_000, 1_000, None, True),  # truncated successfully
        ("big", 10_000_000, 1_000, None, False),  # not truncated
    ],
)
def test_from_streaming_truncation(
    hub_public_csv: str,
    hub_public_big: str,
    get_job_runner: GetJobRunner,
    app_config: AppConfig,
    name: str,
    rows_max_bytes: int,
    columns_max_number: int,
    error_code: str,
    truncated: bool,
) -> None:
    dataset = hub_public_csv if name == "public" else hub_public_big
    config, split = get_default_config_split()
    job_runner = get_job_runner(
        dataset,
        config,
        split,
        replace(
            app_config,
            common=replace(app_config.common, hf_token=None),
            first_rows=replace(
                app_config.first_rows,
                max_number=1_000_000,
                min_number=10,
                max_bytes=rows_max_bytes,
                min_cell_bytes=10,
                columns_max_number=columns_max_number,
            ),
        ),
    )

    if error_code:
        with pytest.raises(CustomError) as error_info:
            job_runner.compute()
        assert error_info.value.code == error_code
    else:
        response = job_runner.compute().content
        assert get_json_size(response) <= rows_max_bytes
        assert response["truncated"] == truncated
