# SPDX-License-Identifier: Apache-2.0
# Copyright 2023 The HuggingFace Authors.

from http import HTTPStatus

import pytest

from libcommon.constants import DEFAULT_DIFFICULTY_MAX, DIFFICULTY_BONUS_BY_FAILED_RUNS
from libcommon.dtos import JobOutput, JobResult, Priority, Status
from libcommon.orchestrator import AfterJobPlan, finish_job, has_pending_ancestor_jobs, remove_dataset, set_revision
from libcommon.processing_graph import Artifact, ProcessingGraph
from libcommon.queue import JobDocument, Queue
from libcommon.resources import CacheMongoResource, QueueMongoResource
from libcommon.simple_cache import (
    CachedResponseDocument,
    get_response_metadata,
    has_some_cache,
    upsert_response_params,
)

from .utils import (
    ARTIFACT_CA_1,
    ARTIFACT_CA_2,
    ARTIFACT_DA,
    ARTIFACT_DB,
    ARTIFACT_DC,
    ARTIFACT_DD,
    ARTIFACT_DE,
    ARTIFACT_DG,
    ARTIFACT_DH,
    CONFIG_NAMES_CONTENT,
    DATASET_NAME,
    DIFFICULTY,
    JOB_RUNNER_VERSION,
    PROCESSING_GRAPH_FAN_IN_OUT,
    PROCESSING_GRAPH_GENEALOGY,
    PROCESSING_GRAPH_ONE_STEP,
    PROCESSING_GRAPH_PARALLEL,
    REVISION_NAME,
    STEP_CB,
    STEP_DA,
    STEP_DC,
    STEP_DD,
    artifact_id_to_job_info,
)


@pytest.fixture(autouse=True)
def queue_mongo_resource_autouse(queue_mongo_resource: QueueMongoResource) -> QueueMongoResource:
    return queue_mongo_resource


@pytest.fixture(autouse=True)
def cache_mongo_resource_autouse(cache_mongo_resource: CacheMongoResource) -> CacheMongoResource:
    return cache_mongo_resource


@pytest.mark.parametrize(
    "processing_graph,artifacts_to_create",
    [
        (PROCESSING_GRAPH_ONE_STEP, []),
        (PROCESSING_GRAPH_GENEALOGY, [ARTIFACT_DC]),
        (PROCESSING_GRAPH_FAN_IN_OUT, [ARTIFACT_CA_1, ARTIFACT_CA_2]),
        (PROCESSING_GRAPH_PARALLEL, [ARTIFACT_DG, ARTIFACT_DH]),
    ],
)
def test_after_job_plan(
    processing_graph: ProcessingGraph,
    artifacts_to_create: list[str],
) -> None:
    job_info = artifact_id_to_job_info(ARTIFACT_DA)
    # put the cache (to be able to get the config names - case PROCESSING_GRAPH_FAN_IN_OUT)
    upsert_response_params(
        # inputs
        kind=STEP_DA,
        job_params=job_info["params"],
        job_runner_version=JOB_RUNNER_VERSION,
        # output
        content=CONFIG_NAMES_CONTENT,
        http_status=HTTPStatus.OK,
        error_code=None,
        details=None,
        progress=1.0,
    )
    after_job_plan = AfterJobPlan(
        processing_graph=processing_graph,
        job_info=job_info,
        failed_runs=0,
    )
    if len(artifacts_to_create):
        assert after_job_plan.as_response() == [f"CreateJobs,{len(artifacts_to_create)}"]
    else:
        assert after_job_plan.as_response() == []

    after_job_plan.run()
    pending_jobs_df = Queue().get_pending_jobs_df(dataset=DATASET_NAME)
    assert len(pending_jobs_df) == len(artifacts_to_create)
    artifact_ids = [
        Artifact.get_id(
            dataset=row["dataset"],
            revision=row["revision"],
            config=row["config"],
            split=row["split"],
            processing_step_name=row["type"],
        )
        for _, row in pending_jobs_df.iterrows()
    ]
    assert set(artifact_ids) == set(artifacts_to_create)


