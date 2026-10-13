import os
import asyncio
import json
import uuid
import base64
from typing import Dict, Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.api.cardkit.v1 import *
from lark_oapi.api.contact.v3 import *

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)


feishu_client = lark.Client.builder() \
    .app_id(os.getenv("cli_a976d2e502b9dcb6")) \
    .app_secret(os.getenv("CFHXjN6mkUE1KXDIfckVJeSRQVD2ywiC")) \
    .log_level(lark.LogLevel.DEBUG) \
    .build()


messages_buffer = {}
semaphore = asyncio.Semaphore(10)


def parse_requests(data):
    # 有@
    if data.event.message.mentions:
        print(
            f"data第一层\n{json.dumps(data.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        print(
            f"event第一层\n{json.dumps(data.event.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        print(
            f"sender第一层\n{json.dumps(data.event.sender.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        print(
            f"sender第二层(sender_id):\n{json.dumps(data.event.sender.sender_id.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        print(
            f"message第一层\n{json.dumps(data.event.message.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        for i in range(len(data.event.message.mentions)):
            print(
                f"message第二层(mentions):\n{json.dumps(data.event.message.mentions[i].__dict__, indent=2, ensure_ascii=False, default=str)}\n")
            print(
                f"message第三层(id):\n{json.dumps(data.event.message.mentions[i].id.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
    # 无@
    else:
        print(
            f"data第一层\n{json.dumps(data.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        print(
            f"event第一层\n{json.dumps(data.event.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        print(
            f"sender第一层\n{json.dumps(data.event.sender.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        print(
            f"sender第二层(sender_id):\n{json.dumps(data.event.sender.sender_id.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
        print(
            f"message第一层\n{json.dumps(data.event.message.__dict__, indent=2, ensure_ascii=False, default=str)}\n")


def parse_group_requests(response):
    print(json.dumps(response.__dict__, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(response.data.__dict__,
          indent=2, ensure_ascii=False, default=str))
    for i in range(len(response.data.items)):
        print(json.dumps(
            response.data.items[i].__dict__, indent=2, ensure_ascii=False, default=str))
        print(json.dumps(
            response.data.items[i].sender.__dict__, indent=2, ensure_ascii=False, default=str))
        print(json.dumps(
            response.data.items[i].body.__dict__, indent=2, ensure_ascii=False, default=str))


async def delayed_process(chat_id, buffer_time):
    try:
        await asyncio.sleep(buffer_time)

        chat_content = messages_buffer.get(chat_id)
        if not chat_content:
            return

        session_content = dict(chat_content)
        session_content["messages"] = chat_content["messages"]

        print(f"获取: {session_content['messages']}")

        # await reply_requests(request_info=session_content)

        if chat_id in messages_buffer:
            messages_buffer[chat_id]["messages"] = []
            messages_buffer[chat_id]["task"] = None

    except asyncio.CancelledError:
        print("用户继续输入，重置消息缓冲时间")


def whether_bot_working(data) -> bool:

    if data.event.message.chat_type == "group" and data.event.message.mentions:
        for mention in data.event.message.mentions:
            if mention.name == os.getenv("FEISHU_ROBOT_NAME"):
                # if mention.id.open_id == "ou_05884e6c7172f0520a72a1941664b024":
                return True

    return False


async def process_requests(data):

    try:
        parse_requests(data)

        if data.event.message.chat_type == "p2p":
            chat_id = data.event.message.chat_id

            user_message = {
                "message_id": data.event.message.message_id,
                "type": data.event.message.message_type,
                "content": json.loads(data.event.message.content),
            }

            if chat_id not in messages_buffer:
                messages_buffer[chat_id] = {
                    "messages": [],
                    "open_id": data.event.sender.sender_id.open_id,
                    "chat_type": "p2p",
                    "task": None,
                    "last_ai_reply_index": -1,
                }

            messages_buffer[chat_id]["messages"].append(user_message)

            old_task = messages_buffer[chat_id]["task"]
            if old_task:
                old_task.cancel()
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass

            buffer_time = 15 if messages_buffer[chat_id]["messages"][-1]["type"] == "image" else 2
            messages_buffer[chat_id]["task"] = asyncio.create_task(
                delayed_process(chat_id=chat_id, buffer_time=buffer_time))

        if data.event.message.chat_type == "group":
            chat_id = data.event.message.chat_id

            user_message = {
                "message_id": data.event.message.message_id,
                "type": data.event.message.message_type,
                "content": json.loads(data.event.message.content)
            }

            if chat_id not in messages_buffer:
                messages_buffer[chat_id] = {
                    "messages": [],
                    "open_id": chat_id,
                    "message_id": data.event.message.message_id,
                    "chat_type": "group",
                    "task": None,
                    "last_ai_reply_index": -1,
                }

            messages_buffer[chat_id]["messages"].append(user_message)
            messages_buffer[chat_id]["messages"] = messages_buffer[chat_id]["messages"][-5:]

            request_info = dict(messages_buffer[chat_id])
            request_info["messages"] = messages_buffer[chat_id]["messages"][:]

            print(f"获取: {request_info}")

            if whether_bot_working(data):
                asyncio.create_task(
                    reply_requests(request_info=request_info)
                )

            messages_buffer[chat_id]["messages"] = [
                msg for msg in messages_buffer[chat_id]["messages"]
                if msg["type"] != "image"
            ]

    except Exception as e:
        print(f"解析消息异常!: {e}")


async def rebuild_messages(request_info: Dict[str, Any]):

    async def image_key_to_base64(message_id: str, image_key: str):

        try:
            request: GetMessageResourceRequest = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()

            response: GetMessageResourceResponse = await asyncio.to_thread(feishu_client.im.v1.message_resource.get, request)

            image_bytes = response.file.getvalue()
            img_base64 = base64.b64encode(image_bytes).decode('utf-8')

            return img_base64

        except Exception as e:
            print(f"解码失败: {e}")
            return None

    image_messages = []
    image_tasks = []
    for msg in request_info["messages"]:
        if msg["type"] == "image":
            image_messages.append(msg)
            image_tasks.append(
                image_key_to_base64(msg["message_id"],
                                    msg["content"]["image_key"])
            )
    results = await asyncio.gather(*image_tasks)

    id_url_mapping = {}
    for msg, base64_str in zip(image_messages, results):
        if base64_str:
            id_url_mapping[msg["message_id"]
                           ] = f"data:image/jpeg;base64,{base64_str}"

    if request_info["chat_type"] == "p2p":
        open_id = request_info["open_id"]
        chat_type = request_info["chat_type"]

        if "image" in [msg["type"] for msg in request_info["messages"]]:
            messages = []
            content = []
            for msg in request_info["messages"]:
                if msg["type"] == "text":
                    text = msg["content"].get("text")
                    if text:
                        content.append({
                            "type": "input_text",
                            "text": text
                        })

                elif msg["type"] == "image":
                    image_url = id_url_mapping.get(msg["message_id"])
                    if image_url:
                        content.append({
                            "type": "input_image",
                            "image_url": image_url
                        })

            if content:
                messages.append({"role": "user", "content": content})
            return open_id, messages, chat_type

        else:
            messages = [
                {"role": "user", "content": request_info["messages"][-1]["content"].get("text", "")}]
            return open_id, messages, chat_type

    if request_info["chat_type"] == "group":
        open_id = request_info["open_id"]
        chat_type = request_info["chat_type"]

        if "image" in [msg["type"] for msg in request_info["messages"]]:
            messages = []
            content = []
            for msg in request_info["messages"]:
                if msg["type"] == "text":
                    text = msg["content"].get("text")
                    if text:
                        content.append({
                            "type": "input_text",
                            "text": text
                        })

                elif msg["type"] == "image":
                    image_url = id_url_mapping.get(msg["message_id"])
                    if image_url:
                        content.append({
                            "type": "input_image",
                            "image_url": image_url
                        })

            if content:
                messages.append({"role": "user", "content": content})

            return open_id, messages, chat_type

        else:
            history = "\n".join(msg["content"].get("text", "")
                                for msg in request_info["messages"])

            messages = [{"role": "user", "content": history}]

            return open_id, messages, chat_type


# async def reply_requests(request_info: Dict[str, Any]):

#     async with semaphore:

#         print(f"传达: {request_info}")

#         open_id, messages, chat_type = await rebuild_messages(request_info)

#         if not (open_id and messages):
#             return

#         # 创建卡片请求
#         request = CreateCardRequest.builder() \
#             .request_body(CreateCardRequestBody.builder()
#                           .type("template")
#                           .data(json.dumps({"template_id": "AAqtgslIXyZvt", "template_variable": {}, "template_version_name": "1.0.7"}))
#                           .build()) \
#             .build()
#         response = await asyncio.to_thread(feishu_client.cardkit.v1.card.create, request)
#         print(f"创建卡片请求: {response.code} - {response.msg}")
#         if response.code != 0 or not response.data.card_id:
#             return
#         card_id = response.data.card_id

#         # 配置卡片请求
#         count = 1
#         request = SettingsCardRequest.builder() \
#             .card_id(card_id) \
#             .request_body(SettingsCardRequestBody.builder()
#                           .settings(json.dumps({
#                               "config": {
#                                   "streaming_mode": True,
#                                   "streaming_config": {
#                                       "print_frequency_ms": {"default": 70, "android": 70, "ios": 70, "pc": 70},
#                                       "print_step": {"default": 1, "android": 1, "ios": 1, "pc": 1},
#                                       "print_strategy": "fast",
#                                   }
#                               }
#                           }))
#                           .uuid(str(uuid.uuid4()))
#                           .sequence(count)
#                           .build()) \
#             .build()
#         response = await asyncio.to_thread(feishu_client.cardkit.v1.card.settings, request)
#         print(f"配置卡片请求: {response.code} - {response.msg}")
#         count += 1

#         # 发送卡片请求
#         request = CreateMessageRequest.builder() \
#             .receive_id_type("chat_id" if chat_type == "group" else "open_id") \
#             .request_body(CreateMessageRequestBody.builder()
#                           .receive_id(open_id)
#                           .msg_type("interactive")
#                           .content(json.dumps({"type": "card", "data": {"card_id": card_id}}))
#                           .uuid(str(uuid.uuid4()))
#                           .build()) \
#             .build()
#         response = await asyncio.to_thread(feishu_client.im.v1.message.create, request)
#         print(f"发送卡片请求: {response.code} - {response.msg}")

#         chunks = []
#         last_update_time = 0
#         loop = asyncio.get_running_loop()

#         try:
#             async with asyncio.timeout(300):

#                 async for token in MAS.stream_generator(
#                     query=messages,
#                     session_id=open_id,
#                     collection_name=["disney"]
#                 ):

#                     if token is None:
#                         continue

#                     chunks.append(token)

#                     now = loop.time()

#                     if now - last_update_time >= 0.07:

#                         current_text = "".join(chunks)

#                         # 更新卡片内容
#                         update_req = (
#                             ContentCardElementRequest.builder()
#                             .card_id(card_id)
#                             .element_id("streaming_txt")
#                             .request_body(
#                                 ContentCardElementRequestBody.builder()
#                                 .uuid(str(uuid.uuid4()))
#                                 .content(current_text)
#                                 .sequence(count)
#                                 .build()
#                             )
#                             .build()
#                         )

#                         await asyncio.to_thread(
#                             feishu_client.cardkit.v1.card_element.content,
#                             update_req
#                         )

#                         count += 1
#                         last_update_time = now

#         except asyncio.TimeoutError:
#             print("stream timeout")

#         except Exception as e:
#             print(f"ERROR: {e}")

#         finally:

#             if chunks:

#                 final_text = "".join(chunks)

#                 # 兜底更新卡片完整内容
#                 update_req = (
#                     ContentCardElementRequest.builder()
#                     .card_id(card_id)
#                     .element_id("streaming_txt")
#                     .request_body(
#                         ContentCardElementRequestBody.builder()
#                         .uuid(str(uuid.uuid4()))
#                         .content(final_text)
#                         .sequence(count)
#                         .build()
#                     )
#                     .build()
#                 )

#                 await asyncio.to_thread(
#                     feishu_client.cardkit.v1.card_element.content,
#                     update_req
#                 )


async def main():

    loop = asyncio.get_running_loop()

    def get_requests(data: lark.im.v1.P2ImMessageReceiveV1):
        asyncio.run_coroutine_threadsafe(process_requests(data), loop)

    event_handler = lark.EventDispatcherHandler.builder(
        "", "").register_p2_im_message_receive_v1(get_requests).build()

    websocket_client = lark.ws.Client(
        "cli_a976d2e502b9dcb6",
        "CFHXjN6mkUE1KXDIfckVJeSRQVD2ywiC",
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    asyncio.create_task(asyncio.to_thread(websocket_client.start))

    await asyncio.Event().wait()


if __name__ == "__main__":

    print("=" * 50)
    print("🚀 飞书智能体已启动!")
    print("💡 在飞书 App 中向机器人发送消息测试")
    print("=" * 50)

    asyncio.run(main())