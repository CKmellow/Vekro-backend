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


def require_member(current_user: CurrentUser) -> User:
    """Any active non-admin account: every member can buy (docs/accounts-and-modes.md)."""
    if current_user.role == UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin accounts can't buy or sell.",
        )
    return current_user


def require_store_owner(current_user: CurrentUser) -> User:
    """A member who has opened a store, active or closed: its seller history stays readable."""
    if not current_user.has_store:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Open a store to do this.",
        )
    return current_user


def require_active_store(current_user: CurrentUser) -> User:
    """A member whose store is open: needed to list or change what it sells."""
    if not current_user.store_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your store is closed. Reopen it to do this."
                if current_user.has_store
                else "Open a store to do this."
            ),
        )
    return current_user


def require_admin_user(current_user: CurrentUser) -> User:
    return require_role(current_user, {UserRole.ADMIN})
