import asyncio
from openai_codex import AsyncCodex, Sandbox

async def main():
    async with AsyncCodex() as codex:
        thread = await codex.thread_start(
            model="deepseek-v4-flash",
            sandbox=Sandbox.workspace_write,
        )
        handle = await thread.turn("きさらぎ駅的传说是什么")
        async for event in handle.stream():
            with open("./stream_output.txt", "a", encoding="utf-8") as f:
                f.write(str(event))


if __name__=="__main__":
    asyncio.run(main())