import asyncio
import os
from api.v1.dependencies.redis_manager import redis_manager
from api.services.utils import (
    save_debug_screenshot, 
    log_to_db, 
    record_success_count, 
    check_abort
)
import random


class BlogIdError(Exception):
    """네이버 아이디와 글쓰기 아이디가 다를때 발생하는 커스텀 에러"""
    pass

# --- [유틸리티 함수 추가: 인간의 지연 시간 모사] ---
async def human_delay(min_sec=0.5, max_sec=1.5):
    await asyncio.sleep(random.uniform(min_sec, max_sec))

async def human_typing(page, text, delay_range=(50, 150)):
    # 🔎 현재 포커스 확인 함수
    async def ensure_focus():
        active = await page.evaluate("document.activeElement && document.activeElement.tagName")
        if active in ["TEXTAREA", "DIV"]:
            return True

        # 입력 가능한 요소가 있으면 첫 번째 클릭
        locator = page.locator("textarea, [contenteditable='true']").first
        if await locator.count() > 0:
            try:
                await locator.click(timeout=5000)
                return True
            except:
                return False
        return False

    # 포커스 확보 시도
    await ensure_focus()

    # ===============================
    # 70% 직접 타이핑 모드
    # ===============================
    if len(text) < 10 or random.random() < 0.7:
        print(f"⌨️ 타이핑 모드 작동 중... (길이: {len(text)})")

        for char in text:
            try:
                await page.keyboard.type(char, delay=random.uniform(10, 30))
            except:
                # 포커스 날아가면 복구 후 재시도
                await ensure_focus()
                await page.keyboard.type(char, delay=random.uniform(10, 30))

            delay = random.uniform(delay_range[0], delay_range[1]) / 1000
            if random.random() < 0.05:
                delay += random.uniform(0.2, 0.5)
            await asyncio.sleep(delay)

    # ===============================
    # 복붙 모드 (Timeout 완전 방어)
    # ===============================
    else:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        print(f"📋 인간형 복붙 실행 (길이: {len(text)})")

        try:
            await ensure_focus()
            # 🔹 여기서부터 실제 입력 시작
            await page.keyboard.insert_text(text)
            # 🔹 렌더링 대기
            await asyncio.sleep(0.3)

        except Exception as e:
            print(f"⚠️ insert_text 실패 → 빠른 타이핑 fallback: {e}")
            await ensure_focus()
            await page.keyboard.type(text, delay=random.uniform(1, 3))

        wait_time = min(2.0, 0.5 + (len(text) * 0.005))
        await asyncio.sleep(random.uniform(wait_time * 0.8, wait_time * 1.2))

        if random.random() < 0.2:
            await page.keyboard.press("ArrowLeft")
            await asyncio.sleep(random.uniform(0.1, 0.2))
            await page.keyboard.press("ArrowRight")

