"""Push admission and durable notifier retries use real adapter/SQLite lifecycles."""
import asyncio

import pytest

from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import Platform, PlatformConfig
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.wake import admit_internal_event, deliver_wake
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


def setup_route(raft=False):
    if raft:
        from plugins.platforms.raft.adapter import RaftAdapter
        adapter = RaftAdapter(PlatformConfig(enabled=True, typing_indicator=False,
            extra={"bridge_token": "owned-test-token", "port": 0}))
    else:
        adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.adapters = {adapter.platform: adapter}
    runner._adapter_for_source = lambda source: adapter
    runner._kanban_dispatcher_lock_handle = object()
    source = SessionSource(platform=adapter.platform, chat_id="42", user_id="42", chat_type="dm")
    return runner, adapter, source, build_session_key(source)


async def drain(adapter):
    while adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks))


@pytest.mark.asyncio
@pytest.mark.parametrize("raft", [False, True])
async def test_push_receipt_requires_real_admission_without_displacing_user(raft, monkeypatch):
    runner, adapter, source, key = setup_route(raft)
    if raft:
        monkeypatch.setattr(adapter, "_spawn_bridge", lambda port: None)
    release, started = asyncio.Event(), asyncio.Event()
    received = []

    async def handler(event):
        received.append(event.text)
        started.set()
        await release.wait()
        # Real runner FIFO promotion at the fake model boundary.
        if key not in adapter._pending_messages:
            pending = runner._promote_queued_event(key, adapter, None)
            if pending is not None:
                adapter._pending_messages[key] = pending

    adapter.set_message_handler(handler)
    await adapter.connect()
    try:
        await deliver_wake(adapter, text="idle", source=source)
        await asyncio.wait_for(started.wait(), 2)
        assert await adapter.handle_message(MessageEvent(text="human", source=source)) is None
        with pytest.raises(RuntimeError, match="not accepted"):
            await deliver_wake(adapter, text="no-fifo", source=source)
        assert adapter._pending_messages[key].text == "human"
        adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
        runner._BUSY_QUEUE_MAX_PENDING = 1
        with pytest.raises(RuntimeError, match="not accepted"):
            await deliver_wake(adapter, text="at-cap", source=source)
        assert adapter._pending_messages[key].text == "human"
        runner._BUSY_QUEUE_MAX_PENDING = 3
        await deliver_wake(adapter, text="busy", source=source)
        assert runner._queue_depth(key, adapter=adapter) == 2
        rejected = MessageEvent(text="wrong-key", source=source, internal=True,
                                metadata={"gateway_session_key": "agent:wrong"})
        with pytest.raises(RuntimeError, match="not accepted"):
            await admit_internal_event(adapter, rejected)
        assert rejected._gateway_accepted is False
        release.set()
        await drain(adapter)
        assert received == ["idle", "human", "busy"]
        adapter.set_message_handler(None)
        with pytest.raises(RuntimeError, match="not accepted"):
            await deliver_wake(adapter, text="no-handler", source=source)
    finally:
        release.set()
        await drain(adapter)
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_notifier_retries_unaccepted_wake_without_repeating_pings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    runner, adapter, source, key = setup_route()
    conn = kbc.connect()
    tids = {}
    try:
        for mode in ("notify+wake", "wake", "notify"):
            tid = kb.create_task(conn, title=mode, assignee="worker", session_id=key)
            kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="42",
                               user_id="42", chat_type="dm", delivery_mode=mode)
            kb.complete_task(conn, tid, summary="handoff")
            tids[mode] = tid
    finally:
        conn.close()

    failures = {}

    async def tick():
        deliveries = await asyncio.to_thread(_notifier_collect, runner, kb,
            notifier_profile=None, gc_due=False, gc_retention_days=30)
        for delivery in deliveries:
            await _KanbanNotification(runner, delivery, platform_cls=Platform,
                                      sub_fail_counts=failures).deliver()

    def unseen(mode):
        conn = kbc.connect()
        try:
            return kbn.unseen_events_for_sub(conn, task_id=tids[mode], platform="telegram",
                                            chat_id="42", kinds=["completed"])[1]
        finally:
            conn.close()

    await adapter.connect()
    await tick()  # send works, but no message handler has been installed yet
    assert len(adapter.wire) == 2
    assert unseen("notify+wake") and unseen("wake")
    assert not unseen("notify")
    # New notifier instances and DB connections replay the durable claim, not the ping.
    for _ in range(13):
        await tick()
    assert len(adapter.wire) == 2
    assert unseen("notify+wake") and unseen("wake")
    assert failures == {}
    received = []

    async def handler(event):
        received.append(event.text)

    adapter.set_message_handler(handler)
    await tick()
    await drain(adapter)
    await tick()
    assert len(received) == 2
    assert all("handoff" in text for text in received)
    assert len(adapter.wire) == 2
    assert not any(unseen(mode) for mode in tids)
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_usable_output_notifier_retries_two_confirmed_failures_then_delivers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    runner, adapter, _source, _key = setup_route()
    adapter.provider_calls = 0

    async def flaky_send(chat_id, content, reply_to=None, metadata=None):
        adapter.provider_calls += 1
        if adapter.provider_calls < 3:
            return SendResult(success=False, error="confirmed pre-send failure")
        adapter.wire.append(content)
        return SendResult(success=True, message_id="native-output-1")

    adapter.send = flaky_send
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="progress", assignee="worker")
        kbn.add_notify_sub(conn, task_id=task, platform="telegram", chat_id="42",
                           user_id="42", chat_type="dm", delivery_mode="notify")
        assert kb.claim_task(conn, task) is not None
        assert kb.publish_usable_output(
            conn, task, idempotency_key="usable-1", content="usable result")

    failures = {}

    async def tick():
        rows = await asyncio.to_thread(
            _notifier_collect, runner, kb, notifier_profile=None,
            gc_due=False, gc_retention_days=30)
        for row in rows:
            await _KanbanNotification(
                runner, row, platform_cls=Platform, sub_fail_counts=failures).deliver()

    await tick()
    await tick()
    await tick()
    with kbc.connect() as conn:
        outbox = kb.get_usable_output_outbox(conn, task, "usable-1")
        sub = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id=? AND platform='telegram' AND chat_id='42'",
            (task,),
        ).fetchone()
        assert outbox["state"] == "delivered"
        assert outbox["native_message_id"] == "native-output-1"
        assert kb.get_task(conn, task).status == "running"
        assert sub is not None
    assert adapter.provider_calls == 3
    assert len(adapter.wire) == 1 and "usable result" in adapter.wire[0]


