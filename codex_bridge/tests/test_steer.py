"""Offline steer routing tests using SDK status types and controlled concurrency."""

from __future__ import annotations
import asyncio
import ast
from contextlib import ExitStack, aclosing
from pathlib import Path
from queue import Queue
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from openai_codex import AsyncThread, InvalidRequestError, LocalImageInput
from openai_codex.types import TurnStatus, TurnCompletedNotification
from openai_codex.generated.v2_all import AgentMessageThreadItem, ThreadItem, ThreadStatus

from codex_bridge.codex import codex_execution, codex_runtime, thread_manager
from codex_bridge.session import session_codex, session_history
from codex_bridge.codex import codex_execution as codex
from codex_bridge.codex.codex_execution import Codex, LiveTurn
from codex_bridge.configs.loader import CONFIG
from codex_bridge.middleware.message_collector import batch_from_chat_history
import test_stop_command as helpers


def status(kind):
    payload = {"type": kind}
    if kind == "active":
        payload["activeFlags"] = []
    return NS(thread=NS(status=ThreadStatus.model_validate(payload)))


class BackendSteerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(session_history, "register_session", AsyncMock()))
        self.enterContext(patch.object(session_codex, "_schedule_session_title"))
        self.enterContext(patch.object(session_codex, "_sync_session_time", AsyncMock()))
        stack = self.enterContext(ExitStack())
        for name, value in (("_active_turns", {}), ("_live_turns", {}),
                            ("_pending_interrupts", set()), ("_turns_guard", asyncio.Lock())):
            stack.enter_context(patch.object(Codex, name, value))
        route = CONFIG["codex"]["models"]["text"]
        self.handle = NS(id="turn-1", steer=AsyncMock(return_value=NS(turn_id="turn-1")))
        self.thread = NS(id="thread-1", read=AsyncMock(return_value=status("active")))
        self.live = LiveTurn(self.thread, self.handle, route["model"], route["provider"])
        Codex._live_turns["run"] = self.live

    async def test_active_checks_status_and_steers_existing_handle(self) -> None:
        for prompt in ("second", "third"):
            self.assertEqual(await Codex.steer("run", prompt), {"type": "steered"})
        self.assertEqual(self.thread.read.await_count, 2)
        self.assertEqual([call.args for call in self.handle.steer.await_args_list],
                         [("second",), ("third",)])

    async def test_idle_closed_missing_and_stopped_never_steer(self) -> None:
        self.thread.read.return_value = status("idle")
        self.assertEqual(await Codex.steer("run", "x"), {"type": "idle"})
        self.live.closed = True
        self.assertEqual(await Codex.steer("run", "x"), {"type": "idle"})
        self.assertEqual(await Codex.steer("missing", "x"), {"type": "idle"})
        self.live.closed = False
        Codex._pending_interrupts.add("run")
        self.assertEqual(await Codex.steer("run", "x"), {"type": "idle"})
        self.handle.steer.assert_not_awaited()

    async def test_failed_status_is_not_treated_as_idle(self) -> None:
        for kind in ("systemError", "notLoaded"):
            self.thread.read.return_value = status(kind)
            self.assertEqual((await Codex.steer("run", "x"))["type"], "error")
        self.thread.read.side_effect = RuntimeError("connection closed")
        self.assertEqual((await Codex.steer("run", "x"))["type"], "error")
        self.handle.steer.assert_not_awaited()

    async def test_completion_race_falls_back_only_after_explicit_rejection_and_idle(self) -> None:
        self.thread.read.side_effect = [status("active"), status("idle")]
        self.handle.steer.side_effect = InvalidRequestError(-32600, "no active turn to steer")
        self.assertEqual(await Codex.steer("run", "x"), {"type": "idle"})
        self.handle.steer.assert_awaited_once_with("x")

    async def test_mismatch_active_review_and_uncertain_errors_do_not_replay(self) -> None:
        for error in (InvalidRequestError(-32600, "expected active turn id `a` but found `b`"),
                      InvalidRequestError(-32600, "cannot steer a review turn"),
                      TimeoutError("response lost")):
            self.handle.steer.side_effect = error
            self.assertEqual((await Codex.steer("run", "x"))["type"], "error")
        self.assertEqual(self.handle.steer.await_count, 3)

    async def test_stop_during_status_read_prevents_submission(self) -> None:
        async def read():
            Codex._pending_interrupts.add("run")
            return status("active")
        self.thread.read.side_effect = read
        self.assertEqual(await Codex.steer("run", "x"), {"type": "idle"})
        self.handle.steer.assert_not_awaited()

    async def test_recalled_input_during_status_read_is_not_submitted(self) -> None:
        recalled = False
        async def read():
            nonlocal recalled
            recalled = True
            return status("active")
        self.thread.read.side_effect = read
        self.assertEqual(await Codex.steer("run", "x", cancelled=lambda: recalled),
                         {"type": "cancelled"})
        self.handle.steer.assert_not_awaited()

    async def test_image_route_is_validated_without_model_switch(self) -> None:
        prompt = [LocalImageInput(path="/tmp/example.png")]
        self.live.model = "text-only"
        self.assertEqual((await Codex.steer("run", prompt))["type"], "error")
        self.handle.steer.assert_not_awaited()
        route = CONFIG["codex"]["models"]["image"]
        self.live.model, self.live.provider = route["model"], route["provider"]
        self.assertEqual(await Codex.steer("run", prompt), {"type": "steered"})
        self.handle.steer.assert_awaited_once_with(prompt)

    async def test_running_keeps_client_alive_during_steer_and_logs_once_without_usage(self) -> None:
        finish, steering, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def stream():
            await finish.wait()
            yield NS(method="turn/completed", payload=NS(turn=NS(
                duration_ms=10, status=TurnStatus.completed)))
        async def steer(prompt):
            steering.set()
            await release.wait()
            return NS(turn_id="turn-1")
        self.handle.stream = stream
        self.handle.steer.side_effect = steer
        self.thread.turn = AsyncMock(return_value=self.handle)
        with (patch.object(codex_runtime, "AsyncCodex") as client_class,
              patch.object(thread_manager, "get_user_thread", AsyncMock(return_value=None)),
              patch.object(thread_manager, "set_user_thread", AsyncMock()),
              patch.object(codex_execution, "prepare_workspace", AsyncMock()),
              patch.object(codex_execution, "SavingLog", AsyncMock()) as saving):
            client_class.return_value.__aenter__.return_value.thread_start.return_value = self.thread
            events = Codex.running("user", "hello", "actual", notify_started=True)
            self.assertEqual((await anext(events))["type"], "started")
            control = asyncio.create_task(Codex.steer("actual", "second"))
            await asyncio.wait_for(steering.wait(), 1)
            async def drain():
                return [event async for event in events]
            consumer = asyncio.create_task(drain())
            try:
                finish.set()
                await asyncio.sleep(0)
                self.assertFalse(consumer.done())
                client_class.return_value.__aexit__.assert_not_awaited()
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(control, consumer), 1)
            self.assertNotIn("actual", Codex._live_turns)
            saving.assert_not_awaited()

    async def test_stream_preserves_message_phase_and_hides_tool_content(self) -> None:
        async def stream():
            for item_id, phase in [("progress", "commentary"), ("final", "final_answer")]:
                item = AgentMessageThreadItem(id=item_id, phase=phase, text="", type="agentMessage")
                yield NS(method="item/started", payload=NS(item=NS(root=item)))
                yield NS(method="item/agentMessage/delta", payload=NS(item_id=item_id, delta="text"))
                yield NS(method="item/completed", payload=NS(item=NS(root=item)))
                yield NS(method="item/commandExecution/outputDelta", payload=NS(item_id="tool", delta="tool output"))
                if phase == "commentary":
                    yield NS(method="item/reasoning/textDelta", payload=NS(item_id="r", delta="reasoning"))
            yield NS(method="turn/completed", payload=NS(turn=NS(duration_ms=10, status=TurnStatus.completed)))
        self.handle.stream = stream
        self.thread.turn = AsyncMock(return_value=self.handle)
        with (patch.object(codex_runtime, "AsyncCodex") as client_class,
              patch.object(thread_manager, "get_user_thread", AsyncMock(return_value=None)),
              patch.object(thread_manager, "set_user_thread", AsyncMock()),
              patch.object(codex_execution, "prepare_workspace", AsyncMock()),
              patch.object(codex_execution, "SavingLog", AsyncMock())):
            client_class.return_value.__aenter__.return_value.thread_start.return_value = self.thread
            events = [event async for event in Codex.running("user", "hello", "phases")]
        self.assertEqual(events, [
            {"type": "answer", "content": "text", "item_id": "progress", "phase": "commentary"},
            {"type": "reasoning", "content": "reasoning", "item_id": "r"},
            {"type": "answer", "content": "text", "item_id": "final", "phase": "final_answer"},
            {"type": "done"},
        ])

    async def test_tool_statuses_hide_details_and_keep_probe_activity(self) -> None:
        from datetime import datetime, timezone

        # Fixture fields follow the current SDK; body markers verify that no details bypass filtering.
        command = dict(type="commandExecution", command="PRIVATE command", cwd="/private",
                       commandActions=[], aggregatedOutput="PRIVATE output")
        mcp = dict(type="mcpToolCall", server="files", tool="read", arguments={"path": "PRIVATE"},
                   result={"content": [{"text": "PRIVATE result"}]})
        dynamic = dict(type="dynamicToolCall", namespace="custom", tool="lookup",
                       arguments={"query": "PRIVATE"})
        cases = [
            (dict(command, status="completed", exitCode=0), "commandExecution", "succeeded"),
            (dict(command, status="failed", exitCode=1), "commandExecution", "failed"),
            (dict(command, status="completed", exitCode=2), "commandExecution", "failed"),
            (dict(command, status="declined"), "commandExecution", "failed"),
            (dict(command, status="failed", exitCode=0), "commandExecution", "failed"),
            (dict(mcp, status="completed"), "files.read", "succeeded"),
            (dict(mcp, status="failed", error={"message": "PRIVATE error"}), "files.read", "failed"),
            (dict(mcp, status="completed", error={"message": "PRIVATE error"}), "files.read", "failed"),
            (dict(dynamic, status="completed", success=False), "custom.lookup", "failed"),
            (dict(dynamic, status="completed", success=True), "custom.lookup", "succeeded"),
            (dict(dynamic, namespace=None, status="completed"), "lookup", "succeeded"),
            (dict(type="fileChange", changes=[], status="declined"), "fileChange", "failed"),
            (dict(type="fileChange", changes=[], status="completed"), "fileChange", "succeeded"),
            (dict(type="collabAgentToolCall", tool="spawnAgent", senderThreadId="main",
                  receiverThreadIds=[], agentsStates={}, status="interrupted", prompt="PRIVATE"),
             "spawnAgent", "failed"),
            (dict(type="webSearch", query="PRIVATE", results=["PRIVATE"]),
             "webSearch", "finished (status unknown)"),
            (dict(type="imageView", path="/PRIVATE"), "imageView", "finished (status unknown)"),
        ]
        expected = []
        received = []

        async def stream():
            for index, (data, name, suffix) in enumerate(cases):
                item_id = f"tool-{index}"
                item = ThreadItem.model_validate(dict(data, id=item_id))
                for method, text in (("item/started", ""), ("item/completed", ": " + suffix)):
                    expected.append({"type": "progress", "item_id": item_id,
                                     "content": f"Agent executing {name} tool{text}\n"})
                    yield NS(method=method, payload=NS(item=item))
            # Unknown and non-tool items are not treated as tools; unknown notifications do not emit JSON.
            for kind in ("unknownTool", "plan", "contextCompaction", "subAgentActivity"):
                yield NS(method="item/completed", payload=NS(item=NS(root=NS(
                    id="hidden", type=kind, content="PRIVATE"))))
            for method in ("item/commandExecution/outputDelta", "item/fileChange/outputDelta",
                           "item/mcpToolCall/progress", "hook/started", "hook/completed",
                           "turn/plan/updated", "turn/diff/updated", "unknown/event"):
                yield NS(method=method, payload=NS(item_id="hidden", delta="PRIVATE", message="PRIVATE"))
            # Do not fabricate success for a tool without a completion event.
            yield NS(method="item/started", payload=NS(item=NS(root=NS(
                id="pending", type="commandExecution"))))
            expected.append({"type": "progress", "item_id": "pending",
                             "content": "Agent executing commandExecution tool\n"})
            yield NS(method="turn/completed", payload=NS(turn=NS(
                duration_ms=10, status=TurnStatus.completed)))

        async def tracked_stream():
            async for event in stream():
                received.append(event)
                yield event

        self.handle.stream = tracked_stream
        probe = Mock()
        events = [event async for event in Codex._stream_turn(
            self.live, "user", None, False, datetime.now(timezone.utc), probe)]
        self.assertEqual(events, expected + [{"type": "done"}])
        self.assertNotIn("PRIVATE", str(events))
        self.assertEqual([call.args[0] for call in probe.activity.call_args_list], received)

    async def test_reasoning_delta_variants_reach_gateway_before_answer(self) -> None:
        # Event formats come from the two root-level stream samples; short text verifies forwarding and replacement only.
        source = ast.parse((Path(__file__).resolve().parents[1] / "middleware/gateway_execution.py").read_text())
        reply = next(n for n in ast.walk(source)
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reply_content")
        class CardReplace:
            def __init__(self, content):
                self.content = content
        namespace = dict(aclosing=aclosing, CardReplace=CardReplace,
                         _run_was_interrupted=AsyncMock(return_value=False))
        exec(compile(ast.Module(body=[reply], type_ignores=[]), "gateway_execution.py", "exec"), namespace)
        adapter = NS(runtime=NS(
            codex=namespace.get("Codex"),
            _run_was_interrupted=namespace["_run_was_interrupted"],
        ))

        for method, phase in [("item/reasoning/textDelta", "final_answer"),
                              ("item/reasoning/summaryTextDelta", None)]:
            with self.subTest(method=method):
                async def stream():
                    item = NS(type="reasoning", id="r", content=[], summary=[])
                    yield NS(method="item/started", payload=NS(item=NS(root=item)))
                    for text in ("Reasoning 1", "Reasoning 2"):
                        yield NS(method=method, payload=NS(item_id="r", delta=text,
                                                          summary_index=0, content_index=0))
                    item.summary = ["Reasoning 1Reasoning 2"] if phase is None else []
                    item.content = [] if phase is None else ["Reasoning 1Reasoning 2"]
                    yield NS(method="item/completed", payload=NS(item=NS(root=item)))
                    tool = NS(type="commandExecution", id="tool", command="test command", status="inProgress")
                    yield NS(method="item/started", payload=NS(item=NS(root=tool)))
                    yield NS(method="item/commandExecution/outputDelta", payload=NS(item_id="tool", delta="unique output"))
                    tool.aggregated_output = "unique output plus tail"
                    tool.status = "completed"
                    yield NS(method="item/completed", payload=NS(item=NS(root=tool)))
                    yield NS(method="item/completed", payload=NS(item=NS(root=NS(
                        type="commandExecution", id="fallback", aggregated_output="completion only"))))
                    answer = AgentMessageThreadItem(id="a", phase=phase, text="", type="agentMessage")
                    yield NS(method="item/started", payload=NS(item=NS(root=answer)))
                    yield NS(method="item/agentMessage/delta", payload=NS(item_id="a", delta="Answer"))
                    yield NS(method="item/completed", payload=NS(item=NS(root=answer)))
                    yield NS(method="hook/completed", payload=NS(status="completed"))
                    yield NS(method="turn/completed", payload=NS(turn=NS(
                        duration_ms=10, status=TurnStatus.completed)))
                self.handle.stream = stream
                self.thread.turn = AsyncMock(return_value=self.handle)
                with (patch.object(codex_runtime, "AsyncCodex") as client_class,
                      patch.object(thread_manager, "get_user_thread", AsyncMock(return_value=None)),
                      patch.object(thread_manager, "set_user_thread", AsyncMock()),
                      patch.object(codex_execution, "prepare_workspace", AsyncMock()),
                      patch.object(codex_execution, "SavingLog", AsyncMock())):
                    client_class.return_value.__aenter__.return_value.thread_start.return_value = self.thread
                    events = Codex.running("user", "hello", "reasoning-variants")
                    rendered = [chunk async for chunk in namespace["_reply_content"](adapter,
                        None, None, NS(), events=events)]
                self.assertEqual(rendered[:2], ["Reasoning 1", "Reasoning 2"])
                progress = "".join(rendered[:-1])
                self.assertIn("Agent executing commandExecution tool\n", progress)
                self.assertIn("Agent executing commandExecution tool: succeeded\n", progress)
                self.assertIn("Agent executing commandExecution tool: finished (status unknown)\n", progress)
                for detail in ("test command", "unique output", " plus tail", "completion only"):
                    self.assertNotIn(detail, progress)
                self.assertNotIn("hook/completed", progress)
                self.assertIsInstance(rendered[-1], CardReplace)
                self.assertEqual(rendered[-1].content, "Answer")


class GatewaySteerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        helpers.StopTests.setUp(self)
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.starts = []
        self.cards = []
        self.tasks = []
        async def running(**kwargs):
            self.starts.append(kwargs["prompt"])
            yield {"type": "started"}
            self.started.set()
            yield {"type": "answer", "content": "before"}
            await self.finish.wait()
            yield {"type": "answer", "content": "after"}
        async def card(union_id, content, *, session=None):
            if isinstance(content, str):
                self.cards.append(content)
                return
            chunks = []
            self.cards.append(chunks)
            try:
                async for chunk in content:
                    if isinstance(chunk, self.card_control_type):
                        chunk.ready.set_result(True)
                        if await chunk.decision:
                            chunks = []
                            self.cards.append(chunks)
                        chunk.applied.set_result(True)
                    else:
                        chunks.append(chunk)
            except self.card_module.CardStreamStopped:
                pass
        self.runtime.codex.running = running
        self.runtime.codex.steer.return_value = {"type": "steered"}
        self.runtime.send_card.side_effect = card
        self.execution.assemble_input.side_effect = lambda batch: NS(
            codex_input="|".join(m.content.get("text", "attachment") for m in batch.messages), notices=())

    async def asyncTearDown(self) -> None:
        self.finish.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def batch(self, text, mid, chat="chat-1", items=None):
        return batch_from_chat_history(helpers.event(text, chat=chat, message_id=mid), items or [])

    async def start(self):
        task = asyncio.create_task(self.execution._handle_message_batch(self.batch("first", "m1"), 0))
        self.tasks.append(task)
        await asyncio.wait_for(self.started.wait(), 1)
        return task

    async def test_running_messages_steer_once_and_rotate_cards(self) -> None:
        task = await self.start()
        for index in (2, 3):
            batch = self.batch(f"next-{index}", f"m{index}", items=[
                helpers.history(f"next-{index}", f"m{index}"), helpers.history("first", "m1")])
            await asyncio.wait_for(self.execution._handle_message_batch(batch, 0), 1)
        self.assertFalse(task.done())
        self.assertEqual(self.starts, ["first"])
        self.assertEqual(len(self.cards), 3)
        self.assertEqual([c.args[1] for c in self.runtime.codex.steer.await_args_list], ["next-2", "next-3"])
        owner = self.runtime.cache.active_runs_by_chat["chat-1"]
        self.assertEqual(owner.message_ids, frozenset({"m1", "m2", "m3"}))
        self.finish.set()
        await task
        await self.execution._handle_message_batch(self.batch("fresh", "m4"), 0)
        self.assertEqual(self.starts, ["first", "fresh"])
        self.assertFalse(self.runtime.cache.active_runs_by_message_id)

    async def test_idle_race_drains_old_stream_then_starts_new_turn(self) -> None:
        task = await self.start()
        self.runtime.codex.steer.return_value = {"type": "idle"}
        follow = asyncio.create_task(self.execution._handle_message_batch(self.batch("second", "m2"), 0))
        self.tasks.append(follow)
        await asyncio.sleep(0)
        self.assertEqual(self.starts, ["first"])
        self.finish.set()
        await asyncio.wait_for(asyncio.gather(task, follow), 1)
        self.assertEqual(self.starts, ["first", "second"])

    async def test_new_command_stops_and_never_steers(self) -> None:
        task = await self.start()
        await asyncio.wait_for(self.router.processing(helpers.event("/new", message_id="m2")), 1)
        await asyncio.wait_for(task, 1)
        self.runtime.codex.steer.assert_not_awaited()
        self.runtime.codex.reset_thread.assert_awaited_once_with("user-1")
        self.assertEqual(self.starts, ["first"])
        self.assertIn(CONFIG["messages"]["newThreadCreated"], self.cards)

    async def test_recall_accepted_steer_interrupts_owner_and_cleans_all_reactions(self) -> None:
        task = await self.start()
        await self.execution._handle_message_batch(self.batch("second", "m2"), 0)
        owner = self.runtime.cache.active_runs_by_chat["chat-1"]
        self.runtime.cache.reaction_message_ids = {"chat-1": {"m1": "r1", "m2": "r2"}}
        await self.commands.processing_recall(NS(event=NS(chat_id="chat-1", message_id="m2", recall_type="message_owner")))
        self.runtime.codex.interrupt_and_confirm.assert_awaited_once_with(owner.run_id)
        self.finish.set()
        await task
        self.assertFalse(self.runtime.cache.reaction_message_ids)

    async def test_recall_during_steer_ack_interrupts_original_turn(self) -> None:
        task = await self.start()
        async def steer(*args, **kwargs):
            await self.commands.processing_recall(NS(event=NS(
                chat_id="chat-1", message_id="m2", recall_type="message_owner")))
            return {"type": "steered"}
        self.runtime.codex.steer.side_effect = steer
        owner = self.runtime.cache.active_runs_by_chat["chat-1"]
        await self.execution._handle_message_batch(self.batch("second", "m2"), 0)
        self.assertTrue(owner.interrupted)
        self.runtime.codex.interrupt_and_confirm.assert_awaited_once_with(owner.run_id)
        self.finish.set()
        await task

    async def test_stop_during_steer_prevents_next_old_submission(self) -> None:
        task = await self.start()
        async def steer(*args, **kwargs):
            await self.commands.processing_stop(helpers.event())
            return {"type": "idle"}
        self.runtime.codex.steer.side_effect = steer
        follow = asyncio.create_task(self.execution._handle_message_batch(self.batch("second", "m2"), 0))
        self.tasks.append(follow)
        await asyncio.sleep(0)
        self.finish.set()
        await asyncio.wait_for(asyncio.gather(task, follow), 1)
        self.assertEqual(self.starts, ["first"])

    async def test_failed_steer_does_not_start_or_close_original_stream(self) -> None:
        task = await self.start()
        self.runtime.codex.steer.return_value = {"type": "error", "content": "failed"}
        await self.execution._handle_message_batch(self.batch("second", "m2"), 0)
        self.assertEqual(self.starts, ["first"])
        self.assertFalse(task.done())
        self.assertEqual(self.cards[-1], "failed")

    async def test_repeated_delivery_and_late_reaction_do_not_resubmit(self) -> None:
        self.finish.set()
        await self.router.processing(helpers.event("first", message_id="m1"))
        await self.router.processing(helpers.event("first", message_id="m1"))
        self.assertEqual(self.starts, ["first"])
        self.router.add_reaction.assert_awaited_once()
        await self.execution._handle_message_batch(self.batch("history", "m2"), 0)
        await self.router.processing(helpers.event("history", message_id="m2"))
        self.assertEqual(self.starts, ["first", "history"])
        self.assertFalse(self.runtime.cache.reaction_message_ids)

    async def test_completed_steer_is_filtered_from_next_history(self) -> None:
        task = await self.start()
        await self.execution._handle_message_batch(self.batch("supplement", "m2"), 0)
        self.finish.set()
        await task
        # Even if history has not returned the new card, the accepted input
        # must not be replayed with the next turn.
        batch = self.batch("fresh", "m3", items=[
            helpers.history("fresh", "m3"), helpers.history("supplement", "m2"),
            {"message_id": "old-card", "sender": {"sender_type": "app"}},
            helpers.history("first", "m1"),
        ])
        await self.execution._handle_message_batch(batch, 0)
        self.assertEqual(self.starts, ["first", "fresh"])
        self.runtime.codex.steer.assert_awaited_once()

    def real_card_sender(self):
        """Exercise the real adapter/controller/writer; mock only network I/O."""
        card = self.card_module
        sent = asyncio.Event()
        bodies, writes, closed = [], [], []
        def create(request):
            import json
            bodies.append(json.loads(request.request_body.data))
            return NS(success=lambda: True, data=NS(card_id=f"card-{len(bodies)}"))
        def send(request):
            sent.set()
            return NS(success=lambda: True, data=NS(message_id=f"message-{len(bodies)}"))
        def write(request):
            writes.append((request.card_id, request.request_body.content))
            return NS(success=lambda: True)
        def close(request):
            closed.append(request.card_id)
            return NS(success=lambda: True)
        client = NS(im=NS(v1=NS(message=NS(create=Mock(side_effect=send)))),
                    cardkit=NS(v1=NS(card=NS(create=Mock(side_effect=create), settings=Mock(side_effect=close)),
                                     card_element=NS(content=Mock(side_effect=write)))))
        self.enterContext(patch.object(card, "client", client))
        self.enterContext(patch.object(card, "UPDATE_INTERVAL", 999))
        async def call(operation, request):
            return operation(request)
        self.enterContext(patch.object(card, "_call", side_effect=call))
        self.runtime.send_card.side_effect = card.sending_card
        return card, client, sent, bodies, writes, closed

    async def test_real_sender_rotates_while_silent_and_final_goes_to_new_card(self) -> None:
        card, client, sent, bodies, writes, closed = self.real_card_sender()
        task = await self.start()
        await asyncio.wait_for(sent.wait(), 1)
        owner = self.runtime.cache.active_runs_by_chat["chat-1"]
        await self.execution._handle_message_batch(self.batch("supplement", "m2"), 0)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(closed, ["card-1"])
        self.assertEqual(bodies[1]["body"]["elements"][0]["content"], CONFIG["messages"]["steerAccepted"])
        self.assertIs(self.runtime.cache.active_runs_by_chat["chat-1"], owner)
        self.finish.set()
        await task
        self.assertEqual(writes, [("card-2", "after")])
        self.assertEqual(closed, ["card-1", "card-2"])
        self.assertEqual(self.starts, ["first"])

    async def test_real_sender_completion_during_steer_retains_final_answer(self) -> None:
        card, client, sent, bodies, writes, closed = self.real_card_sender()
        task = await self.start()
        await asyncio.wait_for(sent.wait(), 1)
        async def steer(*args, **kwargs):
            self.finish.set()
            await asyncio.sleep(0)
            return {"type": "steered"}
        self.runtime.codex.steer.side_effect = steer
        await asyncio.wait_for(self.execution._handle_message_batch(self.batch("supplement", "m2"), 0), 1)
        await asyncio.wait_for(task, 1)
        self.assertEqual(writes, [("card-2", "after")])
        self.assertEqual(closed, ["card-1", "card-2"])

    async def test_real_sender_rotation_failure_does_not_replay_or_stop_model(self) -> None:
        card, client, sent, bodies, writes, closed = self.real_card_sender()
        task = await self.start()
        await asyncio.wait_for(sent.wait(), 1)
        client.cardkit.v1.card.create.side_effect = card.CardRequestError("offline")
        await self.execution._handle_message_batch(self.batch("supplement", "m2"), 0)
        self.assertFalse(task.done())
        self.finish.set()
        await task
        self.assertEqual(self.starts, ["first"])
        self.runtime.codex.interrupt_and_confirm.assert_not_awaited()
        self.runtime.codex.steer.assert_awaited_once()

    async def test_real_sender_stop_during_close_never_sends_new_work_card(self) -> None:
        card, client, sent, bodies, writes, closed = self.real_card_sender()
        task = await self.start()
        await asyncio.wait_for(sent.wait(), 1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def call(operation, request):
            if operation is client.cardkit.v1.card.settings:
                entered.set()
                await release.wait()
            return operation(request)
        self.enterContext(patch.object(card, "_call", side_effect=call))
        follow = asyncio.create_task(self.execution._handle_message_batch(self.batch("supplement", "m2"), 0))
        self.tasks.append(follow)
        await asyncio.wait_for(entered.wait(), 1)
        stopped = asyncio.create_task(self.commands.processing_stop(helpers.event(message_id="stop")))
        self.tasks.append(stopped)
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, follow, stopped), 1)
        self.assertEqual(len(bodies), 1)

    async def test_other_chat_can_start_while_first_is_running(self) -> None:
        await self.start()
        other = asyncio.create_task(self.execution._handle_message_batch(self.batch("other", "m2", "chat-2"), 0))
        self.tasks.append(other)
        await asyncio.sleep(0)
        async with asyncio.timeout(1):
            while len(self.starts) < 2:
                await asyncio.sleep(0)
        self.assertEqual(self.starts, ["first", "other"])
        self.runtime.codex.steer.assert_not_awaited()

    async def test_startup_window_does_not_create_two_turns(self) -> None:
        entering, release = asyncio.Event(), asyncio.Event()
        original = self.runtime.codex.running
        async def slow_start(**kwargs):
            entering.set()
            await release.wait()
            async for event in original(**kwargs):
                yield event
        self.runtime.codex.running = slow_start
        first = asyncio.create_task(self.execution._handle_message_batch(self.batch("first", "m1"), 0))
        self.tasks.append(first)
        await asyncio.wait_for(entering.wait(), 1)
        second = asyncio.create_task(self.execution._handle_message_batch(self.batch("second", "m2"), 0))
        self.tasks.append(second)
        await asyncio.sleep(0)
        self.runtime.codex.steer.assert_not_awaited()
        release.set()
        await asyncio.wait_for(second, 1)
        self.assertEqual(self.starts, ["first"])
        self.runtime.codex.steer.assert_awaited_once()

    async def test_finish_during_steer_ack_keeps_message_cleanup(self) -> None:
        task = await self.start()
        self.runtime.cache.reaction_message_ids = {"chat-1": {"m1": "r1", "m2": "r2"}}
        async def steer(*args, **kwargs):
            self.finish.set()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            return {"type": "steered"}
        self.runtime.codex.steer.side_effect = steer
        await self.execution._handle_message_batch(self.batch("second", "m2"), 0)
        await asyncio.wait_for(task, 1)
        self.assertFalse(self.runtime.cache.reaction_message_ids)
        self.assertFalse(self.runtime.cache.active_runs_by_message_id)
        self.assertEqual(self.runtime.delete_reaction.await_count, 2)

    async def test_buffered_attachment_flush_steers_running_turn(self) -> None:
        await self.start()
        self.router.buffer_seconds = lambda: 0
        data = helpers.event("image", message_id="m2")
        data.event.message.message_type = "image"
        await self.router.processing(data)
        await asyncio.wait_for(self.runtime.cache.buffer_tasks["chat-1"], 1)
        self.runtime.codex.steer.assert_awaited_once()
        self.assertEqual(self.starts, ["first"])

    async def test_gateway_backend_and_real_sdk_handles_share_turn_and_stream(self) -> None:
        queue = Queue()
        subscription = NS(next=lambda: queue.get(timeout=2), close=Mock())
        entered = asyncio.Event()
        async def start_turn(*args, **kwargs):
            entered.set()
            return NS(turn=NS(id="turn-1")), subscription
        low = NS(
            # Installed SDK 0.154.0 returns a separate subscription for each handle.
            _start_turn=AsyncMock(side_effect=start_turn),
            thread_read=AsyncMock(return_value=status("active")),
            turn_steer=AsyncMock(return_value=NS(turn_id="turn-1")),
        )
        runtime = NS(_client=low, _ensure_initialized=AsyncMock())
        thread = AsyncThread(runtime, "thread-1")
        runtime.thread_start = AsyncMock(return_value=thread)
        self.runtime.codex = Codex
        with (patch.object(codex_runtime, "AsyncCodex") as client_class,
              patch.object(session_history, "register_session", AsyncMock()),
              patch.object(session_codex, "_schedule_session_title"),
              patch.object(session_codex, "_sync_session_time", AsyncMock()),
              patch.object(thread_manager, "get_user_thread", AsyncMock(return_value=None)),
              patch.object(thread_manager, "set_user_thread", AsyncMock()),
              patch.object(codex_execution, "prepare_workspace", AsyncMock()),
              patch.object(codex_execution, "SavingLog", AsyncMock()) as saving,
              patch.object(Codex, "_live_turns", {}),
              patch.object(Codex, "_active_turns", {}),
              patch.object(Codex, "_pending_interrupts", set())):
            client_class.return_value.__aenter__.return_value = runtime
            task = asyncio.create_task(self.execution._handle_message_batch(self.batch("first", "m1"), 0))
            self.tasks.append(task)
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await self.execution._handle_message_batch(self.batch("second", "m2"), 0)
                low.thread_read.assert_awaited_once_with("thread-1", include_turns=False)
                low.turn_steer.assert_awaited_once()
                args = low.turn_steer.call_args.args
                self.assertEqual(args[:2], ("thread-1", "turn-1"))
                self.assertEqual(args[2][0]["text"], "second")
                low._start_turn.assert_awaited_once()
                self.assertEqual(len(self.cards), 2)
            finally:
                completed = TurnCompletedNotification.model_validate({
                    "threadId": "thread-1", "turn": {
                        "id": "turn-1", "status": "completed", "items": [],
                        "error": None, "startedAt": None, "completedAt": None,
                        "durationMs": 10, "itemsView": "summary",
                    },
                })
                queue.put(NS(method="turn/completed", payload=completed))
                await asyncio.wait_for(task, 1)
            saving.assert_not_awaited()
            subscription.close.assert_called_once_with()


class CollectorSteerTests(unittest.TestCase):
    def test_newer_message_in_history_is_not_submitted_by_older_event(self) -> None:
        batch = batch_from_chat_history(helpers.event("first", message_id="m1"), [
            helpers.history("second", "m2"), helpers.history("first", "m1")])
        self.assertEqual([m.message_id for m in batch.messages], ["m1"])


if __name__ == "__main__":
    unittest.main()