async def post_to_blog(link_top_text, page, blog_id, naver_id, blog_data, current_user_id, task_id, short_url=None, base_delay=5, image_dir="./static/blog_images"):
    title = blog_data.get("title", "블로그 포스팅")
    introduction = blog_data.get("introduction", "")
    sections = blog_data.get("sections", [])
    conclusion = blog_data.get("conclusion", "")

    print(f"🚀 {naver_id} 포스팅 시작: {title}")
    await log_to_db(current_user_id, naver_id, "네이버 글 포스팅", "네이버 로그인 브라우저 실행 중")

    # 브라우저 기본 다이얼로그 무조건 닫기
    page.on("dialog", lambda dialog: dialog.dismiss())

    target_url = f"https://blog.naver.com/{blog_id}?Redirect=Write"
    try:
        response = await page.goto(target_url, wait_until="load", timeout=60000)

        # 1. HTTP 404 에러 (존재하지 않는 블로그 아이디)
        if response and response.status == 404:
            msg = f"❌ [아이디 오류] 존재하지 않는 블로그 아이디입니다. (HTTP 404: {blog_id})"
            print(msg)
            await log_to_db(current_user_id, naver_id, title, msg, status="FAIL")
            await redis_manager.publish(task_id, msg)
            # 🧨 return 대신 raise로 상위 로직에 에러를 던집니다.
            raise BlogIdError(msg)

        # 2. 리다이렉트 체크 (아이디가 잘못되어 메인으로 튕김)
        if "section.blog.naver.com" in page.url:
            msg = f"❌ [아이디 오류] 블로그 메인으로 리다이렉트되었습니다. 아이디 '{blog_id}'를 확인하세요."
            print(msg)
            await log_to_db(current_user_id, naver_id, title, msg, status="FAIL")
            await redis_manager.publish(task_id, msg)
            # 🧨 여기도 마찬가지로 raise!
            raise BlogIdError(msg)
    
    except BlogIdError:
        # ✅ 내가 정의한 에러는 상위로 그대로 던져서 작업을 중단시킴
        raise
        
    except Exception as e:
        print(f"⚠️ 페이지 로딩 지연(타임아웃 발생 시도): {e}")
    
    await asyncio.sleep(base_delay) 

    # 에디터 프레임 확보
    target_frame = None
    for frame in page.frames:
        try:
            if await frame.get_by_text("제목", exact=True).is_visible(timeout=3000):
                target_frame = frame
                break
        except: continue
    if not target_frame: target_frame = page

    print(f"⏳ 프록시 지연 대기 중 ({base_delay}초)... 팝업 대기 중")
    await asyncio.sleep(base_delay)

    # --- ✨ 팝업 제거 로직 ---
    await target_frame.evaluate("""() => {
        document.querySelector('.se-popup-button-cancel')?.click();
        document.querySelector('.se-help-panel-close-button')?.click();
    }""")

    popup_btns = [
        ".se-popup-button-cancel", ".se-help-panel-close-button", 
        "button:has-text('취소')", "button:has-text('닫기')", ".se-popup-close-button"
    ]

    for btn_selector in popup_btns:
        try:
            for container in [page, target_frame]:
                loc = container.locator(btn_selector).first
                if await loc.is_visible(timeout=1000):
                    await loc.click(force=True)
                    await asyncio.sleep(0.5)
        except: continue

    # 🎨 취소선 해제 로직
    try:
        strike_btn = target_frame.locator('button[data-name="strikethrough"][data-group="propertyToolbar"]').first
        if await strike_btn.is_visible(timeout=5000):
            class_attr = await strike_btn.get_attribute("class")
            if "se-is-selected" in class_attr:
                await strike_btn.click(force=True)
                await asyncio.sleep(0.5)
    except Exception as e:
        print(f"ℹ️ 취소선 로직 처리 중 오류: {e}")

    await check_abort(task_id)

    # 1. 제목 입력
    try:
        title_placeholder = target_frame.get_by_text("제목", exact=True)
        # force=True 대신 실제로 마우스를 움직여 클릭하게 함
        await title_placeholder.scroll_into_view_if_needed()
        await title_placeholder.click(delay=random.uniform(100, 300)) 
        await human_delay(0.8, 1.5)
        
        # 일정한 delay 대신 human_typing 사용
        await human_typing(page, title)
        await redis_manager.publish(task_id, f"📝 포스팅 제목 작성 완료")
    except Exception as e:
        # 실패 시 좌표 클릭도 랜덤성을 줌
        await page.mouse.click(400 + random.randint(-5, 5), 250 + random.randint(-5, 5))
        await human_delay()
        await human_typing(page, title)
    
    await page.keyboard.press("Enter")
    await human_delay(1.0, 2.0)
    await check_abort(task_id)

    # 2. 서론 입력
    await human_typing(page, introduction, (30, 80))
    await page.keyboard.press("Enter")
    enter_count = random.randint(2, 5)
    for _ in range(enter_count):
        await page.keyboard.press("Enter")
        # 연타 사이에도 아주 미세한 지연(0.03~0.1초)을 주어 기계적인 느낌 제거
        await asyncio.sleep(random.uniform(0.03, 0.1))
    await human_delay(0.5, 1.0)

    # 3. QR/단축 URL 삽입
    if short_url:
        try:
            await human_typing(page, link_top_text)
            await page.keyboard.press("Enter")
            await human_delay(1.5, 2.5) # 링크 버튼 누르기 전 고민하는 척

            link_btn = target_frame.locator('button[data-name="oglink"]')
            await link_btn.click() # force=True 제거 (가급적)
            
            input_selector = 'input.se-popup-oglink-input'
            await target_frame.wait_for_selector(input_selector, state="visible")
            await human_delay(0.5, 1.2)
            
            # 링크는 붙여넣기가 자연스러움
            await target_frame.locator(input_selector).fill(short_url)
            await human_delay(0.7, 1.5)
            await target_frame.locator('button.se-popup-oglink-button').click()

            # 확인 버튼 대기 및 클릭
            confirm_btn = target_frame.locator('button.se-popup-button-confirm')
            await confirm_btn.wait_for(state="visible")
            await human_delay(1.0, 2.0)
            await confirm_btn.click()

            await human_delay(base_delay, base_delay + 2)
            await redis_manager.publish(task_id, "🔗 본문에 QR 링크(단축URL) 삽입 완료")
        except Exception as e:
            await save_debug_screenshot(page, "qr_failed", current_user_id)
            await redis_manager.publish(task_id, "⚠️ QR 링크 삽입 중 오류 발생 (건너뜀)")
    
    # 커서 최하단 이동
    try:
        last_p = target_frame.locator('.se-component.se-text').last
        await last_p.scroll_into_view_if_needed()
        box = await last_p.bounding_box()
        if box:
            await page.mouse.click(box['x'] + (box['width'] / 2), box['y'] + box['height'] + 10)
    except:
        await target_frame.locator('body').click()

    await page.keyboard.press("End") 
    for _ in range(3): await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")

    # 4. 섹션 반복 (인용구 + 이미지 + 본문)
    for i, section in enumerate(sections):
        await check_abort(task_id)
        if section.get("subtitle"):
            try:
                quote_btn = target_frame.locator('button[data-name="quotation"]').first
                await quote_btn.click(force=True)
                await asyncio.sleep(1)
                await page.keyboard.type(section["subtitle"], delay=80)
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
            except:
                await page.keyboard.type(f"■ {section['subtitle']}\n")

        if section.get("image_url"):
            file_name = section["image_url"].split('/')[-1]
            img_path = os.path.abspath(os.path.join(image_dir, file_name))
            
            try:
                if os.path.exists(img_path):
                    # 1. 자연스러운 준비 과정
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(random.uniform(0.5, 1.2)) # 잠시 멈춤
                    await page.keyboard.press("End")
                    await asyncio.sleep(random.uniform(0.8, 1.5)) # 스크롤 후 대기

                    # 2. 버튼 찾기 및 마우스 이동 시뮬레이션
                    photo_btn = target_frame.locator('button[data-name="image"]').first
                    await photo_btn.scroll_into_view_if_needed()
                    
                    # 버튼 위로 마우스를 올리는(Hover) 동작 추가 (매우 중요)
                    await photo_btn.hover()
                    await asyncio.sleep(random.uniform(0.3, 0.8))

                    # 3. 실제 클릭 시뮬레이션 (dispatch_event 대신 click 사용)
                    async with page.expect_file_chooser() as fc_info:
                        # 인간처럼 약간의 딜레이를 두고 클릭
                        await photo_btn.click(delay=random.randint(150, 300)) 
                    
                    file_chooser = await fc_info.value
                    
                    # 4. 파일 선택 전 '고민하는' 시간 추가
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                    await file_chooser.set_files(img_path)
                    
                    # 5. 업로드 대기 (일정한 7초가 아닌 랜덤 범위 적용)
                    # 파일 크기에 따라 업로드 시간이 다른 것처럼 보이게 함
                    upload_delay = random.uniform(5.0, 9.0) + base_delay
                    print(f"📸 이미지 업로드 중... ({upload_delay:.1f}초 대기)")
                    await asyncio.sleep(upload_delay) 
                    
                    # 6. 마무리 동작
                    await page.keyboard.press("End")
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    await page.keyboard.press("Enter")
                    
            except Exception as e:
                print(f"❌ 이미지 업로드 실패: {e}")
                await save_debug_screenshot(page, f"img_failed_{i}", current_user_id)
            finally:
                # 파일 삭제 로직은 동일
                if os.path.exists(img_path):
                    try: os.remove(img_path)
                    except: pass
        
        await human_typing(page, section.get("content", ""), (20, 50))
        await human_delay(1.0, 2.5)
        enter_count = random.randint(2, 5)
        for _ in range(enter_count):
            await page.keyboard.press("Enter")
            # 연타 사이에도 아주 미세한 지연(0.03~0.1초)을 주어 기계적인 느낌 제거
            await asyncio.sleep(random.uniform(0.03, 0.1))

        await redis_manager.publish(task_id, f"✅ {i+1}번 섹션 작성 완료")

    await check_abort(task_id)
    if conclusion:
        await human_typing(page, conclusion, (30, 80)) # 이 부분 수정!
        await page.keyboard.press("Enter")
        await redis_manager.publish(task_id, "🏁 결론 작성 완료")

    # 5. 강조 문구 서식 적용 (폰트 19px, 빨간색)
    try:
        msg_to_style = f"{link_top_text}"
        target_text = target_frame.get_by_text(msg_to_style).first
        
        # 1. 요소가 보일 때까지 스크롤 및 대기
        await target_text.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # 2. 텍스트 전체 선택 (Triple Click 모사)
        # force=True를 빼고 실제 마우스로 세 번 클릭하여 블록 지정
        await target_text.click(click_count=3, delay=random.randint(50, 150)) 
        await asyncio.sleep(random.uniform(0.8, 1.2)) # 툴바가 뜨는 물리적 시간 대기

        # 3. 글꼴 크기 변경
        size_open_btn = target_frame.locator('button.se-font-size-code-toolbar-button').first
        if await size_open_btn.is_visible():
            await size_open_btn.hover() # 마우스 올리기
            await asyncio.sleep(random.uniform(0.2, 0.4))
            await size_open_btn.click() # 클릭 (force 없이)
            
            await asyncio.sleep(random.uniform(0.5, 0.8)) # 드롭다운 애니메이션 대기
            
            size_19 = target_frame.locator('button.se-toolbar-option-font-size-code-fs19-button').first
            await size_19.hover()
            await size_19.click(delay=random.randint(100, 200))

        await asyncio.sleep(random.uniform(0.6, 1.0)) # 다음 동작 전 망설임

        # 4. 글자 색상 변경 (빨간색)
        color_btn = target_frame.locator('button.se-font-color-toolbar-button').first
        if await color_btn.is_visible():
            await color_btn.hover()
            await color_btn.click()
            
            await asyncio.sleep(random.uniform(0.5, 0.8)) # 팔레트 열리는 시간
            
            red_palette = target_frame.locator('button.se-color-palette[data-color="#ff0010"]').first
            await red_palette.hover()
            await red_palette.click(delay=random.randint(100, 200))

        # 5. 서식 적용 후 빈 공간 클릭하여 블록 해제 (자연스러운 마무리)
        await page.keyboard.press("ArrowRight")
        await asyncio.sleep(random.uniform(0.5, 1.0))

    except Exception as e:
        print(f"ℹ️ 서식 적용 중 건너뜀(인간적인 실수 모사): {e}")
        await page.keyboard.press("Escape") # 꼬였을 경우를 대비해 팝업 닫기 시도

    await check_abort(task_id)

    # 6. 발행
    print("📤 발행 시도 중...")
    try:
        # 1. 우측 사이드바 등이 열려있다면 닫기 (자연스럽게)
        try:
            aside_close = page.locator('button.se-aside-close-button')
            if await aside_close.is_visible(timeout=2000):
                await aside_close.hover() # 마우스 먼저 올리기
                await asyncio.sleep(random.uniform(0.3, 0.7))
                await aside_close.click() # force=True 제거
                await asyncio.sleep(random.uniform(0.5, 1.2))
        except: pass

        # 2. 첫 번째 '발행' 버튼 찾기 (상단 바에 있는 것)
        publish_btn = page.locator('button[class*="publish_btn"]').filter(has_text="발행")
        if await publish_btn.count() == 0:
            publish_btn = target_frame.locator('button[class*="publish_btn"]').filter(has_text="발행")
        
        # 3. 인간적인 확인 시간 (발행 전 2~4초간 멈춤)
        await publish_btn.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(2.0, 4.0)) 
        
        # 4. 첫 번째 발행 버튼 클릭 (실제 마우스 클릭 모사)
        await publish_btn.hover() # 버튼 위로 마우스 이동
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await publish_btn.click(delay=random.randint(100, 300)) # 누르는 동작 지연 추가
        
        # 5. 설정 팝업이 뜨는 시간 대기
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # 6. 최종 '발행' 버튼 (팝업 내에 있는 것)
        # .last 대신 명확한 속성을 쓰거나, 팝업 내 버튼임을 인지하게 함
        final_btn = page.locator('button').filter(has_text="발행").last
        if await final_btn.count() == 0:
            final_btn = target_frame.locator('button').filter(has_text="발행").last

        # 7. 최종 발행 전 마지막 망설임 (0.8~1.5초)
        await final_btn.hover()
        await asyncio.sleep(random.uniform(0.8, 1.5))
        
        # 실제 클릭 (force=True 제거)
        await final_btn.click(delay=random.randint(150, 400))
        
        # 8. 발행 후 결과 페이지 로딩 대기 (가장 중요)
        # 발행 버튼 누르자마자 브라우저를 끄면 봇으로 강력 의심받음
        post_delay = 5 + random.uniform(2.0, 5.0)
        print(f"🎉 발행 버튼 클릭 완료. {post_delay:.1f}초간 결과 대기 후 종료합니다.")
        await asyncio.sleep(post_delay)

        await redis_manager.publish(task_id, "🎉 네이버 블로그 포스팅 발행 완료!")
        await record_success_count(current_user_id, naver_id, title)

    except Exception as e:
        await save_debug_screenshot(page, "final_real_error", current_user_id)
        print(f"❌ 발행 오류 발생: {str(e)}")
        await redis_manager.publish(task_id, f"❌ 발행 오류: {str(e)}")