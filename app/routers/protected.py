from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth_context import (
    require_admin_user,
    require_buyer_user,
    require_seller_user,
)
from app.models.user import User

router = APIRouter(prefix="/protected", tags=["Auth"])


@router.get(
    "/buyer",
    summary="Buyer-only protected test route",
    description="Sample route demonstrating buyer role enforcement.",
    responses={
        401: {"description": "Authentication required."},
        403: {"description": "Authenticated user does not have buyer role."},
    },
)
def buyer_only_route(
    current_user: Annotated[User, Depends(require_buyer_user)],
) -> dict[str, str]:
    return {
        "message": "Buyer access granted.",
        "role": current_user.role.value,
    }


@router.get(
    "/seller",
    summary="Seller-only protected test route",
    description="Sample route demonstrating seller role enforcement.",
    responses={
        401: {"description": "Authentication required."},
        403: {"description": "Authenticated user does not have seller role."},
    },
)
def seller_only_route(
    current_user: Annotated[User, Depends(require_seller_user)],
) -> dict[str, str]:
    return {
        "message": "Seller access granted.",
        "role": current_user.role.value,
    }


@router.get(
    "/admin",
    summary="Admin-only protected test route",
    description="Sample route demonstrating admin role enforcement.",
    responses={
        401: {"description": "Authentication required."},
        403: {"description": "Authenticated user does not have admin role."},
    },
)
def admin_only_route(
    current_user: Annotated[User, Depends(require_admin_user)],
) -> dict[str, str]:
    return {
        "message": "Admin access granted.",
        "role": current_user.role.value,
    }
