import asyncio
import re
from api.v1.dependencies.redis_manager import redis_manager
from api.services.utils import log_to_db, save_debug_screenshot

async def get_naver_qr_url(page, wp_url, current_user_id, naver_id, task_id, base_delay=5):
    print("🌐 네이버 QR 생성 중 (ID 기반 버전)...")
    await redis_manager.publish(task_id, "📱 네이버 QR 코드 생성 프로세스를 시작합니다...")

    # 페이지 탭 정리
    try:
        pages = page.context.pages
        if len(pages) > 1:
            for p in pages[1:]:
                await p.close()
            print(f"🧹 불필요한 탭 {len(pages)-1}개를 정리했습니다.")
    except Exception as pe:
        print(f"⚠️ 탭 정리 중 오류(무시 가능): {pe}")

    await asyncio.sleep(0.1)
    queue_message = ""
    try:
        await log_to_db(current_user_id, naver_id, f"네이버 QR 생성 ID: {naver_id}", "네이버 로그인 브라우저 실행 중")
        
        # 1. 페이지 접속
        queue_message = "🌐 QR 생성 페이지 접속 시도 중..."
        for attempt in range(3):
            try:
                await page.goto("https://qr.naver.com/create", wait_until="domcontentloaded", timeout=60000)
                break 
            except Exception as e:
                if attempt == 2: raise e
                print(f"⚠️ 접속 지연(시도 {attempt+1}/3): 재시도 합니다...")
                await asyncio.sleep(5)

        # 2. 1단계 -> 2단계 이동
        queue_message = "⚙️ QR 설정 단계 이동 중 (1/3)..."
        btn = page.locator('button[data-testid="form-sumbit-btn"]').first
        await btn.wait_for(state="visible", timeout=30000)
        await btn.click(force=True)
        await asyncio.sleep(3)

        # 3. 2단계 -> 3단계 이동
        queue_message = "⚙️ QR 설정 단계 이동 중 (2/3)..."
        btn2 = page.locator('button[data-testid="form-sumbit-btn"]').first
        await btn2.wait_for(state="visible", timeout=20000)
        await btn2.click(force=True)
        await asyncio.sleep(2)

        # 4. 정보 입력 단계 (3단계)
        queue_message = "📝 QR 코드에 링크(워드프레스) 연결 중..."
        title_input = page.locator('input[name="sections[0].title"]')
        await title_input.wait_for(state="visible", timeout=10000)
        await title_input.fill("확인하기")
        
        url_input = page.locator('input[name="sections[1].url"]')
        await url_input.fill(wp_url)
        await asyncio.sleep(0.5)
        await url_input.press("Enter")
        await asyncio.sleep(1)

        queue_message = "⚙️ 링크를 첨부하는 중..."
        attach_btn = page.locator('button:has-text("링크첨부")').first
        await attach_btn.wait_for(state="visible", timeout=10000)
        await attach_btn.click(force=True)
        await asyncio.sleep(2)

        try:
            alert_ok_btn = page.locator('button:has-text("확인")').first
            if await alert_ok_btn.is_visible(timeout=2000):
                await alert_ok_btn.click()
        except:
            pass

        # 5. 3단계 완료 및 억지 팝업 대응
        queue_message = "⚙️ QR 생성 버튼을 클릭하고 예외를 확인하는 중..."
        final_btn_selector = 'button[data-testid="form-sumbit-btn"]'
        
        for attempt in range(3):
            final_btn = page.locator(final_btn_selector).first
            await final_btn.wait_for(state="visible", timeout=(10 + base_delay) * 1000)
            await final_btn.click(force=True)
            await asyncio.sleep(1 + (base_delay * 0.2))

            # --- [추가] 일일 생성량 초과 팝업 체크 ---
            limit_popup = page.locator('.BasicPopup_basic-popup-wrapper__oZWMF:has-text("일일 QR코드 생성량을 초과")').first
            if await limit_popup.is_visible(timeout=3000):
                error_msg = "🚫 일일 QR코드 생성량을 초과하여 기존 URL로 대체합니다. (네이버 계정 한도)"
                print(f"❌ {error_msg}")
                await redis_manager.publish(task_id, error_msg)
                await log_to_db(current_user_id, naver_id, "네이버 QR 생성 실패", "일일 한도 초과", status="FAIL")
                
                # 확인 버튼 눌러서 팝업 닫아주는 매너 (생략 가능)
                try:
                    confirm_btn = limit_popup.locator('button:has-text("확인")').first
                    await confirm_btn.click()
                except: pass
                
                return wp_url # 즉시 종료하고 원본 URL 반환
            # ---------------------------------------

            error_popup = page.locator('.BasicPopup_basic-popup-wrapper__oZWMF:has-text("필수입력")').first
            if await error_popup.is_visible(timeout=3000):
                confirm_btn = page.locator('button.button_confirm-button__uZCEd:has-text("확인")').first
                await confirm_btn.click()
                await asyncio.sleep(1)

                delete_all_btn = page.locator('button.button_delete-all-button___6V13').first
                if await delete_all_btn.is_visible():
                    await delete_all_btn.click()
                    await asyncio.sleep(1)
                    del_confirm = page.locator('button.button_confirm-button__uZCEd:has-text("삭제")').first
                    if await del_confirm.is_visible():
                        await del_confirm.click()
                        await asyncio.sleep(2)

                url_input = page.locator('input[name="sections[1].url"]')
                await url_input.fill(wp_url)
                await url_input.press("Enter")
                await asyncio.sleep(2)

                attach_btn = page.locator('button:has-text("링크첨부")').first
                await attach_btn.click(force=True)
                await asyncio.sleep(4)
                continue 
            
            # --- [강력 수정] 성공 페이지 전환 대기 (기존 7초에서 대폭 연장) ---
            try:
                # 프록시가 느려도 버틸 수 있게 (최소 15초 + 지연시간)
                await page.wait_for_url("**/success-qr/**", timeout=(15 + base_delay) * 1000)
                print("✅ QR 생성 성공 페이지 진입 확인")
                break # 성공 시 루프 탈출
            except:
                # [수정] 필수입력 팝업 등 예외 대응 (기존 로직 유지하되 대기시간 보정)
                error_popup = page.locator('.BasicPopup_basic-popup-wrapper__oZWMF:has-text("필수입력")').first
                if await error_popup.is_visible(timeout=2000):
                    confirm_btn = page.locator('button.button_confirm-button__uZCEd:has-text("확인")').first
                    await confirm_btn.click()
                    # ... (이후 재시도 로직은 형님 기존 코드와 동일하되 sleep에 base_delay 적용)
                    await asyncio.sleep(1 + base_delay)
                    continue 
                
                print(f"⚠️ 페이지 전환 지연 중 (시도 {attempt+1}/3)...")
                continue
        
        # 6. 결과 페이지에서 단축 URL 추출
        queue_message = "🔍 생성된 단축 URL 추출 중..."
        qr_url = None
        try:
            await page.wait_for_url("**/success-qr/**", timeout=(20 + base_delay) * 1000)
            target_selector = 'div[class*="SuccessQR_qr-option-value"] a'
            try:
                target_loc = page.locator(target_selector).first
                await target_loc.wait_for(state="visible", timeout=(15 + base_delay) * 1000)
                qr_url = await target_loc.get_attribute("href")
            except:
                all_links = page.locator('a[href*="m.site.naver.com"]')
                if await all_links.count() > 0:
                    qr_url = await all_links.first.get_attribute("href")

            if not qr_url:
                content = await page.content()
                match = re.search(r'https://m\.site\.naver\.com/[a-zA-Z0-9]+', content)
                if match: qr_url = match.group()
        except Exception as e:
            print(f"❌ 추출 프로세스 오류: {e}")

        if qr_url:
            await redis_manager.publish(task_id, f"✅ QR 생성 성공: {qr_url}")
            await log_to_db(current_user_id, naver_id, "네이버 QR 링크 생성 성공", "QR 링크 완료")
            return qr_url
        else:
            await save_debug_screenshot(page, f"QR_FINAL_FAIL", current_user_id)
            return wp_url

    except Exception as e:
        await redis_manager.publish(task_id, f"⚠️ 현재 위치: {queue_message}")
        await redis_manager.publish(task_id, f"❌ QR 생성 실패: {str(e)[:50]}...")
        await log_to_db(current_user_id, naver_id, "네이버 QR 링크 생성 실패", f"QR 실패: {str(e)}", status="FAIL", error=e)
        return wp_url