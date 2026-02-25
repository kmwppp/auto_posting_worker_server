from enum import Enum
from pydantic import BaseModel, ConfigDict
from typing import List, Optional
from datetime import datetime

# --- 블로그 관련 스키마 정의 ---

class UserAuthModel(BaseModel):
    current_user_id: str
    site_name: str
    external_id: str
    external_pw: str
    postingCount: int
    blog_id: str
    proxy_id: str
    proxy_pw: str
    port: str

class MainBlogType(str, Enum):
    WORDPRESS = "wordPress"
    BLOGSPOT = "blogSpot"

class PostTitleType(str, Enum):
    KEYWORD = "keyword"
    URL = "url"

class PostType(str, Enum):
    COMMERCIAL = "commercial"
    INFORMATIVE = "informative"

class PostingTermType(str, Enum):
    IMMEDIATELY = "immediately"
    RESERVATION = "reservation"

class PostKeywordModel(BaseModel):
    main_keyword: str
    posting_title: str

class PostUrlModel(BaseModel):
    posting_title: str
    url: str

class BlogBulkRequest(BaseModel):
    proxy: str
    proxyUse: bool
    authList: List[UserAuthModel]
    mainBlogType: MainBlogType
    postType: PostType
    siteUrl: str
    linkTopText: str
    postTitleType: PostTitleType
    postKeywordTitleList: Optional[List[PostKeywordModel]] = None
    postURLTitleList: Optional[List[PostUrlModel]] = None
    autoChangeQRLink: bool
    aiWriteRole: str
    postingTerm: int
    postingTermType: PostingTermType

class CredentialResponse(BaseModel):
    owner_id: int  # str에서 int로 변경 (로그에서 input이 5이므로)
    login_id: str
    login_pw: str
    proxy_id: Optional[str] = None
    proxy_pw: Optional[str] = None
    proxy_port: Optional[str] = None
    
    # credential_type은 DB에 없으므로 삭제하거나 Optional로 변경
    # credential_type: Optional[str] = None 

    model_config = ConfigDict(from_attributes=True)
