
import os
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def migrate(database_path: str | os.PathLike[str] | None = None) -> Path:
    """按文件名顺序执行 migrations 目录下的全部 SQL 脚本。

    每个脚本自带幂等保护（CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE），
    并自行登记 schema_migrations，因此可重复执行。
    """
    path = Path(database_path or os.getenv("DATABASE_PATH", "data/app.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if script.stem in applied:
                continue
            connection.executescript(script.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (script.stem,),
            )
    return path


if __name__ == "__main__":
    print(f"数据库迁移完成：{migrate()}")
