"""Re-encrypt every stored webhook signing secret with the primary (first) key in
WEBHOOK_SECRET_KEYS, so that older keys can be removed afterwards.

    cd backend
    uv run python -m scripts.rotate_webhook_secrets --dry-run
    uv run python -m scripts.rotate_webhook_secrets

    # in the containers
    docker compose exec api python -m scripts.rotate_webhook_secrets

Run it only after the API and the worker were restarted with the new key first in
WEBHOOK_SECRET_KEYS (see README, "Rotating a key"); otherwise they would keep encrypting
new subscriptions with the old key.

- Batches of --batch-size rows, one transaction each, in id order.
- Idempotent: a secret the primary key already decrypts is left alone, so a second run
  changes nothing. A row is only rewritten if its ciphertext is still the one that was
  read.
- --dry-run reports what would change and writes nothing.
- A secret no configured key can decrypt is reported by subscription id and left as it
  is (its deliveries already fail); the exit code is then 1. Secrets are never printed.
"""

import argparse
import asyncio
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.crypto import SecretBox, SecretBoxError, secret_box_from_settings
from app.db.models import Subscription, SubscriptionChannel
from app.db.session import create_engine, create_sessionmaker

DEFAULT_BATCH_SIZE = 500


@dataclass(slots=True)
class RotationReport:
    dry_run: bool
    checked: int = 0
    rotated: int = 0  # re-encrypted (or, in a dry run, would be)
    already_current: int = 0
    undecryptable: list[uuid.UUID] = field(default_factory=list)

    def summary(self) -> str:
        verb = "would re-encrypt" if self.dry_run else "re-encrypted"
        text = (
            f"checked {self.checked} webhook secret(s): {verb} {self.rotated}, "
            f"{self.already_current} already on the primary key"
        )
        if self.undecryptable:
            ids = ", ".join(str(i) for i in self.undecryptable)
            text += (
                f"; {len(self.undecryptable)} cannot be decrypted with any configured key "
                f"(subscription ids: {ids})"
            )
        return text


async def rotate_webhook_secrets(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> RotationReport:
    report = RotationReport(dry_run=dry_run)
    after: uuid.UUID | None = None
    while True:
        async with session_factory() as session, session.begin():
            query = (
                select(Subscription.id, Subscription.webhook_secret_encrypted)
                .where(
                    Subscription.channel == SubscriptionChannel.WEBHOOK,
                    Subscription.webhook_secret_encrypted.is_not(None),
                )
                .order_by(Subscription.id)
                .limit(batch_size)
            )
            if after is not None:
                query = query.where(Subscription.id > after)
            rows = (await session.execute(query)).all()
            for row_id, token in rows:
                report.checked += 1
                if token is not None:  # filtered in SQL; narrows the type
                    await _rotate_one(session, box, row_id, token, report)
        if len(rows) < batch_size:
            return report
        after = rows[-1][0]


async def _rotate_one(
    session: AsyncSession, box: SecretBox, row_id: uuid.UUID, token: str, report: RotationReport
) -> None:
    if box.is_current(token):
        report.already_current += 1
        return
    try:
        rotated = box.rotate(token)
    except SecretBoxError:
        report.undecryptable.append(row_id)
        return
    if report.dry_run:
        report.rotated += 1
        return
    written = await session.scalar(
        update(Subscription)
        # Unchanged since it was read; otherwise leave it to the next run.
        .where(Subscription.id == row_id, Subscription.webhook_secret_encrypted == token)
        .values(webhook_secret_encrypted=rotated)
        .returning(Subscription.id)
    )
    if written is not None:
        report.rotated += 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-encrypt stored webhook signing secrets with the primary key."
    )
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    return args


async def _main(args: argparse.Namespace) -> RotationReport:
    settings = get_settings()
    box = secret_box_from_settings(settings)
    if box is None:
        raise SystemExit("refusing: WEBHOOK_SECRET_KEYS is not set")
    engine = create_engine(settings)
    try:
        return await rotate_webhook_secrets(
            create_sessionmaker(engine), box, batch_size=args.batch_size, dry_run=args.dry_run
        )
    finally:
        await engine.dispose()


def main() -> None:
    report = asyncio.run(_main(_parse_args()))
    print(report.summary())
    if report.undecryptable:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
