# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

import logging
from http import HTTPStatus

from libcommon.dtos import SplitHubFile
from libcommon.exceptions import PreviousStepFormatError
from libcommon.simple_cache import (
    CacheEntryDoesNotExistError,
    get_previous_step_or_raise,
    get_response,
)

from worker.dtos import (
    ConfigParquetResponse,
    DatasetParquetResponse,
    JobResult,
    PreviousJob,
)
from worker.job_runners.dataset.dataset_job_runner import DatasetJobRunner


def compute_parquet_response(dataset: str) -> tuple[DatasetParquetResponse, float]:
    """
    Get the response of dataset-parquet for one specific dataset on huggingface.co.
    Args:
        dataset (`str`):
            A namespace (user or an organization) and a repo name separated
            by a `/`.
    Returns:
        `DatasetParquetResponse`: An object with the parquet_response (list of parquet files).
    Raises the following errors:
        - [`libcommon.simple_cache.CachedArtifactError`]
          If the previous step gave an error.
        - [`libcommon.exceptions.PreviousStepFormatError`]
            If the content of the previous step has not the expected format
    """
    logging.info(f"get parquet files for dataset={dataset}")

    config_names_best_response = get_previous_step_or_raise(kinds=["dataset-config-names"], dataset=dataset)
    content = config_names_best_response.response["content"]
    if "config_names" not in content:
        raise PreviousStepFormatError("Previous step did not return the expected content: 'config_names'.")

    try:
        parquet_files: list[SplitHubFile] = []
        total = 0
        pending = []
        failed = []
        partial = False
        for config_item in content["config_names"]:
            config = config_item["config"]
            total += 1
            try:
                response = get_response(kind="config-parquet", dataset=dataset, config=config)
            except CacheEntryDoesNotExistError:
                logging.debug("No response found in previous step for this dataset: 'config-parquet' endpoint.")
                pending.append(
                    PreviousJob(
                        {
                            "kind": "config-parquet",
                            "dataset": dataset,
                            "config": config,
                            "split": None,
                        }
                    )
                )
                continue
            if response["http_status"] != HTTPStatus.OK:
                logging.debug(f"Previous step gave an error: {response['http_status']}.")
                failed.append(
                    PreviousJob(
                        {
                            "kind": "config-parquet",
                            "dataset": dataset,
                            "config": config,
                            "split": None,
                        }
                    )
                )
                continue
            config_parquet_content = ConfigParquetResponse(
                parquet_files=response["content"]["parquet_files"],
                partial=response["content"]["partial"],
                features=None,  # we can keep it None since we don't pass it to DatasetParquetResponse anyway
            )
            parquet_files.extend(config_parquet_content["parquet_files"])
            partial = partial or config_parquet_content["partial"]
    except Exception as e:
        raise PreviousStepFormatError("Previous step did not return the expected content.", e) from e

    progress = (total - len(pending)) / total if total else 1.0

    return (
        DatasetParquetResponse(parquet_files=parquet_files, pending=pending, failed=failed, partial=partial),
        progress,
    )


class DatasetParquetJobRunner(DatasetJobRunner):
    @staticmethod
    def get_job_type() -> str:
        return "dataset-parquet"

    def compute(self) -> JobResult:
        response_content, progress = compute_parquet_response(dataset=self.dataset)
        return JobResult(response_content, progress=progress)
