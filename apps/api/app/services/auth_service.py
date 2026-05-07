import logging
import bcrypt
import pyotp
import qrcode
from io import BytesIO
import base64
from datetime import datetime, timedelta
from typing import Optional, Tuple
from bson import ObjectId
from app.database import get_db
from app.schemas import UserCreate, UserResponse, UserRole
from app.config import settings

logger = logging.getLogger(__name__)

class AuthService:
    """Serviço de autenticação e segurança"""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Criptografa uma senha usando bcrypt"""
        salt = bcrypt.gensalt(rounds=12)
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
    
    @staticmethod
    def verify_password(password: str, hashed_password: str) -> bool:
        """Verifica se a senha corresponde ao hash"""
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    
    @staticmethod
    def create_user(user_data: UserCreate) -> dict:
        """Cria um novo usuário no banco de dados"""
        db = get_db()
        
        # Verificar se o email já existe
        existing_user = db.users.find_one({"email": user_data.email})
        if existing_user:
            raise ValueError("Email já cadastrado")
        
        # Hash da senha
        hashed_password = AuthService.hash_password(user_data.password)
        
        # Criar documento do usuário
        user_doc = {
            "email": user_data.email,
            "name": user_data.name,
            "phone": user_data.phone,
            "password_hash": hashed_password,
            "role": UserRole.USER.value,
            "mfa_enabled": False,
            "mfa_verified": False,
            "mfa_secret": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "last_signed_in": datetime.utcnow(),
            "is_active": True,
        }
        
        result = db.users.insert_one(user_doc)
        user_doc["_id"] = result.inserted_id
        
        logger.info(f"✓ Usuário criado: {user_data.email}")
        return user_doc
    
    @staticmethod
    def get_user_by_email(email: str) -> Optional[dict]:
        """Obtém um usuário pelo email"""
        db = get_db()
        return db.users.find_one({"email": email})
    
    @staticmethod
    def get_user_by_id(user_id: str) -> Optional[dict]:
        """Obtém um usuário pelo ID"""
        db = get_db()
        try:
            return db.users.find_one({"_id": ObjectId(user_id)})
        except:
            return None
    
    @staticmethod
    def authenticate_user(email: str, password: str) -> Optional[dict]:
        """Autentica um usuário com email e senha"""
        user = AuthService.get_user_by_email(email)
        
        if not user:
            logger.warning(f"✗ Tentativa de login com email inexistente: {email}")
            return None
        
        if not user.get("is_active"):
            logger.warning(f"✗ Tentativa de login com usuário inativo: {email}")
            return None
        
        if not AuthService.verify_password(password, user.get("password_hash", "")):
            logger.warning(f"✗ Tentativa de login com senha incorreta: {email}")
            return None
        
        # Atualizar último acesso
        db = get_db()
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"last_signed_in": datetime.utcnow()}}
        )
        
        logger.info(f"✓ Usuário autenticado: {email}")
        return user
    
    @staticmethod
    def setup_mfa(user_id: str) -> Tuple[str, str]:
        """Configura MFA para um usuário e retorna secret e QR code"""
        db = get_db()
        
        # Gerar secret TOTP
        secret = pyotp.random_base32()
        
        # Gerar QR code
        totp = pyotp.TOTP(secret)
        qr_uri = totp.provisioning_uri(
            name=f"SentimentoIA ({user_id})",
            issuer_name="SentimentoIA"
        )
        
        # Criar imagem QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(qr_uri)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        qr_code_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        # Salvar secret no banco (ainda não verificado)
        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"mfa_secret": secret, "mfa_enabled": False}}
        )
        
        logger.info(f"✓ MFA configurado para usuário: {user_id}")
        return secret, qr_code_base64
    
    @staticmethod
    def verify_mfa_code(user_id: str, code: str) -> bool:
        """Verifica um código MFA"""
        db = get_db()
        user = db.users.find_one({"_id": ObjectId(user_id)})
        
        if not user or not user.get("mfa_secret"):
            return False
        
        totp = pyotp.TOTP(user["mfa_secret"])
        
        # Permitir código atual e anterior (para sincronização de relógio)
        if totp.verify(code):
            # Marcar MFA como verificado
            db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"mfa_verified": True, "mfa_enabled": True}}
            )
            logger.info(f"✓ MFA verificado para usuário: {user_id}")
            return True
        
        logger.warning(f"✗ Código MFA inválido para usuário: {user_id}")
        return False
    
    @staticmethod
    def disable_mfa(user_id: str) -> bool:
        """Desabilita MFA para um usuário"""
        db = get_db()
        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"mfa_enabled": False, "mfa_verified": False, "mfa_secret": None}}
        )
        logger.info(f"✓ MFA desabilitado para usuário: {user_id}")
        return True
    
    @staticmethod
    def log_audit(user_id: str, action: str, details: dict = None, ip_address: str = None):
        """Registra uma ação de auditoria"""
        db = get_db()
        
        audit_log = {
            "user_id": user_id,
            "action": action,
            "details": details or {},
            "ip_address": ip_address,
            "timestamp": datetime.utcnow(),
        }
        
        db.audit_logs.insert_one(audit_log)
        logger.info(f"✓ Auditoria registrada: {action} para usuário: {user_id}")
