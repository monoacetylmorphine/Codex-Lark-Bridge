# -*- coding: utf-8 -*-
import os
import time
import json
import base64
import asyncio
import aiohttp
import aiofiles
import argparse
from urllib.parse import urlparse 
import urllib.parse

from volcenginesdkarkruntime import AsyncArk

import lark_oapi as lark
from lark_oapi.api.im.v1 import *


from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)


from datetime import datetime, timezone, timedelta
eastern8 = timezone(timedelta(hours=8))
runningTimestamp = datetime.now(eastern8)


class VideoGenerator():
    def __init__(self, api_key, base_url, model, app_id, app_secret, workspace):

        self.api_key = api_key
        self.base_url = base_url
        
        self.app_id = app_id
        self.app_secret = app_secret

        self.model = model
        self.workspace = workspace

        self.doubao_client = AsyncArk(
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
        """从 URL 中提取文件名（去除查询参数）"""
        parsed = urlparse(url)
        return os.path.basename(parsed.path)
    
    def _encode_media_file(self, file_path: str) -> str:

        file_path = os.path.expanduser(file_path)

        if not os.path.isfile(file_path):
            raise FileNotFoundError(
                f"文件不存在或不是有效文件: {file_path}"
            )
        
        with open(file_path, "rb") as file:
            media = file.read()

        media_data = base64.b64encode(media).decode("utf-8")

        return media_data
    
    def _rebuild_prompt(self, prompt: list):
        """
        重构输入提示词，合并所有文本为一个描述，媒体项按原顺序保留。
        支持：普通文本、HTTP/HTTPS 链接、本地文件路径（自动 Base64 编码）。
        """
        EXT_TO_MIME = {
            # 图片
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.gif': 'image/gif',
            '.webp': 'image/webp', '.bmp': 'image/bmp',
            '.tiff': 'image/tiff', '.ico': 'image/x-icon',
            '.icns': 'image/icns', '.sgi': 'image/sgi',
            '.jp2': 'image/jp2', '.heic': 'image/heic',
            '.heif': 'image/heif',
            # 视频
            '.mp4': 'video/mp4', '.avi': 'video/avi',
            '.mov': 'video/mov',
            # 音频
            '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
            '.aac': 'audio/aac', '.m4a': 'audio/m4a',
        }

        text_parts = []      # 收集所有文本片段
        media_items = []     # 收集所有媒体项（保持原顺序）

        for p in prompt:
            # ---------- 1. 判断是否为 HTTP/HTTPS URL ----------
            if isinstance(p, str) and p.lower().startswith(('http://', 'https://')):
                parsed = urllib.parse.urlparse(p)
                path = parsed.path
                ext = os.path.splitext(path)[1].lower()
                if ext in EXT_TO_MIME:
                    mime = EXT_TO_MIME[ext]
                    if mime.startswith('image/'):
                        media_items.append({
                            "type": "image_url",
                            "image_url": {"url": p},
                            "role": "reference_image",
                        })
                    elif mime.startswith('video/'):
                        media_items.append({
                            "type": "video_url",
                            "video_url": {"url": p},
                            "role": "reference_video",
                        })
                    elif mime.startswith('audio/'):
                        media_items.append({
                            "type": "audio_url",
                            "audio_url": {"url": p},
                            "role": "reference_audio",
                        })
                    continue
                else:
                    # URL 扩展名未知，作为普通文本
                    text_parts.append(p)
                    continue

            # ---------- 2. 判断是否为本地文件 ----------
            if os.path.isfile(p):
                ext = os.path.splitext(p)[1].lower()
                if ext in EXT_TO_MIME:
                    mime = EXT_TO_MIME[ext]
                    encoded = self._encode_media_file(p)
                    if mime.startswith('image/'):
                        media_items.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                            "role": "reference_image",
                        })
                    elif mime.startswith('video/'):
                        media_items.append({
                            "type": "video_url",
                            "video_url": {"url": f"data:{mime};base64,{encoded}"},
                            "role": "reference_video",
                        })
                    elif mime.startswith('audio/'):
                        media_items.append({
                            "type": "audio_url",
                            "audio_url": {"url": f"data:{mime};base64,{encoded}"},
                            "role": "reference_audio",
                        })
                    continue

            # ---------- 3. 其他情况（普通文本） ----------
            text_parts.append(p)

        # 合并所有文本为一个字符串（用换行分隔）
        combined_text = "\n".join(text_parts) if text_parts else ""

        results = []
        if combined_text:
            results.append({"type": "text", "text": combined_text})
        results.extend(media_items)   # 媒体保持原顺序
        return results

    async def video_generation(self, prompt: list):

        print("----- create request -----")
        create_result = await self.doubao_client.content_generation.tasks.create(
            model=self.model,
            content=self._rebuild_prompt(prompt),
            generate_audio=True,
            resolution="1080p",
            ratio="adaptive",
            duration=10,
            watermark=False,
        )
        print(create_result)


        # Polling query section
        print("----- polling task status -----")
        task_id = create_result.id
        while True:
            get_result = await self.doubao_client.content_generation.tasks.get(task_id=task_id)
            status = get_result.status
            if status == "succeeded":
                print("----- task succeeded -----")
                print(get_result)
                break
            elif status == "failed":
                print("----- task failed -----")
                print(f"Error: {get_result.error}")
                break
            else:
                print(f"Current status: {status}, Retrying after 30 seconds...")
                time.sleep(30)

        try:

            # 取出视频 URL
            video_url = get_result.content.video_url
            if not video_url:
                print("❌ 未从响应中提取到视频 URL")
                return None

            # 提取文件名
            original_filename = self._extract_filename_from_url(video_url)
            if not original_filename:
                print("❌ 无法从 URL 解析出文件名")
                return None

            # 构建本地路径
            local_file = f"{self.workspace}/media/outbound/{runningTimestamp.strftime("%Y%m%d%H%M%S")}/{original_filename}"
            os.makedirs(os.path.dirname(local_file), exist_ok=True)

            print(f"⏳ 开始下载视频: {video_url}")
            async with aiohttp.ClientSession() as session:
                async with session.get(video_url) as resp:
                    if resp.status != 200:
                        print(f"❌ 下载失败, HTTP {resp.status}")
                        return None

                    # 流式写入，避免大文件内存溢出
                    async with aiofiles.open(local_file, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):  # 1MB 块
                            await f.write(chunk)

            # 检查文件是否生成且非空
            if os.path.getsize(local_file) == 0:
                print("❌ 下载的文件为空，可能出错")
                os.remove(local_file)  # 清理空文件
                return None

            print(f"✅ 视频下载完成！已存储至 {local_file}")
            return local_file

        except Exception as e:
            print(f"❌ 视频下载失败: {e}")
            return None


    async def send_to_feishu(self, local_file: str, open_id: str):

        with open(local_file, "rb") as file:

            request: CreateFileRequest = CreateFileRequest.builder() \
                .request_body(CreateFileRequestBody.builder()
                    .file_type("mp4")
                    .file_name(local_file.split("/")[-1])
                    .file(file)
                    .build()) \
                .build()

            # 发起请求
            response: CreateFileResponse = await asyncio.to_thread(self.feishu_client.im.v1.file.create, request)

            # 处理失败返回
            if not response.success():
                lark.logger.error(
                    f"client.im.v1.file.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
                return

            # 处理业务结果
            lark.logger.info(lark.JSON.marshal(response.data, indent=4))

            request: CreateMessageRequest = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("media")
                    .content(f"{{\"file_key\":\"{response.data.file_key}\"}}")
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
        nargs="+",
        required=True,
        help="describe the video content and reference images (optional)",
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

    model="doubao-seedance-2-0-260128"
    workspace=args.workspace

    videogenerator =  VideoGenerator(api_key, base_url, model, app_id, app_secret, workspace)
    local_file = await videogenerator.video_generation(prompt=args.prompt)
    await videogenerator.send_to_feishu(local_file=local_file, open_id=args.open_id)


if __name__ == "__main__":
    asyncio.run(main())

