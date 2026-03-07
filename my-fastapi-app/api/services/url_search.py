import asyncio
from kiwipiepy import Kiwi
from api.v1.dependencies.redis_manager import redis_manager
from api.services.utils import log_to_db

# 맞춤법 교정을 위한 Kiwi 객체 생성 (파일 로드 시 한 번만 실행됨)
kiwi = Kiwi()

async def get_wordpress_post_url(page, site_url, keyword, current_user_id, naver_id, task_id):
    async def fetch_url(search_keyword):
        """실제 워드프레스에서 URL을 찾는 내부 헬퍼 함수"""
        search_query = search_keyword.replace(' ', '+')
        search_url = f"{site_url.rstrip('/')}/?s={search_query}"
        
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            # 여러 선택자로 링크 확인
            post_link_locator = page.locator('article h2 a, .entry-title a, h2.post-title a, .post-item a').first
            if await post_link_locator.count() > 0:
                return await post_link_locator.get_attribute('href')
            return None
        except Exception:
            return None

    # --- 1차 검색 (원본 키워드) ---
    print(f"🔎 1차 검색 시작: '{keyword}'")
    # 🧨 수정 2: manager.broadcast -> redis_manager.publish
    await redis_manager.publish(task_id, f"🔍 워드프레스에서 '{keyword}' 검색 중...", current_user_id)
    await asyncio.sleep(0.1)
    await log_to_db(current_user_id, naver_id, f"키워드: {keyword}", "워드프레스 1차 검색 시작")
    
    actual_post_url = await fetch_url(keyword)

    # --- 2차 검색 (결과가 없을 경우 교정 후 재시도) ---
    if not actual_post_url:
        fixed_keyword = kiwi.space(keyword.replace(" ", "")) # 공백 다 붙였다가 다시 떼기
        
        # 교정된 키워드가 원본과 다를 때만 2차 검색 진행
        if fixed_keyword != keyword:
            print(f"⚠️ 결과 없음. 2차 검색(교정): '{fixed_keyword}'")
            # 🧨 수정 3: manager.broadcast -> redis_manager.publish
            await redis_manager.publish(task_id, f"🔄 결과 없음. 키워드 교정 후 재검색 중: '{fixed_keyword}'", current_user_id)
            await asyncio.sleep(0.1)
            actual_post_url = await fetch_url(fixed_keyword)

    # --- 최종 결과 처리 ---
    if actual_post_url:
        await redis_manager.publish(task_id, f"🔗 URL 추출 성공: {actual_post_url}", current_user_id)
        await asyncio.sleep(0.1)
        await log_to_db(current_user_id, naver_id, f"추출 URL: {actual_post_url}", "워드프레스 URL 추출 성공")
        return actual_post_url
    else:
        await redis_manager.publish(task_id, f"⚠️ '{keyword}'에 대한 최종 검색 결과가 없습니다.", current_user_id)
        await asyncio.sleep(0.1)
        await log_to_db(current_user_id, naver_id, f"키워드: {keyword}", "검색 결과 없음 (기본 도메인 반환)", status="FAIL")
        return site_url

# --- 블로그 스팟 URL을 찾는 내부 함수 ---
async def get_blogspot_post_url(page, site_url, keyword, current_user_id, naver_id, task_id):
    async def fetch_url(search_keyword):
        """블로그스팟에서 URL을 찾는 내부 헬퍼 함수"""
        search_query = search_keyword.replace(' ', '+')
        # 블로그스팟 표준 검색 URL 패턴
        search_url = f"{site_url.rstrip('/')}/search?q={search_query}"
        
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            # 블로그스팟 주요 선택자: .post-title a, h3.post-title a, .entry-title a
            post_link_locator = page.locator('.post-title a, h3.post-title a, .entry-title a, .post-item a').first
            
            if await post_link_locator.count() > 0:
                return await post_link_locator.get_attribute('href')
            return None
        except Exception as e:
            print(f"❌ BlogSpot 추출 중 오류: {e}")
            return None

    # --- 검색 로직 실행 (워드프레스 로직과 유사하게 유지) ---
    print(f"🔎 BlogSpot 검색 시작: '{keyword}'")
    await redis_manager.publish(task_id, f"🔍 블로그스팟에서 '{keyword}' 검색 중...", current_user_id)
    
    # 1차 검색
    actual_post_url = await fetch_url(keyword)

    # 2차 검색 (결과 없을 시 키워드 교정)
    if not actual_post_url:
        fixed_keyword = kiwi.space(keyword.replace(" ", ""))
        if fixed_keyword != keyword:
            await redis_manager.publish(task_id, f"🔄 결과 없음. BlogSpot 키워드 교정 재검색: '{fixed_keyword}'", current_user_id)
            actual_post_url = await fetch_url(fixed_keyword)

    # 최종 결과 반환
    if actual_post_url:
        await redis_manager.publish(task_id, f"🔗 BlogSpot URL 추출 성공: {actual_post_url}", current_user_id)
        await log_to_db(current_user_id, naver_id, f"추출 URL: {actual_post_url}", "블로그스팟 URL 추출 성공")
        return actual_post_url
    else:
        await redis_manager.publish(task_id, f"⚠️ BlogSpot 검색 결과 없음.", current_user_id)
        return site_url