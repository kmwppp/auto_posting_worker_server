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

    async def publish(self, task_id: str, message: str):
        """비동기로 로그 전송"""
        if self.redis_client:
            await self.redis_client.publish(f"logs:{task_id}", message)

    async def stop_task(self, task_id: str):
        """비동기로 종료 신호 전송"""
        if self.redis_client:
            await self.redis_client.publish(f"logs:{task_id}", "QUIT")

    async def disconnect(self):
        """서버 종료 시 연결 닫기"""
        if self.redis_client:
            await self.redis_client.close()

redis_manager = RedisPubSubManager()