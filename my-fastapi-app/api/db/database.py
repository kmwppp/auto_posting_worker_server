# database.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv
from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime

load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

DATABASE_URL = f"mysql+aiomysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_async_engine(
    DATABASE_URL, 
    echo=True,
    # --- 여기서부터 추가 ---
    pool_pre_ping=True,      # 1. 쿼리 실행 전 연결이 살아있는지 매번 확인 (핵심!)
    pool_recycle=1800,       # 2. 30분(1800초)마다 연결을 자동으로 새로고침 (MySQL 기본 timeout 대비 안전함)
    pool_size=10,            # 3. 기본 연결 풀 크기
    max_overflow=20          # 4. 풀이 꽉 찼을 때 추가로 허용할 연결 수
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# Dependency: DB 세션 주입
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

class PostingLog(Base):
    __tablename__ = "posting_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    current_user_id = Column(String(50), nullable=False)
    external_id = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False)   # SUCCESS / FAIL
    step = Column(String(50))                     # LOGIN, QR, POSTING, PUBLISH 등
    post_title = Column(String(255))
    error_msg = Column(Text)
    screenshot_path = Column(String(255))
    created_at = Column(DateTime, default=datetime.now)

# 비동기 로그 저장 함수
async def insert_posting_log(db: AsyncSession, **kwargs):
    try:
        new_log = PostingLog(
            current_user_id=kwargs.get("current_user_id"),
            external_id=kwargs.get("external_id"),
            status=kwargs.get("status"),
            step=kwargs.get("step"),
            post_title=kwargs.get("post_title"),
            error_msg=kwargs.get("error_msg"),
            screenshot_path=kwargs.get("screenshot_path")
        )
        db.add(new_log)
        await db.commit()
    except Exception as e:
        await db.rollback()
        print(f"⚠️ DB 로그 저장 실패: {e}")