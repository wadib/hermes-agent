"""Trusted gateway acceptance for delivery-required Kanban work."""

from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.asyncio
async def test_gateway_acceptance_requires_matching_authenticated_delivery_source(kanban_home, tmp_path):
    artifact = tmp_path / "evidence.txt"
    artifact.write_text("verified", encoding="utf-8")
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="deliver", delivery_required=True)
        kb.add_attachment(conn, task_id, filename=artifact.name, stored_path=str(artifact), size=artifact.stat().st_size)
        assert kb.complete_task(conn, task_id, summary="technical completion")
        assert kb.record_outbox_delivery(
            conn, task_id, platform="telegram", conversation_ref="chat-1", session_ref="s-1", native_message_id="out-1",
        )
        kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1", user_id="wessam")

    runner = object.__new__(GatewayRunner)
    rejected_source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat-1", user_id="other", message_id="in-1",
    )
    rejected = await runner._hm_kanban_acceptance_command(
        MessageEvent(text=f"/accept {task_id}", source=rejected_source, message_id="in-1"), rejected_source,
    )
    assert rejected == "Acceptance refused: no delivered task matched this authenticated delivery route."

    accepted_source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat-1", user_id="wessam", message_id="in-2",
    )
    accepted = await runner._hm_kanban_acceptance_command(
        MessageEvent(text=f"/accept {task_id}", source=accepted_source, message_id="in-2"), accepted_source,
    )
    assert accepted == f"✓ Accepted {task_id}; it is now Done on board default."

    with kbc.connect() as conn:
        assert kb.get_task(conn, task_id).status == "done"
        acceptance = kb.get_task_acceptance(conn, task_id)
        assert acceptance is not None
        assert acceptance.source == "gateway_authenticated_inbound"
        assert acceptance.user_message_ref == "telegram:chat-1:in-2"
