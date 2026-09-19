from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.models.user import User, UserRole


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


def require_role(user: User, allowed_roles: set[UserRole]) -> User:
    if user.role in allowed_roles:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You are not authorized to access this resource.",
    )


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_buyer_user(current_user: CurrentUser) -> User:
    return require_role(current_user, {UserRole.BUYER})


def require_seller_user(current_user: CurrentUser) -> User:
    return require_role(current_user, {UserRole.SELLER})


def require_admin_user(current_user: CurrentUser) -> User:
    return require_role(current_user, {UserRole.ADMIN})
