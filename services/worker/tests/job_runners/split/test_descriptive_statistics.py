# SPDX-License-Identifier: Apache-2.0
# Copyright 2023 The HuggingFace Authors.
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from http import HTTPStatus
from typing import Optional, Union

import numpy as np
import pandas as pd
import polars as pl
import pytest
from datasets import ClassLabel, Dataset
from libcommon.dtos import Priority
from libcommon.resources import CacheMongoResource, QueueMongoResource
from libcommon.simple_cache import upsert_response
from libcommon.storage import StrPath

from worker.config import AppConfig
from worker.job_runners.config.parquet_and_info import ConfigParquetAndInfoJobRunner
from worker.job_runners.split.descriptive_statistics import (
    DECIMALS,
    MAX_NUM_STRING_LABELS,
    NO_LABEL_VALUE,
    ColumnType,
    SplitDescriptiveStatisticsJobRunner,
    compute_bool_statistics,
    compute_class_label_statistics,
    compute_numerical_statistics,
    compute_string_statistics,
    generate_bins,
)
from worker.resources import LibrariesResource

from ...fixtures.hub import HubDatasetTest
from ..utils import REVISION_NAME

GetJobRunner = Callable[[str, str, str, AppConfig], SplitDescriptiveStatisticsJobRunner]

GetParquetAndInfoJobRunner = Callable[[str, str, AppConfig], ConfigParquetAndInfoJobRunner]

N_BINS = int(os.getenv("DESCRIPTIVE_STATISTICS_HISTOGRAM_NUM_BINS", 10))


@pytest.mark.parametrize(
    "min_value,max_value,column_type,expected_bins",
    [
        (0, 1, ColumnType.INT, [0, 1, 1]),
        (0, 12, ColumnType.INT, [0, 2, 4, 6, 8, 10, 12, 12]),
        (-10, 15, ColumnType.INT, [-10, -7, -4, -1, 2, 5, 8, 11, 14, 15]),
        (0, 9, ColumnType.INT, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9]),
        (0, 10, ColumnType.INT, [0, 2, 4, 6, 8, 10, 10]),
        (0.0, 10.0, ColumnType.FLOAT, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]),
        (0.0, 0.1, ColumnType.FLOAT, [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1]),
        (0, 0, ColumnType.INT, [0, 0]),
        (0.0, 0.0, ColumnType.INT, [0.0, 0.0]),
        (-0.5, 0.5, ColumnType.FLOAT, [-0.5, -0.4, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5]),
        (-100.0, 100.0, ColumnType.FLOAT, [-100.0, -80.0, -60.0, -40.0, -20.0, 0.0, 20.0, 40.0, 60.0, 80.0, 100.0]),
    ],
)
def test_generate_bins(
    min_value: Union[int, float],
    max_value: Union[int, float],
    column_type: ColumnType,
    expected_bins: list[Union[int, float]],
) -> None:
    bins = generate_bins(
        min_value=min_value, max_value=max_value, column_name="dummy", column_type=column_type, n_bins=N_BINS
    )
    assert 2 <= len(bins) <= N_BINS + 1
    if column_type is column_type.FLOAT:
        assert pytest.approx(bins) == expected_bins
    else:
        assert bins == expected_bins


@pytest.fixture
def get_job_runner(
    statistics_cache_directory: StrPath,
    cache_mongo_resource: CacheMongoResource,
    queue_mongo_resource: QueueMongoResource,
) -> GetJobRunner:
    def _get_job_runner(
        dataset: str,
        config: str,
        split: str,
        app_config: AppConfig,
    ) -> SplitDescriptiveStatisticsJobRunner:
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

        return SplitDescriptiveStatisticsJobRunner(
            job_info={
                "type": SplitDescriptiveStatisticsJobRunner.get_job_type(),
                "params": {
                    "dataset": dataset,
                    "revision": REVISION_NAME,
                    "config": config,
                    "split": split,
                },
                "job_id": "job_id",
                "priority": Priority.NORMAL,
                "difficulty": 100,
            },
            app_config=app_config,
            statistics_cache_directory=statistics_cache_directory,
        )

    return _get_job_runner


