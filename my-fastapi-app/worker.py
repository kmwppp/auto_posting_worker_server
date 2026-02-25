import asyncio
import json
import os
import multiprocessing
# redis 직접 임포트 대신 매니저를 가져옵니다.
from api.v1.dependencies.redis_manager import redis_manager 
from api.v1.schemas.blog import BlogBulkRequest
from api.services.blog_service import start_bulk_posting

async def worker_process():
    # 1. 1호점 Redis 주소 강제 주입 (필요시)
    # 이미 .env에 잘 적혀있다면 이 줄은 생략 가능하지만, 확실하게 하기 위해 넣습니다.
    # redis_manager.redis_url = "redis://52.63.149.67:6379/0"

    try:
        # 2. [중요] 전역 redis_manager를 이 프로세스 안에서 연결합니다.
        # 이렇게 해야 start_bulk_posting 내부의 redis_manager가 제대로 작동합니다.
        await redis_manager.connect() 
        print(f"🚀 [일꾼 {multiprocessing.current_process().name}] Redis 전역 매니저 연결 성공!")
    except Exception as e:
        print(f"❌ [일꾼 {multiprocessing.current_process().name}] 연결 실패: {e}")
        return

    while True:
        try:
            # 3. local_redis 대신 redis_manager.redis_client를 사용합니다.
            result = await redis_manager.redis_client.brpop("task_queue", timeout=0)
            
            if result:
                _, raw_data = result
                task_info = json.loads(raw_data)
                task_id = task_info.get('task_id')
                
                print(f"📦 [일꾼 {multiprocessing.current_process().name}] 작업 수신: {task_id}")

                try:
                    payload_obj = BlogBulkRequest(**task_info['payload'])
                    
                    # 이제 함수 내부에서 redis_manager를 호출해도 에러가 나지 않습니다.
                    await start_bulk_posting(
                        payload_obj, 
                        task_id, 
                        task_info.get('target_api_key')
                    )
                    print(f"✅ [일꾼 {multiprocessing.current_process().name}] 작업 완료: {task_id}")

                except Exception as inner_e:
                    print(f"⚠️ [일꾼 {multiprocessing.current_process().name}] 실행 중 에러: {inner_e}")

        except Exception as e:
            print(f"🚨 [워커 루프] 에러 발생: {e}")
            await asyncio.sleep(2)

def main():
    num_workers = 50
    processes = []
    print(f"🔥 총 {num_workers}개의 일꾼 프로세스를 가동합니다.")

    for i in range(num_workers):
        p = multiprocessing.Process(
            target=lambda: asyncio.run(worker_process()), 
            name=f"Worker-{i+1}"
        )
        p.daemon = True 
        p.start()
        processes.append(p)

    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        print("🛑 중단됨")

if __name__ == "__main__":
    main()