// ============================================================================
// Config
// ============================================================================

export interface ConfigTimingDetection {
  enabled: boolean;
  mode: string;
  search_head_seconds: number;
  search_tail_seconds: number;
  min_support_episodes: number;
  frame_step_seconds: number;
  min_segment_seconds: number;
  max_segment_seconds: number;
  feature_sample_rate: number;
  feature_hop_length: number;
  consensus_min_similarity: number;
  pair_match_min_seconds: number;
  cache_enabled: boolean;
  cache_dir: string | null;
  detector_version: string;
  auto_cut_min_confidence: string;
}

export interface ConfigTimingProviders {
  anilibria_enabled: boolean;
  aniskip_enabled: boolean;
}

export interface ConfigEncoding {
  video_codec: string;
  preset: string;
  cq: number;
  segment_video_codec: string;
  segment_preset: string;
  segment_cut_mode: string;
  segment_max_render_seconds: number;
  segment_cq: number;
  segment_pixel_format: string;
  segment_audio_codec?: string;
  segment_audio_bitrate?: string;
  boundary_reencode_seconds?: number;
  audio_codec: string;
}

export interface ConfigDelivery {
  s3_enabled: boolean;
  s3_upload_video: boolean;
  s3_upload_timestamps: boolean;
  s3_upload_manifest: boolean;
  vk_enabled: boolean;
  vk_wall_post_enabled: boolean;
  vk_comment_enabled: boolean;
  vk_privacy_view: number;
  vk_comment_banner_path: string;
  vk_comment_template: string;
}

export interface ConfigCleanup {
  downloads: boolean;
  temp: boolean;
  output: boolean;
}

export interface ConfigProcessing {
  chunk_size_episodes: number;
}

export interface ConfigDefaults {
  output_dir: string;
  watermark_path: string;
  skip_types: string[];
  preferred_audio_language: string;
  cleanup: ConfigCleanup;
  processing: ConfigProcessing;
  timing_detection: ConfigTimingDetection;
  timing_providers: ConfigTimingProviders;
  delivery: ConfigDelivery;
  encoding: ConfigEncoding;
}

export interface ConfigAutomation {
  enabled: boolean;
  provider: string;
  jobs_path: string;
  completed_jobs_path: string;
  state_path: string;
  poll_limit: number;
  download_root: string;
  default_source_type: string;
}

export interface Config {
  defaults: ConfigDefaults;
  automation: ConfigAutomation;
}

// ============================================================================
// Job
// ============================================================================

export interface JobSource {
  type: "magnet" | "local";
  magnet?: string;
  input_dir?: string;
  download_dir?: string;
  variant_codec?: string;
  variant_label?: string;
}

export interface JobAutomation {
  provider?: string;
  release_id?: number;
  is_ongoing?: boolean;
  ongoing_progress_key?: string;
  publish_strategy?: string;
}

export interface Job {
  id?: number;
  title: string;
  title_ru?: string;
  mal_id?: number;
  season: number;
  episodes_range: string;
  processing_mode?: string;
  source: JobSource;
  output_dir?: string;
  watermark_path?: string;
  skip_types?: string[];
  preferred_audio_language?: string;
  encoding?: Partial<ConfigEncoding>;
  cleanup?: Partial<ConfigCleanup>;
  processing?: Partial<ConfigProcessing>;
  timing_detection?: Partial<ConfigTimingDetection>;
  timing_providers?: Partial<ConfigTimingProviders>;
  delivery?: Partial<ConfigDelivery>;
  automation?: JobAutomation;
}

// ============================================================================
// State (SQLite-backed, was state.json)
// ============================================================================

export interface EpisodeTrackingEntry {
  release_id: number;
  episode: number;
  queued_at?: string;
  completed_at?: string;
}

export interface BlacklistItem {
  release_id: number;
  title: string;
  title_ru?: string | null;
  season: number;
  added_at: string;
  source: string;
}

export interface SkippedItem {
  release_id?: number;
  alias?: string;
  title?: string;
  episodes: number[];
  reason: string;
  recorded_at: string;
}

export interface OngoingProgressEntry {
  has_full_publish: boolean;
  last_full_episode: number | null;
  last_full_range: string | null;
  updated_at: string;
}

// ============================================================================
// Pipeline: Episode processing
// ============================================================================

export interface EpisodeInfo {
  episode: number;
  path: string;
  duration: number;
}

export interface EpisodeFile {
  episode: number;
  path: string;
}

export interface ExcludedFile {
  episode?: number;
  path: string;
  reason: string;
}

export interface Interval {
  start: number;
  end: number;
}

export interface Segment {
  type: string;
  start: number;
  end: number;
  source?: string;
  confidence?: string;
}

