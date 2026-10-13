import json
import asyncio
import lark_oapi as lark


async def processing(data):
    print(data)
    print(f"data\n{json.dumps(data.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
    print(f"header\n{json.dumps(data.header.__dict__, indent=2, ensure_ascii=False, default=str)}\n")
    print(f"event\n{json.dumps(data.event.__dict__, indent=2, ensure_ascii=False, default=str)}\n")


async def main() -> None:

    loop = asyncio.get_running_loop()

    def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        print(f'[ do_p2_im_message_receive_v1 access ], data: {lark.JSON.marshal(data, indent=4)}')
        asyncio.run_coroutine_threadsafe(processing(data), loop)

    event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
            .build()
        )

    websocket_client = lark.ws.Client(
        app_id="cli_a976d2e502b9dcb6",
        app_secret="CFHXjN6mkUE1KXDIfckVJeSRQVD2ywiC",
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )

    await asyncio.to_thread(websocket_client.start)


if __name__ == "__main__":
    asyncio.run(main())
