import asyncio
from typing import Dict, List

# 유저 아이디 별로 asynico.Queue를 담을 딕셔너리
class ConnectionManager:
    def __init__(self):
        # task_id 별로 접속한 '모든' 클라이언트의 큐 리스트를 관리
        self.active_connections: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """새로운 클라이언트가 접속할 때마다 전용 큐를 생성해서 반환"""
        if task_id not in self.active_connections:
            self.active_connections[task_id] = []
        
        new_queue = asyncio.Queue()
        self.active_connections[task_id].append(new_queue)
        return new_queue

    async def broadcast(self, task_id: str, message: str):
        """해당 task_id를 보고 있는 모든 클라이언트 큐에 메시지를 뿌림"""
        if task_id in self.active_connections:
            # 큐 리스트를 순회하며 각각의 큐에 메시지 전달
            for queue in self.active_connections[task_id]:
                await queue.put(message)

    def disconnect(self, task_id: str, queue: asyncio.Queue):
        """클라이언트 접속 종료 시 해당 큐만 리스트에서 제거"""
        if task_id in self.active_connections:
            if queue in self.active_connections[task_id]:
                self.active_connections[task_id].remove(queue)
            # 해당 task_id를 보는 사람이 아무도 없으면 딕셔너리에서 삭제
            if not self.active_connections[task_id]:
                del self.active_connections[task_id]

manager = ConnectionManager()