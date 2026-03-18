import os
import time
import uuid
import random
import json
import asyncio

# --- 이미지 처리 (PIL) ---
from PIL import Image, ImageDraw, ImageFont

# --- 비동기 HTTP 및 파일 처리 ---
# 설치 필요: pip install aiohttp aiofiles
import aiohttp
import aiofiles

# --- OpenAI API ---
# 설치 필요: pip install openai
from openai import OpenAI

# --- URL 처리 (무료 API 사용 시 대비) ---
from urllib.parse import quote

from api.v1.dependencies.redis_manager import redis_manager

from api.services.utils import (
    get_smart_wrapped_text,
    check_abort
)


# 이미지 설정
DOMAIN = "https://hntrack.co.kr"
IMAGE_SAVE_DIR = "./static/blog_images"
URL_PATH = "/static/blog_images"
os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)

# ✅ 에러 클래스를 여기로 옮겨서 외부 의존성을 없앱니다.
class ChatGptError(Exception):
    """챗지피티 요금을 충전해야 할 때 발생하는 커스텀 에러"""
    pass

async def generate_blog_content(title: str, ai_role: str, local_client):
    """GPT를 사용하여 블로그 원고를 JSON 형태로 생성합니다."""
    try:
        gpt_response = local_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "너는 블로그 전문가야. JSON {'introduction': '...', 'sections': [{'subtitle': '...', 'content': '...', 'image_description': '...'}], 'conclusion': '...'} 구조로 응답해."},
                {"role": "user", "content": f"'{title}' 주제로 글을 써줘. {ai_role}"}
            ],
            response_format={ "type": "json_object" }
        )
        
        content = json.loads(gpt_response.choices[0].message.content)
        content["title"] = title
        return content

    except json.JSONDecodeError as je:
        print(f"JSON 파싱 에러: {je}")
        return None
    except Exception as e:
        error_msg = str(e)
        if "billing_not_active" in error_msg or "429" in error_msg:
            print(f"‼️ GPT 결제 만료 알림: {error_msg}")
            raise ChatGptError("GPT API 계정이 비활성 상태입니다. 요금 충전 및 결제 수단을 확인해주세요.")
        
        print(f"GPT 생성 에러: {e}")
        return None

# --- [수정] DALL-E 3를 이용한 AI 이미지 생성 로직 ---
async def generate_blog_images_ai(naver_id: str, blog_data: dict, task_id: str, user_id: str, local_client):
    """
    GPT가 제안한 image_description을 기반으로 DALL-E 2 AI 이미지를 생성하고 
    서버 로컬에 저장하여 중복을 방지하는 고유 URL을 매핑합니다.
    """
    # 1. 기초 검증 및 원고 확보
    if not blog_data or "sections" not in blog_data:
        print("⚠️ 블로그 원고 데이터가 없거나 sections가 누락되었습니다. AI 이미지 생성을 건너뜁니다.")
        return blog_data

    sections = blog_data.get("sections", [])
    current_count = 1
    
    # 2. 파일 다운로드를 위한 비동기 세션 생성
    async with aiohttp.ClientSession() as session:
        for sec in sections:
            # GPT가 생성해준 이미지 프롬프트 추출
            # 만약 GPT가 프롬프트를 안 줬으면 subtitle을 대신 사용 (대비책)
            image_prompt = sec.get("image_description", sec.get("subtitle", "")).strip()
            
            # 프롬프트가 아예 없으면 생성 불가
            if not image_prompt or len(image_prompt) < 5:
                print(f"⚠️ {current_count}번 섹션에 이미지 프롬프트가 부족합니다. 생성을 건너뜁니다.")
                sec["image_url"] = None
                current_count += 1
                continue

            try:
                # 3. 사용자 알림 발송
                await redis_manager.publish(task_id, f"🖼️ {current_count}번째 고유 AI 이미지 생성 중... (DALL-E 3)", user_id)
                
                # 4. [핵심] OpenAI DALL-E 3 API 호출 (비동기 처리 불가, 동기 호출을 run_in_executor로 감싸는 게 좋지만 일단 기본 호출)
                # 네이버 블로그에 맞는 16:9 비율(1024x1792는 세로, 세로는 네이버에서 싫어함)을 위해 1024x1024 생성 후 크롭하거나, 
                # 퀄리티를 위해 hd 모드 사용. 비용 절감을 위해 standard 사용 가능.
                print(f"🎨 DALL-E 프롬프트 ({current_count}번): {image_prompt[:50]}...")
                
                # 안전한 프롬프트를 위해 수식어 추가 (검열 방지 및 퀄리티 향상)
                refined_prompt = f"A realistic, high-quality photograph suitable for a professional blog post about: {image_prompt}. Cinematic lighting, detailed, clear focus."
                
                response = local_client.images.generate(
                    model="dall-e-2",
                    prompt=refined_prompt,
                    size="512x512", # 1:1 비율 (네이버에서 무난함)
                    quality="standard", # standard(저렴) 또는 hd(고화질/고비용)
                    n=1,
                )
                
                # DALL-E가 준 임시 URL
                dalle_temp_url = response.data[0].url
                
                # 5. [중요] 임시 URL의 이미지를 서버로 다운로드 (기존 파일명 규칙 유지)
                # 예: i_v2_3456_1.png
                short_id = naver_id[:2]  # 네이버 아이디 앞 2글자
                ts = str(int(time.time()))[-4:]  # 현재 시간 마지막 4자리 (중복 방지)
                file_name = f"i_{short_id}_{ts}_{current_count}.png"
                file_path = os.path.join(IMAGE_SAVE_DIR, file_name)

                # 비동기 다운로드 및 저장
                async with session.get(dalle_temp_url) as resp:
                    if resp.status == 200:
                        f = await aiofiles.open(file_path, mode='wb')
                        await f.write(await resp.read())
                        await f.close()
                        
                        # 6. 최종 고유 URL 구성 (기존 로직 유지)
                        sec["image_url"] = f"{DOMAIN}{URL_PATH}/{file_name}"
                        print(f"✅ AI 이미지 생성 및 저장 완료: {file_name}")
                    else:
                        print(f"❌ DALL-E 이미지 다운로드 실패 (상태코드: {resp.status})")
                        sec["image_url"] = None

                current_count += 1
                
                # DALL-E 3 API는 호출당 대기 시간이 기므로 asyncio.sleep은 필요 없음

            except Exception as e:
                error_msg = str(e)
                print(f"이미지 생성 실패: {error_msg}")
                
                # GPT 결제 실패와 동일하게 이미지 API도 잔액 부족 체크 필요
                if "billing_not_active" in error_msg or "insufficient_quota" in error_msg:
                    await redis_manager.publish(task_id, f"❌ OpenAI 요금 부족으로 이미지 생성 중단", user_id)
                    raise ChatGptError("OpenAI API 요금이 부족하여 이미지 생성을 진행할 수 없습니다. 충전 후 다시 시도해주세요.")

                await redis_manager.publish(task_id, f"⚠️ {current_count}번 AI 이미지 생성 실패 (건너뜀)", user_id)
                sec["image_url"] = None
    
    return blog_data