@pytest.fixture
def get_parquet_and_info_job_runner(
    libraries_resource: LibrariesResource,
    cache_mongo_resource: CacheMongoResource,
    queue_mongo_resource: QueueMongoResource,
) -> GetParquetAndInfoJobRunner:
    def _get_job_runner(
        dataset: str,
        config: str,
        app_config: AppConfig,
    ) -> ConfigParquetAndInfoJobRunner:
        upsert_response(
            kind="dataset-config-names",
            dataset=dataset,
            dataset_git_revision=REVISION_NAME,
            content={"config_names": [{"dataset": dataset, "config": config}]},
            http_status=HTTPStatus.OK,
        )

        return ConfigParquetAndInfoJobRunner(
            job_info={
                "type": ConfigParquetAndInfoJobRunner.get_job_type(),
                "params": {
                    "dataset": dataset,
                    "revision": REVISION_NAME,
                    "config": config,
                    "split": None,
                },
                "job_id": "job_id",
                "priority": Priority.NORMAL,
                "difficulty": 100,
            },
            app_config=app_config,
            hf_datasets_cache=libraries_resource.hf_datasets_cache,
        )

    return _get_job_runner


def count_expected_statistics_for_numerical_column(
    column: pd.Series,  # type: ignore
    dtype: ColumnType,
) -> dict:  # type: ignore
    minimum, maximum, mean, median, std = (
        column.min(),
        column.max(),
        column.mean(),
        column.median(),
        column.std(),
    )
    n_samples = column.shape[0]
    nan_count = column.isna().sum()
    if dtype is ColumnType.FLOAT:
        if minimum == maximum:
            hist, bin_edges = np.array([column[~column.isna()].count()]), np.array([minimum, maximum])
        else:
            hist, bin_edges = np.histogram(column[~column.isna()], bins=N_BINS)
        bin_edges = bin_edges.astype(float).round(DECIMALS).tolist()
    else:
        bins = generate_bins(minimum, maximum, column_name="dummy", column_type=dtype, n_bins=N_BINS)
        hist, bin_edges = np.histogram(column[~column.isna()], bins)
        bin_edges = bin_edges.astype(int).tolist()
    hist = hist.astype(int).tolist()
    if dtype is ColumnType.FLOAT:
        minimum = minimum.astype(float).round(DECIMALS).item()
        maximum = maximum.astype(float).round(DECIMALS).item()
        mean = mean.astype(float).round(DECIMALS).item()  # type: ignore
        median = median.astype(float).round(DECIMALS).item()  # type: ignore
        std = std.astype(float).round(DECIMALS).item()  # type: ignore
    else:
        mean, median, std = list(np.round([mean, median, std], DECIMALS))
    return {
        "nan_count": nan_count,
        "nan_proportion": np.round(nan_count / n_samples, DECIMALS).item() if nan_count else 0.0,
        "min": minimum,
        "max": maximum,
        "mean": mean,
        "median": median,
        "std": std,
        "histogram": {
            "hist": hist,
            "bin_edges": bin_edges,
        },
    }


def count_expected_statistics_for_categorical_column(
    column: pd.Series,  # type: ignore
    class_label_feature: ClassLabel,
) -> dict:  # type: ignore
    n_samples = column.shape[0]
    nan_count = column.isna().sum()
    value_counts = column.value_counts(dropna=True).to_dict()
    no_label_count = int(value_counts.pop(NO_LABEL_VALUE, 0))
    num_classes = len(class_label_feature.names)
    frequencies = {
        class_label_feature.int2str(int(class_id)): value_counts.get(class_id, 0) for class_id in range(num_classes)
    }
    return {
        "nan_count": nan_count,
        "nan_proportion": np.round(nan_count / n_samples, DECIMALS).item() if nan_count else 0.0,
        "no_label_count": no_label_count,
        "no_label_proportion": np.round(no_label_count / n_samples, DECIMALS).item() if no_label_count else 0.0,
        "n_unique": num_classes,
        "frequencies": frequencies,
    }


