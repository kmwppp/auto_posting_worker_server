import redis.asyncio as redis  # 비동기용 라이브러리로 변경
import asyncio

class RedisPubSubManager:
    def __init__(self):
        self.redis_client = None

    async def connect(self):
        """서버 시작할 때 딱 한 번 호출"""
        self.redis_client = redis.Redis(host='52.63.149.67', port=6379, db=0, decode_responses=True)
        # 연결 확인용 테스트
        await self.redis_client.ping()

    async def publish(self, task_id: str, message: str, user_id: str = None):
        """
        비동기로 로그 전송 + DB 저장 (방어적 설계 버전)
        """
        # 1. 실시간 레디스 발행
        if self.redis_client:
            await self.redis_client.publish(f"logs:{task_id}", message)

        # 2. DB 기록 (필요할 때만 모듈을 불러와서 세션 생성)
        if user_id:
            try:
                # 📍 [수정] 함수 내부 임포트로 순환 참조 원천 차단
                from api.db.database import publish_and_log, AsyncSessionLocal
                
                async with AsyncSessionLocal() as db:
                    await publish_and_log(db, user_id, task_id, message)
            except Exception as e:
                print(f"⚠️ Redis 매니저 내 DB 세션 기록 중 오류: {e}")

    async def stop_task(self, task_id: str):
        """비동기로 종료 신호 전송"""
        if self.redis_client:
            await self.redis_client.publish(f"logs:{task_id}", "QUIT")

    async def disconnect(self):
        """서버 종료 시 연결 닫기"""
        if self.redis_client:
            await self.redis_client.close()

redis_manager = RedisPubSubManager()