import asyncio
import os
import requests
import time
import uuid
from playwright.async_api import async_playwright
from openai import OpenAI
from api.db.database import AsyncSessionLocal
from api.v1.dependencies.redis_manager import redis_manager
from sqlalchemy import text
# 워드프레스 검색을 돕기 위한 Kiwi 맞춤법 교정
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import random
from playwright_stealth import Stealth

# 필요한 모델(BlogBulkRequest 등) 임포트
from api.v1.schemas.blog import BlogBulkRequest, PostingTermType, MainBlogType, PostType, PostTitleType

# 유틸 함수 임포트
from api.services.utils import (
    get_smart_wrapped_text,
    log_to_db,
    check_abort
)

# gpt 원고 생성 파일 임포트
from api.services.gpt_create import generate_blog_content, ChatGptError

# 워드프레스, 블로그 스팟 URL 서치 파일 임포트
from api.services.url_search import get_wordpress_post_url, get_blogspot_post_url

# 네이버 세션 저장 파일 임포트
from api.services.naver_session import save_naver_session

# 네이버 큐알 생성 파일 임포트
from api.services.create_qr import get_naver_qr_url

# 블로그 글쓰기 임포트
from api.services.blog_write import post_to_blog, BlogIdError

# 이미지 설정
DOMAIN = "https://hntrack.co.kr"
IMAGE_SAVE_DIR = "./static/blog_images"
URL_PATH = "/static/blog_images"
os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)
# 전역 변수 설정
BASE_DELAY = 5

class ProxyTimeoutError(Exception):
    """프록시 지연 시간이 너무 길 때 발생하는 커스텀 에러"""
    pass


def get_proxy_config(payload, user):
    global BASE_DELAY # 전역 변수를 함수 내부에서 수정하겠다고 선언
    
    if not payload.proxyUse:
        BASE_DELAY = 5 # 프록시 안 쓰면 기본값 유지
        return None
    
    base_ip = payload.proxy.replace("http://", "").split(":")[0]
    proxy_server = f"http://{base_ip}:{user.port}"
    
    # --- ⚡ 프록시 속도 체크 및 BASE_DELAY 갱신 ---
    proxies = {
        "http": f"http://{user.proxy_id}:{user.proxy_pw}@{base_ip}:{user.port}",
        "https": f"http://{user.proxy_id}:{user.proxy_pw}@{base_ip}:{user.port}",
    }

    try:
        start_time = time.time()
        # 네이버 응답 체크
        requests.head("https://blog.naver.com", proxies=proxies, timeout=10)
        latency = time.time() - start_time
        
        # 1. 지연 시간에 따른 BASE_DELAY 설정
        if latency < 1.0:
            BASE_DELAY = 5
        elif latency < 2.5:
            BASE_DELAY = 10
        elif latency < 4.0:
            BASE_DELAY = 15
        elif latency < 5.5:
            BASE_DELAY = 20
        else:
            # 🧨 [수정 포인트 1] 5.5초 이상이면 에러를 던져서 메인 로직을 종료시킴
            raise ProxyTimeoutError(f"프록시 지연 시간 초과 ({latency:.2f}s). 작업을 중단합니다.")
            
        print(f"📡 지연시간: {latency:.2f}s -> BASE_DELAY 갱신: {BASE_DELAY}s")

    except requests.exceptions.RequestException as e:
        # 에러 메시지를 문자열로 변환해서 407(인증 오류)이 있는지 확인합니다.
        error_msg = str(e)
        
        if "407" in error_msg:
            # 🧨 [수정 포인트] 인증 에러일 경우 구체적인 확인 메시지 출력
            raise ProxyTimeoutError(f"프록시 연결 실패: 프록시 IP, ID, Password, Port를 다시 확인해주세요.")
        else:
            # 그 외의 일반적인 연결 에러일 경우 기존 방식 유지
            raise ProxyTimeoutError(f"프록시 연결 실패 또는 응답 없음: {e}")

    # 리턴값은 형님 코드 "그대로" 유지
    return {
        "server": proxy_server,
        "username": user.proxy_id,
        "password": user.proxy_pw
    }

