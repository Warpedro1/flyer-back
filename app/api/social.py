"""Social graph and privacy endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import DbServiceDep, get_current_user_with_token
from app.models.schemas import FollowAction, FollowStatus, PendingFollowOut, ProfileBrief
from app.services.db_service import UniqueViolationError

router = APIRouter(prefix="/social", tags=["social"])


@router.post("/follow/{target_id}", status_code=status.HTTP_201_CREATED)
async def follow_user(
    target_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> dict[str, str]:
    user_id, jwt = auth
    if user_id == target_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot follow yourself.")
    privacy = await db.get_target_privacy(target_id, jwt)
    if privacy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    status_val = FollowStatus.pending if privacy else FollowStatus.accepted
    try:
        await db.create_follow(user_id, target_id, status_val, jwt)
    except UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Follow relationship already exists.",
        ) from None
    return {"status": "created"}


@router.get("/requests", response_model=list[PendingFollowOut])
async def list_follow_requests(
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[PendingFollowOut]:
    user_id, jwt = auth
    rows = await db.get_pending_requests(user_id, jwt)
    return [
        PendingFollowOut(
            follower_id=r["follower_id"],
            following_id=r["following_id"],
            status=FollowStatus(str(r["status"])),
            created_at=r.get("created_at"),
            follower=ProfileBrief(
                id=r["follower_id"],
                name=r.get("follower_name"),
                email=r.get("follower_email"),
            ),
        )
        for r in rows
    ]


@router.patch("/requests/{follower_id}")
async def respond_to_follow_request(
    follower_id: str,
    body: FollowAction,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> dict[str, str]:
    user_id, jwt = auth
    if body.action == "accept":
        await db.accept_follow(follower_id, user_id, jwt)
    else:
        await db.delete_follow(follower_id, user_id, jwt)
    return {"status": "ok"}


@router.delete("/following/{target_id}")
async def unfollow_user(
    target_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> Response:
    user_id, jwt = auth
    await db.delete_follow(user_id, target_id, jwt)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/following", response_model=list[ProfileBrief])
async def list_following(
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[ProfileBrief]:
    user_id, jwt = auth
    rows = await db.get_following_list(user_id, jwt)
    return [ProfileBrief(id=r["id"], name=r.get("name"), email=r.get("email")) for r in rows]


@router.get("/followers", response_model=list[ProfileBrief])
async def list_followers(
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[ProfileBrief]:
    user_id, jwt = auth
    rows = await db.get_followers_list(user_id, jwt)
    return [ProfileBrief(id=r["id"], name=r.get("name"), email=r.get("email")) for r in rows]


@router.get("/health")
async def social_health() -> dict[str, str]:
    return {"status": "ok"}
