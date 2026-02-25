import os
from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from api.v1.endpoints import auth # 로그인 파일 임포트
from api.v1.endpoints import save_auth # 유저 외부 웹 로그인 정보 임포트
from api.v1.endpoints import blog # 추가
from api.v1.endpoints import admin # 관리자용 추가
from fastapi.middleware.cors import CORSMiddleware
from api.v1.dependencies.redis_manager import redis_manager

# 1. 환경 변수 로드
load_dotenv()

app = FastAPI(title="My Project API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 테스트 시에는 전체 허용, 운영 시에는 플러터 도메인만
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. 메인 라우터 생성 (공통 기능을 담음)
# 여기서 prefix="/api"를 한 번만 선언합니다.
main_router = APIRouter(prefix="/api")


@main_router.get("/")
def home():
    api_key = os.getenv("OPENAI_API_KEY")
    db_user = os.getenv("DB_USER")
    return {"db_user": db_user, "api_key": api_key}

@main_router.get("/hello")
def say_hello():
    return {"message": "안녕하세요! 서버가 정상 동작 중입니다.22"}

@app.on_event("startup")
async def startup_event():
    await redis_manager.connect()

@app.on_event("shutdown")
async def shutdown_event():
    await redis_manager.disconnect()



# 3. 다른 기능(auth 등)의 라우터를 메인 라우터에 합치기
# 이렇게 하면 auth.py 안에 있는 /login 은 자동으로 /api/login 이 됩니다.

# 로그인 파일 Api
main_router.include_router(auth.router, tags=["Authentication"])

# 유저 외부 웹 로그인 정보 기입 Api
main_router.include_router(save_auth.router, tags=["Authentication"])

# 포스팅 라우터 추가
main_router.include_router(blog.router, prefix="/blog", tags=["Blog"])

# 관리자 라우터 추가
main_router.include_router(admin.router, prefix="/admin", tags=["Admin"])

# 4. 최종적으로 메인 라우터를 앱에 등록
app.include_router(main_router)