# --- 5. 메인 제어 로직 ---
async def start_bulk_posting(payload: BlogBulkRequest, task_id: str, api_key: str):

    # [핵심] 이 함수 안에서만 쓰일 유저 전용 클라이언트 생성
    # 변수명을 전역과 똑같이 'client'로 지으면 함수 내부에서는 전역 변수를 무시합니다.
    client = OpenAI(api_key=api_key)

    # 1. 이 전체 작업의 주인 ID 추출
    user_system_id = payload.authList[0].current_user_id

    # finally 에러 방지
    browser = None
    try:
        # [추가] 백그라운드 작업 시작 시 비동기 DB 세션 생성
        # async with AsyncSessionLocal() as db:

        # --- [수정] 사용할 타이틀 리스트 결정 로직 ---
        if payload.postTitleType == PostTitleType.KEYWORD:
            target_list = payload.postKeywordTitleList or []
            print(f"📋 키워드 기반 포스팅 모드 (총 {len(target_list)}개)")
        else:
            target_list = payload.postURLTitleList or []
            print(f"📋 URL 기반 포스팅 모드 (총 {len(target_list)}개)")

        # 전체 제목 리스트에서 현재 어디까지 썼는지 기억하는 인덱스
        current_post_idx = 0 
        total_titles_count = len(target_list)

        for user in payload.authList:

            # 🚩 [수정 포인트 1] 계정 로그인 시도 전, 남은 키워드가 있는지 먼저 확인
            if current_post_idx >= total_titles_count:
                msg = "🏁 모든 키워드/제목 소진으로 작업을 종료합니다."
                print(msg)
                await redis_manager.publish(task_id, msg, user_system_id)
                break # 전체 계정 루프 탈출 (더 이상 로그인 안 함)
            
            # 🚩 [체크 포인트 1] 다음 계정으로 넘어가기 전 확인
            await check_abort(task_id)

            auth_path = None
            # ✨ [유저별 예약 대기 로직 시작]
            if payload.postingTermType == PostingTermType.RESERVATION:
                await log_to_db(
                    user.current_user_id, 
                    user.external_id, 
                    "예약 대기", 
                    f"[{user.external_id}] 계정 작업 시작 전 {payload.postingTerm}분 대기 중..."
                )
                msg = f"[{user.external_id}] 계정 작업 시작 전 {payload.postingTerm}분 대기 중..."
                # 🧨 고칠 곳: manager.broadcast -> redis_manager.publish
                await redis_manager.publish(task_id, msg, user_system_id)
                await asyncio.sleep(0.1)
                wait_seconds = payload.postingTerm * 60
                # 🧨 1분 단위가 아니라 10초 단위로 쪼개서 체크하면 더 빠릿합니다.
                for _ in range(0, wait_seconds, 10):
                    await check_abort(task_id) # 여기서 멈추면 바로 finally로!
                    await asyncio.sleep(10)
            # ✨ [유저별 예약 대기 로직 끝]

            proxy_config = get_proxy_config(payload, user)

            # [추가] 세션 생성 시작 로그
            await log_to_db(user.current_user_id, user.external_id, "세션 생성", "네이버 로그인 세션 생성 시작")

            # 2. 세션 저장 시 프록시 적용
            try:
                auth_path = await save_naver_session(user.external_id, user.external_pw, user.current_user_id, task_id, proxy_config)
                
                if auth_path is None:
                    # 🧨 고칠 곳
                    await redis_manager.publish(task_id, f"⚠️ [{user.external_id}] 로그인 실패로 작업을 건너뜁니다.", user_system_id)
                    await asyncio.sleep(0.1)
                    continue # 다음 유저로 넘어감
                    
                # 🧨 고칠 곳
                await redis_manager.publish(task_id, f"✅ [{user.external_id}] 세션 로드 성공. 포스팅을 시작합니다.", user_system_id)
                await asyncio.sleep(0.1)

            except Exception as e:
                # 2. 함수 실행 중 예상치 못한 치명적 에러 발생 (브라우저 크래시 등)
                error_msg = f"🚨 [{user.external_id}] 시스템 오류 발생: {str(e)}"
                print(error_msg)
                # 🧨 고칠 곳
                await redis_manager.publish(task_id, error_msg, user_system_id)
                await asyncio.sleep(0.1)
                await log_to_db(user.current_user_id, user.external_id, "시스템 에러", error_msg, status="FAIL")
                continue # 다음 유저로 넘어감

            for i in range(user.postingCount):

                # 🚩 [체크 포인트 2] 매 포스팅 시작 전 확인
                await check_abort(task_id)

                if current_post_idx >= total_titles_count:
                    print("⚠️ 준비된 키워드/제목 리스트를 모두 소진했습니다.")
                    break

                # --- [수정] 데이터 매핑 분기 ---
                post_data = target_list[current_post_idx]
                current_post_idx += 1

                # 공통 변수 초기화
                main_keyword_for_search = ""
                current_posting_title = post_data.posting_title
                fixed_url = None

                if payload.postTitleType == PostTitleType.KEYWORD:
                    # PostKeywordModel 구조 사용
                    main_keyword_for_search = post_data.main_keyword
                else:
                    # PostUrlModel 구조 사용 (검색 없이 직접 주소 사용)
                    fixed_url = post_data.url
                
                # 🚩 [체크 포인트] 원고 생성 전 확인
                await check_abort(task_id)

                await log_to_db(user.current_user_id, user.external_id, current_posting_title, "GPT 원고 생성 중")
                # 🧨 고칠 곳
                await redis_manager.publish(task_id, f"🧠 GPT 원고 생성 중: {current_posting_title}", user_system_id)
                await asyncio.sleep(0.1)
                
                # GPT 생성 로직
                blog_data = await generate_blog_content(current_posting_title, payload.aiWriteRole, client)

                if not blog_data:
                    msg = f"❌ [{user.external_id}] 원고 생성 실패로 이번 포스팅은 건너뜁니다."
                    # 🧨 고칠 곳
                    await redis_manager.publish(task_id, msg, user_system_id)
                    await asyncio.sleep(0.1)
                    await log_to_db(user.current_user_id, user.external_id, current_posting_title, msg, status="FAIL")
                    continue

                # --- [단계 2: 이미지 생성] ---
                # 🚩 [체크 포인트] 이미지 생성 전 확인
                await check_abort(task_id)
                # 🧨 고칠 곳
                await redis_manager.publish(task_id, f"🎨 이미지 생성 중...", user_system_id)
                await asyncio.sleep(0.1)
                blog_data = await generate_blog_images(user.current_user_id, blog_data, task_id, user_system_id)
                # 🧨 고칠 곳
                await redis_manager.publish(task_id, f"✨ 모든 이미지 작업이 완료되었습니다.", user_system_id)
                await asyncio.sleep(0.1)

                # 브라우저 실행 및 포스팅

                try:
                    async with async_playwright() as p:
                        browser = await p.chromium.launch(
                            headless=True,
                            proxy=proxy_config,
                            args=[
                                "--disable-blink-features=AutomationControlled", # 자동화 제어 신호 비활성화
                                "--disable-infobars",
                                "--no-sandbox",
                                "--disable-dev-shm-usage",
                                "--disable-extensions",
                                "--disable-features=IsolateOrigins,site-per-process",
                                "--disable-gpu",
                                # --single-process 등 비표준 인자는 제거하여 일반 브라우저처럼 보이게 함
                            ]
                        )

                        # 실제 브라우저 버전에 맞는 UA 생성
                        version_raw = browser.version

                        if "/" in version_raw:
                            browser_version = version_raw.split("/")[1]
                        else:
                            browser_version = version_raw

                        user_agent = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser_version} Safari/537.36"


                        # 2. 컨텍스트 설정 (기존 유지 + 뷰포트 고정으로 로딩 속도 향상)
                        context = await browser.new_context(
                            storage_state=auth_path,
                            # 최신 크롬 버전과 유사하게 유지 (주기적 업데이트 필요)
                            user_agent=user_agent,
                            viewport={'width': 1920, 'height': 1080}, # 일반적인 모니터 해상도 사용
                            device_scale_factor=1,
                            is_mobile=False,
                            has_touch=False,
                            locale="ko-KR",
                            timezone_id="Asia/Seoul"
                        )

                        # ===== fingerprint 위장 스크립트 =====
                        await context.add_init_script("""
                        
                        // webdriver 제거
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined
                        });

                        // CPU 코어 수 위장
                        Object.defineProperty(navigator, 'hardwareConcurrency', {
                            get: () => 8
                        });

                        // 메모리 위장
                        Object.defineProperty(navigator, 'deviceMemory', {
                            get: () => 8
                        });

                        // plugins 위장
                        Object.defineProperty(navigator, 'plugins', {
                            get: () => [1,2,3,4,5]
                        });

                        // languages 위장
                        Object.defineProperty(navigator, 'languages', {
                            get: () => ['ko-KR', 'ko']
                        });

                        // Chrome runtime 위장
                        window.chrome = {
                            runtime: {},
                            loadTimes: function(){},
                            csi: function(){}
                        };

                        // permissions 위장
                        const originalQuery = window.navigator.permissions.query;

                        window.navigator.permissions.query = (parameters) => {

                            if (parameters.name === 'notifications') {
                                return Promise.resolve({ state: Notification.permission });
                            }

                            return originalQuery(parameters);
                        };

                        // WebGL vendor 위장
                        const getParameter = WebGLRenderingContext.prototype.getParameter;
                        WebGLRenderingContext.prototype.getParameter = function(parameter) {

                            if (parameter === 37445 || parameter === 7936) {
                                return 'Intel Inc.';
                            }

                            if (parameter === 37446 || parameter === 7937) {
                                return 'Intel Iris OpenGL Engine';
                            }

                            return getParameter.call(this, parameter);
                        };

                        """)
                        
                        page = await context.new_page()

                        # 3. ✨ [강력한 은밀 모드 적용] ✨
                        # 단순히 webdriver만 지우는 것이 아니라 브라우저의 모든 자동화 흔적을 지웁니다.
                        stealth = Stealth()
                        await stealth.apply_stealth_async(page)

                        # 기본 타임아웃 설정 (모든 작업에 개별 타임아웃 주느라 코드 지저분해지는 것 방지)
                        page.set_default_timeout(60000)

                        # 필요 시 컨텍스트에 직접 권한 부여 (더 확실한 방법)
                        await context.grant_permissions(['clipboard-read', 'clipboard-write'])

                        short_url = None
                        main_site_url = None

                        # 🚩 [체크 포인트] URL 추출 전 확인
                        await check_abort(task_id)

                        # --- [수정] URL 추출 로직 분기 ---
                        if payload.postTitleType == PostTitleType.KEYWORD:
                            # 키워드일 때만 워드프레스/블로그스팟 검색 실행
                            if payload.mainBlogType == MainBlogType.WORDPRESS:
                                main_site_url = await get_wordpress_post_url(
                                    page, payload.siteUrl, main_keyword_for_search, 
                                    user.current_user_id, user.external_id, task_id
                                )
                            else:
                                main_site_url = await get_blogspot_post_url(
                                    page, payload.siteUrl, main_keyword_for_search, 
                                    user.current_user_id, user.external_id, task_id
                                )
                        else:
                            # URL 모드일 때는 검색 없이 리스트에 있던 URL 그대로 사용
                            main_site_url = fixed_url
                        
                        # 🚩 [체크 포인트] QR변환 전 확인
                        await check_abort(task_id)

                        # 2. QR 변환 옵션이 켜져 있는 경우
                        if payload.autoChangeQRLink:
                            # 사람처럼 보이기 위한 랜덤 딜레이 (예: 1~3초)
                            await asyncio.sleep(random.uniform(1.0, 3.0))

                            # 네이버 QR 변환 시도
                            short_url = await get_naver_qr_url(
                                page, main_site_url, 
                                user.current_user_id, user.external_id, task_id, BASE_DELAY
                            )

                            # 작업 후 바로 이동하지 않고 잠시 머물다 이동
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                            
                            # [중요] 성공/실패 여부와 상관없이 무조건 페이지 초기화
                            print("🧹 QR 작업 종료 후 페이지 초기화 중...")
                            try:
                                # domcontentloaded 보다는 'networkidle'이 더 자연스러운 사용자의 흐름입니다.
                                await page.goto("https://www.naver.com", wait_until="networkidle", timeout=30000) 
                            except:
                                pass # 메인 이동 실패는 무시하고 진행
                                
                            if short_url is None:
                                await redis_manager.publish(task_id, "⚠️ QR 생성 실패로 인해 원래 링크를 사용합니다.", user_system_id)
                                short_url = main_site_url
                        
                        # 3. QR 옵션이 꺼져 있는 경우
                        else:
                            short_url = main_site_url

                        # 🚩 [체크 포인트] 최종 블로그 글쓰기 전 확인
                        await check_abort(task_id)

                        # 최종적으로 결정된 short_url을 블로그 포스팅 함수에 전달
                        await post_to_blog(
                            payload.linkTopText,    # link_top_text
                            page,                   # page
                            user.blog_id,           # blog_id
                            user.external_id,       # naver_id
                            blog_data,              # blog_data
                            user.current_user_id,   # current_user_id
                            task_id,                # task_id
                            short_url,              # short_url
                            BASE_DELAY,             # base_delay (추가)
                            IMAGE_SAVE_DIR          # image_dir (추가)
                        )

                except Exception as post_error:
                    # 여기서 에러를 잡으면 global_e로 가지 않고 다음 루프로 넘어갑니다.
                    error_msg = f"❌ [{user.external_id}] 작업 중 오류: {str(post_error)}"
                    print(error_msg)
                    # 🧨 고칠 곳
                    await redis_manager.publish(task_id, error_msg, user_system_id)
                    await log_to_db(user.current_user_id, user.external_id, "포스팅 실패", error_msg, status="FAIL")
                    
                    # 에러 시에도 다음 포스팅을 시도하게 함
                finally:
                    # ⭐ [가장 중요한 안전장치] ⭐
                    # 정상 종료되었든, 에러가 났든 브라우저가 살아있다면 무조건 죽입니다.
                    if browser:
                        try:
                            await browser.close()
                            print(f"🧹 [{user.external_id}] 좀비 브라우저 강제 정리 완료")
                        except:
                            pass
                        browser = None

                # ✨ [바로 여기!] 포스팅 작업 하나가 "성공"적으로 끝난 직후에 넣으세요.
                # 다음 포스팅할 제목이 남아있을 때만 기다립니다.
                if i < user.postingCount - 1 and current_post_idx < total_titles_count:
                    wait_minutes = payload.postingTerm
                    await redis_manager.publish(task_id, f"⏳ 포스팅 성공! {wait_minutes}분 대기 후 다음 글을 작성합니다.", user_system_id)

                    # 🧨 [수정] 포스팅 사이 대기 시간도 10초 단위로 쪼개서 중단 체크
                    total_wait_seconds = wait_minutes * 60
                    for _ in range(0, total_wait_seconds, 10):
                        await check_abort(task_id) # 대기 중에 중단 버튼 누르면 즉시 반응
                        await asyncio.sleep(10)
                    

            # 각 유저 로직이 끝날때 세션파일 삭제
            if auth_path and os.path.exists(auth_path):
                os.remove(auth_path)
                print(f"🗑️ {user.external_id} 세션 정리 완료")
            
            print("모든 로직이 성공적으로 완료되었습니다.")

    # ✅ 중단 에러 전용 처리
    except InterruptedError as e:
        stop_msg = f"🛑 중단 성공: {str(e)}"
        print(stop_msg)
        await redis_manager.publish(task_id, stop_msg, user_system_id)
        await log_to_db(user_system_id, "SYSTEM", "사용자 취소", stop_msg, status="CANCEL")

    # ✅ 1순위: 프록시 속도 문제로 인한 종료 처리
    except ProxyTimeoutError as e:
        error_msg = f"🛑 프록시 영향으로 작업 강제 종료: {str(e)}"
        print(error_msg)
        await redis_manager.publish(task_id, error_msg, user_system_id)
        # 💡 [추가 권장] DB에도 왜 죽었는지 로그 한 줄 남겨주기
        await log_to_db(user_system_id, "SYSTEM", "프록시 중단", error_msg, status="FAIL")

    except ChatGptError as ce:
        # 🧨 [지피티 요금 부족 시 처리]
        error_msg = f"🛑 [시스템 중단] GPT 요금 충전 필요: {ce}"
        await redis_manager.publish(task_id, error_msg, user_system_id)
        await log_to_db(user.current_user_id, user.external_id, "GPT 에러", error_msg, status="CRITICAL")

    except BlogIdError as be:
        # 🧨 [지피티 요금 부족 시 처리]
        error_msg = f"🛑 [시스템 중단] 네이버 블로그 글쓰기 아이디 확인 필요 : {be}"
        await redis_manager.publish(task_id, error_msg, user_system_id)
        await log_to_db(user.current_user_id, user.external_id, "네이버 블로그 글쓰기 아이디 에러", error_msg, status="CRITICAL")

    except Exception as global_e:
        # 예상치 못한 전체 프로세스 에러 발생 시 로그
        print(f"🚨 [FATAL ERROR] {task_id} 프로세스 중단: {str(global_e)}")
        # 🧨 고칠 곳
        await redis_manager.publish(task_id, f"🚨 시스템 오류로 작업이 중단되었습니다: {str(global_e)}", user_system_id)

    finally:
        # 1. 🛑 [보완] 브라우저 확실히 닫기
        if browser:
            try:
                await browser.close()
                print(f"🧹 최종 정리: 브라우저를 닫았습니다.")
            except Exception as e:
                print(f"⚠️ 브라우저 종료 시도 중 오류(무시): {e}")
            finally:
                browser = None  # 참조 해제

        # 작업이 끝났으므로 Redis의 중단 깃발 삭제
        await redis_manager.redis_client.delete(f"stop_signal:{task_id}")

        # ✨ [2. 여기서 상태를 해제합니다] ✨
        # 어떤 에러가 발생해도, 혹은 정상 종료되어도 이 부분은 무조건 실행됩니다.
        try:
            async with AsyncSessionLocal() as db:
                print(f"🔄 유저 {user_system_id}의 작업 상태를 '대기'로 변경합니다.")
                update_query = text("""
                    UPDATE user_work_status 
                    SET is_running = FALSE 
                    WHERE user_id = :user_id
                """)
                await db.execute(update_query, {"user_id": user_system_id})
                await db.commit()
        except Exception as final_e:
            print(f"⚠️ 상태 변경 중 오류 발생 (무시): {final_e}")

        # SSE 연결 종료 신호
        print("모든 로직이 완료되어 SSE 서버를 끊습니다.")
        await redis_manager.publish(task_id, "🏁 모든 블로그 포스팅 작업이 완료되었습니다.", user_system_id)
        await redis_manager.stop_task(task_id) # QUIT 전송


