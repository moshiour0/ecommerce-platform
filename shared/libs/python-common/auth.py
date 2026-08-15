import jwt
from typing import Dict, Any, Optional

def verify_jwt(token: str, secret_or_public_key: str, algorithms: list[str] = ["HS256", "RS256"]) -> Optional[Dict[str, Any]]:
    """
    Securely decodes and validates a JWT token.
    Gracefully handles expired signatures and invalid tokens.
    """
    try:
        # We assume the issuer/audience validation is either handled by the API Gateway
        # or we explicitly validate it here if provided in a real configuration.
        decoded_payload = jwt.decode(
            token,
            secret_or_public_key,
            algorithms=algorithms,
            options={"verify_signature": True, "verify_exp": True}
        )
        return decoded_payload
    except jwt.ExpiredSignatureError:
        print("JWT Token has expired")
        return None
    except jwt.InvalidTokenError as e:
        print(f"Invalid JWT Token: {e}")
        return None
