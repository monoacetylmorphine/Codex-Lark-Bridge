"""Exercise the real assembly pipeline with only download and ASR I/O mocked."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, patch

from openai_codex import LocalImageInput, MentionInput, TextInput
from bridge.middleware.message_collector import CollectedMessage, MessageBatch
from bridge.configs.loader import CONFIG


def batch(*messages):
    return MessageBatch("chat", "user", "p2p", tuple(messages))


def message(kind, content, sequence=1):
    return CollectedMessage(f"m{sequence}", kind, content, sequence)


class MessageAssemblyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # A private module avoids leaking stubbed imports into other test files.
        name = "assembly_pipeline_tests"
        spec = importlib.util.spec_from_file_location(name,
            Path(__file__).resolve().parents[1] / "middleware/message_assemble.py")
        self.assembly = importlib.util.module_from_spec(spec)
        stub = ModuleType("bridge.services.lark.lark_tools")
        self.download = stub.download_msg_resource = AsyncMock()
        with patch.dict(sys.modules, {name: self.assembly, stub.__name__: stub}):
            spec.loader.exec_module(self.assembly)
        self.transcribe = self.enterContext(patch.object(self.assembly.ASR, "transfer", new_callable=AsyncMock))

    async def assemble(self, *messages):
        return await self.assembly.assemble_codex_input(batch(*messages))

    async def test_single_text_is_trimmed_without_any_external_io(self) -> None:
        result = await self.assemble(message("text", {"text": "  Hello, café\n"}))
        self.assertEqual(result.codex_input, "Hello, café")
        self.assertEqual(result.notices, ())
        self.download.assert_not_awaited()
        self.transcribe.assert_not_awaited()

    async def test_multiple_texts_follow_sequence_not_tuple_order(self) -> None:
        result = await self.assemble(message("text", {"text": "second"}, 2), message("text", {"text": "first"}))
        self.assertTrue(all(isinstance(item, TextInput) for item in result.codex_input))
        self.assertEqual([item.text for item in result.codex_input], ["first", "second"])

    async def test_empty_and_nonstring_text_raise_user_facing_error(self) -> None:
        for messages in ((), (message("text", {}),), (message("text", {"text": " \n"}),),
                         (message("text", {"text": 7}),)):
            with self.subTest(messages=messages), self.assertRaises(self.assembly.InputAssemblyError) as caught:
                await self.assemble(*messages)
            self.assertEqual(caught.exception.user_message, CONFIG["messages"]["emptyInput"])

    async def test_unsupported_message_type_fails_before_download(self) -> None:
        with self.assertRaisesRegex(self.assembly.InputAssemblyError, "sticker, video"):
            await self.assemble(message("video", {}), message("sticker", {}, 2))
        self.download.assert_not_awaited()

    async def test_images_and_documents_use_real_sdk_input_types(self) -> None:
        self.download.side_effect = ["/tmp/PHOTO.PNG", "/tmp/report.PDF"]
        result = await self.assemble(message("image", {"image_key": "image"}),
                                     message("file", {"file_key": "file", "file_name": "report.PDF"}, 2))
        image, document = result.codex_input
        self.assertIsInstance(image, LocalImageInput)
        self.assertEqual(image.path, str(Path("/tmp/PHOTO.PNG").resolve()))
        self.assertIsInstance(document, MentionInput)
        self.assertEqual((document.name, document.path), ("report.PDF", str(Path("/tmp/report.PDF").resolve())))
        self.assertEqual([call.kwargs for call in self.download.await_args_list], [
            dict(union_id="user", message_id="m1", resource_key="image", resource_type="image"),
            dict(union_id="user", message_id="m2", resource_key="file", resource_type="file")])

    async def test_post_keeps_title_text_image_order_and_skips_invalid_nodes(self) -> None:
        for field in ("content", "content_v2"):
            with self.subTest(field=field):
                self.download.return_value = "/tmp/photo.png"
                result = await self.assemble(message("post", {"title": " title ", field: [
                    "bad row", [None, {"tag": "text", "text": "before"},
                    {"tag": "img", "image_key": "photo"}, {"tag": "text", "text": "after"},
                    {"tag": "at", "user_id": "ignored"}]]}))
                self.assertEqual([type(item) for item in result.codex_input],
                                 [TextInput, TextInput, LocalImageInput, TextInput])
                self.assertEqual([item.text for item in result.codex_input if isinstance(item, TextInput)],
                                 ["title", "before", "after"])

    async def test_content_v2_takes_precedence_over_legacy_content(self) -> None:
        result = await self.assemble(message("post", {
            "content_v2": [[{"tag": "text", "text": "new"}]],
            "content": [[{"tag": "text", "text": "old"}]]}))
        self.assertEqual(result.codex_input, "new")

    async def test_missing_resource_key_prevents_text_only_submission(self) -> None:
        for kind, content in (("image", {}), ("file", {"file_key": 123}), ("audio", {"file_key": ""})):
            with self.subTest(kind=kind):
                result = await self.assemble(message("text", {"text": "summarize"}), message(kind, content, 2))
                self.assertIsNone(result.codex_input)
                self.assertIn(CONFIG["messages"]["missingResourceKeySuffix"], result.notices[0])
        self.download.assert_not_awaited()

    async def test_failed_download_or_unsupported_extension_discards_whole_task(self) -> None:
        for response in (None, "", RuntimeError("offline"), "/tmp/program.exe"):
            with self.subTest(response=response):
                self.download.side_effect = [response]
                result = await self.assemble(message("text", {"text": "inspect"}),
                    message("file", {"file_key": "f", "file_name": "program.exe"}, 2))
                self.assertIsNone(result.codex_input)
                self.assertIn("Neither attachments nor the text task were submitted", result.notices[0])

    async def test_partial_success_keeps_text_and_valid_attachment(self) -> None:
        self.download.side_effect = [RuntimeError("offline"), "/tmp/ok.png"]
        result = await self.assemble(message("text", {"text": "inspect"}),
            message("file", {"file_key": "bad", "file_name": "bad.pdf"}, 2),
            message("image", {"image_key": "ok"}, 3))
        self.assertEqual([type(item) for item in result.codex_input], [TextInput, LocalImageInput])
        self.assertEqual(result.codex_input[0].text, "inspect")
        self.assertIn("bad.pdf", result.notices[0])
        self.assertNotIn("offline", result.notices[0])

    async def test_voice_and_audio_file_become_text_not_file_mentions(self) -> None:
        self.download.side_effect = ["/tmp/voice.bin", "/tmp/upload.MP3"]
        self.transcribe.side_effect = [" first ", " second\n"]
        result = await self.assemble(message("audio", {"file_key": "v"}), message("file", {"file_key": "f"}, 2))
        self.assertTrue(all(isinstance(item, TextInput) for item in result.codex_input))
        self.assertEqual([item.text for item in result.codex_input], ["first", "second"])
        self.assertEqual([call.args[0] for call in self.transcribe.await_args_list],
                         [Path("/tmp/voice.bin").resolve(), Path("/tmp/upload.MP3").resolve()])

    async def test_empty_failed_and_timed_out_transcription_stay_in_notices(self) -> None:
        for response, expected in ((" \n", CONFIG["messages"]["emptyTranscriptionSuffix"]),
            (RuntimeError("private detail"), CONFIG["messages"]["transcriptionFailedSuffix"]),
            (self.assembly.AudioConversionTimeout("conversion deadline"), "conversion deadline")):
            with self.subTest(response=response):
                self.download.return_value = "/tmp/a.ogg"
                self.transcribe.side_effect = [response]
                result = await self.assemble(message("audio", {"file_key": "a"}))
                self.assertIsNone(result.codex_input)
                self.assertIn(expected, result.notices[0])
                self.assertNotIn("private detail", result.notices[0])

    async def test_successful_transcription_survives_other_attachment_failure(self) -> None:
        self.download.side_effect = ["/tmp/a.ogg", None]
        self.transcribe.return_value = "recognized"
        result = await self.assemble(message("audio", {"file_key": "a"}), message("image", {"image_key": "bad"}, 2))
        self.assertEqual(result.codex_input, "recognized")
        self.assertEqual(len(result.notices), 1)

    async def test_repeated_rejection_names_are_reported_once(self) -> None:
        result = await self.assemble(message("file", {"file_name": "same.pdf"}),
                                     message("file", {"file_name": "same.pdf"}, 2))
        self.assertEqual(result.notices[0].count("same.pdf"), 1)

    async def test_parallel_download_completion_cannot_reorder_input(self) -> None:
        second_done = asyncio.Event()
        async def download(**kwargs):
            if kwargs["resource_key"] == "first":
                await second_done.wait()
                return "/tmp/first.png"
            second_done.set()
            return "/tmp/second.png"
        self.download.side_effect = download
        result = await asyncio.wait_for(self.assemble(message("image", {"image_key": "first"}),
            message("image", {"image_key": "second"}, 2)), 2)
        self.assertEqual([Path(item.path).name for item in result.codex_input], ["first.png", "second.png"])

    async def test_cancelling_assembly_cancels_pending_downloads(self) -> None:
        all_started = asyncio.Event()
        started, finished = set(), set()
        async def download(**kwargs):
            key = kwargs["resource_key"]
            started.add(key)
            if len(started) == 2:
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.add(key)
        self.download.side_effect = download
        task = asyncio.create_task(self.assemble(message("image", {"image_key": "a"}),
            message("image", {"image_key": "b"}, 2)))
        try:
            await asyncio.wait_for(all_started.wait(), 2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(finished, {"a", "b"})
        self.transcribe.assert_not_awaited()

    async def test_post_attachment_limit_before_download(self) -> None:
        nodes = [[{'tag': 'img', 'image_key': f'img-{i}'} for i in range(11)]]
        with self.assertRaisesRegex(self.assembly.InputAssemblyError, 'at most 10 attachments'):
            await self.assemble(message('post', {'content': nodes, 'content_v2': nodes}))
        self.download.assert_not_awaited()
        self.transcribe.assert_not_awaited()

    async def test_post_dual_content_counts_once_at_configured_boundary(self) -> None:
        nodes = [[{'tag': 'img', 'image_key': 'img'}, {'tag': 'text', 'text': 'ssssssss'}]]
        self.download.return_value = '/tmp/photo.png'
        with patch.dict(CONFIG['messaging'], historyPageSize=1):
            result = await self.assemble(message('post', {'content': nodes, 'content_v2': nodes}))
        self.download.assert_awaited_once()
        self.assertEqual(len(result.codex_input), 2)

    async def test_image_and_file_share_one_limit(self) -> None:
        with patch.dict(CONFIG['messaging'], historyPageSize=1):
            with self.assertRaisesRegex(self.assembly.InputAssemblyError, 'at most 1 attachments'):
                await self.assemble(message('image', {'image_key': 'i'}),
                                    message('file', {'file_key': 'f'}, 2))
        self.download.assert_not_awaited()

    async def test_post_file_reference_downloads_once_with_parent_message_id(self) -> None:
        name = "Weekly summary&next week plan.xlsx"
        rows = [[{"tag": "text", "text": "ddd", "style": []}]]
        self.download.return_value = f"/tmp/{name}"
        with patch.dict(CONFIG['messaging'], historyPageSize=1):
            result = await self.assemble(message("post", {
                "title": "", "content": rows, "content_v2": rows,
                "files": [{"file_key": "workbook", "file_name": name, "is_folder": False}],
            }))
        text, attachment = result.codex_input
        self.assertEqual(text.text, "ddd")
        self.assertIsInstance(attachment, MentionInput)
        self.assertEqual(attachment.name, name)
        self.assertEqual(attachment.path, str(Path(f"/tmp/{name}").resolve()))
        self.assertEqual(result.notices, ())
        self.download.assert_awaited_once_with(
            union_id="user", message_id="m1", resource_key="workbook", resource_type="file")

    async def test_post_mixed_resources_keep_body_then_file_order(self) -> None:
        self.download.side_effect = ["/tmp/image.png", "/tmp/first.pdf", "/tmp/second.xlsx"]
        result = await self.assemble(message("post", {
            "title": "title", "content": [[{"tag": "img", "image_key": "image"},
                {"tag": "text", "text": "body"}]],
            "files": [{"file_key": "first"}, {"file_key": "second"}],
        }))
        self.assertEqual([type(item) for item in result.codex_input],
                         [TextInput, LocalImageInput, TextInput, MentionInput, MentionInput])
        self.assertEqual([item.name for item in result.codex_input if isinstance(item, MentionInput)],
                         ["first.pdf", "second.xlsx"])

    async def test_post_files_without_body(self) -> None:
        self.download.return_value = "/tmp/only.pdf"
        result = await self.assemble(message("post", {"files": [{"file_key": "only"}]}))
        self.assertEqual(len(result.codex_input), 1)
        self.assertIsInstance(result.codex_input[0], MentionInput)

    async def test_no_file_message_uses_text_type(self) -> None:
        for extra in ({}, {"files": []}):
            with self.subTest(extra=extra):
                result = await self.assemble(message("text", {"text": "ddd", **extra}))
                self.assertEqual(result.codex_input, "ddd")
                self.assertEqual(result.notices, ())
        self.download.assert_not_awaited()

    async def test_post_empty_files_preserves_existing_images(self) -> None:
        self.download.return_value = "/tmp/image.png"
        result = await self.assemble(message("post", {
            "content": [[{"tag": "img", "image_key": "image"}]], "files": [],
        }))
        self.assertIsInstance(result.codex_input[0], LocalImageInput)

    async def test_invalid_post_files_do_not_submit_text_alone(self) -> None:
        for files, reason in (
            ({}, "invalid files format"), ("invalid", "invalid files format"),
            ([None], "invalid attachment format"),
            ([{"file_name": "missing.pdf"}], CONFIG['messages']['missingResourceKeySuffix']),
            ([{"file_key": 123}], CONFIG['messages']['missingResourceKeySuffix']),
            ([{"file_key": "folder", "is_folder": True}], "folders are not supported"),
            ([{"file_key": "folder", "is_folder": "true"}], "invalid is_folder format"),
        ):
            with self.subTest(files=files):
                result = await self.assemble(message("post", {
                    "content": [[{"tag": "text", "text": "inspect"}]], "files": files,
                }))
                self.assertIsNone(result.codex_input)
                self.assertIn(reason, result.notices[0])
        self.download.assert_not_awaited()

    async def test_post_folder_rejection_keeps_valid_file_and_text(self) -> None:
        self.download.return_value = "/tmp/ok.pdf"
        result = await self.assemble(message("post", {
            "content": [[{"tag": "text", "text": "inspect"}]],
            "files": [{"file_key": "folder", "file_name": "folder", "is_folder": True},
                      {"file_key": "ok", "file_name": 123}],
        }))
        self.assertEqual([type(item) for item in result.codex_input], [TextInput, MentionInput])
        self.assertIn("folders are not supported", result.notices[0])
        self.download.assert_awaited_once_with(
            union_id="user", message_id="m1", resource_key="ok", resource_type="file")

    async def test_post_file_failures_discard_text_when_none_usable(self) -> None:
        for response in (None, RuntimeError("offline"), "/tmp/program.exe"):
            with self.subTest(response=response):
                self.download.side_effect = [response]
                result = await self.assemble(message("post", {
                    "content": [[{"tag": "text", "text": "inspect"}]],
                    "files": [{"file_key": "file", "file_name": "attachment"}],
                }))
                self.assertIsNone(result.codex_input)
                self.assertIn("Neither attachments nor the text task were submitted", result.notices[0])

    async def test_post_files_and_images_share_limit_including_rejections(self) -> None:
        for files in ([{"file_key": "a"}, {"file_key": "b"}],
                      [{"file_key": "a"}, {"is_folder": True}]):
            with self.subTest(files=files), patch.dict(CONFIG['messaging'], historyPageSize=2):
                with self.assertRaisesRegex(self.assembly.InputAssemblyError, "at most 2 attachments"):
                    await self.assemble(message("post", {
                        "content": [[{"tag": "img", "image_key": "image"}]], "files": files,
                    }))
        self.download.assert_not_awaited()

    async def test_validation_errors_retain_cause_instead_of_network_failure(self) -> None:
        from bridge.middleware.resource_validator import ResourceValidationError
        self.download.side_effect = ResourceValidationError('Office container is corrupt or lacks required metadata')
        result = await self.assemble(message('file', {'file_key': 'key', 'file_name': 'report.docx'}))
        self.assertIsNone(result.codex_input)
        self.assertIn('Office container is corrupt', result.notices[0])
        self.assertNotIn(CONFIG['messages']['downloadFailedSuffix'], result.notices[0])
