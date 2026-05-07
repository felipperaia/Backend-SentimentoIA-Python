from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime
from enum import Enum

class UserRole(str, Enum):
    USER = "user"
    ADMIN = "admin"

class UserBase(BaseModel):
    email: EmailStr
    name: str
    phone: Optional[str] = None

class UserCreate(UserBase):
    password: str = Field(..., min_length=8)

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(UserBase):
    id: Optional[str] = None
    openId: Optional[str] = None
    role: UserRole = UserRole.USER
    mfa_enabled: bool = False
    mfa_verified: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_signed_in: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class MFASetup(BaseModel):
    secret: str
    qr_code: str

class MFAVerify(BaseModel):
    code: str

class PasswordReset(BaseModel):
    email: EmailStr

class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)

class UserUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    
    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