export interface RemoveSegment extends Segment {
  source: string;
  confidence: string;
}

export interface KeepSegment {
  start: number;
  end: number;
}

export interface Subsegment {
  start: number;
  end: number;
  cut_mode: string;
}

export interface TypeInfo {
  source: string;
  confidence: string;
  interval: Interval | null;
  review_required: boolean;
  removed: boolean;
  reason: string | null;
  consensus_score: number | null;
  support_episode_count: number;
  reference_interval: Interval | null;
  cache_hit: boolean;
  match_strategy: string;
  reference_episode: number | null;
  reference_source: string;
  reference_similarity: number | null;
}

export interface TimingInfo {
  strategy: string;
  per_type: Record<string, PerTypeTimingInfo>;
  used_fallback: boolean;
  request_error: string | null;
  detector_error: string | null;
  confidence: string;
  reference_episodes: Record<string, number[]>;
  review_required: boolean;
  requested_episode_length?: number;
  fallback_from_episode_length?: number | null;
  request_urls?: {
    anilibria: string[];
    aniskip: string[];
  };
}

export interface PerTypeTimingInfo {
  source: string;
  confidence: string;
  interval: Interval | null;
  review_required: boolean;
  removed: boolean;
  reason: string | null;
  consensus_score: number | null;
  support_episode_count: number;
  reference_interval: Interval | null;
  cache_hit: boolean;
  match_strategy: string;
  reference_episode: number | null;
  reference_source: string;
  reference_similarity: number | null;
}

export interface SkipSummary {
  total_removed_seconds: number;
  warnings: string[];
  op?: boolean;
  op_source?: string;
  op_confidence?: string;
  ed?: boolean;
  ed_source?: string;
  ed_confidence?: string;
}

export interface KeptSegmentManifest {
  start: number;
  end: number;
  cut_mode: string;
}

export interface ManifestEpisode {
  episode: number;
  source_file: string;
  original_duration: number;
  cleaned_duration: number;
  segment_cut_mode: string;
  keyframe_aligned: boolean;
  boundary_reencode_seconds?: number;
  timing_info: TimingInfo;
  skip_summary: SkipSummary;
  removed_segments: RemoveSegment[];
  kept_segments: KeptSegmentManifest[];
}

export interface CompactManifestEpisode {
  episode: number;
  source_file: string;
  original_duration: number | null;
  cleaned_duration: number | null;
  removed_duration: number;
  segment_cut_mode: string;
  keyframe_aligned?: boolean;
  timing_info: CompactTimingInfo;
  skip_summary: SkipSummary | Record<string, unknown>;
}

export interface CompactPerTypeInfo {
  source: string;
  confidence: string;
  interval: Interval | null;
  removed: boolean;
  review_required: boolean;
  reason?: string;
  match_strategy?: string;
  reference_source?: string;
  reference_episode?: number;
  reference_similarity?: number | null;
}

export interface CompactTimingInfo {
  strategy: string | null;
  confidence: string | null;
  review_required: boolean;
  per_type: Record<string, CompactPerTypeInfo>;
  used_fallback?: boolean;
  request_error?: string;
  detector_error?: string;
  reference_episodes?: Record<string, number[]>;
}

export interface QualitySummary {
  episodes_count: number;
  episodes_with_warnings: number[];
  episodes_anilibria_only: number;
  episodes_anilibria_with_detector: number;
  episodes_aniskip_only: number;
  episodes_aniskip_with_detector: number;
  episodes_detector_only: number;
  episodes_manual_review: number;
  episodes_detector_completed_op_only: number;
  episodes_detector_completed_ed_only: number;
  episodes_detector_high: number;
  episodes_detector_medium: number;
  episodes_detector_low: number;
  episodes_detector_cache_hits: number;
  episodes_with_op_removed?: number;
  episodes_with_ed_removed?: number;
}

export interface Manifest {
  title: string;
  title_ru?: string;
  mal_id?: number;
  season: string;
  episodes_range: string;
  episodes_count: number;
  source: string;
  source_summary: {
    selected_episode_count: number;
    excluded_file_count: number;
  };
  timing_detection: {
    enabled: boolean;
    available: boolean;
    reason: string | null;
  };
  timing_sources_summary: {
    anilibria_available: boolean;
    aniskip_available: boolean;
    detector_available: boolean;
  };
  display_title: string;
  output_display_name: string;
  output_video: string;
  output_timestamps: string;
  delivery_summary: DeliverySummary;
  quality_summary: QualitySummary | Record<string, unknown>;
  episodes: CompactManifestEpisode[];
  processing?: Record<string, unknown>;
}

export interface JobResult {
  output_video: string;
  output_timestamps: string;
  output_manifest: string;
  delivery_summary: DeliverySummary;
  quality_summary: QualitySummary | Record<string, unknown>;
  output_display_name: string;
  timestamps_description: string;
}

