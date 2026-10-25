from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.auth_context import get_client_identifier, require_seller_user
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.user import User, UserRole
from app.schemas.auth import PhoneCodeRequestResponse
from app.schemas.seller_profile import (
    PayoutCodeRequest,
    PayoutUpdate,
    SellerProfileResponse,
    SellerProfileUpdate,
)
from app.services.notification_delivery import run_in_background
from app.services.phone_auth import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneCodeRateLimitedError,
)
from app.services.seller_profile import (
    LogoRejectedError,
    PayoutCodeRequiredError,
    change_payout,
    clear_logo,
    has_orders,
    payout_phone,
    request_payout_code,
    set_logo,
    update_profile,
)

router = APIRouter(tags=["Seller profile"])

SELLER_ONLY = {
    status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
    status.HTTP_403_FORBIDDEN: {"description": "Not a seller."},
}


def _seller(db: Session, current: User) -> User:
    # The session user belongs to the middleware's DB session; load it into this one to change it.
    user = db.get(User, current.id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return user


def _profile(db: Session, user: User) -> SellerProfileResponse:
    logo_url = (
        f"/sellers/{user.id}/logo?v={int(user.logo_updated_at.timestamp())}"
        if user.logo_updated_at is not None
        else None
    )
    return SellerProfileResponse(
        name=user.name,
        phone=user.phone,
        shop_name=user.shop_name,
        shop_area=user.shop_area,
        shop_about=user.shop_about,
        logo_url=logo_url,
        mpesa_phone=payout_phone(user),
        mpesa_account_name=user.mpesa_account_name,
        has_orders=has_orders(db, user.id),
        updated_at=user.updated_at,
    )


@router.get(
    "/me/profile",
    response_model=SellerProfileResponse,
    summary="The seller's shop profile and payout details",
    responses=SELLER_ONLY,
)
def get_profile(
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SellerProfileResponse:
    return _profile(db, _seller(db, current_user))


@router.patch(
    "/me/profile",
    response_model=SellerProfileResponse,
    summary="Change shop name, area or description",
    description="Only the fields sent change; an empty string clears the area or description.",
    responses=SELLER_ONLY,
)
def patch_profile(
    payload: SellerProfileUpdate,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SellerProfileResponse:
    user = update_profile(db, _seller(db, current_user), payload.model_dump(exclude_unset=True))
    return _profile(db, user)


@router.put(
    "/me/profile/logo",
    response_model=SellerProfileResponse,
    summary="Upload the shop logo",
    description="Raw image body (JPEG, PNG or WebP, at most 1 MB), compressed on the client.",
    responses={
        **SELLER_ONLY,
        status.HTTP_413_CONTENT_TOO_LARGE: {"description": "Larger than 1 MB."},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"description": "Not a JPEG, PNG or WebP."},
    },
)
def put_logo(
    data: Annotated[bytes, Body(media_type="image/jpeg")],
    content_type: Annotated[str, Header()],
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SellerProfileResponse:
    try:
        user = set_logo(db, _seller(db, current_user), content_type, data)
    except LogoRejectedError as exc:
        code = (
            status.HTTP_413_CONTENT_TOO_LARGE
            if exc.too_large
            else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        )
        raise HTTPException(code, detail=str(exc)) from exc
    return _profile(db, user)


@router.delete(
    "/me/profile/logo",
    response_model=SellerProfileResponse,
    summary="Remove the shop logo",
    responses=SELLER_ONLY,
)
def delete_logo(
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SellerProfileResponse:
    return _profile(db, clear_logo(db, _seller(db, current_user)))


@router.get(
    "/sellers/{seller_id}/logo",
    summary="A seller's shop logo",
    description="Public image. The URL carries a version, so it caches for a year.",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}}},
        status.HTTP_404_NOT_FOUND: {"description": "No logo."},
    },
)
def get_logo(seller_id: UUID, db: Annotated[Session, Depends(get_db)]) -> Response:
    user = db.get(User, seller_id)
    if (
        user is None
        or user.role != UserRole.SELLER
        or user.logo_data is None
        or not user.logo_content_type
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No logo.")
    return Response(
        content=user.logo_data,
        media_type=user.logo_content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post(
    "/me/mpesa-number/request-code",
    response_model=PhoneCodeRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Text a code to a new payout number",
    description="Same cooldown, hourly cap and lockout as sign-in codes.",
    responses={
        **SELLER_ONLY,
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many codes. See Retry-After."},
    },
)
def payout_request_code(
    payload: PayoutCodeRequest,
    request: Request,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PhoneCodeRequestResponse:
    try:
        issued = request_payout_code(
            db,
            payload.mpesa_phone,
            client_identifier=get_client_identifier(request),
            dispatch=run_in_background,
        )
    except PhoneCodeRateLimitedError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many codes. Please wait before asking again.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    return PhoneCodeRequestResponse(
        resend_after_seconds=issued.resend_after_seconds,
        expires_at=issued.expires_at,
        dev_code=issued.code if get_settings().phone_code_dev_echo else None,
    )


@router.put(
    "/me/mpesa-number",
    response_model=SellerProfileResponse,
    summary="Change the payout number and account name",
    description=(
        "A new number needs the code texted to it, except before the seller's first order. "
        "The previous number gets an SMS saying it changed. Payouts not yet sent go to the new "
        "number."
    ),
    responses={
        **SELLER_ONLY,
        status.HTTP_410_GONE: {"description": "No active code: it expired or was used."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The code doesn't match."},
        status.HTTP_428_PRECONDITION_REQUIRED: {"description": "A code is needed for this number."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many wrong tries."},
    },
)
def put_payout(
    payload: PayoutUpdate,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SellerProfileResponse:
    try:
        user = change_payout(
            db,
            _seller(db, current_user),
            mpesa_phone=payload.mpesa_phone,
            mpesa_account_name=payload.mpesa_account_name,
            code=payload.code,
            dispatch=run_in_background,
        )
    except PayoutCodeRequiredError as exc:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED, detail="Confirm the new number with its code."
        ) from exc
    except PhoneCodeInvalidError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="That code doesn't match."
        ) from exc
    except PhoneCodeExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="That code has expired.") from exc
    except PhoneCodeRateLimitedError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many tries. Please wait before trying again.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    return _profile(db, user)
