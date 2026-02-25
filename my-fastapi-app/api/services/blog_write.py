import asyncio
import os
from api.v1.dependencies.redis_manager import redis_manager
from api.services.utils import (
    save_debug_screenshot, 
    log_to_db, 
    record_success_count, 
    check_abort
)

class BlogIdError(Exception):
    """네이버 아이디와 글쓰기 아이디가 다를때 발생하는 커스텀 에러"""
    pass

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
        await title_placeholder.click(force=True, timeout=5000)
        await asyncio.sleep(1)
        await page.keyboard.type(title, delay=100)
        await redis_manager.publish(task_id, f"📝 포스팅 제목 작성 완료: {title}")
    except Exception as e:
        await page.mouse.click(400, 250)
        await asyncio.sleep(1)
        await page.keyboard.type(title, delay=100)
        await redis_manager.publish(task_id, f"⚠️ 포스팅 제목 작성 실패: {title}")
    
    await page.keyboard.press("Enter")
    await asyncio.sleep(1)
    await check_abort(task_id)

    # 2. 서론 입력
    await page.keyboard.type(introduction, delay=30)
    await page.keyboard.press("Enter")
    await page.keyboard.press("Enter")
    await check_abort(task_id)

    # 3. QR/단축 URL 삽입
    if short_url:
        try:
            msg = f"{link_top_text}"
            await page.keyboard.type(msg, delay=60)
            await page.keyboard.press("Enter")

            link_btn = target_frame.locator('button[data-name="oglink"]')
            await link_btn.click(force=True)
            
            input_selector = 'input.se-popup-oglink-input'
            await target_frame.wait_for_selector(input_selector, state="visible", timeout=10000)
            await target_frame.locator(input_selector).fill(short_url)
            await target_frame.locator('button.se-popup-oglink-button').click()

            confirm_btn = target_frame.locator('button.se-popup-button-confirm')
            await confirm_btn.wait_for(state="visible", timeout=10000)
            
            for _ in range(10):
                is_disabled = await confirm_btn.get_attribute("disabled")
                if is_disabled is None or is_disabled == "false":
                    await confirm_btn.click(force=True)
                    break
                await asyncio.sleep(1)

            await asyncio.sleep(base_delay)
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
                    await page.keyboard.press("Escape")
                    await page.keyboard.press("End")
                    photo_btn = target_frame.locator('button[data-name="image"]').first
                    async with page.expect_file_chooser() as fc_info:
                        await photo_btn.dispatch_event("click") 
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(img_path)
                    await asyncio.sleep(7 + base_delay) 
                    await page.keyboard.press("End")
                    await page.keyboard.press("Enter")
            except Exception as e:
                await save_debug_screenshot(page, f"img_failed_{i}", current_user_id)
            finally:
                if os.path.exists(img_path):
                    try: os.remove(img_path)
                    except: pass
        
        await page.keyboard.type(section.get("content", ""), delay=30)
        await page.keyboard.press("Enter")
        await page.keyboard.press("Enter")
        await redis_manager.publish(task_id, f"✅ {i+1}번 섹션 작성 완료")

    await check_abort(task_id)
    if conclusion:
        await page.keyboard.type(conclusion, delay=30)
        await page.keyboard.press("Enter")
        await redis_manager.publish(task_id, "🏁 결론 작성 완료")

    # 5. 강조 문구 서식 적용 (폰트 19px, 빨간색)
    try:
        msg_to_style = f"{link_top_text}"
        target_text = target_frame.get_by_text(msg_to_style).first
        await target_text.scroll_into_view_if_needed()
        await target_text.click(click_count=3, force=True) 
        await asyncio.sleep(1)

        size_open_btn = target_frame.locator('button.se-font-size-code-toolbar-button[data-group="propertyToolbar"]').first
        await size_open_btn.click(force=True)
        size_19 = target_frame.locator('button.se-toolbar-option-font-size-code-fs19-button[data-group="propertyToolbar"]').first
        await size_19.click(force=True)

        color_btn = target_frame.locator('button.se-font-color-toolbar-button[data-group="propertyToolbar"]').first
        await color_btn.click(force=True)
        red_palette = target_frame.locator('button.se-color-palette[data-color="#ff0010"]').first
        await red_palette.click(force=True)
    except:
        await page.keyboard.press("Escape")

    await check_abort(task_id)

    # 6. 발행
    print("📤 발행 시도 중...")
    try:
        try:
            await page.locator('button.se-aside-close-button').click(force=True, timeout=2000)
        except: pass

        publish_btn = page.locator('button[class*="publish_btn"]').filter(has_text="발행")
        if await publish_btn.count() == 0:
            publish_btn = target_frame.locator('button[class*="publish_btn"]').filter(has_text="발행")
        
        await publish_btn.scroll_into_view_if_needed()
        await publish_btn.click(force=True)
        await asyncio.sleep(base_delay)

        final_btn = page.locator('button').filter(has_text="발행").last
        if await final_btn.count() == 0:
            final_btn = target_frame.locator('button').filter(has_text="발행").last

        await final_btn.click(force=True)
        await asyncio.sleep(base_delay)

        await redis_manager.publish(task_id, "🎉 네이버 블로그 포스팅 발행 완료!")
        await record_success_count(current_user_id, naver_id, title)
    except Exception as e:
        await save_debug_screenshot(page, "final_real_error", current_user_id)
        await redis_manager.publish(task_id, f"❌ 발행 오류: {str(e)}")