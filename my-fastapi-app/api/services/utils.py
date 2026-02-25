import asyncio
import os
from datetime import datetime
from pathlib import Path

# DB 및 쿼리 관련
from sqlalchemy import text
from api.db.database import AsyncSessionLocal  # DB 세션 생성용
from api.db.database import PostingLog           # 로그 기록용 모델
from api.v1.dependencies.redis_manager import redis_manager

# 타입 힌팅용 (선택 사항)
from typing import Optional


# 루프 밖에서 한 번만 정의
def get_smart_wrapped_text(text: str, start_limit: int = 10) -> str:
    """10자 이후 첫 공백을 찾아 줄바꿈을 삽입하는 유틸리티 함수"""
    if not text or len(text) <= start_limit:
        return text
    
    # start_limit(10자) 이후의 첫 공백 탐색
    break_point = text.find(' ', start_limit)
    
    if break_point != -1:
        return text[:break_point].strip() + "\n" + text[break_point:].strip()
    
    # 10자 이후 공백이 없는데 텍스트가 너무 길면(예: 15자 이상), 
    # 단어 중간이라도 강제로 끊어줘야 이미지를 안 벗어남 (선택 사항)
    if len(text) > 15:
        return text[:12] + "\n" + text[12:]
        
    return text

# 스크린샷 찍는 헬퍼 함수 (필요할 때 호출)
async def save_debug_screenshot(page, name, user_current_id):
    # 1. 경로 설정 (error_screen_shot/유저ID)
    base_dir = Path("error_screen_shot") / str(user_current_id)
    
    # 2. 폴더가 없으면 생성 (parents=True: 상위 폴더까지, exist_ok=True: 이미 있어도 에러 안남)
    base_dir.mkdir(parents=True, exist_ok=True)
    
    # 3. 파일명 구성
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    file_name = f"debug_{name}_{timestamp}.png"
    scr_path = base_dir / file_name
    
    # 4. 스크린샷 저장 (Path 객체를 문자열로 변환하여 전달)
    await page.screenshot(path=str(scr_path), full_page=True)
    
    print(f"📸 디버깅 스크린샷 저장됨: {scr_path}")
    # return str(scr_path) # 나중에 DB에 경로를 저장할 수 있게 반환해주는 것이 좋습니다.


async def log_to_db(current_user_id, naver_id, title, step, status="SUCCESS", error="", screenshot=""):
    """
    함수 내부에서 세션을 직접 생성하여 단계별 로그를 DB에 심는 함수
    (이제 첫 번째 인자였던 'db'는 받지 않습니다.)
    """
    # 1. 여기서 직접 세션을 엽니다.
    async with AsyncSessionLocal() as db:
        try:
            new_log = PostingLog(
                current_user_id=str(current_user_id),
                external_id=naver_id,
                status=status,
                step=step,
                post_title=title,
                error_msg=str(error) if error else None,
                screenshot_path=screenshot if screenshot else None
                # created_at은 모델에서 default=datetime.now로 되어있으면 생략 가능합니다.
            )
            db.add(new_log)
            await db.commit()
            # print(f"📝 DB 로그 저장 완료: {step}") # 디버깅용
        except Exception as db_e:
            await db.rollback()
            print(f"⚠️ DB 로그 기록 실패: {db_e}")
        # async with 블록을 나가면서 세션이 안전하게 닫힙니다.

async def record_success_count(current_user_id, naver_id, title):
    """
    포스팅이 1개 성공할 때마다 호출하여 
    user_post_success_logs 테이블에 (네이버ID, 제목) 기록을 남기는 함수.
    """
    async with AsyncSessionLocal() as db:
        try:
            # 변경된 컬럼명(naver_id, title)에 맞춰 쿼리 수정
            query = text("""
                INSERT INTO user_post_success_logs (user_id, naver_id, title) 
                VALUES (:user_id, :naver_id, :title)
            """)
            
            await db.execute(query, {
                "user_id": str(current_user_id),
                "naver_id": naver_id,
                "title": title
            })
            await db.commit()
            print(f"✅ [카운트] 유저 {current_user_id} - '{title[:15]}...' 발행 기록 완료")
            
        except Exception as e:
            await db.rollback()
            print(f"⚠️ [카운트] 성공 로그 기록 실패: {e}")


# --- [도움 함수] 중단 신호 확인용 ---
async def check_abort(task_id: str):
    # Redis에서 해당 task_id의 중단 신호가 있는지 확인
    signal = await redis_manager.redis_client.get(f"stop_signal:{task_id}")
    if signal:
        # 신호가 있으면 에러를 발생시켜 catch 블록으로 점프
        raise InterruptedError("사용자 요청으로 작업이 중단되었습니다.")