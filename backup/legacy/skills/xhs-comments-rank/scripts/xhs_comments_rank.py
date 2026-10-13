# -*- coding: utf-8 -*-
import os
import json
import asyncio
import pandas as pd


from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)


from apify_client import ApifyClientAsync
apify_client = ApifyClientAsync(os.getenv("APIFY_API_KEY"))


from datetime import datetime, timezone, timedelta
eastern8 = timezone(timedelta(hours=8))
runningTimestamp = datetime.now(eastern8)


async def get_creator_notes(userId:str|list):

    actor_client = apify_client.actor('mHKEEgoDqr6btQQer')

    run_input = {
        "profileUrls": userId if isinstance(userId, list) else [userId],
        # "includeNotes": True,
        "maxNotesPerProfile": 50,
    }

    call_result = await actor_client.call(run_input=run_input)

    if call_result is None:
        print('Actor run failed.')
        return

    dataset_client = apify_client.dataset(call_result.default_dataset_id)
    list_items_result = await dataset_client.list_items()

    print(len(list_items_result.items[0].get("notes", [])))
    
    with open(f"/Users/renxiaoyao/Documents/xbtc-dev/logs/creator_notes_{runningTimestamp.strftime("%Y%m%d%H%M%S")}.json", "w", encoding="utf-8") as f:
        json.dump({"data":list_items_result.items}, f, ensure_ascii=False, indent=2)

    results = {}
    notes = list_items_result.items[0].get("notes", [])
    for note in notes:
        noteId = note.get("id", "unknownId")
        if noteId not in results.keys():
            results[noteId] = {
                "userId":note.get("user", {}).get("userid", None),
                "nickname":note.get("user", {}).get("nickname", None),
                "title":note.get("title", None),
                "desc":note.get("desc", None),
                "createTime":int(note.get("create_time", None)),
                "lastUpdateTime":int(note.get("last_update_time", None)) if int(note.get("last_update_time", None))!=0 else int(note.get("create_time", None)),
                "likes":int(note.get("likes", None)),
                "collected_count":int(note.get("collected_count", None)),
                "comments_count":int(note.get("comments_count", None)),
                "share_count":int(note.get("share_count", None)),
                "noteAttributes":note.get("note_attributes", None),
                "isGoodsNote":note.get("is_goods_note", None),
            }

    print(len(results.keys()))

    df = pd.DataFrame(results)
    df = df.T.reset_index().rename(columns={'index': 'noteId'})
    df.to_csv(f"/Users/renxiaoyao/Documents/xbtc-dev/logs/creator_notes_{runningTimestamp.strftime("%Y%m%d%H%M%S")}.csv", index=False, encoding="utf-8-sig")

    return df


async def get_note_comments(noteId:str|list):

    actor_client = apify_client.actor('sAWbV6wp5XO5aEapx')

    run_input = {
        "noteUrls": noteId if isinstance(noteId, list) else [noteId],
        "sortBy": "latest",
        "includeReplies": False,
        "maxCommentsPerNote": 0,
        # "maxRepliesPerComment": 5,
    }

    call_result = await actor_client.call(run_input=run_input)

    if call_result is None:
        print('Actor run failed.')
        return

    dataset_client = apify_client.dataset(call_result.default_dataset_id)
    list_items_result = await dataset_client.list_items()

    print(len(list_items_result.items))

    with open(f"/Users/renxiaoyao/Documents/xbtc-dev/logs/note_comments_{runningTimestamp.strftime("%Y%m%d%H%M%S")}.json", "w", encoding="utf-8") as f:
        json.dump({"data":list_items_result.items}, f, ensure_ascii=False, indent=2)

    results = []
    comments = list_items_result.items
    for comment in comments:
        results.append(
            {
                "noteId":comment.get("note_id", None),
                "userId":comment.get("user", {}).get("userid", None),
                "redId":comment.get("user", {}).get("red_id", None),
                "nickname":comment.get("user", {}).get("nickname", None),
                "commentContent":comment.get("content", None),
                "noteUrl":comment.get("url_to_note", None),
            }
        )

    print(len(results))

    df = pd.DataFrame(results)
    df.to_csv(f"/Users/renxiaoyao/Documents/xbtc-dev/logs/note_comments_{runningTimestamp.strftime("%Y%m%d%H%M%S")}.csv", index=False, encoding="utf-8-sig")

    return df


async def main():

    userId = "5dfc09e00000000001003539"


    currentMonthStart = datetime(runningTimestamp.year, runningTimestamp.month, 1, tzinfo=eastern8)
    if runningTimestamp.month == 12:
        currentMonthEnd = datetime(runningTimestamp.year + 1, 1, 1, tzinfo=eastern8) - timedelta(seconds=1)
    else:
        currentMonthEnd = datetime(runningTimestamp.year, runningTimestamp.month + 1, 1, tzinfo=eastern8) - timedelta(seconds=1)
    currentMonthStart = int(currentMonthStart.timestamp())
    currentMonthEnd = int(currentMonthEnd.timestamp())


    notes = await get_creator_notes(userId=userId)

    noteId = notes[(notes["createTime"] >= currentMonthStart)&(notes["createTime"] <= currentMonthEnd)]
    noteId = noteId["noteId"].unique().tolist()

    print(len(noteId))

    comments = await get_note_comments(noteId=noteId)

    results = pd.merge(
        left=notes,
        right=comments,
        how="right",
        on="noteId",
    )

    results.rename(columns={
        "userId_x":"creatorUserId",
        "nickname_x":"creatorNickname",
        "userId_y":"userId",
        "nickname_y":"nickname"
        }
    )

    results.to_csv(f"/Users/renxiaoyao/Documents/xbtc-dev/logs/current_month_comments_rank_{runningTimestamp.strftime("%Y%m%d%H%M%S")}.csv", index=False, encoding="utf-8-sig")


if __name__=="__main__":
    asyncio.run(main())