# PIL을 이용한 이미지 생성 로직
async def generate_blog_images(naver_id:str, blog_data: dict, task_id: str, user_id: str):
    """중복 방지를 위해 UUID와 타임스탬프를 적용한 이미지 생성 로직입니다."""
    if not blog_data or "sections" not in blog_data:
        return blog_data

    sections = blog_data.get("sections", [])
    current_count = 1
    
    # 작업 고유 ID (파일명 중복 방지용)
    unique_run_id = str(uuid.uuid4())[:8] 
    
    for sec in sections:
        subtitle = sec.get("subtitle", "").strip()
        # subtitle이 없으면 중복 방지를 위해 순번이라도 넣음
        text_to_draw = subtitle if subtitle else f"Section {current_count}"

        # 🚀 [수정: 외부 함수 호출] 함수 안에서 함수 정의 안 함!
        wrapped_text = get_smart_wrapped_text(text_to_draw)

        try:
            await redis_manager.publish(task_id, f"🖼️ {current_count}번째 유니크 이미지 생성 중...", user_id)
            
            # 1. 파일명 단축 (ID앞자리 + 현재초 + 순번)
            # 예: img_v2_3456_1.png
            short_id = naver_id[:2]  # 네이버 아이디 앞 2글자
            ts = str(int(time.time()))[-4:]  # 현재 시간 마지막 4자리 (중복 방지용 최소치)
            file_name = f"i_{short_id}_{ts}_{current_count}.png"

            file_path = os.path.join(IMAGE_SAVE_DIR, file_name)
            # 2. 이미지 생성
            img = Image.new('RGB', (1200, 630), color=(255, 255, 255))
            d = ImageDraw.Draw(img)

            # --- 테두리 추가 시작 ---
            border_color = (0, 102, 204)  # 파란색 (네이버 느낌의 깔끔한 파랑)
            border_width = 14             # 테두리 두께 (더 굵게 하려면 이 숫자 키우시면 됩니다)
            
            # 이미지 가장자리에 사각형 그리기
            # [시작x, 시작y, 끝x, 끝y]
            d.rectangle([0, 0, 1200, 630], outline=border_color, width=border_width)
            # --- 테두리 추가 끝 ---
            
            # 3. 폰트 설정
            try:
                # 서버 환경에 맞는 경로로 수정 필수
                font_path = "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"
                font = ImageFont.truetype(font_path, 70)
                small_font = ImageFont.truetype(font_path, 20)
            except:
                font = ImageFont.load_default()
                small_font = ImageFont.load_default()
            
            # 🚀 [수정: 멀티라인 중앙 정렬 적용]
            left, top, right, bottom = d.multiline_textbbox((0, 0), wrapped_text, font=font, align="center")
            w, h = right - left, bottom - top
            d.multiline_text(((1200-w)/2, (630-h)/2), wrapped_text, fill=(0, 0, 0), font=font, align="center", spacing=12)
            
            # 6. 이미지 저장
            img.save(file_path)
            
            # 7. 최종 URL 구성
            sec["image_url"] = f"{DOMAIN}{URL_PATH}/{file_name}"
            
            current_count += 1
            # 비동기 루프 배려
            await asyncio.sleep(0.05)

        except Exception as e:
            print(f"이미지 생성 실패: {e}")
            await redis_manager.publish(task_id, f"⚠️ {current_count}번 이미지 중복 생성 방지 처리 실패", user_id)
            sec["image_url"] = None
    
    return blog_data