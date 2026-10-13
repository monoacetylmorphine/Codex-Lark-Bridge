---
name: image-to-image
description: Generate new images through the user's prompts and reference images. Supplement: Reference images currently only support local paths, not image URLs.
---

# Generate Image

Generate new images through the user's prompts and reference images. Supplement: Reference images currently only support local paths, not image URLs.

## When to Use

✅ **USE this skill when:**

- User's prompt with a reference image to create an image.

## When NOT to Use

❌ **DON'T use this skill when:**

- User's prompt is not about to create an image.


## How to Use

- 若用户未提供参考图像, 则在使用脚本前询问用户提供参考图像
- prompt、reference、open_id、workspace这四个参数都是字符串的格式（输入时用单引号包裹）

### 运行技能脚本
```
/home/node/venv_iti/bin/python3 /home/node/.openclaw/skills/image-to-image/scripts/image_to_image.py \
  --prompt <用户描述> \
  --reference <参考图本地路径>
  --open_id <当前用户OpenID>
  --workspace <当前工作的Workspace路径>
```

## Failure Method

- if showing other error in running the script, stop the running and tell the error!
- 若遇见类似下列错误表示用户提供的图像URL不被模型服务方接受, 此时建议用户自行将图片保存本地后再通过消息框发送即可.
```openai.BadRequestError: Error code: 400 - {'error': {'code': 'InvalidParameter', 'message': 'Error while downloading: https://example.webp, status code: 403. Request id: 021784693303619d50fba12c2a6ebe41e423ad6cf0dbaee361672', 'param': '', 'type': ''}}```