def test_after_job_plan_delete() -> None:
    job_info = artifact_id_to_job_info(ARTIFACT_DA)
    # create two jobs for DG, and none for DH
    # one job should be deleted for DG, and one should be created for DH
    Queue().create_jobs([artifact_id_to_job_info(ARTIFACT_DG)] * 2)

    after_job_plan = AfterJobPlan(
        processing_graph=PROCESSING_GRAPH_PARALLEL,
        job_info=job_info,
        failed_runs=0,
    )
    assert after_job_plan.as_response() == ["CreateJobs,1", "DeleteJobs,1"]

    after_job_plan.run()
    pending_jobs_df = Queue().get_pending_jobs_df(dataset=DATASET_NAME)
    assert len(pending_jobs_df) == 2
    artifact_ids = [
        Artifact.get_id(
            dataset=row["dataset"],
            revision=row["revision"],
            config=row["config"],
            split=row["split"],
            processing_step_name=row["type"],
        )
        for _, row in pending_jobs_df.iterrows()
    ]
    assert artifact_ids == [ARTIFACT_DG, ARTIFACT_DH]


@pytest.mark.parametrize(
    "processing_graph,artifacts_to_create",
    [
        (PROCESSING_GRAPH_ONE_STEP, []),
        (PROCESSING_GRAPH_GENEALOGY, [ARTIFACT_DC]),
        (PROCESSING_GRAPH_FAN_IN_OUT, [ARTIFACT_CA_1, ARTIFACT_CA_2]),
        (PROCESSING_GRAPH_PARALLEL, [ARTIFACT_DG, ARTIFACT_DH]),
    ],
)
def test_finish_job(
    processing_graph: ProcessingGraph,
    artifacts_to_create: list[str],
) -> None:
    Queue().add_job(
        dataset=DATASET_NAME,
        revision=REVISION_NAME,
        config=None,
        split=None,
        job_type=STEP_DA,
        priority=Priority.NORMAL,
        difficulty=DIFFICULTY,
    )
    job_info = Queue().start_job()
    job_result = JobResult(
        job_info=job_info,
        job_runner_version=JOB_RUNNER_VERSION,
        is_success=True,
        output=JobOutput(
            content=CONFIG_NAMES_CONTENT,
            http_status=HTTPStatus.OK,
            error_code=None,
            details=None,
            progress=1.0,
        ),
    )
    finish_job(job_result=job_result, processing_graph=processing_graph)

    assert JobDocument.objects(dataset=DATASET_NAME).count() == len(artifacts_to_create)

    done_job = JobDocument.objects(dataset=DATASET_NAME, status=Status.STARTED)
    assert done_job.count() == 0

    waiting_jobs = JobDocument.objects(dataset=DATASET_NAME, status=Status.WAITING)
    assert waiting_jobs.count() == len(artifacts_to_create)
    assert {job.type for job in waiting_jobs} == {Artifact.parse_id(artifact)[4] for artifact in artifacts_to_create}

    assert CachedResponseDocument.objects(dataset=DATASET_NAME).count() == 1
    cached_response = CachedResponseDocument.objects(dataset=DATASET_NAME).first()
    assert cached_response
    assert cached_response.content == CONFIG_NAMES_CONTENT
    assert cached_response.http_status == HTTPStatus.OK
    assert cached_response.error_code is None
    assert cached_response.details == {}
    assert cached_response.progress == 1.0
    assert cached_response.job_runner_version == JOB_RUNNER_VERSION
    assert cached_response.dataset_git_revision == REVISION_NAME


@pytest.mark.parametrize(
    "processing_graph,first_artifacts",
    [
        (PROCESSING_GRAPH_ONE_STEP, [ARTIFACT_DA]),
        (PROCESSING_GRAPH_GENEALOGY, [ARTIFACT_DA, ARTIFACT_DB]),
        (PROCESSING_GRAPH_FAN_IN_OUT, [ARTIFACT_DA]),
        (PROCESSING_GRAPH_PARALLEL, [ARTIFACT_DA]),
    ],
)
def test_set_revision(
    processing_graph: ProcessingGraph,
    first_artifacts: list[str],
) -> None:
    set_revision(
        dataset=DATASET_NAME,
        revision=REVISION_NAME,
        priority=Priority.NORMAL,
        processing_graph=processing_graph,
    )

    pending_jobs_df = Queue().get_pending_jobs_df(dataset=DATASET_NAME)
    assert len(pending_jobs_df) == len(first_artifacts)
    artifact_ids = [
        Artifact.get_id(
            dataset=row["dataset"],
            revision=row["revision"],
            config=row["config"],
            split=row["split"],
            processing_step_name=row["type"],
        )
        for _, row in pending_jobs_df.iterrows()
    ]
    assert set(artifact_ids) == set(first_artifacts)


