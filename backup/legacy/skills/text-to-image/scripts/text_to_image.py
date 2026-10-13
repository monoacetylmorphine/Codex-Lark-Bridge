# -*- coding: utf-8 -*-
import os
import json
import aiohttp
import argparse
import asyncio
import aiofiles
from urllib.parse import urlparse 

from openai import AsyncOpenAI

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)

from datetime import datetime, timezone, timedelta
eastern8 = timezone(timedelta(hours=8))
runningTimestamp = datetime.now(eastern8)

class ImageGenerator():
    def __init__(self, api_key, base_url, model, app_id, app_secret, workspace):

        self.api_key = api_key
        self.base_url = base_url
        
        self.app_id = app_id
        self.app_secret = app_secret

        self.model = model
        self.workspace = workspace

        self.openai_client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        self.feishu_client = lark.Client.builder() \
                .app_id(self.app_id) \
                .app_secret(self.app_secret) \
                .log_level(lark.LogLevel.DEBUG) \
                .build()

    @staticmethod
    def _extract_filename_from_url(url: str) -> str:
        parsed_url = urlparse(url)
        path = parsed_url.path
        filename = os.path.basename(path)
        return filename


    async def image_generation(self, prompt: str):

        try:
            imagesResponse = await self.openai_client.images.generate(
                model=self.model,
                prompt=prompt,
                size="2K",
                response_format="url",
                extra_body={
                    "temperature": 0.7,
                    "watermark": False,
                    "sequential_image_generation": "auto",
                    "sequential_image_generation_options": {
                        "max_images": 1
                    },
                },
            )

            # 取出图片 URL
            image_url = imagesResponse.data[0].url
            if not image_url:
                print("❌ 未从响应中提取到图像 URL")
                return None

            # 从 URL 中提取文件名（urlparse 会自动剥离 query 参数）
            original_filename = self._extract_filename_from_url(image_url)
            if not original_filename:
                print("❌ 无法从 URL 解析出文件名")
                return None

            local_file = f"{self.workspace}/media/outbound/{runningTimestamp.strftime("%Y%m%d%H%M%S")}/{original_filename}"
            os.makedirs(os.path.dirname(local_file), exist_ok=True)

            # 从 URL 下载图片
            print(f"⏳ 正在从 URL 下载图片: {image_url}")
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        print(f"❌ 下载失败, HTTP {resp.status}")
                        return None
                    content = await resp.read()

            if not content:
                print("❌ 下载内容为空")
                return None

            async with aiofiles.open(local_file, "wb") as f:
                await f.write(content)

            print(f"✅ 图片下载完成！已存储至 {local_file}")
            return local_file

        except Exception as e:
            print(f"❌ 图像生成或保存失败: {e}")
            return None


    async def send_to_feishu(self, local_file: str, open_id: str):

        with open(local_file, "rb") as file:

            request: CreateImageRequest = CreateImageRequest.builder() \
                .request_body(CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(file)
                    .build()) \
                .build()

            # 发起请求
            response: CreateImageResponse = await asyncio.to_thread(self.feishu_client.im.v1.image.create, request)

            # 处理失败返回
            if not response.success():
                lark.logger.error(
                    f"client.im.v1.image.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
                return

            # 处理业务结果
            lark.logger.info(lark.JSON.marshal(response.data, indent=4))

            request: CreateMessageRequest = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("image")
                    .content(f"{{\"image_key\":\"{response.data.image_key}\"}}")
                    .build()) \
                .build()

            # 发起请求
            response: CreateMessageResponse = await asyncio.to_thread(self.feishu_client.im.v1.message.create, request)

            # 处理失败返回
            if not response.success():
                lark.logger.error(
                    f"client.im.v1.message.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
                return

            # 处理业务结果
            lark.logger.info(lark.JSON.marshal(response.data, indent=4))


async def main():

    parser = argparse.ArgumentParser(
        description="This is a script to generate an image")
    parser.add_argument(
        "--prompt",
        default=None,
        required=True,
        help="describe the image content",
    )
    parser.add_argument(
        "--open_id",
        default=None,
        required=True,
        help="user's id in current conversation",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        required=True,
        help="your current workspace path",
    )
    args = parser.parse_args()

    api_key=os.getenv("DOUBAO_API_KEY")
    base_url="https://ark.cn-beijing.volces.com/api/v3"

    app_id=os.getenv("AIGC_APP_ID")
    app_secret=os.getenv("AIGC_APP_SECRET")

    model="doubao-seedream-5-0-lite-260128"
    workspace=args.workspace

    imagegenerator = ImageGenerator(api_key, base_url, model, app_id, app_secret, workspace)

    local_file = await imagegenerator.image_generation(prompt=args.prompt)

    await imagegenerator.send_to_feishu(local_file=local_file, open_id=args.open_id)


if __name__=="__main__":
    asyncio.run(main())
