from fastapi import HTTPException, Request, status

from app.models.user import User


def get_current_user(request: Request) -> User:
    user = getattr(request.state, "current_user", None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return user


def get_client_identifier(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"
