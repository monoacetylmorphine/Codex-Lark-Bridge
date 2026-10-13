import csv
import os
import re
import sqlite3

DB_PATH = "/Users/fersk/.fersk/state.sqlite"
OUT_DIR = "/Users/fersk/.fersk/state_csv"   # 导出目录，可自己改

os.makedirs(OUT_DIR, exist_ok=True)


def quote_ident(name: str) -> str:
    """安全引用 SQLite 表名/字段名，处理表名里有双引号的情况"""
    return '"' + name.replace('"', '""') + '"'


def safe_filename(name: str) -> str:
    """把表名转换成安全的文件名，避免 / : 等字符"""
    # 保留字母、数字、下划线、中文、点、连字符，其他替换成下划线
    name = re.sub(r'[^\w.-]+', '_', name)
    name = name.strip('._')
    return name or "table"


conn = sqlite3.connect(DB_PATH)

try:
    cur = conn.cursor()

    # 查询所有表名
    cur.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    tables = [row[0] for row in cur.fetchall()]

    print(f"发现 {len(tables)} 个表：{tables}")

    for table in tables:
        csv_name = safe_filename(table) + ".csv"
        csv_path = os.path.join(OUT_DIR, csv_name)

        # 读取整张表
        cur.execute(f"SELECT * FROM {quote_ident(table)}")
        columns = [desc[0] for desc in cur.description]

        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(columns)   # 写表头
            writer.writerows(cur)      # 流式写数据，适合大表

        print(f"已导出：{table} -> {csv_path}")

finally:
    conn.close()