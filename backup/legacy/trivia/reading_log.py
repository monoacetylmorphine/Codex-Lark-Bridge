# -*- coding: utf-8 -*-
import sqlite3
import pandas as pd

# 读取LangGraph版智能体系统历史对话数据库内容

with sqlite3.connect('/Users/renxiaoyao/Downloads/state.db') as conn:

    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()

    table_set = []
    for table in tables:
        table_set.append(table[0])
    print(table_set)

    for name in table_set:
        df = pd.read_sql_query(f"SELECT * FROM '{name}'", conn)
        df.to_csv(
            f"/Users/renxiaoyao/Downloads/{name}.csv", index=False, encoding="utf-8")

# 读取open-webui数据库内容

# with sqlite3.connect('/Users/strawberrycharlotte/OrbStack/ubuntu/home/strawberrycharlotte/app/database/open-webui/webui.db') as conn:

#     cursor = conn.cursor()
#     cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
#     tables = cursor.fetchall()

#     table_set = []
#     for table in tables:
#         table_set.append(table[0])
#     print(table_set)

#     for name in table_set:
#         df = pd.read_sql_query(f"SELECT * FROM '{name}'", conn)
#         df.to_csv(
#             f"/Users/strawberrycharlotte/OrbStack/ubuntu/home/strawberrycharlotte/table/webui/{name}.csv", index=False, encoding="utf-8")


# 读取OpenAI版智能体系统session数据库内容

# with sqlite3.connect('/Users/strawberrycharlotte/OrbStack/ubuntu/home/strawberrycharlotte/app/database/sql/openai_conversations_history.db') as conn:

#     agent_sessions_df = pd.read_sql_query("SELECT * FROM agent_sessions", conn)
#     agent_messages_df = pd.read_sql_query("SELECT * FROM agent_messages", conn)
#     message_structure_df = pd.read_sql_query(
#         "SELECT * FROM message_structure", conn)
#     turn_usage_df = pd.read_sql_query("SELECT * FROM turn_usage", conn)

#     print(agent_sessions_df)
#     print(agent_messages_df)
#     print(message_structure_df)
#     print(turn_usage_df)

#     agent_sessions_df.to_csv("./agent_sessions.csv",
#                              index=False, encoding="utf-8")
#     agent_messages_df.to_csv("./agent_messages.csv",
#                              index=False, encoding="utf-8")
#     message_structure_df.to_csv("./message_structure.csv",
#                                 index=False, encoding="utf-8")
#     turn_usage_df.to_csv("./turn_usage.csv",
#                          index=False, encoding="utf-8")
