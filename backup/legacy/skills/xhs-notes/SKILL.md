---
name: get-xiaohongshu-notes
description: get xiaohongshu Top 5 liked notes detail through keyword search.
---

# Get Xiaohongshu Notes

get xiaohongshu Top 5 liked notes detail through keyword search.

## When to Use

✅ **USE this skill when:**

- User's prompt is about to get xiaohongshu hot notes through keyword search.

## When NOT to Use

❌ **DON'T use this skill when:**

- User's prompt is not about to get xiaohongshu hot notes.


## How to Use

- keywords参数都是字符串的格式（输入时用单引号包裹）, 并支持多个搜索关键词.
- 搜索关键词注意：例如具体的名称（如三文鱼、北极贝）直接输入参数即可，带有其他标识名称的（如挪威三文鱼、加拿大北极贝）需要带上标识名称，而不是只输入三文鱼、北极贝

```
/Users/renxiaoyao/.openclaw/venv/bin/python3 /Users/renxiaoyao/.openclaw/skills/xhs_notes/scripts/xhs_notes.py \
  --keywords <用户提供相关搜索关键词描述(如三文鱼、海参、生蚝等)>
```

## Output rule

### 格式如下：
- 🥇 Note title
- 💡 Note description
- 👍 点赞：xxx | ⭐ 收藏：xxx | 💬 评论：xxx | 🔄 分享：xxx
- 🔗 查看笔记(超链接, 不要直接显示URL, 是那种可以点击“查看笔记”进行打开的那种)

- 剩余第二、第三等以此类推、注意记得修改Note title前的名次emoji

## Failure Method

- if showing other error in running the script, stop the running and tell the error!
