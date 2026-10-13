---
name: speak-to-text
description: Transcript the user's voice message into text to understand the user's intention.
---

# Speak to Text

- Transcript the user's voice message into text to understand the user's intention.

## When to Use

✅ **USE this skill when:**

- User's message is voice, not a text.

## When NOT to Use

❌ **DON'T use this skill when:**

- User's message is text, not a voice.


## How to Use

### 飞书群聊语音消息处理正确流程（step by step）

收到音频时，直接按以下步骤执行：

**Step 1** — 如果发送者未授权，先触发授权
  - 用 `feishu_im_user_get_messages(chat_id=群ID, page_size=10)` 试探
  - 如果返回 `awaiting_authorization` → 让用户去点链接授权

**Step 2** — 授权完成后，重新调用 `feishu_im_user_get_messages`
  - 从返回结果的 `content` 中获取**完整**的 file_key（如 `file_v3_0013k_xxx`）
  - 确认 message_id（音频消息自己的 ID，不是 @mention 消息的 ID）

**Step 3** — 调用 `feishu_im_user_fetch_resource`
  - 参数：`message_id`, `file_key`（完整版）, `type="file"`
  - 成功后立即复制文件：
    `cp <saved_path> ~/.openclaw/media/inbound/<名称>.ogg`

**Step 4** - 运行技能脚本
```
/home/node/venv_asr/bin/python3 /home/node/.openclaw/skills/feishu-voice-asr/scripts/speak-to-text.py \
  --file_path <语音消息音频文件路径>
```


### ❌ 常见错误

| 错误 | 后果 |
|------|------|
| 从上下文直接复制 `file_v…xxx`（带省略号） | key 不完整，API 返回 400 |
| 用 `feishu_im_bot_image` 下载音频 | 不支持音频，返回 400 |
| 不先 get_messages 拿完整 key | 永远拿不到正确的 file_key |
| 下载后不马上 cp 到 inbound | temp 文件被清理，白下载 |
| 拿 @mention 消息的 message_id 去下载资源 | message_id 不对，资源不属于该消息 |
| 用用户A的 message_id 下载用户B的音频 | 张冠李戴，400 错误 |


### 授权相关

- bot 工具（feishu_im_bot_image）只适用于图片和普通文件，不适用于音频


## Failure Method

- if showing any error in running the script, stop the running and tell the error!
- if show "ModuleNotFoundError: No module named xxx", please use "uv pip install xxx" to fix the issues.