def count_expected_statistics_for_string_column(column: pd.Series) -> dict:  # type: ignore
    n_samples = column.shape[0]
    nan_count = column.isna().sum()
    value_counts = column.value_counts(dropna=True).to_dict()
    n_unique = len(value_counts)
    if n_unique <= MAX_NUM_STRING_LABELS:
        return {
            "nan_count": nan_count,
            "nan_proportion": np.round(nan_count / n_samples, DECIMALS).item() if nan_count else 0.0,
            "no_label_count": 0,
            "no_label_proportion": 0.0,
            "n_unique": n_unique,
            "frequencies": value_counts,
        }

    lengths_column = column.map(lambda x: len(x) if x is not None else None)
    return count_expected_statistics_for_numerical_column(lengths_column, dtype=ColumnType.INT)


def count_expected_statistics_for_bool_column(column: pd.Series) -> dict:  # type: ignore
    n_samples = column.shape[0]
    nan_count = column.isna().sum()
    value_counts = column.value_counts(dropna=True).to_dict()
    return {
        "nan_count": nan_count,
        "nan_proportion": np.round(nan_count / n_samples, DECIMALS).item() if nan_count else 0.0,
        "frequencies": {str(key): freq for key, freq in value_counts.items()},
    }


@pytest.fixture
def descriptive_statistics_expected(datasets: Mapping[str, Dataset]) -> dict:  # type: ignore
    ds = datasets["descriptive_statistics"]
    df = ds.to_pandas()
    expected_statistics = {}
    for column_name in df.columns:
        column_type = ColumnType(column_name.split("__")[0])
        if column_type is ColumnType.STRING_LABEL:
            column_stats = count_expected_statistics_for_string_column(df[column_name])
        elif column_type in [ColumnType.FLOAT, ColumnType.INT]:
            column_stats = count_expected_statistics_for_numerical_column(df[column_name], dtype=column_type)
            if sum(column_stats["histogram"]["hist"]) != df.shape[0] - column_stats["nan_count"]:
                raise ValueError(column_name, column_stats)
        elif column_type is ColumnType.CLASS_LABEL:
            column_stats = count_expected_statistics_for_categorical_column(
                df[column_name], class_label_feature=ds.features[column_name]
            )
        elif column_type is ColumnType.BOOL:
            column_stats = count_expected_statistics_for_bool_column(df[column_name])
        expected_statistics[column_name] = {
            "column_name": column_name,
            "column_type": column_type,
            "column_statistics": column_stats,
        }
    return {"num_examples": df.shape[0], "statistics": expected_statistics}


@pytest.fixture
def descriptive_statistics_string_text_expected(datasets: Mapping[str, Dataset]) -> dict:  # type: ignore
    ds = datasets["descriptive_statistics_string_text"]
    df = ds.to_pandas()
    expected_statistics = {}
    for column_name in df.columns:
        column_stats = count_expected_statistics_for_string_column(df[column_name])
        if sum(column_stats["histogram"]["hist"]) != df.shape[0] - column_stats["nan_count"]:
            raise ValueError(column_name, column_stats)
        expected_statistics[column_name] = {
            "column_name": column_name,
            "column_type": ColumnType.STRING_TEXT,
            "column_statistics": column_stats,
        }
    return {"num_examples": df.shape[0], "statistics": expected_statistics}


@pytest.mark.parametrize(
    "column_name",
    [
        "int__column",
        "int__nan_column",
        "int__negative_column",
        "int__cross_zero_column",
        "int__large_values_column",
        "int__only_one_value_column",
        "int__only_one_value_nan_column",
        "float__column",
        "float__nan_column",
        "float__negative_column",
        "float__cross_zero_column",
        "float__large_values_column",
        "float__only_one_value_column",
        "float__only_one_value_nan_column",
    ],
)
def test_numerical_statistics(
    column_name: str,
    descriptive_statistics_expected: dict,  # type: ignore
    datasets: Mapping[str, Dataset],
) -> None:
    expected = descriptive_statistics_expected["statistics"][column_name]["column_statistics"]
    data = datasets["descriptive_statistics"].to_dict()
    computed = compute_numerical_statistics(
        df=pl.from_dict(data),
        column_name=column_name,
        n_bins=N_BINS,
        n_samples=len(data[column_name]),
        column_type=ColumnType.INT if column_name.startswith("int__") else ColumnType.FLOAT,
    )
    expected_hist, computed_hist = expected.pop("histogram"), computed.pop("histogram")  # type: ignore
    assert computed_hist["hist"] == expected_hist["hist"]
    assert pytest.approx(computed_hist["bin_edges"]) == expected_hist["bin_edges"]
    assert pytest.approx(computed) == expected
    assert computed["nan_count"] == expected["nan_count"]
    if column_name.startswith("int__"):
        assert computed["min"] == expected["min"]
        assert computed["max"] == expected["max"]