async def generate_hybrid_blog_images(naver_id: str, blog_data: dict, task_id: str, user_id: str, local_client):
    """
    이미지를 생성하고 외부 URL이 아닌 '서버 로컬 절대 경로'를 저장합니다.
    파일명 또한 일반적인 사진 파일처럼 랜덤화하여 봇 흔적을 제거합니다.
    """
    if not blog_data or "sections" not in blog_data:
        return blog_data

    sections = blog_data.get("sections", [])
    total_sections = len(sections)
    
    async with aiohttp.ClientSession() as session:
        for idx, sec in enumerate(sections):
            current_count = idx + 1
            is_first = (idx == 0)
            is_last = (idx == total_sections - 1)
            
            # 1. 파일명 세탁: 봇 냄새나는 규칙 대신 일반적인 파일명처럼 생성
            # 예: IMG_5829.png 또는 k_20260309_82.png
            random_suffix = random.randint(1000, 9999)
            file_name = f"IMG_{random_suffix}_{current_count}.png"
            file_path = os.path.join(IMAGE_SAVE_DIR, file_name)
            # 절대 경로 확보 (Playwright 업로드용)
            abs_file_path = os.path.abspath(file_path)

            # --- [AI 이미지 생성: 첫 번째/마지막] ---
            if is_first or is_last:
                try:
                    await redis_manager.publish(task_id, f"🎨 {current_count}번 섹션: 고퀄리티 AI 이미지 생성 중...", user_id)
                    
                    image_prompt = sec.get("image_description", sec.get("subtitle", "")).strip()
                    # 프롬프트에 '사진처럼' 느낌을 강하게 줌
                    refined_prompt = f"A real photo taken with a smartphone: {image_prompt}. Natural lighting, high resolution."
                    
                    response = local_client.images.generate(
                        model="dall-e-2",
                        prompt=refined_prompt,
                        size="512x512",
                        n=1,
                    )
                    
                    dalle_temp_url = response.data[0].url
                    
                    async with session.get(dalle_temp_url) as resp:
                        if resp.status == 200:
                            async with aiofiles.open(file_path, mode='wb') as f:
                                await f.write(await resp.read())
                            # URL 대신 로컬 절대 경로 저장
                            sec["image_path"] = abs_file_path
                        else:
                            sec["image_path"] = None
                except Exception as e:
                    print(f"AI 이미지 생성 실패: {e}")
                    sec["image_path"] = None

            # --- [PIL 이미지 생성: 중간 섹션] ---
            else:
                try:
                    await redis_manager.publish(task_id, f"🖼️ {current_count}번 섹션: 텍스트 기반 이미지 생성 중...", user_id)
                    
                    img = Image.new('RGB', (1200, 630), color=(255, 255, 255))
                    d = ImageDraw.Draw(img)
                    
                    border_color = (0, 102, 204)
                    d.rectangle([0, 0, 1200, 630], outline=border_color, width=14)
                    
                    text_to_draw = sec.get("subtitle", f"Section {current_count}").strip()
                    wrapped_text = get_smart_wrapped_text(text_to_draw)
                    
                    try:
                        font = ImageFont.truetype("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", 70)
                    except:
                        font = ImageFont.load_default()

                    left, top, right, bottom = d.multiline_textbbox((0, 0), wrapped_text, font=font, align="center")
                    w, h = right - left, bottom - top
                    d.multiline_text(((1200-w)/2, (630-h)/2), wrapped_text, fill=(0, 0, 0), font=font, align="center", spacing=12)
                    
                    img.save(file_path)
                    # URL 대신 로컬 절대 경로 저장
                    sec["image_path"] = abs_file_path
                except Exception as e:
                    print(f"PIL 이미지 생성 실패: {e}")
                    sec["image_path"] = None

            await asyncio.sleep(0.1)

    return blog_data