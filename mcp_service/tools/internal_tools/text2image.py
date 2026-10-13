import os
import asyncio
from typing import Optional

from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APITimeoutError,
    APIStatusError,
    OpenAIError,
)

from mcp_service.configs.loader import CONFIG
from mcp_service.utils.terminal_log import get_logger

logger = get_logger("IMAGE")


def params_validator() -> tuple[str, str, str]:
    """校验图像模型配置 """

    logger.info("开始校验图像生成模型配置")

    try:
        mcp_config = CONFIG.get("mcp")
        if not isinstance(mcp_config, dict):
            raise TypeError("CONFIG['mcp'] 不存在或不是字典")

        image_model_config = mcp_config.get("imageModel")
        if not isinstance(image_model_config, dict):
            raise TypeError(
                "CONFIG['mcp']['imageModel'] 不存在或不是字典"
            )

        model = image_model_config.get("model")
        api_key_env = image_model_config.get("apiKeyEnv")
        base_url = image_model_config.get("baseUrl")

        if not model:
            raise ValueError(
                "缺少配置 CONFIG['mcp']['imageModel']['model']"
            )

        if not isinstance(model, str):
            raise TypeError(
                "CONFIG['mcp']['imageModel']['model'] 必须是字符串"
            )

        model = model.strip()

        if not model:
            raise ValueError("图像生成模型 model 不能为空")

        if not api_key_env:
            raise ValueError(
                "缺少配置 CONFIG['mcp']['imageModel']['apiKeyEnv']"
            )

        if not isinstance(api_key_env, str):
            raise TypeError(
                "CONFIG['mcp']['imageModel']['apiKeyEnv'] 必须是字符串"
            )

        api_key_env = api_key_env.strip()

        api_key = os.getenv(api_key_env)

        if not api_key:
            raise ValueError(
                f"环境变量 {api_key_env!r} 未配置或值为空"
            )

        api_key = api_key.strip()

        if not base_url:
            raise ValueError(
                "缺少配置 CONFIG['mcp']['imageModel']['baseUrl']"
            )

        if not isinstance(base_url, str):
            raise TypeError(
                "CONFIG['mcp']['imageModel']['baseUrl'] 必须是字符串"
            )

        base_url = base_url.strip().rstrip("/")

        if not (
            base_url.startswith("http://")
            or base_url.startswith("https://")
        ):
            raise ValueError(
                f"baseUrl 格式非法: {base_url!r}，"
                "必须以 http:// 或 https:// 开头"
            )

        # 不要打印完整 API Key
        masked_api_key = (
            f"{api_key[:4]}***{api_key[-4:]}"
            if len(api_key) >= 8
            else "***"
        )

        logger.info(
            "图像模型参数校验完成: "
            "model=%s, base_url=%s, api_key_env=%s, api_key=%s",
            model,
            base_url,
            api_key_env,
            masked_api_key,
        )

        return model, api_key, base_url

    except (ValueError, TypeError):
        logger.exception("图像生成模型配置校验失败")
        raise

    except Exception:
        logger.exception("校验图像生成模型配置时发生未知异常")
        raise


async def image_generator(prompt: str) -> str:
    """图像生成"""

    if not isinstance(prompt, str):
        logger.error(
            "图像生成失败: prompt 类型错误，实际类型=%s",
            type(prompt).__name__,
        )
        raise TypeError("prompt 必须是字符串")

    prompt = prompt.strip()

    if not prompt:
        logger.error("图像生成失败: prompt 不能为空")
        raise ValueError("prompt 不能为空")

    # 配置来自统一挂载的 config.json 和 .env；缺失只影响本次工具调用。
    model, api_key, base_url = params_validator()

    prompt_preview = (
        prompt
        if len(prompt) <= 200
        else f"{prompt[:200]}..."
    )

    logger.info(
        "开始生成图片: model=%s, prompt=%r",
        model,
        prompt_preview,
    )

    try:

        async with AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=120.0, max_retries=2,
        ) as openai_client:
            images_response = await openai_client.images.generate(
                model=model,
                prompt=prompt,
                size="2K",
                response_format="url",
                extra_body={
                    "temperature": 0.7,
                    "watermark": False,
                    "sequential_image_generation": "auto",
                    "sequential_image_generation_options": {
                        "max_images": 1,
                    },
                },
            )

        logger.info("图像生成 API 请求完成，开始解析响应")

        if images_response is None:
            raise ValueError("图像生成接口返回空响应")

        if not getattr(images_response, "data", None):
            raise ValueError(
                "图像生成接口响应中不存在 data"
            )

        first_image = images_response.data[0]

        if first_image is None:
            raise ValueError(
                "图像生成接口 data[0] 为空"
            )

        image_url: Optional[str] = getattr(
            first_image,
            "url",
            None,
        )

        if not image_url:
            raise ValueError(
                "图像生成成功，但响应中不存在图片 URL"
            )

        if not isinstance(image_url, str):
            raise ValueError(
                f"图片 URL 类型异常: "
                f"{type(image_url).__name__}"
            )

        image_url = image_url.strip()

        if not image_url:
            raise ValueError("图片 URL 为空字符串")

        logger.info(
            "图片生成成功: url=%s",
            image_url,
        )

        return image_url

    except APITimeoutError as exc:
        logger.error(
            "图像生成请求超时: model=%s, error=%s",
            model,
            exc,
            exc_info=True,
        )
        raise

    except APIConnectionError as exc:
        logger.error(
            "无法连接图像生成服务: "
            "model=%s, base_url=%s, error=%s",
            model,
            base_url,
            exc,
            exc_info=True,
        )
        raise

    except APIStatusError as exc:
        logger.error(
            "图像生成 API 返回错误: "
            "status_code=%s, request_id=%s, error=%s",
            exc.status_code,
            getattr(exc, "request_id", None),
            exc,
            exc_info=True,
        )
        raise

    except OpenAIError as exc:
        logger.error(
            "图像生成 OpenAI SDK 异常: %s",
            exc,
            exc_info=True,
        )
        raise

    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        logger.error(
            "图像生成响应解析失败: %s",
            exc,
            exc_info=True,
        )
        raise

    except asyncio.CancelledError:
        logger.warning("图像生成任务被取消")
        raise

    except Exception as exc:
        logger.exception(
            "图像生成过程中发生未知异常: %s",
            exc,
        )
        raise
