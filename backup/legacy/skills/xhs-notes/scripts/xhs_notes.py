# -*- coding: utf-8 -*-
import os
import json
import time
import requests
import argparse
import pandas as pd
from datetime import datetime

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)

BASE_URL = "https://api.justoneapi.com"


def xhs_blogger_content_search(userId:str):
    
    url = BASE_URL + f"/api/xiaohongshu/get-user-note-list/v4?token={os.getenv('JUSTONE_API_TOKEN')}&userId={userId}"

    response = requests.get(url, timeout=120)
    print(response.status_code)
    if response.content:
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                # print(response.json())
                with open("/Users/renxiaoyao/.openclaw/media/output/bloger_notes.json", "w") as output:
                    json.dump(response.json(), output, ensure_ascii=False, indent=2)
                return response.json()
            except ValueError:
                print(response.text)
        elif content_type.startswith("text/") or "xml" in content_type or content_type.split(";", 1)[0].strip() in {"application/javascript", "application/x-www-form-urlencoded", "application/graphql"}:
            print(response.text)
        else:
            with open("response.bin", "wb") as output:
                output.write(response.content)
            print(f"Saved {len(response.content)} bytes to response.bin")


def xhs_keyword_search(keyword:str):
        
    url = BASE_URL + f"/api/xiaohongshu/search-note/v4?token={os.getenv('JUSTONE_API_TOKEN')}&keyword={keyword}"
    response = requests.get(url, timeout=120)
    print(response.status_code)
    if response.content:
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                # print(response.json())
                with open("/Users/renxiaoyao/.openclaw/media/output/keyword_search.json", "w") as output:
                    json.dump(response.json(), output, ensure_ascii=False, indent=2)
                return response.json()
            except ValueError:
                print(response.text)
        elif content_type.startswith("text/") or "xml" in content_type or content_type.split(";", 1)[0].strip() in {"application/javascript", "application/x-www-form-urlencoded", "application/graphql"}:
            print(response.text)
        else:
            with open("response.bin", "wb") as output:
                output.write(response.content)
            print(f"Saved {len(response.content)} bytes to response.bin")


def xhs_note_detail(noteId:str):

    url = BASE_URL + f"/api/xiaohongshu/get-note-detail/v1?token={os.getenv('JUSTONE_API_TOKEN')}&noteId={noteId}"

    response = requests.get(url, timeout=120)
    print(response.status_code)
    if response.content:
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                # print(response.json())
                with open("/Users/renxiaoyao/.openclaw/media/output/note_detail.json", "w") as output:
                    json.dump(response.json(), output, ensure_ascii=False, indent=2)
                return response.json()
            except ValueError:
                print(response.text)
        elif content_type.startswith("text/") or "xml" in content_type or content_type.split(";", 1)[0].strip() in {"application/javascript", "application/x-www-form-urlencoded", "application/graphql"}:
            print(response.text)
        else:
            with open("response.bin", "wb") as output:
                output.write(response.content)
            print(f"Saved {len(response.content)} bytes to response.bin")


def xhs_data_cleaning(content):

    notes = content.get("data", {}).get("notes", [])
    if not notes:
        return pd.DataFrame()

    def get_first(d, keys, default=None):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return default

    df = pd.DataFrame()
    for idx, note in enumerate(notes):
        user = note.get("user", {})

        df.loc[idx, "bloggerId"] = user.get("userid", "")
        df.loc[idx, "bloggerName"] = user.get("nickname", "")

        df.loc[idx, "noteId"] = note.get("id", "")
        df.loc[idx, "noteTitle"] = note.get("title", "")
        df.loc[idx, "noteDesc"] = note.get("desc", "")

        pub_time = get_first(note, ["create_time", "timestamp"])
        df.loc[idx, "noteTimestamp"] = int(pub_time) if pub_time is not None else 0

        likes = get_first(note, ["likes", "liked_count"])
        df.loc[idx, "likedCount"] = int(likes) if likes is not None else 0

        collected = get_first(note, ["collected_count"])
        df.loc[idx, "collectedCount"] = int(collected) if collected is not None else 0

        comments = get_first(note, ["comments_count"])
        df.loc[idx, "commentedCount"] = int(comments) if comments is not None else 0

        shares = get_first(note, ["share_count", "shared_count"])
        df.loc[idx, "sharedCount"] = int(shares) if shares is not None else 0

        df.loc[idx, "isGoodsNote"] = "YES" if note.get("is_goods_note", False) else "NO"
        df.loc[idx, "isVideo"] = "YES" if note.get("has_music", False) else "NO"

    return df


def main():
    parser = argparse.ArgumentParser(
        description="This is a script to get top 5 liked xiaohongshu notes")
    parser.add_argument(
        "--keywords",
        default=None,
        nargs="+",
        required=True,
        help="keywords for searching are required",
    )
    args = parser.parse_args()

    all_keywords_top5 = {}

    for kw in args.keywords:

        keyword_content_json = xhs_keyword_search(keyword=kw)
        keyword_content_df = xhs_data_cleaning(keyword_content_json)

        if keyword_content_df.empty:
            all_keywords_top5[kw] = []
            continue

        top5_df = keyword_content_df.sort_values(by="likedCount", ascending=False).head(5)
        top5_note_ids = top5_df["noteId"].tolist()

        notes = []
        for note_id in top5_note_ids:
            try:
                detail = xhs_note_detail(noteId=note_id)
                if detail is None:
                    continue
                note = detail['data'][0]['note_list'][0]
                item = {
                    "title": note.get("title"),
                    "description": note.get("desc"),
                    "liked_count": note.get("liked_count"),
                    "collected_count": note.get("collected_count"),
                    "comments_count": note.get("comments_count"),
                    "shared_count": note.get("shared_count"),
                    "source": note.get("mini_program_info", {}).get("webpage_url"),
                }
                notes.append(item)
                time.sleep(1)
            except (IndexError, KeyError, TypeError, AttributeError):
                continue

        all_keywords_top5[kw] = notes

    print(all_keywords_top5)

    with open("/Users/renxiaoyao/.openclaw/media/output/notes.json", "w", encoding="utf-8") as f:
        json.dump(all_keywords_top5, f, ensure_ascii=False, indent=2)

    print("程序运行安全结束")

if __name__=="__main__":
    main()