// ============================================================================
// Delivery
// ============================================================================

export interface S3Summary {
  enabled: boolean;
  uploaded: boolean;
  error: string | null;
  uploaded_files: Record<string, string>;
}

export interface VkSummary {
  enabled: boolean;
  uploaded: boolean;
  video_uploaded: boolean;
  post_created: boolean;
  comment_created: boolean;
  error: string | null;
  video_title?: string;
  video_description?: string;
  video_id?: number;
  owner_id?: number;
  video_url?: string;
  video_group_id?: number;
  wall_group_id?: number;
  post_id?: number;
  comment_id?: number;
  comment_attachment?: string;
  errors_by_stage: Record<string, string>;
  post_mode?: string;
  post_message?: string;
}

export interface DeliverySummary {
  s3: S3Summary;
  vk: VkSummary;
}

// ============================================================================
// Detector
// ============================================================================

export interface DetectorInputs {
  aniskip_by_episode: Record<string, ProviderResult>;
  anilibria_by_episode: Record<string, ProviderResult>;
}

export interface DetectorConfig {
  enabled: boolean;
  mode: string;
  search_head_seconds: number;
  search_tail_seconds: number;
  min_support_episodes: number;
  frame_step_seconds: number;
  min_segment_seconds: number;
  max_segment_seconds: number;
  feature_sample_rate: number;
  feature_hop_length: number;
  consensus_min_similarity: number;
  pair_match_min_seconds: number;
  cache_enabled: boolean;
  cache_dir: string | null;
  detector_version: string;
  auto_cut_min_confidence: string;
}

export interface DetectorResult {
  found: boolean;
  source: string;
  confidence: string;
  start: number | null;
  end: number | null;
  review_required: boolean;
  reason: string | null;
  support_episode_count: number;
  consensus_score: number | null;
  reference_interval: Interval | null;
  cache_hit: boolean;
  match_strategy: string;
  reference_episode: number | null;
  reference_source: string;
  reference_similarity: number | null;
}

export interface DetectorContext {
  enabled: boolean;
  available: boolean;
  reason: string | null;
  config: DetectorConfig;
  results: Record<string, Record<number, DetectorResult>>;
  reference_episodes: Record<string, number[]>;
  reference_intervals: Record<string, Interval | null>;
  consensus_scores: Record<string, number | null>;
  zone_confidences: Record<string, string>;
  input_reference_episodes: Record<string, number[]>;
  analysis_dir: string;
  cache_key: string;
  cache_root: string;
}

// ============================================================================
// API responses
// ============================================================================

export interface ProviderResult {
  segments: Segment[];
  request_error: string | null;
  request_urls: string[];
  provider: string;
}

export interface AniSkipSegment {
  type: string;
  start: number;
  end: number;
  source: string;
  confidence: string;
}

export interface AniSkipResult {
  segments: AniSkipSegment[];
  per_type_sources: Record<string, string>;
  used_fallback: boolean;
  request_error: string | null;
  requested_episode_length: number;
  fallback_from_episode_length: number | null;
  request_urls: string[];
  provider: string;
}

export interface AniLibriaResult {
  segments: Segment[];
  request_error: string | null;
  request_urls: string[];
  provider: string;
}

export interface ReleaseName {
  ru?: string;
  en?: string;
  english?: string;
  alternative?: string;
  main?: string;
}

export interface ReleasePayload {
  id?: number;
  release_id?: number;
  alias?: string;
  title?: string;
  name?: ReleaseName;
  names?: ReleaseName;
  season_number?: number;
  seasonNumber?: number;
  is_ongoing?: boolean;
  fresh_at?: string;
  updated_at?: string;
  created_at?: string;
  episodes?: ReleaseEpisode[];
  torrents?: ReleaseTorrent[] | Record<string, ReleaseTorrent[]>;
  torrent?: ReleaseTorrent;
  external_ids?: Record<string, unknown>;
  externalIds?: Record<string, unknown>;
  codes?: Record<string, unknown>;
  player?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  qualities?: unknown;
  quality?: unknown;
  variants?: unknown;
  versions?: unknown;
  [key: string]: unknown;
}

export interface ReleaseEpisode {
  number?: number;
  episode?: number;
  ordinal?: number;
  skips?: {
    op?: SkipInterval;
    opening?: SkipInterval;
    ed?: SkipInterval;
    ending?: SkipInterval;
  };
  [key: string]: unknown;
}

export interface SkipInterval {
  start?: number;
  from?: number;
  startTime?: number;
  start_time?: number;
  end?: number;
  to?: number;
  stop?: number;
  endTime?: number;
  end_time?: number;
}

