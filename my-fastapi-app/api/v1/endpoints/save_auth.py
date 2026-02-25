from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession  # 타입 힌트용
from sqlalchemy import text
from pydantic import BaseModel
from api.db.database import get_db

# Table Name: user_credentials
# +------------+--------------+------+-----+---------------------+----------------+
# | Field      | Type         | Null | Key | Default             | Extra          |
# +------------+--------------+------+-----+---------------------+----------------+
# | id         | int(11)      | NO   | PRI | NULL                | auto_increment |
# | owner_id   | int(11)      | NO   | MUL | NULL                |                |
# | site_name  | varchar(100) | NO   |     | NULL                |                |
# | login_id   | varchar(100) | NO   |     | NULL                |                |
# | login_pw   | varchar(255) | NO   |     | NULL                |                |
# | created_at | timestamp    | YES  |     | current_timestamp() |                |
# +------------+--------------+------+-----+---------------------+----------------+

router = APIRouter()

class SaveRequest(BaseModel):
    currunt_user_id: str    # 사용자 고유 id
    site_name: str          # 어떤 사이트인지 추가 (예: 네이버, 티스토리)
    external_id: str        # 사용자 외부 로그인 id
    external_pw: str        # 사용자 외부 로그인 password
    

@router.post("/save-external-account")
async def saveAuth(request:SaveRequest, db: AsyncSession = Depends(get_db)):
    # 1. 중복 조회 쿼리
    # 동일한 owner_id가 동일한 site_name에 똑같은 external_id를 이미 저장했는지 확인
    check_query = text("""
        SELECT id FROM user_credentials 
        WHERE owner_id = :owner_id AND site_name = :site_name AND login_id = :login_id
    """)
    
    result = await db.execute(check_query, {
        "owner_id": request.currunt_user_id,
        "site_name": request.site_name,
        "login_id": request.external_id
    })

    existing_entry = result.fetchone()

    # 2. 이미 존재한다면 에러 반환
    if existing_entry:
        raise HTTPException(status_code=400, detail="이미 해당 사이트에 저장된 동일한 아이디가 있습니다.")

    # 3. 존재하지 않으면 Insert 실행
    insert_query = text("""
        INSERT INTO user_credentials (owner_id, site_name, login_id, login_pw)
        VALUES (:owner_id, :site_name, :login_id, :login_pw)
    """)

    try:
        await db.execute(insert_query, {
            "owner_id": request.currunt_user_id,
            "site_name": request.site_name,
            "login_id": request.external_id,
            "login_pw": request.external_pw  # 필요 시 암호화 로직 추가 가능
        })
        
        # 4. DB 변경사항 확정 (Insert/Update/Delete 시 필수)
        await db.commit()
        
    except Exception as e:
        # 에러 발생 시 롤백
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"저장 중 오류가 발생했습니다: {str(e)}")

    return { 
        "status": "success", 
        "message": f"{request.site_name} 계정 정보가 성공적으로 저장되었습니다." 
    }