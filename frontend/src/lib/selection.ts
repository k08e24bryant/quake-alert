/** A quake chosen with "Tampilkan di peta". */
export interface Selection {
  id: string;
  /** New on every click, so the map reacts again even to the same quake (e.g. reopens a
   * popup the user closed in between). An id alone would compare equal and do nothing. */
  seq: number;
}

export function select(previous: Selection | null, id: string): Selection {
  return { id, seq: (previous?.seq ?? 0) + 1 };
}
