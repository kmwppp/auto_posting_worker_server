from fastapi import APIRouter, BackgroundTasks, Request, Depends, HTTPException
from api.services.blog_service import start_bulk_posting
from api.v1.schemas.blog import BlogBulkRequest, CredentialResponse  # 모델 임포트
from api.v1.dependencies.sse_manager import manager
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession  # 타입 힌트용
from sqlalchemy import text
from datetime import datetime, time
from api.db.database import get_db
import asyncio
import uuid  # 상단에 추가
from api.v1.dependencies.redis_manager import redis_manager # 새로 만든 유틸
import json
from typing import List, Optional
# 여기서 BlogBulkRequest 등 스키마 정의/임포트

router = APIRouter()

@router.post("/posting")
async def create_posting_task(payload: BlogBulkRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    # 1. 리스트가 비어있는지 먼저 체크 (IndexError 방지)
    if not payload.authList:
        return {"status": "fail", "message": "요청된 계정 정보가 없습니다."}

    # 유저의 고유 아이디 
    user_id = payload.authList[0].current_user_id
    now = datetime.now()

    # 이번 요청 수량 합산
    total_requested_count = sum(auth.postingCount for auth in payload.authList)
    
    # 오늘 실제 성공한 수량 조회
    today_start = datetime.combine(now.date(), time.min)
    success_count_query = text("""
        SELECT COUNT(*) 
        FROM user_post_success_logs 
        WHERE user_id = :user_id AND created_at >= :today_start
    """)
    result = await db.execute(success_count_query, {"user_id": user_id, "today_start": today_start})
    already_done_count = result.scalar() or 0
    
    MAX_DAILY_LIMIT = 50
    remaining_count = MAX_DAILY_LIMIT - already_done_count

    # 이미 50개 다 쓴 경우
    if already_done_count >= MAX_DAILY_LIMIT:
        return {
            "status": "fail",
            "message": "오늘 발행 가능한 50개를 모두 소진하였습니다. 내일 다시 시도해주세요.",
            "currentUserId": user_id
        }
    
    # 요청 수량이 잔여 수량보다 많은 경우
    if total_requested_count > remaining_count:
        return {
            "status": "fail",
            "message": f"현재 오늘 발행 가능한 잔여 수량은 {remaining_count}개입니다. 수량을 조절해주세요.",
            "currentUserId": user_id
        }

    # --- [추가] 유저의 OpenAI API KEY 조회 ---
    # users 테이블의 id(PK) 혹은 user_id 컬럼 중 형님 DB 구조에 맞는 것을 사용하세요.
    user_query = text("SELECT open_api_key FROM users WHERE id = :user_id")
    user_result = await db.execute(user_query, {"user_id": user_id})
    user_data = user_result.fetchone()

    if not user_data or not user_data.open_api_key:
        return {
            "status": "fail",
            "message": "등록된 OpenAI API 키가 없습니다. 관리자에게 문의해주세요.",
            "currentUserId": user_id
        }
    
    # DB에서 가져온 키 변수 할당
    target_api_key = user_data.open_api_key
    # ---------------------------------------

    # 1. 고유한 작업 ID 생성 (예: 'a1b2c3d4...')
    task_id = str(uuid.uuid4())

    # 유저의 상태를 조회함
    query = text("SELECT user_id, is_running FROM user_work_status WHERE user_id = :user_id")
    result = await db.execute(query, {"user_id": user_id})
    user_status = result.fetchone()

    # 2. 작업 중인지 체크
    if user_status and user_status.is_running:
        return {
            "status": "fail",
            "message": "이미 진행 중인 작업이 있습니다. 완료 후 다시 시도해주세요.",
            "streamUrl": "",
            "currentUserId": user_id
        }

    # --- [1] 계정 및 프록시 정보 저장/갱신 (독립적인 try) ---
    try:
        for auth in payload.authList:
            cred_insert_query = text("""
                INSERT IGNORE INTO user_credentials (
                    owner_id, site_name, login_id, login_pw, 
                    proxy_id, proxy_pw, proxy_port
                )
                VALUES (
                    :owner_id, :site_name, :login_id, :login_pw, 
                    :proxy_id, :proxy_pw, :proxy_port
                )
            """)
            
            await db.execute(cred_insert_query, {
                "owner_id": user_id,
                "site_name": auth.site_name,
                "login_id": auth.external_id,
                "login_pw": auth.external_pw,
                "proxy_id": auth.proxy_id,
                "proxy_pw": auth.proxy_pw,
                "proxy_port": auth.port
            })
        # 계정 정보만 먼저 커밋 (다른 작업에 영향 주지 않기 위함)
        await db.commit()
        print(f"✅ 유저 {user_id}의 계정 정보 동기화 완료")
    except Exception as e:
        await db.rollback()
        # 계정 저장 실패는 로그만 남기고 작업은 계속 진행하도록 할 수 있습니다.
        print(f"⚠️ 계정 정보 동기화 중 오류 발생 (무시하고 진행): {e}")

    # --- [추가] user_save_info 정보 저장/갱신 로직 ---
    try:
        # 1. 먼저 해당 유저의 정보가 있는지 조회
        save_info_query = text("""
            SELECT ip, wp_url, link_comment 
            FROM user_save_info 
            WHERE owner_id = :user_id
        """)
        save_info_result = await db.execute(save_info_query, {"user_id": user_id})
        existing_info = save_info_result.fetchone()

        if not existing_info:
            # 1-1. 데이터가 없으면 신규 INSERT
            insert_save_query = text("""
                INSERT INTO user_save_info (owner_id, ip, wp_url, link_comment)
                VALUES (:owner_id, :ip, :wp_url, :link_comment)
            """)
            await db.execute(insert_save_query, {
                "owner_id": user_id,
                "ip": payload.proxy,
                "wp_url": payload.siteUrl,
                "link_comment": payload.linkTopText
            })
            print(f"✅ 유저 {user_id}의 save_info 신규 등록 완료")
        
        else:
            # 1-2. 데이터가 있으면 변경사항이 있을 때만 UPDATE
            # 튜플 형태나 객체 형태에 따라 접근 (existing_info.ip 혹은 existing_info[0])
            if (existing_info.ip != payload.proxy or 
                existing_info.wp_url != payload.siteUrl or 
                existing_info.link_comment != payload.linkTopText):
                
                update_save_query = text("""
                    UPDATE user_save_info 
                    SET ip = :ip, 
                        wp_url = :wp_url, 
                        link_comment = :link_comment
                    WHERE owner_id = :owner_id
                """)
                await db.execute(update_save_query, {
                    "owner_id": user_id,
                    "ip": payload.proxy,
                    "wp_url": payload.siteUrl,
                    "link_comment": payload.linkTopText
                })
                print(f"✅ 유저 {user_id}의 save_info 변경사항 업데이트 완료")
            else:
                print(f"ℹ️ 유저 {user_id}의 save_info 변경사항 없음 (스키마 유지)")

        await db.commit()
    except Exception as e:
        await db.rollback()
        print(f"⚠️ user_save_info 동기화 중 오류 발생: {e}")
    # ----------------------------------------------

    try:
        if not user_status:
            # 처음 발행하는 유저: task_id 추가!
            insert_query = text("""
                INSERT INTO user_work_status (user_id, is_running, last_started_at, current_task_id)
                VALUES (:user_id, :is_running, :last_started_at, :task_id)
            """)
            await db.execute(insert_query, {
                "user_id": user_id,
                "is_running": True,
                "last_started_at": now,
                "task_id": task_id  # <--- 이거 추가
            })
        else:
            # 기존 유저: task_id 업데이트!
            update_query = text("""
                UPDATE user_work_status 
                SET is_running = :is_running, 
                    last_started_at = :last_started_at,
                    current_task_id = :task_id
                WHERE user_id = :user_id
            """)
            await db.execute(update_query, {
                "user_id": user_id,
                "is_running": True,
                "last_started_at": now,
                "task_id": task_id  # <--- 이거 추가
            })

        await db.commit()

        # 백그라운드에서 실행하여 API 응답은 즉시 반환
        asyncio.create_task(start_bulk_posting(payload, task_id, target_api_key))

        return {
            "status": "success",
            "message": "작업이 성공적으로 시작되었습니다.",
            "streamUrl": f"/api/blog/stream/{task_id}", # 실제 ID로 치환됨
            "currentUserId": user_id
        }
    except Exception as e:
        await db.rollback()
        return {
            "status": "error",
            "message": f"서버 오류 발생: {str(e)}",
            "currentUserId": user_id
        }

# 2. 정보 삭제 (Delete) - 누락되었던 부분 추가!
@router.delete("/credentials")
async def delete_user_credential(
    owner_id: int, 
    login_id: str, 
    db: AsyncSession = Depends(get_db)
):
    """
    특정 유저(owner_id)의 특정 로그인 계정(login_id) 정보를 삭제합니다.
    """
    # 쿼리 수정: id 대신 login_id와 owner_id를 조건으로 사용
    query = text("""
        DELETE FROM user_credentials 
        WHERE owner_id = :owner_id AND login_id = :login_id
    """)
    
    try:
        result = await db.execute(query, {"owner_id": owner_id, "login_id": login_id})
        await db.commit()
        
        # 삭제된 행이 0개라면 해당 조건의 데이터가 없는 것
        if result.rowcount == 0:
            raise HTTPException(
                status_code=404, 
                detail="삭제할 계정 정보를 찾을 수 없습니다."
            )
            
        return {
            "status": "success", 
            "message": f"유저 {owner_id}의 계정 '{login_id}' 정보가 삭제되었습니다."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        print(f"Delete Error: {e}")
        raise HTTPException(status_code=500, detail=f"삭제 실패: {str(e)}")

# 3. 정보 리스트 조회 (Read)
@router.get("/credentials/{owner_id}")
async def get_user_credentials(owner_id: int, db: AsyncSession = Depends(get_db)):
    """
    유저의 계정 리스트(user_credentials)와 설정 정보(user_save_info)를 함께 반환합니다.
    """
    
    try:
        # 1. user_credentials 조회 (리스트)
        cred_query = text("""
            SELECT owner_id, login_id, login_pw, proxy_id, proxy_pw, proxy_port 
            FROM user_credentials 
            WHERE owner_id = :owner_id
            ORDER BY id DESC
        """)
        cred_result = await db.execute(cred_query, {"owner_id": owner_id})
        credentials = cred_result.fetchall()

        # 2. user_save_info 조회 (단일 객체)
        save_info_query = text("""
            SELECT ip, wp_url, link_comment 
            FROM user_save_info 
            WHERE owner_id = :owner_id
        """)
        save_info_result = await db.execute(save_info_query, {"owner_id": owner_id})
        save_info = save_info_result.fetchone()

        # 3. 데이터 구조화
        # user_credentials 가공
        cred_list = [
            {
                "owner_id": row.owner_id,
                "login_id": row.login_id,
                "login_pw": row.login_pw,
                "proxy_id": row.proxy_id,
                "proxy_pw": row.proxy_pw,
                "proxy_port": row.proxy_port
            } for row in credentials
        ]

        # user_save_info 가공 (데이터가 없을 경우를 대비해 기본값 처리)
        info_data = {
            "ip": save_info.ip if save_info else "",
            "wp_url": save_info.wp_url if save_info else "",
            "link_comment": save_info.link_comment if save_info else ""
        }

        # 최종 리턴 구조
        return {
            "user_credentials": cred_list,
            "user_save_info": info_data
        }

    except Exception as e:
        print(f"❌ 조회 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail=f"조회 실패: {str(e)}")
    
    
# 2. 실시간 로그 스트림 (단방향 통신 채널)
@router.get("/stream/{task_id}")
async def stream_logs(request: Request, task_id: str):
    print(f"📡 [SSE] 사용자 {task_id} Redis 채널 구독 시작")

    async def event_generator():
        # Redis PubSub 객체 생성
        pubsub = redis_manager.redis_client.pubsub()
        await pubsub.subscribe(f"logs:{task_id}")

        try:
            while True:
                if await request.is_disconnected():
                    break

                # Redis 메세지 확인 (비차단 방식)
                # get_message는 바로 리턴하므로 잠시 대기(sleep)가 필요함
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                
                if message:
                    # Redis에서 온 데이터는 bytes 타입이므로 decode 필수!
                    log_data = message['data']
                    if isinstance(log_data, bytes):
                        log_data = log_data.decode('utf-8')
                    
                    # 3. 종료 신호 체크
                    if log_data == "QUIT":
                        yield {
                            "event": "close",
                            "data": "작업이 모두 완료되었습니다."
                        }
                        break

                    yield {
                        "event": "message",
                        "data": log_data
                    }
                
                # 대기 시간 살짝 줄여서 반응성 높임
                await asyncio.sleep(0.05)

        except Exception as e:
            print(f"⚠️ [SSE] 에러: {e}")
        finally:
            await pubsub.unsubscribe(f"logs:{task_id}")
            await pubsub.close()
            print(f"🛑 [SSE] {task_id} 구독 종료")

    return EventSourceResponse(event_generator(), ping=30)


@router.post("/status/{user_id}")
async def check_user_work_status(user_id: int, db: AsyncSession = Depends(get_db)):
    """
    유저의 현재 작업 상태를 확인하고 정해진 규격에 맞춰 응답을 반환합니다.
    """
    # 1. user_work_status에서 user_id 조회
    query = text("""
        SELECT is_running, current_task_id 
        FROM user_work_status 
        WHERE user_id = :user_id
    """)
    
    try:
        result = await db.execute(query, {"user_id": user_id})
        status_record = result.fetchone()

        # 2. 데이터가 아예 없거나, 작업 중이 아닌 경우 (is_running이 False 또는 0)
        if not status_record or not status_record.is_running:
            return {
                "status": "none",
                "streamUrl": ""
            }

        # 3. 작업 중인 경우 (is_running == True)
        task_id = status_record.current_task_id
        return {
            "status": "working",
            "streamUrl": f"/api/blog/stream/{task_id}" if task_id else ""
        }

    except Exception as e:
        # 에러 발생 시에도 규격을 맞추거나 혹은 에러 메시지를 전달
        print(f"상태 조회 오류: {e}")
        return {
            "status": "none",
            "streamUrl": ""
        }

# 사용자 프로세스 중단
@router.post("/stop/{user_id}")
async def stop_user_posting(user_id: int, db: AsyncSession = Depends(get_db)):
    # 1. DB에서 해당 유저의 현재 실행 중인 task_id 조회
    query = text("""
        SELECT current_task_id, is_running 
        FROM user_work_status 
        WHERE user_id = :user_id
    """)
    result = await db.execute(query, {"user_id": user_id})
    row = result.fetchone()

    # 2. 실행 중이 아니거나 작업 ID가 없으면 종료
    if not row or not row.current_task_id or row.is_running == 0:
        return {"status": "error", "message": "현재 실행 중인 작업이 없거나 이미 종료되었습니다."}
    
    target_task_id = row.current_task_id

    # 3. Redis에 해당 task_id 중단 신호 기록 (1시간 유지)
    await redis_manager.redis_client.set(f"stop_signal:{target_task_id}", "true", ex=3600)
    
    # 4. 사용자에게 즉시 중단 알림 전송 (선택)
    await redis_manager.publish(target_task_id, "🛑 사용자가 작업 중단을 요청했습니다. 곧 종료됩니다.")
    
    print(f"📡 [STOP_SIGNAL] 유저 {user_id}의 태스크 {target_task_id} 중단 신호 발생")
    
    return {
        "status": "success", 
        "user_id": user_id, 
        "stopped_task_id": target_task_id
    }