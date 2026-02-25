from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession  # 타입 힌트용
from sqlalchemy import text
from pydantic import BaseModel
from api.db.database import get_db

router = APIRouter()

class LoginRequest(BaseModel):
    user_id: str
    password: str

# 상태 변경 요청을 위한 모델
class StatusUpdateRequest(BaseModel):
    id: int      # 대상 사용자 고유 ID
    status: int  # 변경할 상태 값 (0: 대기, 1: 정상, 2: 정지 등)

class SuccessListRequest(BaseModel):
    id: int  # 로그 테이블의 user_id와 매칭

# 관리자 전용 로그인
@router.post("/login")
async def admin_login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    # 1. admin_users 테이블에서 비동기 쿼리 실행
    # (id, user_id, password 만 있는 테이블 구조 기준)
    query = text("SELECT id, user_id, password FROM admin_users WHERE user_id = :user_id")
    result = await db.execute(query, {"user_id": request.user_id})
    
    # 2. 결과 가져오기
    admin = result.fetchone()

    # 등록된 관리자 계정이 없는 경우
    if not admin:
        return {
            "status": "fail",
            "message": "등록된 관리자 계정이 없습니다.",
            "user_id": "",
            "error_code" : "01"
        }

    # 비밀번호가 틀린 경우 (admin[2]가 password)
    if request.password != admin[2]:
        return {
            "status": "fail",
            "message": "관리자 비밀번호가 일치하지 않습니다.",
            "user_id": "",
            "error_code" : "02"
        }

    # 관리자 로그인 성공
    return {
        "status": "success",
        "message": f"관리자 {admin[1]}님, 시스템에 접속합니다.",
        "user_id": f"{admin[0]}",
        "error_code": "00"
    }

# 등록된 사용자 리스트
@router.post("/userList")
async def get_user_list(db: AsyncSession = Depends(get_db)):
    # 1. id 컬럼을 추가하여 쿼리 실행
    query = text("SELECT id, user_id, full_name, status FROM users ORDER BY created_at DESC")
    result = await db.execute(query)
    
    # 2. 모든 행 가져오기
    rows = result.fetchall()

    if not rows:
        return {
            "status": "success",
            "message": "등록된 사용자가 없습니다.",
            "data": []
        }

    # 3. id를 포함하여 리스트 생성 (row[0]이 id가 됩니다)
    user_list = [
        {
            "id": row[0],
            "user_id": row[1],
            "full_name": row[2],
            "status": row[3]
        } for row in rows
    ]

    return {
        "status": "success",
        "message": f"총 {len(user_list)}명의 사용자를 불러왔습니다.",
        "data": user_list
    }

# 사용자 상태 업데이트
@router.post("/status")
async def update_user_status(request: StatusUpdateRequest, db: AsyncSession = Depends(get_db)):
    # 1. 해당 사용자가 존재하는지 먼저 확인 (선택 사항이지만 안전함)
    check_query = text("SELECT id FROM users WHERE id = :id")
    check_result = await db.execute(check_query, {"id": request.id})
    if not check_result.fetchone():
        return {
            "status": "error",
            "message": "해당 ID를 가진 사용자를 찾을 수 없습니다.",
            "error_code": "01"
        }

    # 2. status 업데이트 수행
    update_query = text("UPDATE users SET status = :status, updated_at = CURRENT_TIMESTAMP WHERE id = :id")
    
    try:
        await db.execute(update_query, {"status": request.status, "id": request.id})
        await db.commit()  # 변경 사항 DB 반영
        
        return {
            "status": "success",
            "message": f"ID {request.id} 사용자의 상태가 {request.status}(으)로 변경되었습니다.",
            "data": {
                "id": request.id,
                "status": request.status
            }
        }
    except Exception as e:
        await db.rollback() # 오류 발생 시 롤백
        return {
            "status": "error",
            "message": f"업데이트 중 오류가 발생했습니다: {str(e)}",
            "error_code": "99"
        }

@router.post("/userInfo/successList")
async def get_user_success_list(request: SuccessListRequest, db: AsyncSession = Depends(get_db)):
    # 2. 쿼리 수정 (매개변수 이름을 모델 필드와 일치시키는 게 안 헷갈립니다)
    query = text("""
        SELECT DATE(created_at) as work_date, COUNT(id) as count
        FROM user_post_success_logs
        WHERE user_id = :val_id
        GROUP BY DATE(created_at)
        ORDER BY work_date DESC
    """)
    
    try:
        # 3. request.id (int)를 쿼리의 :val_id에 정확히 매핑
        result = await db.execute(query, {"val_id": request.id})
        rows = result.fetchall()
        
        success_data = [
            {"date": str(row.work_date), "count": row.count}
            for row in rows
        ]
        
        return {
            "status": "success",
            "message": f"'{request.id}'님의 일자별 작업 통계를 성공적으로 불러왔습니다.",
            "data": success_data
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"통계를 불러오는 중 오류가 발생했습니다: {str(e)}",
            "error_code": "98"
        }