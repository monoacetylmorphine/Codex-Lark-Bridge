---
name: text-to-image
description: generating an image through user's prompt.
---

# Generate Image

Generating an image through user's prompt.

## When to Use

✅ **USE this skill when:**

- User's prompt is about to create an image.

## When NOT to Use

❌ **DON'T use this skill when:**

- User's prompt is not about to create an image.


## How to Use

- prompt、open_id、workspace这三个参数都是字符串的格式（输入时用单引号包裹）
- 说明：此script运行AI生成图片需要消耗比较多的运行时间


### 运行技能脚本
```
/home/node/venv_tti/bin/python3 /home/node/.openclaw/skills/text-to-image/scripts/text_to_image.py \
  --prompt <用户描述> \
  --open_id <当前用户OpenID> \
  --workspace <当前工作的Workspace路径>
```

## Failure Method

- if showing other error in running the script, stop the running and tell the error!
