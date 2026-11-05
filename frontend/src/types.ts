export type ImageRecord = {
  id: string;
  path: string;
  filename: string;
  thumbnail: string | null;
  preview?: string | null;
  size_bytes?: number;
  width?: number | null;
  height?: number | null;
  format?: string | null;
  error?: string | null;
};
export type QualityResult = { path: string; photo_id?: string; warnings: string[]; sharpness?: number | null };
export type Recommendation = {
  category: string;
  paths: string[];
  preferred_path: string | null;
  reasons: string[];
  confidence: string;
  quality_scores?: Record<string, number>;
  removal_candidate_paths?: string[];
};
export type PhotoGroup = {
  id: string;
  type: "exact_duplicate" | "near_duplicate_candidate" | "possible_burst" | "similar_only";
  member_photo_ids: string[];
  member_paths: string[];
  similarity: string;
  keeper_photo_id: string | null;
  keeper_path: string | null;
  confidence: string;
  needs_review: boolean;
  recommended_keeper_id: string | null;
  recommendation_confidence: string;
  recommendation_reason: string;
  removal_candidate_ids: string[];
};
export type Report = {
  images: ImageRecord[];
  groups: PhotoGroup[];
  exact_duplicate_groups: string[][];
  near_duplicate_pairs: Array<Record<string, unknown>>;
  quality: QualityResult[];
  recommendations: Recommendation[];
  summary: { discovered: number; processed: number; errors: number };
  progress?: { percent: number; stage: string };
};
export type AnalysisJobStatus = {
  job_id: string;
  status: "queued" | "running" | "completed" | "cancelled" | "failed";
  progress_percent: number;
  processed_images: number;
  total_images: number;
  current_stage: string;
  results?: Report;
  error?: string;
};

export type GoogleDriveConfig = {
  configured: boolean;
  oauth_available: boolean;
  api_key_available: boolean;
  authorized: boolean;
};
