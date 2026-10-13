# -*- coding: utf-8 -*-
import json
import uuid
import glob
import os


def extract_goods(userMessages):
    """
    从消息列表中尝试提取商品名称，仅从卡片消息（contentType=101）中提取。
    优先级：E2_title -> itemList[0].title -> shopTitle -> title
    若均未找到，返回空字符串。
    """
    for msg in userMessages:
        if msg.get("contentType") != "101":
            continue
        extmap = msg.get("extMap", {})
        dynamic_content = extmap.get("dynamic_msg_content", "")
        if not dynamic_content:
            continue
        try:
            cards = json.loads(dynamic_content)
            if not cards:
                continue
            card = cards[0]
            template_data = card.get("templateData", {})
            if "E2_title" in template_data and template_data["E2_title"]:
                return template_data["E2_title"]
            if "itemList" in template_data and template_data["itemList"]:
                first_item = template_data["itemList"][0]
                if "title" in first_item and first_item["title"]:
                    return first_item["title"]
            if "shopTitle" in template_data and template_data["shopTitle"]:
                return template_data["shopTitle"]
            if "title" in template_data and template_data["title"]:
                return template_data["title"]
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
    return ""


def main():
    data = {}  # 最终输出的字典，key 为 sessionId，value 为包含列表的字典
    json_dir = "/Users/renxiaoyao/Downloads/json/"
    json_files = glob.glob(os.path.join(json_dir, "*.json"))

    for file_path in json_files:
        with open(file_path, "r", encoding="utf-8") as f:
            content = json.load(f)

        userMessages = content.get("data", {}).get("userMessages", [])
        if not userMessages:
            continue

        goods = extract_goods(userMessages)

        sessionId = str(uuid.uuid4())

        # 初始化每个字段的列表
        data[sessionId] = {
            "messageId": [],
            "goods": [],
            "chatMembers": [],
            "content": [],
            "timestamp": []
        }

        for msg in userMessages:
            if msg.get("contentType") == "101":   # 过滤系统/卡片消息，只保留文本消息
                continue

            # 解析消息内容
            try:
                content_text = json.loads(msg.get("content", "")).get("text", "")
            except (json.JSONDecodeError, TypeError):
                content_text = msg.get("content", "")

            sender = msg.get("extMap", {}).get("sender_nick", "").removeprefix("cntaobao")

            # 往各个列表追加数据
            data[sessionId]["messageId"].append(msg.get("messageId", ""))
            data[sessionId]["goods"].append(goods)
            data[sessionId]["chatMembers"].append(sender)
            data[sessionId]["content"].append(content_text)
            data[sessionId]["timestamp"].append(msg.get("sendTime", ""))

    # 将所有数据写入 temp.json
    with open("/Users/renxiaoyao/Downloads/temp.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()