@pytest.mark.parametrize(
    "processing_graph,first_artifacts",
    [
        (PROCESSING_GRAPH_ONE_STEP, [ARTIFACT_DA]),
        (PROCESSING_GRAPH_GENEALOGY, [ARTIFACT_DA, ARTIFACT_DB]),
        (PROCESSING_GRAPH_FAN_IN_OUT, [ARTIFACT_DA]),
        (PROCESSING_GRAPH_PARALLEL, [ARTIFACT_DA]),
    ],
)
def test_set_revision_handle_existing_jobs(
    processing_graph: ProcessingGraph,
    first_artifacts: list[str],
) -> None:
    # create two pending jobs for DA
    Queue().create_jobs([artifact_id_to_job_info(ARTIFACT_DA)] * 2)
    set_revision(
        dataset=DATASET_NAME,
        revision=REVISION_NAME,
        priority=Priority.NORMAL,
        processing_graph=processing_graph,
    )

    pending_jobs_df = Queue().get_pending_jobs_df(dataset=DATASET_NAME)
    assert len(pending_jobs_df) == len(first_artifacts)
    artifact_ids = [
        Artifact.get_id(
            dataset=row["dataset"],
            revision=row["revision"],
            config=row["config"],
            split=row["split"],
            processing_step_name=row["type"],
        )
        for _, row in pending_jobs_df.iterrows()
    ]
    assert set(artifact_ids) == set(first_artifacts)


@pytest.mark.parametrize(
    "processing_graph,pending_artifacts,processing_step_names,expected_has_pending_ancestor_jobs",
    [
        (PROCESSING_GRAPH_ONE_STEP, [ARTIFACT_DA], [STEP_DA], True),
        (PROCESSING_GRAPH_GENEALOGY, [ARTIFACT_DA, ARTIFACT_DB], [STEP_DA], True),
        (PROCESSING_GRAPH_GENEALOGY, [ARTIFACT_DB], [STEP_DD], True),
        (PROCESSING_GRAPH_GENEALOGY, [ARTIFACT_DD], [STEP_DC], False),
        (PROCESSING_GRAPH_FAN_IN_OUT, [ARTIFACT_DA], [STEP_CB], True),
        (PROCESSING_GRAPH_FAN_IN_OUT, [ARTIFACT_DE], [STEP_CB], False),
    ],
)
def test_has_pending_ancestor_jobs(
    processing_graph: ProcessingGraph,
    pending_artifacts: list[str],
    processing_step_names: list[str],
    expected_has_pending_ancestor_jobs: bool,
) -> None:
    Queue().create_jobs([artifact_id_to_job_info(artifact) for artifact in pending_artifacts])
    assert (
        has_pending_ancestor_jobs(
            dataset=DATASET_NAME, processing_step_names=processing_step_names, processing_graph=processing_graph
        )
        == expected_has_pending_ancestor_jobs
    )


def test_remove_dataset() -> None:
    Queue().create_jobs([artifact_id_to_job_info(artifact) for artifact in [ARTIFACT_DA, ARTIFACT_DB]])
    Queue().start_job()
    job_info = artifact_id_to_job_info(ARTIFACT_DA)
    upsert_response_params(
        # inputs
        kind=STEP_DA,
        job_params=job_info["params"],
        job_runner_version=JOB_RUNNER_VERSION,
        # output
        content=CONFIG_NAMES_CONTENT,
        http_status=HTTPStatus.OK,
        error_code=None,
        details=None,
        progress=1.0,
    )

    pending_jobs_df = Queue().get_pending_jobs_df(dataset=DATASET_NAME)
    assert len(pending_jobs_df) > 0
    assert has_some_cache(dataset=DATASET_NAME) is True

    remove_dataset(dataset=DATASET_NAME)

    pending_jobs_df = Queue().get_pending_jobs_df(dataset=DATASET_NAME)
    assert len(pending_jobs_df) == 0
    assert has_some_cache(dataset=DATASET_NAME) is False


