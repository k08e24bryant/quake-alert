/** Two decimals (about 1 km), like the backend stores subscriptions: precise enough for a
 * radius of 50 km or more, and the exact position never leaves the browser. */
export function roundCoordinate(value: number): number {
  return Math.round(value * 100) / 100;
}

export function isLatitude(value: number): boolean {
  return Number.isFinite(value) && value >= -90 && value <= 90;
}

export function isLongitude(value: number): boolean {
  return Number.isFinite(value) && value >= -180 && value <= 180;
}

/** Indonesian message for a failed navigator.geolocation request. */
export function geolocationErrorMessage(code: number | null): string {
  switch (code) {
    case 1:
      return "Izin lokasi ditolak. Izinkan akses lokasi di browser untuk memakai fitur ini.";
    case 2:
      return "Lokasi tidak dapat ditentukan saat ini.";
    case 3:
      return "Menentukan lokasi terlalu lama. Coba lagi.";
    default:
      return "Browser ini tidak mendukung penentuan lokasi.";
  }
}
