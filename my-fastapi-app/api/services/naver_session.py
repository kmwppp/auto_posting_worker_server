import asyncio
import os
from playwright.async_api import async_playwright
from api.v1.dependencies.redis_manager import redis_manager
from api.services.utils import log_to_db

async def save_naver_session(naver_id, naver_pw, current_user_id, task_id, proxy_config=None):
    async with async_playwright() as p:
        print(f"🔐 {naver_id} 로그인 세션 생성 중...")
        # Redis 전송
        await redis_manager.publish(task_id, f"🔐 [{naver_id}] 네이버 로그인 시도 중...", current_user_id)
        # DB 로그 기록
        await log_to_db(current_user_id, naver_id, f"세션 생성 ID: {naver_id}", "네이버 로그인 브라우저 실행 중")

        # 브라우저 실행
        browser = await p.chromium.launch(
            headless=True,
            proxy=proxy_config,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
                "--js-flags='--max-old-space-size=256'"
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 800}
        )

        page = await context.new_page()
        page.set_default_timeout(60000)

        # ✨ [은밀 모드 적용] ✨
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        # 1. 네이버 로그인 페이지 접속 (재시도 로직 포함)
        success = False
        for attempt in range(3):
            try:
                print(f"🔐 네이버 로그인 페이지 접속 중... (시도 {attempt+1}/3)")
                await page.goto("https://nid.naver.com/nidlogin.login", timeout=30000)
                success = True
                break
            except Exception as e:
                if attempt < 2:
                    print(f"⚠️ 터널링 에러 또는 접속 지연 발생: {e}. 3초 후 재시도...")
                    await asyncio.sleep(3)
                else:
                    raise e

        if success:
            await asyncio.sleep(1)

        # 아이디/비밀번호 입력
        await page.evaluate(f'() => {{ document.querySelector("#id").value = "{naver_id}"; }}')
        await asyncio.sleep(0.5)
        await page.evaluate(f'() => {{ document.querySelector("#pw").value = "{naver_pw}"; }}')
        await asyncio.sleep(1)

        # 로그인 버튼 클릭
        await page.click(r"#log\.login")
        
        try:
            # 로그인 성공 대기
            await page.wait_for_url("https://www.naver.com/", timeout=30000)
            
            # sessions 폴더 생성 및 저장
            os.makedirs("sessions", exist_ok=True)
            auth_path = os.path.join("sessions", f"auth_{naver_id}.json")

            await context.storage_state(path=auth_path)
            await browser.close()
            
            print(f"✅ {naver_id} 세션 저장 완료")
            await redis_manager.publish(task_id, f"✅ [{naver_id}] 로그인 성공 및 세션 확보", current_user_id)
            await log_to_db(current_user_id, naver_id, f"세션 생성 ID: {naver_id}", "네이버 로그인 세션 생성 완료")
            
            return auth_path

        except Exception as e:
            print(f"❌ {naver_id} 로그인 실패: {e}")
            await redis_manager.publish(task_id, f"❌ [{naver_id}] 로그인 실패. (비번 확인 또는 2차 인증 필요)", current_user_id)
            await browser.close()
            await log_to_db(current_user_id, naver_id, f"세션 생성 ID: {naver_id}", f"로그인 세션 생성 실패: {str(e)}", status="FAIL", error=e)
            return None