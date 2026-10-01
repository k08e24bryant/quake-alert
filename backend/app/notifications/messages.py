"""Everything the bot says, in Indonesian (its users read BMKG in Indonesian).

Wording rules (CLAUDE.md, safety & legal): calm and factual, no emoji, never "peringatan
dini" (this is not an early-warning system), BMKG's Potensi text verbatim under the label
"Potensi (BMKG)" and never presented as tsunami information, and every alert ends with
"Sumber: BMKG" and a link to bmkg.go.id. Times in WIB, as BMKG publishes them.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.notifications.subscriptions import (
    DEFAULT_MIN_MAGNITUDE,
    DEFAULT_RADIUS_KM,
    MAX_MAGNITUDE,
    MAX_RADIUS_KM,
    MIN_MAGNITUDE,
    MIN_RADIUS_KM,
    SubscriptionView,
)

# Indonesia has no daylight saving time, so a fixed offset is exact.
WIB = timezone(timedelta(hours=7), "WIB")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")

# Verbatim from the project owner; also used in the README and the frontend footer.
DISCLAIMER = (
    "Layanan ini tidak resmi dan hanya meneruskan data dari BMKG. Notifikasi bisa terlambat "
    "atau tidak terkirim. Untuk informasi resmi dan arahan keselamatan, ikuti BMKG "
    "(bmkg.go.id / aplikasi InfoBMKG) dan BPBD setempat."
)

# Last line of every alert: attribution with a link (CLAUDE.md, safety & legal).
SOURCE_LINE = "Sumber: BMKG (https://www.bmkg.go.id)"

# First line of every alert about a synthetic quake (scripts/dev_fake_quake.py).
TEST_PREFIX = "[TES - BUKAN GEMPA NYATA]"

COMMANDS_HELP = (
    "Perintah:\n"
    f"/radius <km> - ubah radius ({MIN_RADIUS_KM}-{MAX_RADIUS_KM} km)\n"
    f"/minmag <nilai> - ubah magnitudo minimal ({MIN_MAGNITUDE}-{MAX_MAGNITUDE})\n"
    "/list - lihat langganan Anda\n"
    "/stop - berhenti dan hapus data Anda"
)

START = (
    "Bot ini mengirim info gempa dari data terbuka BMKG bila terjadi gempa di sekitar lokasi "
    "yang Anda bagikan.\n\n"
    "Untuk mulai, bagikan lokasi lewat tombol di bawah atau lampiran > Lokasi. Lokasi "
    "disimpan dalam bentuk dibulatkan (sekitar 1 km).\n"
    f"Pengaturan awal: radius {DEFAULT_RADIUS_KM} km, magnitudo minimal "
    f"{DEFAULT_MIN_MAGNITUDE}.\n\n"
    f"{COMMANDS_HELP}\n\n"
    f"{DISCLAIMER}"
)

UNKNOWN = f"Bagikan lokasi untuk berlangganan, atau gunakan perintah berikut.\n\n{COMMANDS_HELP}"
NO_SUBSCRIPTION = "Anda belum berlangganan. Bagikan lokasi untuk mulai."
RADIUS_USAGE = (
    f"Format: /radius <km>, bilangan bulat {MIN_RADIUS_KM} sampai {MAX_RADIUS_KM}. "
    "Contoh: /radius 150"
)
MIN_MAGNITUDE_USAGE = (
    f"Format: /minmag <nilai>, {MIN_MAGNITUDE} sampai {MAX_MAGNITUDE}, paling banyak satu "
    "angka desimal. Contoh: /minmag 4.5"
)
STOPPED = "Langganan dan lokasi Anda sudah dihapus. Kirim /start untuk berlangganan lagi."
NOTHING_TO_STOP = "Tidak ada langganan untuk dihapus."

SHARE_LOCATION_KEYBOARD: dict[str, Any] = {
    "keyboard": [[{"text": "Bagikan lokasi", "request_location": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}
REMOVE_KEYBOARD: dict[str, Any] = {"remove_keyboard": True}


def format_wib(moment: datetime) -> str:
    local = moment.astimezone(WIB)
    return f"{local.day:02d} {_MONTHS[local.month - 1]} {local.year} {local:%H:%M:%S} WIB"


def format_distance(distance_km: float) -> str:
    if distance_km < 1:
        return "kurang dari 1 km"
    return f"sekitar {round(distance_km)} km"


def describe(subscription: SubscriptionView) -> str:
    status = "aktif" if subscription.is_active else "nonaktif"
    return (
        f"Lokasi: {subscription.latitude}, {subscription.longitude} (dibulatkan, sekitar 1 km)\n"
        f"Radius: {subscription.radius_km} km\n"
        f"Magnitudo minimal: {subscription.min_magnitude}\n"
        f"Status: {status}"
    )


def location_saved(subscription: SubscriptionView) -> str:
    return (
        f"Lokasi tersimpan.\n{describe(subscription)}\n\n"
        f"Anda akan menerima info gempa M{subscription.min_magnitude} ke atas dalam radius "
        f"{subscription.radius_km} km dari lokasi ini."
    )


def settings_updated(subscription: SubscriptionView) -> str:
    return f"Pengaturan diperbarui.\n{describe(subscription)}"


def subscription_list(subscription: SubscriptionView) -> str:
    return f"Langganan Anda:\n{describe(subscription)}"


@dataclass(frozen=True, slots=True)
class Alert:
    magnitude: Decimal
    region: str
    depth_km: int
    occurred_at: datetime
    distance_km: float  # from the subscriber's (rounded) location, computed by PostGIS
    potential: str | None
    shakemap_url: str | None
    # Origin time of a quake this subscriber was already alerted about that is probably the
    # same event stored as another row (dedup prefers duplicates to wrong merges).
    possible_duplicate_of: datetime | None = None
    is_test: bool = False  # a synthetic dev quake, not a real one


def render_alert(alert: Alert) -> str:
    lines = [TEST_PREFIX] if alert.is_test else []
    if alert.possible_duplicate_of is not None:
        lines.append(
            "Catatan: mungkin kejadian yang sama dengan info gempa sebelumnya "
            f"({format_wib(alert.possible_duplicate_of)}), dilaporkan oleh feed BMKG lain."
        )
    lines += [
        "Info gempa",
        f"Magnitudo: {alert.magnitude}",
        f"Wilayah: {alert.region}",
        f"Waktu: {format_wib(alert.occurred_at)}",
        f"Kedalaman: {alert.depth_km} km",
        f"Jarak dari lokasi Anda: {format_distance(alert.distance_km)}",
    ]
    if alert.potential:
        lines.append(f"Potensi (BMKG): {alert.potential}")
    if alert.shakemap_url:
        lines.append(f"Peta guncangan (shakemap): {alert.shakemap_url}")
    lines.append(SOURCE_LINE)
    return "\n".join(lines)
