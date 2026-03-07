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

async def human_mouse_move(page, start=None, end=None, steps=20):
    width = 1200
    height = 800

    if start is None:
        start = (random.randint(100, width-100), random.randint(100, height-100))
    if end is None:
        end = (random.randint(100, width-100), random.randint(100, height-100))

    x1, y1 = start
    x2, y2 = end

    for i in range(steps):
        x = x1 + (x2-x1) * (i/steps) + random.uniform(-3,3)
        y = y1 + (y2-y1) * (i/steps) + random.uniform(-3,3)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.01,0.03))

async def human_typing(page, text):

    # 🔎 현재 포커스 확인
    async def ensure_focus():
        active = await page.evaluate("document.activeElement && document.activeElement.tagName")
        if active in ["TEXTAREA", "DIV"]:
            return True

        locator = page.locator("textarea, [contenteditable='true']").first
        if await locator.count() > 0:
            try:
                await locator.click(timeout=5000)
                return True
            except:
                return False
        return False

    await ensure_focus()

    # ===============================
    # 입력 모드 선택
    # ===============================
    mode = random.choice(["typing", "paste", "burst"])

    print(f"⌨️ 입력 모드: {mode} (길이: {len(text)})")

    # ===============================
    # 1️⃣ 타이핑 모드
    # ===============================
    if mode == "typing":

        for char in text:
            try:
                await page.keyboard.type(char, delay=random.uniform(20, 80))
            except:
                await ensure_focus()
                await page.keyboard.type(char, delay=random.uniform(20, 80))

            await asyncio.sleep(random.uniform(0.03, 0.12))

            # 인간 pause
            if random.random() < 0.04:
                await asyncio.sleep(random.uniform(0.2, 0.6))


    # ===============================
    # 2️⃣ 복붙 모드
    # ===============================
    elif mode == "paste":

        await asyncio.sleep(random.uniform(0.5, 1.2))

        try:
            await ensure_focus()
            await page.keyboard.insert_text(text)
        except Exception as e:
            print(f"⚠️ insert_text 실패 → typing fallback: {e}")
            await page.keyboard.type(text, delay=random.uniform(5, 20))

        await asyncio.sleep(random.uniform(0.5, 1.5))


    # ===============================
    # 3️⃣ burst 모드 (사람이 빠르게 치는 느낌)
    # ===============================
    elif mode == "burst":

        words = text.split(" ")

        for part in words:
            try:
                await page.keyboard.type(part)
                await page.keyboard.press("Space")
            except:
                await ensure_focus()
                await page.keyboard.type(part)

            await asyncio.sleep(random.uniform(0.1, 0.4))

        # 가끔 커서 이동
        if random.random() < 0.25:
            await page.keyboard.press("ArrowLeft")
            await asyncio.sleep(random.uniform(0.1, 0.3))
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
            await redis_manager.publish(task_id, msg, current_user_id)
            # 🧨 return 대신 raise로 상위 로직에 에러를 던집니다.
            raise BlogIdError(msg)

        # 2. 리다이렉트 체크 (아이디가 잘못되어 메인으로 튕김)
        if "section.blog.naver.com" in page.url:
            msg = f"❌ [아이디 오류] 블로그 메인으로 리다이렉트되었습니다. 아이디 '{blog_id}'를 확인하세요."
            print(msg)
            await log_to_db(current_user_id, naver_id, title, msg, status="FAIL")
            await redis_manager.publish(task_id, msg, current_user_id)
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
        await redis_manager.publish(task_id, f"📝 포스팅 제목 작성 완료", current_user_id)
    except Exception as e:
        # 실패 시 좌표 클릭도 랜덤성을 줌
        await page.mouse.click(400 + random.randint(-5, 5), 250 + random.randint(-5, 5))
        await human_delay()
        await human_typing(page, title)
    
    await page.keyboard.press("Enter")
    await human_delay(1.0, 2.0)
    await check_abort(task_id)

    # 2. 서론 입력
    await human_typing(page, introduction)
    await page.keyboard.press("Enter")
    for _ in range(random.randint(1,4)):
        await page.keyboard.press("Enter")
        if random.random() < 0.4:
            await asyncio.sleep(random.uniform(0.3,1.0))

    await human_delay(0.5, 1.0)

    # 3. QR/단축 URL 삽입
    if short_url:
        try:
            msg = f"{link_top_text}"
            await page.keyboard.type(msg, delay=60)
            await page.keyboard.press("Enter")
            await human_delay(1.5, 2.5) # 링크 버튼 누르기 전 고민하는 척

            link_btn = target_frame.locator('button[data-name="oglink"]')
            await link_btn.click() # force=True 제거 (가급적)
            
            input_selector = 'input.se-popup-oglink-input'
            await target_frame.wait_for_selector(input_selector, state="visible")
            await human_delay(0.5, 1.2)

            short_url = short_url.replace(" ", "").strip()
            
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
            await redis_manager.publish(task_id, "🔗 본문에 QR 링크(단축URL) 삽입 완료", current_user_id)
        except Exception as e:
            await save_debug_screenshot(page, "qr_failed", current_user_id)
            await redis_manager.publish(task_id, "⚠️ QR 링크 삽입 중 오류 발생 (건너뜀)", current_user_id)
    
    # 커서 최하단 이동 (수정 제안)
    try:
        # 마지막 문단(p태그) 찾기
        last_p = target_frame.locator('.se-component.se-text').last
        await last_p.scroll_into_view_if_needed()
        
        # [수정] 좌표 계산 클릭 대신, 마지막 문단의 끝부분(오른쪽)을 클릭
        # 이렇게 하면 글감 버튼을 누를 확률이 제로에 가깝습니다.
        await last_p.click(position={'x': 10, 'y': 10}) # 문단의 왼쪽 상단 안전하게 클릭
        
        await page.keyboard.press("End") # 문장의 끝으로 이동
        for _ in range(2): 
            await page.keyboard.press("Enter") # 새 줄 만들기
            
    except Exception as e:
        print(f"커서 이동 중 예외 발생: {e}")
        await target_frame.locator('body').click()

    # 보험: 그래도 팝업이 떴다면 여기서 컷!
    await close_movie_popup(target_frame)

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
                    await page.keyboard.press("Escape")
                    await page.keyboard.press("End")
                    photo_btn = target_frame.locator('button[data-name="image"]').first
                    async with page.expect_file_chooser() as fc_info:
                        await photo_btn.dispatch_event("click") 
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(img_path)
                    await asyncio.sleep(7 + base_delay)
                    await page.keyboard.press("End")
                    await human_delay(1.0, 2.5)
                    await page.keyboard.press("Enter")
                    await human_delay(1.0, 2.5)
            except Exception as e:
                await save_debug_screenshot(page, f"img_failed_{i}", current_user_id)
            finally:
                if os.path.exists(img_path):
                    try: os.remove(img_path)
                    except: pass
        
        await human_typing(page, section.get("content", ""))
        await human_delay(1.0, 2.5)
        for _ in range(random.randint(1,4)):
            await page.keyboard.press("Enter")

            if random.random() < 0.4:
                await asyncio.sleep(random.uniform(0.3,1.0))

        await redis_manager.publish(task_id, f"✅ {i+1}번 섹션 작성 완료", current_user_id)

    await check_abort(task_id)
    if conclusion:
        await human_typing(page, conclusion) # 이 부분 수정!
        await page.keyboard.press("Enter")
        await redis_manager.publish(task_id, "🏁 결론 작성 완료", current_user_id)

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

    # 발행전 혹시나 영화검색 팝업이 떠있으면 팝업 제거
    await close_movie_popup(target_frame)

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
        box = await publish_btn.bounding_box()

        if box:
            await human_mouse_move(page)
            await human_mouse_move(
                page,
                end=(box["x"] + box["width"]/2, box["y"] + box["height"]/2),
                steps=random.randint(20,35)
            )

        await asyncio.sleep(random.uniform(0.5,1.2))
        
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

        await redis_manager.publish(task_id, "🎉 네이버 블로그 포스팅 발행 완료!", current_user_id)
        await record_success_count(current_user_id, naver_id, title)

    except Exception as e:
        await save_debug_screenshot(page, "final_real_error", current_user_id)
        print(f"❌ 발행 오류 발생: {str(e)}")
        await redis_manager.publish(task_id, f"❌ 발행 오류: {str(e)}", current_user_id)


async def close_movie_popup(target_frame):
    try:
        # 1. 팝업 닫기 버튼을 찾습니다.
        close_btn = target_frame.locator('button.se-popup-flayer-close-button[data-log="matflt*sch.close"]')
        
        # 2. 팝업이 화면에 있는지 확인합니다.
        if await close_btn.is_visible(timeout=2000):
            print("🎬 영화 검색 팝업 감지됨. 닫는 중...")
            await close_btn.click(force=True)
            await asyncio.sleep(0.5)
            print("✅ 팝업 닫기 완료")
    except Exception as e:
        print(f"ℹ️ 팝업 닫기 처리 중 특이사항 없음: {e}")