from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession  # 타입 힌트용
from sqlalchemy import text
from pydantic import BaseModel, Field
from api.db.database import get_db
from openai import AsyncOpenAI # 비동기 클라이언트를 씁시다

router = APIRouter()

class LoginRequest(BaseModel):
    user_id: str
    password: str

class RegistRequest(BaseModel):
    user_id: str
    password: str
    name: str
    phoneNum: str
    # 무조건 있어야 하므로 기본값 없이 선언
    openai_key: str = Field(..., description="OpenAI API Key")


# RESPONSE
# status : success / fail / error
# message : "환영합니다."
# user_id : 1
# error_code : "00" / "01" / "02" / "03" / "04"
# 00 : 로그인 성공 / 01 : 등록된 계정 없음 / 02 : 아이디, 비밀번호가 일치하지 않는 경우 / 
# 03 : 가입 승인 대기중인 경우 / 04 : 정지된 계정인 경우
@router.post("/login")
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    # 1. 비동기 쿼리 실행 (반드시 await를 붙여야 합니다)
    query = text("SELECT id, user_id, password, status FROM users WHERE user_id = :user_id")
    result = await db.execute(query, {"user_id": request.user_id})
    
    # 2. 결과 가져오기 (비동기 결과 객체에서 한 행을 추출)
    user = result.fetchone()

    # 등록된 계정이 없는 경우
    if not user:
        return {
            "status": "fail",
            "message": "현재 등록된 계정이 없습니다.",
            "user_id": "",
            "error_code" : "01"
        }

    # 아이디의 비밀번호가 틀린 경우
    if request.password != user[2]:
        return {
            "status": "fail",
            "message": "아이디 또는 비밀번호가 틀렸습니다.",
            "user_id": "",
            "error_code" : "02"
        }
    # 가입 승인 대기중인 경우
    if user[3] == 0:
        return {
            "status": "fail",
            "message": "해당 계정은 현재 가입 승인 대기중인 계정입니다.",
            "user_id": "",
            "error_code" : "03"
        }

    # 계정이 정지중인 경우
    if user[3] == 2:
        return {
            "status": "fail",
            "message": "해당 계정은 현재 정지된 계정입니다.",
            "user_id": "",
            "error_code" : "04"
        }

    # 로그인을 성공한 경우
    return {
        "status": "success",
        "message": f"{user[1]}님 환영합니다!",
        "user_id": f"{user[0]}",
        "error_code": "00"
    }

@router.post("/regist")
async def regist(request: RegistRequest, db: AsyncSession = Depends(get_db)):
    # 1. 현재 가입된 정보가 있는지 찾는다
    # 2. 가입된 정보가 있다면 가입된 정보가 있다고 리턴한다
    # 3. 가입된 정보가 없다면 회원가입이 성공한다.

    # 1. 아이디 중복만 체크 (전화번호는 중복 허용)
    check_query = text("SELECT user_id FROM users WHERE user_id = :user_id")
    result = await db.execute(check_query, {"user_id": request.user_id})
    existing_user = result.fetchone()

    if existing_user:
        return {
            "status": "fail", 
            "message": "이미 사용 중인 아이디입니다.", 
            "error_code": "01"
        }

    # --- OpenAI API 키 길이 및 공백 검증 ---
    api_key = request.openai_key.strip()
    if len(api_key) < 10:
        return {
            "status": "fail",
            "message": "OpenAI API 키가 너무 짧거나 올바르지 않습니다. 다시 확인해주세요.",
            "error_code": "02"
        }

    # 2. OpenAI API 키 유효성 검증
    print(f"🔍 API 키 유효성 검사 시작: {request.openai_key[:10]}...")
    if not await is_valid_openai_key(request.openai_key.strip()):
        return {
            "status": "fail",
            "message": "입력하신 OpenAI API 키가 유효하지 않습니다. 키를 다시 확인해주세요.",
            "error_code": "03"
        }
    # ------------------------------------------

    insert_query = text("""
        INSERT INTO users (user_id, password, full_name, phone_number, birth_date, status, open_api_key)
        VALUES (:user_id, :password, :full_name, :phone_number, :birth_date, :status, :open_api_key)
    """)

    try:
        # 2. 실행 (Request 모델의 필드와 DB 컬럼 매핑)
        await db.execute(insert_query, {
            "user_id": request.user_id,
            "password": request.password,      # 실제 운영 시 해싱 필수!
            "full_name": request.name,         # name -> full_name
            "phone_number": request.phoneNum,  # phoneNum -> phone_number
            "birth_date": "1900-01-01",        # DB가 NO NULL이므로 임시값 투입
            "status": 0,                       # 대기 상태
            "open_api_key": request.openai_key.strip()  # 무조건 직접 투입
        })
        
        # 3. 트랜잭션 커밋
        await db.commit()
        return {"status": "success", "message": "회원가입 신청을 하였습니다. 관리자에게 문의하여 승인을 받아주세요.", "error_code" : "00"}

    except Exception as e:
        # 에러 발생 시 롤백
        print(e)
        await db.rollback()
        return {"status": "error", "message": str(e), "error_code" : "99"}
    

async def is_valid_openai_key(api_key: str) -> bool:
    """OpenAI API 키가 유효한지 체크합니다."""
    try:
        # 비동기 클라이언트 생성
        temp_client = AsyncOpenAI(api_key=api_key)
        
        # 모델 리스트를 조회해봅니다 (실제 AI 생성을 안 하므로 비용 X)
        # 키가 틀리면 여기서 바로 에러가 납니다.
        await temp_client.models.list()
        return True
    except Exception as e:
        print(f"❌ API Key 검증 실패: {e}")
        return False