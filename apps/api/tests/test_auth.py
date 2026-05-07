from uuid import uuid4

from app.auth_utils import create_access_token, decode_access_token
from app.services.auth_service import AuthService


def test_hash_password_roundtrip() -> None:
    password = "SenhaSegura123!"
    hashed = AuthService.hash_password(password)

    assert hashed != password
    assert AuthService.verify_password(password, hashed)
    assert not AuthService.verify_password("senha-invalida", hashed)


def test_access_token_roundtrip() -> None:
    token = create_access_token(subject="user-test", role="admin")
    payload = decode_access_token(token)

    assert payload["sub"] == "user-test"
    assert payload["role"] == "admin"
    assert payload["type"] == "access"


def test_auth_register_login_and_me(client) -> None:
    email = f"auth-{uuid4().hex[:10]}@example.com"
    register_response = client.post(
        "/api/auth/register",
        json={
            "name": "Usuario Teste",
            "email": email,
            "phone": "+55 11 99999-0000",
            "password": "SenhaSegura123!",
        },
    )
    assert register_response.status_code == 201, register_response.text

    token = register_response.json()["access_token"]

    login_response = client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": "SenhaSegura123!",
        },
    )
    assert login_response.status_code == 200, login_response.text

    me_response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_response.status_code == 200, me_response.text
    payload = me_response.json()
    assert payload["email"] == email
    assert payload["name"] == "Usuario Teste"