export interface ReleaseTorrent {
  codec?: string;
  video_codec?: string;
  videoCodec?: string;
  label?: string;
  title?: string;
  name?: string;
  quality_label?: string;
  qualityLabel?: string;
  resolution?: string;
  quality?: string;
  video_quality?: string;
  videoQuality?: string;
  magnet?: string;
  [key: string]: unknown;
}

export interface ReleaseVariant {
  codec: string;
  resolution?: string | null;
  magnet: string;
  available_episodes: number[];
  label?: string | null;
}

export interface ReleaseDetails {
  release: ReleasePayload;
  request_url: string;
}

export interface RecentReleases {
  releases: ReleasePayload[];
  request_urls: string[];
}

// ============================================================================
// Runtime (was runtime_status.json + runtime_errors.json)
// ============================================================================

export interface QueueProgress {
  current_job_index: number;
  total_jobs: number;
  jobs_processed: number;
  jobs_failed: number;
}

export interface CurrentJobRuntime {
  title?: string;
  title_ru?: string;
  season?: number;
  episodes_range?: string;
  stage: string;
  started_at?: string;
  current_episode?: number | null;
  total_episodes?: number | null;
  current_episode_file?: string | null;
  current_chunk_index?: number | null;
  total_chunks?: number | null;
  current_chunk_episode_range?: string | null;
}

export interface LastRunRuntime {
  status: string;
  finished_at: string;
  title?: string;
  title_ru?: string;
  season?: number;
  episodes_range?: string;
  stage: string;
  current_episode?: number | null;
  total_episodes?: number | null;
  jobs_processed: number;
  jobs_failed: number;
  started_at?: string;
}

export interface RuntimeStatus {
  schema_version: number;
  updated_at: string | null;
  run_status: string;
  run_started_at: string | null;
  run_finished_at: string | null;
  current_stage: string | null;
  queue_progress: QueueProgress;
  current_job: CurrentJobRuntime | null;
  last_run: LastRunRuntime | null;
}

export interface RuntimeErrorEntry {
  id: string;
  created_at: string;
  run_status: string;
  context: string;
  stage: string | null;
  title?: string;
  title_ru?: string;
  season?: number;
  episodes_range?: string;
  current_episode?: number | null;
  total_episodes?: number | null;
  message: string;
  error_type: string;
}

// ============================================================================
// Telegram bot
// ============================================================================

export interface TelegramState {
  schema_version: number;
  last_update_id: number | null;
  last_handled_at: number | null;
  pending_actions: Record<string, PendingAction>;
  jobs_pagination: Record<string, number>;
  notification_details: Record<string, NotificationDetails>;
}

export interface PendingAction {
  type: string;
  source: string;
  index: number;
  job_identity: string;
  created_at: string;
  job_snapshot?: Job;
  blacklist_item?: BlacklistItem;
}

export interface NotificationDetails {
  type: string;
  created_at: string;
  job: {
    title?: string;
    title_ru?: string;
    season?: number;
    episodes_range?: string;
  };
  quality_summary: QualitySummary | Record<string, unknown>;
  delivery_summary: DeliverySummary;
}

// ============================================================================
// Completed jobs
// ============================================================================

export interface CompletedJobEntry {
  status: string;
  completed_at: string;
  job: Job;
  output_display_name: string | null;
  output_video: string | null;
  output_timestamps: string | null;
  output_manifest: string | null;
  delivery_summary: DeliverySummary | Record<string, unknown>;
  partial_vk: boolean;
  completion_source?: string;
  completion_note?: string;
}

// ============================================================================
// Processing summary (returned by run_jobs)
// ============================================================================

export interface ProcessingSummary {
  jobs_found: number;
  jobs_processed: number;
  jobs_failed: number;
  jobs_skipped: number;
  failed_titles: string[];
}

// ============================================================================
// Discovery summary (returned by discover_jobs)
// ============================================================================

export interface DiscoverySummary {
  created_jobs: number;
  updated_jobs: number;
  skipped_items: number;
  queued_release_episodes: number;
  completed_release_episodes: number;
  blacklisted_releases: number;
  request_urls: string[];
  status?: string;
}

// ============================================================================
// Audio stream
// ============================================================================

export interface AudioStream {
  audio_index: number;
  stream_index: number;
  language: string | null;
  title: string | null;
  handler_name: string | null;
  is_default: boolean;
}

// ============================================================================
// Segment encoding (computed from job encoding config)
// ============================================================================

export interface SegmentEncoding {
  video_codec: string;
  preset: string;
  cq: number;
  audio_codec: string;
  audio_bitrate: string;
  pixel_format: string;
  cut_mode: string;
  boundary_reencode_seconds: number;
  max_render_seconds: number;
}
