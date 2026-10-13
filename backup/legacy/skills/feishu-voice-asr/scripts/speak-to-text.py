# -*- coding: utf-8 -*-
import os
import av
import base64
import argparse
import asyncio
import wave
import tempfile
from pathlib import Path


from openai import AsyncOpenAI


from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)


class ASR:

    SUPPORTED_AUDIO_TYPES = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".amr": "audio/amr",
    }

    def __init__(self, api_key, base_url, model):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

        self.openai_client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    @staticmethod
    def _encode_audio(file_path):
        with open(file_path, "rb") as read_file:
            return base64.b64encode(read_file.read()).decode("utf-8")

    @staticmethod
    def _convert_to_wav(input_path: str | Path, output_path: str | Path) -> None:
        input_path = Path(input_path)
        output_path = Path(output_path)

        with av.open(str(input_path)) as container:
            audio_stream = next(
                (
                    stream
                    for stream in container.streams
                    if stream.type == "audio"
                ),
                None,
            )

            if audio_stream is None:
                raise ValueError(f"文件中没有找到音频流: {input_path}")

            resampler = av.AudioResampler(
                format="s16",
                layout="mono",
                rate=16000,
            )

            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)

                def write_frames(frames):
                    if frames is None:
                        return

                    if not isinstance(frames, list):
                        frames = [frames]

                    for resampled_frame in frames:
                        wav_file.writeframes(
                            resampled_frame.to_ndarray().tobytes()
                        )

                for frame in container.decode(audio_stream):
                    write_frames(resampler.resample(frame))

                write_frames(resampler.resample(None))

    async def transfer(self, file_path):
        input_path = Path(file_path).expanduser().resolve()

        if not input_path.is_file():
            raise FileNotFoundError(f"音频文件不存在: {input_path}")

        suffix = input_path.suffix.lower()
        temporary_path = None

        try:
            if suffix in self.SUPPORTED_AUDIO_TYPES:
                upload_path = input_path
                mime_type = self.SUPPORTED_AUDIO_TYPES[suffix]
            else:
                with tempfile.NamedTemporaryFile(
                    suffix=".wav",
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)

                self._convert_to_wav(input_path, temporary_path)

                upload_path = temporary_path
                mime_type = "audio/wav"

            base64_file = self._encode_audio(upload_path)

            response = await self.openai_client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "audio_url": (
                                    f"data:{mime_type};base64,{base64_file}"
                                ),
                            },
                            {
                                "type": "input_text",
                                "text": (
                                    "请识别音频中的内容，"
                                    "要求文字语言和音频语言保持一致。"
                                ),
                            },
                        ],
                    }
                ],
            )

            for item in response.output:
                if item.type != "message":
                    continue

                for content in item.content:
                    if content.type == "output_text":
                        print(content.text)
                        return content.text

            raise RuntimeError("接口响应中没有找到识别文本")

        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


async def main():

    parser = argparse.ArgumentParser(
        description="This is a script to transfer an audio to the text")
    parser.add_argument(
        "--file_path",
        default=None,
        required=True,
        help="audio file path",
    )
    args = parser.parse_args()

    api_key = os.getenv("DOUBAO_API_KEY")

    if not api_key:
        raise RuntimeError("未设置 DOUBAO_API_KEY 环境变量")

    base_url="https://ark.cn-beijing.volces.com/api/v3"

    model="doubao-seed-2-0-lite-260428"

    asr = ASR(api_key, base_url, model)
    await asr.transfer(file_path=args.file_path)


if __name__=="__main__":
    asyncio.run(main())
