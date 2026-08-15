"""Initialize or migrate Serenity Tracker's SQLite database."""

from storage import DB_PATH, database_counts, init_db


def main() -> None:
    result = init_db()
    if result["migrated"]:
        print(
            "資料庫遷移完成："
            f"移除 {result['removed_duplicates']} 筆重複紀錄；"
            f"備份位於 {result['backup_path']}"
        )
    elif result["schema_updated"]:
        print(
            "資料庫欄位升級完成：已加入引用脈絡與立場判定依據；"
            f"備份位於 {result['backup_path']}"
        )
    counts = database_counts()
    print(
        f"資料庫就緒 ({DB_PATH})：{counts['posts']} 則貼文、"
        f"{counts['mentions']} 筆個股分析、{counts['pending']} 待解析、"
        f"{counts['failed']} 失敗待重試。"
    )


if __name__ == "__main__":
    main()
