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

DB_PATH = "/Users/fersk/OrbStack/ubuntu/home/fersk/.fersk/logs.db"
TABLE_NAME = "token_usage"

def read_logs():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 查询前10条记录
    cursor.execute(f"SELECT * FROM {TABLE_NAME} ORDER BY id DESC LIMIT 10;")
    rows = cursor.fetchall()
    
    # 获取列名
    column_names = [description[0] for description in cursor.description]
    print(column_names)
    
    for row in rows:
        print(row)
    
    conn.close()

if __name__ == "__main__":
    read_logs()