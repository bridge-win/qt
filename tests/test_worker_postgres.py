"""Cross-connection worker admission and expired-lease recovery regressions."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, update

from qt.control.service import ControlService
from qt.persistence import Conflict, Repository
from qt.persistence.schema import jobs
from tests.test_production_postgres import postgres as postgres
from tests.test_production_review import _case

pytestmark = pytest.mark.integration


def test_pg_different_request_keys_admit_one_frozen_experiment(postgres):
    spec, _, _, _ = _case(postgres)

    def submit(number):
        control = ControlService(Repository(postgres.repository.engine), postgres.datasets.root,
                                 tenant=postgres.tenant, signing_key=postgres.signing_key)
        return control.create_experiment(spec, f"request-{number}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        submitted = list(executor.map(submit, range(24)))
    assert len({job["job_id"] for job in submitted}) == 1
    with postgres.repository.transaction() as connection:
        rows = connection.execute(select(jobs).where(jobs.c.tenant_id == postgres.tenant)).all()
    assert len(rows) == 1
    assert len(postgres.experiments.trial_population(postgres.tenant, spec["campaign_id"])) == 1
    with pytest.raises(Conflict):
        postgres.create_experiment({**spec, "seed": 38}, "request-23")
    assert len(postgres.experiments.trial_population(postgres.tenant, spec["campaign_id"])) == 1


def test_pg_heartbeat_cannot_revive_lease_expired_while_waiting_on_row_lock(postgres):
    repo = postgres.repository
    repo.enqueue(postgres.tenant, "lock-clock", {"fixture": "lease"}, "locked")
    job = repo.claim("lock-clock", tenant=postgres.tenant)
    started = threading.Event()

    def renew():
        started.set()
        return Repository(repo.engine).heartbeat(job)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with repo.engine.begin() as connection:
            connection.execute(select(jobs).where(jobs.c.job_id == job["job_id"]).with_for_update()).one()
            expires = time.time_ns() + 150_000_000
            connection.execute(update(jobs).where(jobs.c.job_id == job["job_id"]).values(lease_until_ns=expires))
            renewed = executor.submit(renew)
            assert started.wait(1)
            time.sleep(.3)
            assert not renewed.done()
        assert renewed.result(timeout=2) is False
    assert repo.job(postgres.tenant, job["job_id"])["lease_until_ns"] == expires
    replacement = repo.claim("lock-clock", tenant=postgres.tenant)
    assert replacement["lease_token"] != job["lease_token"]


def test_pg_different_campaigns_cannot_share_one_request_key(postgres):
    first, _, _, _ = _case(postgres)
    second, _, _, _ = _case(postgres, suffix="b")
    specs = (first, second)

    def submit(number):
        control = ControlService(Repository(postgres.repository.engine), postgres.datasets.root,
                                 tenant=postgres.tenant, signing_key=postgres.signing_key)
        try:
            return control.create_experiment(specs[number % 2], "cross-campaign-key")["job_id"]
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(submit, range(24)))
    assert len({identity for identity in results if identity is not None}) == 1
    assert results.count(None) == 12
    populations = [postgres.experiments.trial_population(postgres.tenant, spec["campaign_id"]) for spec in specs]
    assert sorted(len(items) for items in populations) == [0, 1]
    assert len(postgres.repository.list(postgres.tenant, "experiment_spec")) == 1