@pytest.mark.parametrize(
    "column_name",
    [
        "string_text__column",
        "string_text__nan_column",
        "string_text__large_string_column",
        "string_text__large_string_nan_column",
        "string_label__column",
        "string_label__nan_column",
    ],
)
def test_string_statistics(
    column_name: str,
    descriptive_statistics_expected: dict,  # type: ignore
    descriptive_statistics_string_text_expected: dict,  # type: ignore
    datasets: Mapping[str, Dataset],
) -> None:
    if column_name.startswith("string_text__"):
        expected = descriptive_statistics_string_text_expected["statistics"][column_name]["column_statistics"]
        data = datasets["descriptive_statistics_string_text"].to_dict()
    else:
        expected = descriptive_statistics_expected["statistics"][column_name]["column_statistics"]
        data = datasets["descriptive_statistics"].to_dict()
    computed = compute_string_statistics(
        df=pl.from_dict(data),
        column_name=column_name,
        n_bins=N_BINS,
        n_samples=len(data[column_name]),
        dtype="large_string" if "_large_string_" in column_name else None,
    )
    if column_name.startswith("string_text__"):
        expected_hist, computed_hist = expected.pop("histogram"), computed.pop("histogram")  # type: ignore
        assert expected_hist["hist"] == computed_hist["hist"]
        assert expected_hist["bin_edges"] == pytest.approx(computed_hist["bin_edges"])
        assert expected == pytest.approx(computed)
    else:
        assert expected == computed


@pytest.mark.parametrize(
    "column_name",
    [
        "class_label__column",
        "class_label__nan_column",
        "class_label__less_classes_column",
        "class_label__string_column",
        "class_label__string_nan_column",
    ],
)
def test_class_label_statistics(
    column_name: str,
    descriptive_statistics_expected: dict,  # type: ignore
    datasets: Mapping[str, Dataset],
) -> None:
    expected = descriptive_statistics_expected["statistics"][column_name]["column_statistics"]
    class_label_feature = datasets["descriptive_statistics"].features[column_name]
    data = datasets["descriptive_statistics"].to_dict()
    computed = compute_class_label_statistics(
        df=pl.from_dict(data),
        column_name=column_name,
        n_samples=len(data[column_name]),
        class_label_feature=class_label_feature,
    )
    assert expected == computed


@pytest.mark.parametrize(
    "column_name",
    [
        "bool__column",
        "bool__nan_column",
    ],
)
def test_bool_statistics(
    column_name: str,
    descriptive_statistics_expected: dict,  # type: ignore
    datasets: Mapping[str, Dataset],
) -> None:
    expected = descriptive_statistics_expected["statistics"][column_name]["column_statistics"]
    data = datasets["descriptive_statistics"].to_dict()
    computed = compute_bool_statistics(
        df=pl.from_dict(data),
        column_name=column_name,
        n_samples=len(data[column_name]),
    )
    assert computed == expected