@pytest.mark.asyncio
async def test_usable_output_notifier_reconciles_ack_after_receipt_crash_without_resend(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    runner, adapter, _source, _key = setup_route()
    adapter.provider_calls = 0

    async def accepted_send(chat_id, content, reply_to=None, metadata=None):
        adapter.provider_calls += 1
        adapter.wire.append(content)
        return SendResult(success=True, message_id="native-output-crash")

    adapter.send = accepted_send
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="progress", assignee="worker")
        kbn.add_notify_sub(conn, task_id=task, platform="telegram", chat_id="42",
                           thread_id="thread-1", user_id="42", chat_type="dm",
                           delivery_mode="notify")
        assert kb.claim_task(conn, task) is not None
        assert kb.publish_usable_output(
            conn, task, idempotency_key="usable-crash", content="durable result")

    failures = {}

    async def tick():
        rows = await asyncio.to_thread(
            _notifier_collect, runner, kb, notifier_profile=None,
            gc_due=False, gc_retention_days=30)
        for row in rows:
            await _KanbanNotification(
                runner, row, platform_cls=Platform, sub_fail_counts=failures).deliver()

    real_reconcile = kb.reconcile_usable_output_delivery
    monkeypatch.setattr(
        kb, "reconcile_usable_output_delivery",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("receipt DB crash")))
    await tick()
    with kbc.connect() as conn:
        held = kb.get_usable_output_outbox(conn, task, "usable-crash")
        token = held["send_attempt_token"]
        assert held["state"] == "sending"
        assert held["native_message_id"] == "native-output-crash"
        assert held["thread_id"] == "thread-1"
        assert token
        assert not kb.reset_usable_output_send_attempt(
            conn, task, "usable-crash", send_attempt_token=token)
    assert adapter.provider_calls == 1

    monkeypatch.setattr(kb, "reconcile_usable_output_delivery", real_reconcile)
    await tick()
    with kbc.connect() as conn:
        delivered = kb.get_usable_output_outbox(conn, task, "usable-crash")
        assert delivered["state"] == "delivered"
        assert kb.reconcile_usable_output_delivery(
            conn, task, "usable-crash", send_attempt_token=token)
        assert kb.get_task(conn, task).status == "running"
    assert adapter.provider_calls == 1
