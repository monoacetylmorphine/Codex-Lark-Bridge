# from datetime import datetime, timezone, timedelta
# eastern8 = timezone(timedelta(hours=8))
# runningTimestamp = datetime.now(eastern8)

# print(runningTimestamp)



# user_id = "ou_7d8a6e6df7621556ce0d21922b676706ccs"
# user_thread = {"ou_7d8a6e6df7621556ce0d21922b676706ccs":"6e243b92-5c44-4b5f-8b83-7c7879503ac9"}
# model = "deepseek-v4-flash"
# usage = {'cache_write_input_tokens': 0, 'cached_input_tokens': 10624, 'input_tokens': 10722, 'output_tokens': 57, 'reasoning_output_tokens': 40, 'total_tokens': 10779}

# log = {
#             "timeStamp":runningTimestamp.strftime('%Y%m%d%H%M%S%f'),
#             "userId":user_id,
#             "threadId":user_thread[user_id],
#             "model":model,
#             **usage,
#         }

# print(log)


import sqlite3

DB_PATH = "/Users/fersk/OrbStack/ubuntu/home/fersk/.fersk/state.sqlite"
TABLE_NAME = "token_usage"
# TABLE_NAME = "user_thread"

def read_logs():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 查询前10条记录
    cursor.execute(f"SELECT * FROM {TABLE_NAME};")
    rows = cursor.fetchall()
    
    # 获取列名
    column_names = [description[0] for description in cursor.description]
    print(column_names)
    
    for row in rows:
        print(row)
    
    conn.close()

if __name__ == "__main__":
    read_logs()


# import sqlite3

# DB_PATH = "/Users/fersk/OrbStack/ubuntu/home/fersk/.fersk/logs.db"
# TABLE_NAME = "token_usage"

# def drop_table():
#     conn = sqlite3.connect(DB_PATH)
#     cursor = conn.cursor()
    
#     # 执行删除操作（危险！）
#     cursor.execute(f"DROP TABLE IF EXISTS {TABLE_NAME};")
#     conn.commit()
#     print(f"表 '{TABLE_NAME}' 已删除。")
    
#     conn.close()

# if __name__ == "__main__":
#     # 强烈建议先确认再执行
#     confirm = input(f"确定要删除表 '{TABLE_NAME}' 吗？(yes/no): ")
#     if confirm.lower() == "yes":
#         drop_table()
#     else:
#         print("操作已取消。")

# import asyncio
# from im_bridge.lark_tools import download_msg_resource

# async def main():
#     # image_saved = await download_msg_resource(
#     #     open_id="ou_df791f1b82ad20cb1371ff2269f6ce7b",
#     #     message_id="om_x100b66b990ae08a8b301a4d3214eaa7",
#     #     resource_key="img_v3_02156_9f9d9ec5-7a75-402a-a4b3-157d3476c1fg",
#     #     resource_type="image"
#     # )

#     file_saved = await download_msg_resource(
#         open_id="ou_df791f1b82ad20cb1371ff2269f6ce7b",
#         message_id="om_x100b66bb6a510488c24427323fad376",
#         resource_key="file_v3_00156_0942e810-5201-496c-9392-f29776a3470g",
#         resource_type="file"
#     )

#     # audio_saved = await download_msg_resource(
#     #     open_id="ou_df791f1b82ad20cb1371ff2269f6ce7b",
#     #     message_id="om_x100b66bb5762b8b4b4a18b39b52ce73",
#     #     resource_key="file_v3_00156_04979a6b-51c8-4bcd-961f-ae3f1589edag",
#     #     resource_type="file"
#     # )

#     print(file_saved)

# if __name__ == "__main__":
#     asyncio.run(main())