from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gpu_resource_manager.db")

# WAL + busy_timeout：sqlite 默认 rollback-journal 模式下，一个长期敞开的事务
# （例如 agent 流式对话在 LLM 联网等待期间持有的读事务）会把想提交的写请求顶到
# PENDING，进而堵死所有新读写 → "database is locked" 500 风暴。
#   - journal_mode=WAL：读写互不阻塞（单写者 + 任意读者），长读不再冻结提交。
#   - busy_timeout：瞬时写竞争改为排队等待而不是立刻报错。
#   - synchronous=NORMAL：WAL 下 fsync 一次/检查点，足够安全且大幅降延迟。
# journal_mode 是持久化到库文件的设置，放 connect 监听里对每个连接幂等执行即可。
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.fetchone()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
else:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency that provides a database session and auto-closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
