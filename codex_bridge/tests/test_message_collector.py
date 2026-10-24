"""History ordering, eventual consistency and control-message boundaries."""
from __future__ import annotations
import copy
import json
from types import SimpleNamespace as NS
from codex_bridge.middleware.message_collector import CollectedMessage, MessageBatch, batch_from_chat_history, is_new_command, is_stop_command

def event(message_id='current', chat_type='p2p', create_time='20'):
    return NS(event=NS(message=NS(message_id=message_id, chat_id='chat', chat_type=chat_type, message_type='text', content='{"text":"current"}', create_time=create_time), sender=NS(sender_id=NS(union_id='user'))))

def history(message_id, sender='user', **kwargs):
    return dict(message_id=message_id, sender={'sender_type': sender}, msg_type='text', body={'content': json.dumps({'text': message_id})}, **kwargs)

class TestMessageCollector:

    def test_post_files_survive_history_and_event_fallback(self) -> None:
        content = {'title': '', 'content': [[{'tag': 'text', 'text': 'ddd'}]], 'files': [{'file_key': 'workbook', 'file_name': 'Work plan.xlsx', 'is_folder': False}]}
        incoming = event()
        incoming.event.message.message_type = 'post'
        incoming.event.message.content = json.dumps(content, ensure_ascii=False)
        item = history('current')
        item['msg_type'] = 'post'
        item['body']['content'] = incoming.event.message.content
        for items in ([item], []):
            result = batch_from_chat_history(incoming, items)
            assert len(result.messages) == 1
            assert result.messages[0].message_type == 'post'
            assert result.messages[0].content == content

    def test_history_is_chronological_and_stops_at_app_reply(self) -> None:
        items = [history('current'), history('older'), history('reply', 'app'), history('stale')]
        original = copy.deepcopy(items)
        result = batch_from_chat_history(event(), items)
        assert [m.message_id for m in result.messages] == ['older', 'current']
        assert [m.sequence for m in result.messages] == [1, 2]
        assert items == original

    def test_deleted_unknown_sender_and_unsupported_messages_are_skipped(self) -> None:
        unsupported = history('unsupported')
        unsupported['msg_type'] = 'sticker'
        items = [history('current'), history('deleted', deleted=True), unsupported, history('system', 'system'), history('kept')]
        result = batch_from_chat_history(event(), items)
        assert [m.message_id for m in result.messages] == ['kept', 'current']

    def test_missing_event_is_inserted_once_after_older_history(self) -> None:
        result = batch_from_chat_history(event(), [history('older')])
        assert [m.message_id for m in result.messages] == ['older', 'current']
        assert result.messages[-1].create_time == '20'

    def test_event_anchor_excludes_later_messages_and_app_replies(self) -> None:
        result = batch_from_chat_history(event(), [history('later', 'app'), history('current'), history('older')])
        assert [m.message_id for m in result.messages] == ['older', 'current']

    def test_missing_anchor_uses_numeric_timestamp_not_lexical_order(self) -> None:
        result = batch_from_chat_history(event(), [history('future', create_time='100'), history('equal', create_time='20'), history('older', create_time='9')])
        assert [m.message_id for m in result.messages] == ['older', 'equal', 'current']

    def test_nonnumeric_timestamp_does_not_crash_or_drop_history(self) -> None:
        result = batch_from_chat_history(event(), [history('older', create_time='unknown')])
        assert [m.message_id for m in result.messages] == ['older', 'current']

    def test_sdk_objects_and_dict_history_have_same_result(self) -> None:
        item = history('current', create_time='20')
        sdk = NS(**dict(item, sender=NS(**item['sender']), body=NS(**item['body'])))
        assert batch_from_chat_history(event(), [item]) == batch_from_chat_history(event(), [sdk])

    def test_group_recipient_is_chat_and_direct_recipient_is_user(self) -> None:
        for kind, target in (('p2p', 'user'), ('group', 'chat')):
            result = batch_from_chat_history(event(chat_type=kind), [])
            assert (result.chat_id, result.union_id, result.chat_type) == ('chat', target, kind)

    def test_bad_json_is_retained_as_raw_content(self) -> None:
        for raw in ('{broken', None):
            item = history('current')
            item['body']['content'] = raw
            assert batch_from_chat_history(event(), [item]).messages[0].content == {'raw': raw}

    def test_deleted_control_message_does_not_cut_off_history(self) -> None:
        command = history('deleted', deleted=True)
        command['body']['content'] = '{"text":"/stop"}'
        result = batch_from_chat_history(event(), [history('current'), command, history('older')])
        assert [m.message_id for m in result.messages] == ['older', 'current']

    def test_commands_reject_nonobject_nontext_and_malformed_payloads(self) -> None:
        for command in (is_new_command, is_stop_command):
            for raw in (None, '[]', 'null', '"/new"', '1', 'true', '{', '{"text":null}'):
                assert not command('text', raw)

    def test_batch_properties_include_unique_unsupported_types(self) -> None:
        empty = MessageBatch('c', 'u', 'p2p', ())
        assert empty.unsupported_message_types == set()
        batch = MessageBatch('c', 'u', 'p2p', tuple((CollectedMessage(str(i), kind, {}, i) for i, kind in enumerate(('text', 'sticker', 'sticker', 'video')))))
        assert batch.unsupported_message_types == {'sticker', 'video'}