@pytest.mark.parametrize(
    "hub_dataset_name,expected_error_code",
    [
        ("descriptive_statistics", None),
        ("descriptive_statistics_partial", None),
        ("descriptive_statistics_string_text", None),
        ("gated", None),
        ("audio", "NoSupportedFeaturesError"),
        ("big", "SplitWithTooBigParquetError"),
    ],
)
def test_compute(
    app_config: AppConfig,
    get_job_runner: GetJobRunner,
    get_parquet_and_info_job_runner: GetParquetAndInfoJobRunner,
    hub_responses_descriptive_statistics: HubDatasetTest,
    hub_responses_descriptive_statistics_string_text: HubDatasetTest,
    hub_responses_gated_descriptive_statistics: HubDatasetTest,
    hub_responses_audio: HubDatasetTest,
    hub_responses_big: HubDatasetTest,
    hub_dataset_name: str,
    expected_error_code: Optional[str],
    descriptive_statistics_expected: dict,  # type: ignore
    descriptive_statistics_string_text_expected: dict,  # type: ignore
) -> None:
    hub_datasets = {
        "descriptive_statistics": hub_responses_descriptive_statistics,
        "descriptive_statistics_partial": hub_responses_descriptive_statistics,
        "descriptive_statistics_string_text": hub_responses_descriptive_statistics_string_text,
        "gated": hub_responses_gated_descriptive_statistics,
        "audio": hub_responses_audio,
        "big": hub_responses_big,
    }
    expected = {
        "descriptive_statistics": descriptive_statistics_expected,
        "descriptive_statistics_partial": descriptive_statistics_expected,
        "gated": descriptive_statistics_expected,
        "descriptive_statistics_string_text": descriptive_statistics_string_text_expected,
    }
    dataset = hub_datasets[hub_dataset_name]["name"]
    splits_response = hub_datasets[hub_dataset_name]["splits_response"]
    config, split = splits_response["splits"][0]["config"], splits_response["splits"][0]["split"]
    expected_response = expected.get(hub_dataset_name)

    partial = hub_dataset_name.endswith("_partial")
    app_config = (
        app_config
        if not partial
        else replace(
            app_config,
            parquet_and_info=replace(
                app_config.parquet_and_info, max_dataset_size_bytes=1, max_row_group_byte_size_for_copy=1
            ),
        )
    )

    # computing and pushing real parquet files because we need them for stats computation
    parquet_job_runner = get_parquet_and_info_job_runner(dataset, config, app_config)
    parquet_and_info_response = parquet_job_runner.compute()
    assert parquet_and_info_response
    assert parquet_and_info_response.content["partial"] is partial

    upsert_response(
        "config-parquet-and-info",
        dataset=dataset,
        dataset_git_revision=REVISION_NAME,
        config=config,
        http_status=HTTPStatus.OK,
        content=parquet_and_info_response.content,
    )

    job_runner = get_job_runner(dataset, config, split, app_config)
    job_runner.pre_compute()
    if expected_error_code:
        with pytest.raises(Exception) as e:
            job_runner.compute()
        assert e.typename == expected_error_code
    else:
        response = job_runner.compute()
        assert sorted(response.content.keys()) == ["num_examples", "statistics"]
        assert response.content["num_examples"] == expected_response["num_examples"]  # type: ignore
        response = response.content["statistics"]
        expected = expected_response["statistics"]  # type: ignore
        assert len(response) == len(expected)  # type: ignore
        assert set([column_response["column_name"] for column_response in response]) == set(  # type: ignore
            expected
        )  # assert returned feature names are as expected

        for column_response in response:  # type: ignore
            expected_column_response = expected[column_response["column_name"]]
            assert column_response["column_name"] == expected_column_response["column_name"]
            assert column_response["column_type"] == expected_column_response["column_type"]
            column_response_stats = column_response["column_statistics"]
            expected_column_response_stats = expected_column_response["column_statistics"]
            assert column_response_stats.keys() == expected_column_response_stats.keys()
            if column_response["column_type"] in [ColumnType.FLOAT, ColumnType.INT, ColumnType.STRING_TEXT]:
                hist, expected_hist = (
                    column_response_stats.pop("histogram"),
                    expected_column_response_stats.pop("histogram"),
                )
                assert hist["hist"] == expected_hist["hist"]
                assert pytest.approx(hist["bin_edges"]) == expected_hist["bin_edges"]
                assert column_response_stats["nan_count"] == expected_column_response_stats["nan_count"]
                assert pytest.approx(column_response_stats) == expected_column_response_stats
                if column_response["column_type"] is ColumnType.INT:
                    assert column_response_stats["min"] == expected_column_response_stats["min"]
                    assert column_response_stats["max"] == expected_column_response_stats["max"]
            elif column_response["column_type"] in [ColumnType.STRING_LABEL, ColumnType.CLASS_LABEL]:
                assert column_response_stats == expected_column_response_stats
            elif column_response["column_type"] is ColumnType.BOOL:
                assert pytest.approx(
                    column_response_stats.pop("nan_proportion")
                ) == expected_column_response_stats.pop("nan_proportion")
                assert column_response_stats == expected_column_response_stats
            else:
                raise ValueError("Incorrect data type")
