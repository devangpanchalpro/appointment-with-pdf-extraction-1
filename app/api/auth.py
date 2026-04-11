import binascii
import logging
from jose import jwe, jwt, JWSError, JWTError, ExpiredSignatureError
from fastapi import APIRouter, Security, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from cryptography.hazmat.primitives.serialization import load_der_public_key
from cryptography.hazmat.backends import default_backend
from app.config.settings import settings

logger = logging.getLogger(__name__)
security = HTTPBearer()

# Pre-load the public ECDSA key to speed up API requests for token signature verification
try:
    if settings.JWT_SIGNING_KEY:
        der_bytes = binascii.unhexlify(settings.JWT_SIGNING_KEY)
        _SIGNING_PUB_KEY = load_der_public_key(der_bytes, backend=default_backend())
    else:
        _SIGNING_PUB_KEY = None
except Exception as e:
    logger.error(f"Failed to load ECDSA signing key from config: {e}")
    _SIGNING_PUB_KEY = None

def verify_jwt(auth: HTTPAuthorizationCredentials = Security(security)):
    """
    Verify the Nested JWE -> JWS token passed in the Authorization header.
    Expects 'Bearer <token>' where token is a signed or encrypted JWT string.
    """
    token = auth.credentials
    try:
        parts = token.split('.')
        payload_str = token
        
        # If it's a JWE (encrypted token), decrypt it first using our Private RSA key
        if len(parts) == 5:
            if not settings.JWT_ENCRYPTION_KEY:
                raise HTTPException(status_code=500, detail="Encryption key not configured for JWE decryption")
            
            # Ensure our RSA PEM is correctly formatted with newlines (handle escaping from env strings)
            priv_key_pem = settings.JWT_ENCRYPTION_KEY.replace('\\r\\n', '\n').replace('\\n', '\n')
            
            try:
                decrypted_bytes = jwe.decrypt(token, priv_key_pem)
                payload_str = decrypted_bytes.decode('utf-8')
            except JWSError as e:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Failed to decrypt JWE wrapper: {str(e)}")
        
        # Now payload_str must be a standard JWS (Signed JWT)
        # Verify the signature using the external UI's Public ECDSA signing key
        if not _SIGNING_PUB_KEY:
            raise HTTPException(status_code=500, detail="ECDSA Signing key not configured for JWS verification")
        
        # We decode utilizing the public key and standard algorithms
        payload = jwt.decode(
            payload_str,
            _SIGNING_PUB_KEY,
            algorithms=['ES512', 'ES521', 'ES384', 'ES256', 'RS256', 'RS512', 'RS384', 'HS256', 'HS512', 'PS256', 'PS512'],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER
        )
        return payload
        
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token signature has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token signature or claims validation failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except HTTPException:
        # Pass through HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"General authentication failure: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate token credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
