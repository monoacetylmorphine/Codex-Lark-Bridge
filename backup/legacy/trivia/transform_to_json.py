# -*- coding: utf-8 -*-
import os
import json
import re
from tqdm import tqdm
# ===== 配置 =====
INPUT_DIR = "/Users/renxiaoyao/Downloads/total"   # 输入目录
OUTPUT_DIR = "/Users/renxiaoyao/Downloads/json"   # 输出到同一目录（如果想分开放，改为另一路径）

# 可选：跳过已有输出的文件（防止重复处理）？
SKIP_EXISTING = False

# ===== 核心转换函数（完全复用你的代码） =====
def convert_file(file_path, output_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = f.read()
    except Exception as e:
        print(f"读取失败 {file_path}: {e}")
        return False

    if data.startswith('\ufeff'):
        data = data[1:]
    data = data.strip()

    # 提取 JSONP 内容（沿用你的严格匹配）
    match = re.search(r'^mtopjsonp\d+\((.*)\)$', data, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        print(f"不是有效的 JSONP 格式，跳过: {file_path}")
        return False

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败 {file_path}: {e}")
        return False

    # 保存
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)
    return True

# ===== 批量处理 =====
def main():
    if not os.path.exists(INPUT_DIR):
        print(f"目录不存在: {INPUT_DIR}")
        return

    # 确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    for filename in tqdm(os.listdir(INPUT_DIR)):
        file_path = os.path.join(INPUT_DIR, filename)
        if not os.path.isfile(file_path):
            continue   # 跳过子目录

        # 输出文件名：原文件名 + .json
        output_filename = filename + ".json"
        output_path = os.path.join(OUTPUT_DIR, output_filename)

        # 可选：跳过已存在的输出文件
        if SKIP_EXISTING and os.path.exists(output_path):
            print(f"输出文件已存在，跳过: {output_filename}")
            continue

        print(f"正在处理: {filename}")
        success = convert_file(file_path, output_path)
        if success:
            print(f"  已保存到: {output_filename}")
        else:
            print(f"  处理失败")

    print("全部完成！")

if __name__ == "__main__":
    main()