@pytest.mark.parametrize("is_big,failed_runs", [(False, 0), (True, 0), (False, 1), (True, 3)])
def test_after_job_plan_gives_bonus_difficulty(is_big: bool, failed_runs: int) -> None:
    bonus_difficulty_if_dataset_is_big = 10
    processing_graph = ProcessingGraph(
        {
            "dataset_step": {"input_type": "dataset"},
            "config-split-names-from-streaming": {
                "input_type": "config",
                "triggered_by": "dataset_step",
            },
            "config-info": {
                "input_type": "config",
                "triggered_by": "dataset_step",
            },
            "config_step_with_bonus": {
                "input_type": "config",
                "bonus_difficulty_if_dataset_is_big": bonus_difficulty_if_dataset_is_big,
                "triggered_by": "config-info",
            },
            "split_step_with_bonus": {
                "input_type": "split",
                "bonus_difficulty_if_dataset_is_big": bonus_difficulty_if_dataset_is_big,
                "triggered_by": "config-info",
            },
        },
        min_bytes_for_bonus_difficulty=1000,
    )
    job_info = artifact_id_to_job_info("config-split-names-from-streaming,dataset_name,revision_hash,config_name")
    upsert_response_params(
        # inputs
        kind=job_info["type"],
        job_params=job_info["params"],
        job_runner_version=1,
        # output
        content={"splits": [{"dataset_name": "dataset_name", "config": "config_name", "split": "split_name"}]},
        http_status=HTTPStatus.OK,
        error_code=None,
        details=None,
        progress=1.0,
    )
    job_info = artifact_id_to_job_info("config-info,dataset_name,revision_hash,config_name")
    upsert_response_params(
        # inputs
        kind=job_info["type"],
        job_params=job_info["params"],
        job_runner_version=1,
        # output
        content={"dataset_info": {"dataset_size": 10000 if is_big else 10}},
        http_status=HTTPStatus.OK,
        error_code=None,
        details=None,
        progress=1.0,
    )

    after_job_plan = AfterJobPlan(job_info=job_info, processing_graph=processing_graph, failed_runs=failed_runs)
    assert len(after_job_plan.job_infos_to_create) == 2
    config_jobs_with_bonus = [
        job for job in after_job_plan.job_infos_to_create if job["type"] == "config_step_with_bonus"
    ]
    assert len(config_jobs_with_bonus) == 1
    split_jobs_with_bonus = [
        job for job in after_job_plan.job_infos_to_create if job["type"] == "split_step_with_bonus"
    ]
    assert len(split_jobs_with_bonus) == 1

    expected_difficulty = min(
        DEFAULT_DIFFICULTY_MAX,
        DIFFICULTY
        + (0 if not is_big else bonus_difficulty_if_dataset_is_big)
        + failed_runs * DIFFICULTY_BONUS_BY_FAILED_RUNS,
    )
    assert config_jobs_with_bonus[0]["difficulty"] == expected_difficulty
    assert split_jobs_with_bonus[0]["difficulty"] == expected_difficulty


def assert_failed_runs(failed_runs: int) -> None:
    cached_entry_metadata = get_response_metadata(kind=STEP_DA, dataset=DATASET_NAME)
    assert cached_entry_metadata is not None
    assert cached_entry_metadata["failed_runs"] == failed_runs
    assert CachedResponseDocument.objects().count() == 1


def run_job(revision: str, http_status: HTTPStatus) -> None:
    Queue().add_job(
        dataset=DATASET_NAME,
        revision=revision,
        config=None,
        split=None,
        job_type=STEP_DA,
        priority=Priority.NORMAL,
        difficulty=DIFFICULTY,
    )
    job_info = Queue().start_job()
    job_result = JobResult(
        job_info=job_info,
        job_runner_version=JOB_RUNNER_VERSION,
        is_success=True,
        output=JobOutput(
            content=CONFIG_NAMES_CONTENT,
            http_status=http_status,
            error_code=None,
            details=None,
            progress=1.0,
        ),
    )
    finish_job(job_result=job_result, processing_graph=PROCESSING_GRAPH_GENEALOGY)
    # clear generated jobs when finishing jobs
    Queue().delete_dataset_jobs(DATASET_NAME)


def test_upsert_response_failed_runs() -> None:
    first_revision = "revision"
    second_revision = "new_revision"

    # new cache record with success result
    run_job(first_revision, HTTPStatus.OK)
    assert_failed_runs(0)

    # overwrite cache record with success result and same revision
    run_job(first_revision, HTTPStatus.OK)
    assert_failed_runs(0)

    # overwrite cache record with failed result and same revision
    run_job(first_revision, HTTPStatus.INTERNAL_SERVER_ERROR)
    assert_failed_runs(1)

    # overwrite cache record with failed result and same revision
    run_job(first_revision, HTTPStatus.INTERNAL_SERVER_ERROR)
    assert_failed_runs(2)

    # overwrite cache record with failed result and new revision
    run_job(second_revision, HTTPStatus.INTERNAL_SERVER_ERROR)
    assert_failed_runs(0)

    # overwrite cache record with success result and new revision
    run_job(second_revision, HTTPStatus.OK)
    assert_failed_runs(0)
