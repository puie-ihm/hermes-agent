import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _create_request(db, request_id="req-1", **overrides):
    values = {
        "request_id": request_id,
        "platform": "slack",
        "session_key": "agent:main:slack:channel:C1",
        "user_id": "U1",
        "chat_id": "C1",
        "thread_id": "T1",
        "platform_message_id": f"message-{request_id}",
        "client_message_id": f"client-{request_id}",
        "payload_json": '{"text":"hello"}',
        "received_at": 100.0,
    }
    values.update(overrides)
    return db.create_or_get_gateway_request(**values)


def test_gateway_request_schema_reconciles_columns_and_deferred_indexes(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE gateway_requests (request_id TEXT PRIMARY KEY);
        INSERT INTO gateway_requests (request_id) VALUES ('legacy-request');
        """
    )
    conn.close()

    session_db = SessionDB(db_path=db_path)
    try:
        columns = {
            row[1]
            for row in session_db._conn.execute(
                "PRAGMA table_info(gateway_requests)"
            ).fetchall()
        }
        assert columns == {
            "request_id",
            "platform",
            "session_key",
            "user_id",
            "chat_id",
            "thread_id",
            "platform_message_id",
            "client_message_id",
            "payload_json",
            "status",
            "delivery_state",
            "received_at",
            "queued_at",
            "admitted_at",
            "started_at",
            "finalized_at",
            "delivery_started_at",
            "delivered_at",
            "updated_at",
            "response_hash",
            "delivery_message_id",
            "public_error",
        }
        indexes = {
            row[0]
            for row in session_db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'gateway_requests'"
            ).fetchall()
        }
        assert "idx_gateway_requests_status_received" in indexes
        assert "idx_gateway_requests_inbound_identity" in indexes
        legacy = session_db.get_gateway_request("legacy-request")
        assert legacy["status"] == "RECEIVED"
        assert legacy["delivery_state"] == "NONE"
    finally:
        session_db.close()


def test_duplicate_inbound_identity_atomically_returns_original(tmp_path):
    db_path = tmp_path / "state.db"
    first = SessionDB(db_path=db_path)
    second = SessionDB(db_path=db_path)
    barrier = Barrier(2)

    def create(session_db, request_id):
        barrier.wait()
        return _create_request(
            session_db,
            request_id,
            platform_message_id="same-platform-message",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda args: create(*args),
                    [(first, "request-a"), (second, "request-b")],
                )
            )
        rows = [row for row, _created in results]
        assert sum(created for _row, created in results) == 1
        assert rows[0]["request_id"] == rows[1]["request_id"]
        assert first._conn.execute("SELECT COUNT(*) FROM gateway_requests").fetchone()[0] == 1
        empty_first = _create_request(
            first, "empty-a", platform_message_id=""
        )
        empty_second = _create_request(
            first, "empty-b", platform_message_id=""
        )
        assert empty_first[1] is True
        assert empty_second[1] is True
    finally:
        first.close()
        second.close()


def test_gateway_request_status_compare_and_set_and_terminal_immutability(db):
    _create_request(db)

    queued = db.compare_and_set_gateway_request_status(
        "req-1", from_statuses=["RECEIVED"], to_status="QUEUED", now=110.0
    )
    assert queued["status"] == "QUEUED"
    assert queued["queued_at"] == 110.0

    assert db.compare_and_set_gateway_request_status(
        "req-1", from_statuses=["RECEIVED"], to_status="WORKING", now=120.0
    ) is None

    working = db.compare_and_set_gateway_request_status(
        "req-1", from_statuses=["QUEUED"], to_status="WORKING", now=130.0
    )
    assert working["status"] == "WORKING"
    assert working["admitted_at"] == 130.0
    assert working["started_at"] == 130.0

    final = db.compare_and_set_gateway_request_status(
        "req-1",
        from_statuses=["WORKING"],
        to_status="FINAL",
        response_hash="sha256:response",
        now=140.0,
    )
    assert final["status"] == "FINAL"
    assert final["finalized_at"] == 140.0
    assert final["response_hash"] == "sha256:response"

    assert db.compare_and_set_gateway_request_status(
        "req-1", from_statuses=["FINAL"], to_status="FAILED", now=150.0
    ) is None
    assert db.get_gateway_request("req-1")["status"] == "FINAL"


def test_gateway_request_delivery_claim_is_single_winner_and_failed_is_reclaimable(tmp_path):
    db_path = tmp_path / "state.db"
    first = SessionDB(db_path=db_path)
    second = SessionDB(db_path=db_path)
    _create_request(first)
    barrier = Barrier(2)

    def claim(session_db):
        barrier.wait()
        return session_db.claim_gateway_request_delivery("req-1", now=200.0)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, (first, second)))
        assert sum(claimed is not None for claimed in claims) == 1
        assert first.get_gateway_request("req-1")["delivery_state"] == "SENDING"

        failed = first.finish_gateway_request_delivery(
            "req-1", delivery_state="FAILED", now=210.0
        )
        assert failed["delivery_state"] == "FAILED"
        reclaimed = second.claim_gateway_request_delivery("req-1", now=220.0)
        assert reclaimed["delivery_state"] == "SENDING"
        assert reclaimed["delivery_started_at"] == 220.0
        delivered = second.finish_gateway_request_delivery(
            "req-1",
            delivery_state="DELIVERED",
            delivery_message_id="outbound-1",
            now=230.0,
        )
        assert delivered["delivery_state"] == "DELIVERED"
        assert delivered["delivered_at"] == 230.0
        assert delivered["delivery_message_id"] == "outbound-1"
        assert first.claim_gateway_request_delivery("req-1") is None
    finally:
        first.close()
        second.close()


def test_list_gateway_requests_filters_statuses_in_received_fifo_order(db):
    _create_request(db, "newest", received_at=300.0)
    _create_request(db, "oldest", received_at=100.0)
    _create_request(db, "middle", received_at=200.0)
    db.compare_and_set_gateway_request_status(
        "middle", from_statuses=["RECEIVED"], to_status="QUEUED", now=210.0
    )

    received = db.list_gateway_requests(["RECEIVED"])
    assert [row["request_id"] for row in received] == ["oldest", "newest"]
    combined = db.list_gateway_requests(["QUEUED", "RECEIVED"])
    assert [row["request_id"] for row in combined] == [
        "oldest",
        "middle",
        "newest",
    ]


def test_gateway_request_methods_reject_invalid_json_and_states(db):
    with pytest.raises(ValueError, match="valid JSON"):
        _create_request(db, payload_json="not-json")
    _create_request(db)
    with pytest.raises(ValueError, match="invalid gateway request status"):
        db.compare_and_set_gateway_request_status(
            "req-1", from_statuses=["RECEIVED"], to_status="UNKNOWN"
        )
    with pytest.raises(ValueError, match="DELIVERED or FAILED"):
        db.finish_gateway_request_delivery("req-1", delivery_state="SENDING")
