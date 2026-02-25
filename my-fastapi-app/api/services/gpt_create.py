import json
from openai import OpenAI

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