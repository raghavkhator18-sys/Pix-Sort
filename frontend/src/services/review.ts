import type { ImageRecord, PhotoGroup, Report } from "../types";

export type ReviewState = { selected: Set<string>; keepers: Map<string, string> };
export const isDuplicateGroup = (group: PhotoGroup) =>
  group.type === "exact_duplicate" || group.type === "near_duplicate_candidate";

export function initialReview(report: Report): ReviewState {
  const selected = new Set<string>();
  const keepers = new Map<string, string>();
  for (const group of report.groups) {
    const keeper = group.keeper_photo_id;
    if (isDuplicateGroup(group) && !group.needs_review && keeper && group.member_photo_ids.includes(keeper)) {
      keepers.set(group.id, keeper);
      group.removal_candidate_ids.forEach(id => {
        if (id !== keeper && group.member_photo_ids.includes(id)) selected.add(id);
      });
    }
  }
  return { selected, keepers };
}

export function chooseKeeper(state: ReviewState, group: PhotoGroup, id: string): ReviewState {
  if (!group.member_photo_ids.includes(id)) return state;
  const selected = new Set(state.selected);
  const previous = state.keepers.get(group.id);
  selected.delete(id);
  // Swap the retained copy only in groups already approved for automatic selection.
  if (isDuplicateGroup(group) && !group.needs_review && previous && previous !== id) selected.add(previous);
  return { selected, keepers: new Map(state.keepers).set(group.id, id) };
}

export function qualityWarnings(report: Report, image: ImageRecord): string[] {
  return report.quality.find(item => item.photo_id ? item.photo_id === image.id : item.path === image.path)?.warnings ?? [];
}

export function selectionManifest(report: Report, state: ReviewState) {
  return {
    application: "PixSort", version: 1, exported_at: new Date().toISOString(),
    action: "review_only", originals_modified: false,
    selected_for_removal: report.images.filter(image => state.selected.has(image.id)).map(image => ({
      id: image.id, filename: image.filename, path: image.path, size_bytes: image.size_bytes ?? 0,
    })),
    keepers: Array.from(state.keepers, ([group_id, photo_id]) => ({ group_id, photo_id })),
  };
}
