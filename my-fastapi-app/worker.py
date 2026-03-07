import asyncio
import json
import os
import multiprocessing
import time
# redis 직접 임포트 대신 매니저를 가져옵니다.
from api.v1.dependencies.redis_manager import redis_manager 
from api.v1.schemas.blog import BlogBulkRequest
from api.services.blog_service import start_bulk_posting

async def connect_redis():
    for _ in range(5):
        try:
            await redis_manager.connect()
            return
        except Exception:
            await asyncio.sleep(2)
    raise Exception("Redis connection failed")

async def worker_process():
    """일꾼: 작업 딱 1개만 하고 스스로 종료함"""
    try:
        # 1. Redis 연결
        await connect_redis()
        print(f"🚀 [일꾼 {multiprocessing.current_process().name}] Redis 연결 성공!")
        
        # 2. 작업 하나 가져오기 (무한루프 while True 제거됨)
        # timeout을 주지 않으면 작업이 올 때까지 여기서 대기합니다.
        result = await redis_manager.redis_client.brpop("task_queue", timeout=0)
        
        if result:
            _, raw_data = result
            task_info = json.loads(raw_data)
            task_id = task_info.get('task_id')
            
            print(f"📦 [일꾼 {multiprocessing.current_process().name}] 작업 수신: {task_id}")

            try:
                payload_obj = BlogBulkRequest(**task_info['payload'])
                
                # 실제 포스팅 작업 (10시간이 걸려도 끝날 때까지 기다림)
                await start_bulk_posting(
                    payload_obj, 
                    task_id, 
                    task_info.get('target_api_key')
                )
                print(f"✅ [일꾼 {multiprocessing.current_process().name}] 작업 완료! 메모리 정리를 위해 종료합니다.")

            except Exception as inner_e:
                print(f"⚠️ [일꾼 {multiprocessing.current_process().name}] 실행 중 에러: {inner_e}")
    
    except Exception as e:
        print(f"🚨 [일꾼] 치명적 에러: {e}")
    finally:
        # 연결 해제 후 프로세스 종료 (자연스럽게 죽음)
        try:
            await redis_manager.disconnect()
        except Exception:
            pass

def run_worker():
    asyncio.run(worker_process())

def main():
    num_workers = 50
    processes = {}  # PID를 키로 관리하여 더 정확하게 체크
    print(f"🔥 총 {num_workers}개의 일꾼 프로세스를 '상시 충원' 모드로 가동합니다.")

    try:
        while True:
            # 1. 죽은 일꾼(작업 마치고 종료된 프로세스) 정리
            dead_pids = [pid for pid, p in processes.items() if not p.is_alive()]
            for pid in dead_pids:
                processes[pid].join() # 좀비 프로세스 방지
                del processes[pid]
                print(f"🧹 일꾼(PID: {pid}) 작업 완료 후 퇴근. 현재 남은 일꾼: {len(processes)}명")

            # 2. 부족한 만큼 새 일꾼 투입 (항상 50개 유지)
            while len(processes) < num_workers:
                # 프로세스 이름에 타임스탬프를 넣어 중복 방지
                p = multiprocessing.Process(
                    target=run_worker,
                    name=f"Worker-{time.time()}"
                )
                # p.daemon = True 
                p.start()
                processes[p.pid] = p
                print(f"➕ 새 일꾼 투입 (PID: {p.pid}). 총 일꾼: {len(processes)}명")

            # 3. 메인 루프 과부하 방지를 위한 짧은 휴식
            time.sleep(1)

    except KeyboardInterrupt:
        print("🛑 운영 중단 - 모든 일꾼을 해산합니다.")
        for p in processes.values():
            p.terminate()

if __name__ == "__main__":
    main()