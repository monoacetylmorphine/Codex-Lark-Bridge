---
name: reference-to-video
description: Support the generation of new videos through prompts + optional materials (images/videos/audio). Images and audio accept local paths or URLs, and video materials only accept URL links.
---

# Generate Video

Support the generation of new videos through prompts + optional materials (images/videos/audio). Images and audio accept local paths or URLs, and video materials only accept URL links.

## When to Use

✅ **USE this skill when:**

- User's prompt is about to create a video.

## When NOT to Use

❌ **DON'T use this skill when:**

- User's prompt is not about to create a video.


## How to Use

- prompt、open_id、workspace这三个参数都是字符串的格式（输入时用单引号包裹）
- 说明：此script运行AI生成视频需要消耗比较多的运行时间
- 运行时, 每隔30秒会进行轮询当前任务的状态

### 运行技能脚本
```
/home/node/venv_video/bin/python3 /home/node/.openclaw/skills/reference-to-video/scripts/reference_to_video.py \
  --prompt <用户文字描述（必须）以及参考视频、图片等文件路径（可选）> \
  --open_id <当前用户OpenID> \
  --workspace <当前工作的Workspace路径>
```

## Failure Method

- if showing other error in running the script, stop the running and tell the error!