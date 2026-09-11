# Telegram processing: audit, contract, validation and deployment

## Architecture and audit findings

The Portal is `naraboxt-lara`, a Laravel/Filament application. Movie and episode `VideoSourcesRelationManager` classes already share source-management concerns. `VideoSource` is polymorphic through `sourceable_type/sourceable_id`; its `metadata` cast holds provider lifecycle state. `processing_job_id`, `file_size`, `duration_seconds`, storage identity, quality/format, primary/active flags, and health fields already exist. There is no standalone `fetch_status` column: provider fetch state lives in metadata. `telegram_imports` / `TelegramImport` are the legacy CDN ingest inbox; they are not the Telescope processing queue. Subtitles use their own polymorphic table and external VTT/SRT URLs.

Telescope already had a SQLite WAL queue in Teletyde, deterministic Portal job IDs, multipart upload checkpoints, and an HMAC callback outbox. `TelescopeImportService`, `TelebotClientService`, `/api/v1/teletyde/storage-events`, and scheduled `telescope:sync-video-sources` already formed its Portal boundary. It previously streamed original Telegram chunks straight into object storage without ffprobe or normalization. Its primary flag could be assigned before the final source was ready.

The other path, Tele-OB, calls existing `NbxVideoSourceService` and delegates to NBX. The watcher/legacy `/api/process` API uses `TelegramPipeWorker`, Telethon's authenticated MTProto client, and `CdnClient` upload/shared-path/signed-URL handoff. Existing `TelegramToContaboImportJob` handles legacy queued imports. NBX asset/backfill logic remains independent: Telescope registers its final object directly, without a duplicate NBX encode. Raw download-only and delegated NBX handoffs intentionally remain original-file workflows.

Legacy local `video_prepare` previously used extension and a 1 GB size cap to trigger video encoding, including compatible MKV. It mapped the first audio track without checking intended VJ/default audio. The new shared engine replaces that decision tree. Other findings addressed: filename collisions in legacy temp files; no staged local Telescope validation; premature object completion based only on size; cancellation of `to_thread` leaving its encoder alive; cleanup crossing the Telescope working-directory boundary; and retry callback identities shared between attempts.

Database audit was **read-only** through `/Applications/XAMPP/bin/mysql -u root`. The local `naraboxt-lara` schema was inspected with SHOW/DESCRIBE/SELECT. `movies`, `video_sources`, and `telegram_imports` each contained **zero rows**, so there were no representative production records to inspect. No live migrations, queue dispatches, media imports, or storage writes were executed for this work. PHP integration tests explicitly use SQLite `:memory:`.

## Compatibility contract

The web repo uses Vidstack/browser media and HLS.js (`app/watch/[id]/PlayerClient.tsx`) with external subtitle tracks. The mobile/TV Expo app uses Expo Video (`app/watch/[id].tsx`, `src/components/WatchPlayerLayer.tsx`), backed by AVPlayer/ExoPlayer. The universal profile therefore requires actual MP4 with AVC `avc1`, H.264 Baseline/Constrained Baseline/Main/High up to level 4.2, progressive 8-bit `yuv420p`, at most 1920×1080 and 60 fps, and AAC LC mono/stereo at 44.1/48 kHz when audio exists. Silent video remains silent. Wider/taller, rotated, interlaced, HDR, or otherwise incompatible streams use video conversion. Dimensions preserve display aspect ratio; smaller inputs are never enlarged. HDR compatibility conversion uses FFmpeg zscale/tonemap; missing required filters cause an explicit failure.

Automatic profiles preserve suitable frame rates; compression profiles have a 30 fps **ceiling**, not forced 30 fps. The measured 23.976 fps fixture stays approximately 23.976 fps (MKV timestamp quantization can slightly change the reported average). Output aspect ratio is checked within 2% for integer rounding. HEVC always needs video conversion for the universal contract, even when some clients can play it.

All standard universal outputs have Fast Start. Custom can explicitly disable progressive optimization, while keeping the codec contract; this is recorded in its options. Keep Original preserves the validated original container/streams and makes no universal-playback guarantee.

## Profiles and decision tree

| Mode | Height ceiling | RF default | Frame-rate ceiling | Behavior |
|---|---:|---:|---:|---|
| Auto | Universal 1080p limits | 23 if needed | Preserve, up to 60 | Cheapest safe operation |
| Fast MP4 Conversion | Universal limits | 23 if needed | Preserve, up to 60 | Copy compatible streams; convert only incompatible streams |
| 720p Balanced | 720 | 23 | 30 | Intentional constant-quality compression |
| 720p Compact | 720 | 26 | 30 | Smaller files, subject to content |
| 720p Extra Compact | 720 | 27 | 30 | More detail loss for storage savings |
| 1080p Balanced | 1080 | 23 | 30 | Intentional constant-quality compression |
| Keep Original | Original | Unused | Original | Validate original, preserve all streams |
| Custom | 480/720/1080 or universal limit | 23 | Optional 24/25/30/50/60 | Safe administrator-controlled fields |

RF accepts integers **18–30**; speed accepts `veryfast`, `faster`, `fast`; audio index accepts 0–255 or null. Unknown keys, argument strings, nonnumeric or boolean RF, and invalid enum values are rejected. Reference contract examples are in `contracts/processing-v1.json`; Laravel has the matching test fixture. RF controls quality, never an output-size promise.

1. Probe and validate source structure; Keep Original returns its original only when healthy.
2. Select requested absolute audio stream index, a sole audio track, or a unique non-commentary default. Ambiguity fails with available indices/languages/titles so an administrator can create a source with the intended VJ track. An arbitrary first language/commentary track is never silently substituted.
3. Explicit compression or incompatible video: `video_transcode` if audio can copy, otherwise `full_transcode`.
4. Compatible video, unsuitable audio: `audio_transcode`.
5. Compatible optimized MP4 with only intended streams: `passthrough`.
6. Compatible MP4 needing metadata relocation only: `faststart`.
7. Otherwise copy selected video/audio into MP4: `remux`.

A stream-copy/validation failure gets one compatibility video repair, acquiring the video-encoding slot first. Cancellation, timeout, disk shortage, and a failed full encode do not trigger repeated encoding loops. Size alone never selects a transcode. Embedded subtitles/data/attachments are omitted for universal output; external Portal subtitles are unchanged. No subtitle burning is selected.

## Inspection, commands, and validation

Every source and final file is inspected with this argument-array command (actual generated path substituted):

```text
ffprobe -v error -protocol_whitelist file,pipe
  -format_whitelist mov,matroska,webm,avi,mpeg,mpegts,flv,asf,ogg -print_format json
  -show_format -show_streams -show_packets -read_intervals %+3
  -show_entries packet=stream_index,pts_time,dts_time <local-file>
```

It captures container and actual MP4 brand, size, duration, bitrate, video codec/profile/tag/level, pixel format, width/height, display/sample aspect ratio, frame rate, rotation, field order/color transfer; all audio indices/codecs/profiles/rates/channels/layouts/languages/titles/default/commentary flags; and subtitle streams. Attached pictures are excluded as primary video. Packet PTS/DTS validity is **sampled over the first three seconds**, not represented as a complete decode scan. ffprobe has a 90-second timeout. Input protocol access is restricted to local file/pipe for FFmpeg/ffprobe, and a container-demuxer whitelist rejects playlists/reference formats before they can load other files.

FFmpeg maps the inspected video index and selected audio index deliberately, disables subtitles/data and inherited metadata/chapters, and uses `-c:v copy -c:a copy -tag:v avc1 -movflags +faststart -f mp4` for remux. Audio-only adds `-c:a aac -profile:a aac_low -b:a 160k -ac 2 -ar 48000` while video remains copy. `+genpts` and negative-timestamp normalization assist container repair; reported corrupt packets/unrepaired DTS are not accepted silently.

Compatibility video encoding uses `libx264`, RF via `-crf`, `yuv420p`, High/4.2, a bounded 20 Mbps/40 Mbps VBV, configured threads and a practical encoder preset. Geometry is derived from the source display aspect ratio and requested maximums, including rotation/SAR; filters normalize SAR and only cap excessive frame rates. Explicit compression uses **HandBrakeCLI**, x264 RF, speed, bounded threads/VBV, disabled auto-cropping, non-anamorphic aspect preservation, maximum dimensions, explicit audio selection/copy-AAC or AAC encode, no subtitles, peak frame rate, and `--optimize`. HDR/interlaced/rotated inputs use explicit FFmpeg filters instead. Actual CLI presets/AAC encoder support are discovered from the installed binary's `--preset-list`/`--help`; no unverified preset name is executed.

Validation requires a readable nontrivial file, video/dimensions, finite positive duration, retained audio when present, expected codec/pixel format/stream counts, timestamp sample, frame-rate and resolution limits, display aspect ratio, and Fast Start when requested. Container and video-stream duration must match source within `max(2, min(10, source_duration * 0.005))` seconds. This rejects severe truncation. MP4 box headers are scanned with seeks (including 64-bit boxes); `moov` must precede `mdat`. No multi-GB file is loaded into memory. Subprocess exit zero and plausible bytes alone are insufficient.

## Lifecycle, resources, recovery and storage

Stages include queued/resolving, downloading, inspecting, waiting for media, preparing MP4, converting audio, optimizing video, validating, uploading, verifying, ready/completed, failed and cancelled. UI labels are human-readable. Overall and stage progress, elapsed time and ETA are throttled (default 3 seconds); durable multipart checkpoints still commit each completed part. Results record actual source/output size, signed bytes/percentage saved (negative if larger), processing method/profile/duration, selected audio and full probe diagnostics.

Work resides under `TEMP_DIR/telescope/<sha256(job-id)>/`: `input.part`, `source.media`, `output.part.mp4`, `output.mp4`, `processing-receipt.json`, `.job.lock`. Telegram filenames never become local paths. Completed downloads are byte-count checked and renamed; output is probed, validated, fsynced and renamed before upload. Receipts bind source size/mtime, profile and contract version, and re-probe output before reuse. A SHA-256 digest binds multipart checkpoints to the exact final bytes; regenerated output cannot accidentally resume parts from a different encode. Partial downloads resume on a 4096-byte boundary; changed Telegram document identity invalidates partial bytes.

Admission and stage semaphores bound local work. Defaults: 4 admitted jobs, 2 downloads, 2 per channel, 2 inspections, 2 remux/audio operations, 1 video encode, 2 uploads. Encoding also shares the legacy worker's `MAX_CONCURRENT_TRANSCODES` limit. Cheap processing does not queue behind HandBrake. Admission/disk limits deliberately stop further downloads when the local spool is full.

Disk reservation accounts for source + output + overhead across admitted jobs (default 3× input plus a 2 GiB free reserve), subtracting bytes already on disk. Disk floors are checked during downloads and encoder execution. Capacity failures are visible and retryable. Software encoders default to two threads and niceness 10; Compose additionally limits the whole service to 2 CPUs/3 GiB (configurable). Compiled NVENC/QSV/VAAPI/VideoToolbox capabilities may be reported, but hardware is never selected automatically. Compiled support does not establish a usable GPU.

A database advisory lock is acquired **before recovery**; only one scheduler owns a state DB. Each job also has a flock inherited by its encoder child. After a parent crash, a replacement waits for that child to exit instead of launching a duplicate encoder. Graceful cancellation signals and joins the owning thread and terminates only that child's process group. No broad process killing is used. Restart requeues nonterminal jobs and preserves completed checkpoints. Network calls in boto3 threads are joined before releasing ownership. Incomplete remote multipart uploads should also have a provider lifecycle expiration as a crash backstop.

Retry after encode failure reuses source; retry after upload failure reuses validated local output and multipart parts; restart after object completion verifies the existing object before doing more work; callback failure retries only the durable HMAC outbox. Callback event identity includes the attempt, and Laravel rejects stale attempts. Terminal states missing their outbox enqueue are reconciled at startup. Success files clean up after callback delivery (or when no callback is configured); failed/cancelled files retain for 72 hours, then expire without touching active locks. If retained source files expire, a later manual retry must download again.

Existing `ObjectStorageManager` / `S3MultipartUploader` serve Contabo and R2. Target choice uses configured enabled/writable/capacity/priorities and in-process reservations; configured usage must still be kept current externally. The final actual container sets MIME; MP4 uses `video/mp4`, no Content-Encoding. HEAD checks ContentLength, MIME and encoding after multipart completion. Result URLs continue using each target's configured public URL. No storage credentials travel in the Portal contract.

## Production deployment

The Dockerfile installs FFmpeg (including ffprobe), `handbrake-cli`, curl and OpenSSL on `python:3.12-slim-bookworm`. It verifies binaries and lists HandBrake presets during build. Pin the resulting image digest in your production release system for exact rollback/reproducibility. Development testing used FFmpeg/ffprobe 8.1 and official HandBrakeCLI 1.11.2; Debian's packaged version can differ. Docker is not available on the audited Mac, so **the production image build and deployed-device playback have not been executed here**.

From the telebot repository, after setting the existing Telegram login/session, worker token, storage target and callback secrets in `.env`:

```bash
docker compose -f deploy/compose.yaml build --pull teletyde
docker compose -f deploy/compose.yaml up -d teletyde
docker compose -f deploy/compose.yaml exec teletyde ffmpeg -version
docker compose -f deploy/compose.yaml exec teletyde ffprobe -version
docker compose -f deploy/compose.yaml exec teletyde HandBrakeCLI --version
docker compose -f deploy/compose.yaml exec teletyde HandBrakeCLI --preset-list
curl --fail http://127.0.0.1:8765/health
curl --fail http://127.0.0.1:8765/api/health
```

The exact production process is **`python -m app.web`**, one process/ASGI worker. It includes queue dispatch, download, processing, upload, callback dispatch and retention cleanup. Separate stage processes are not needed. In Coolify keep that command and port 8765; mount persistent `/data`, with `DB_PATH=/data/telebot_state.db`, `TEMP_DIR=/data/temp`, `TG_SESSION_NAME=/data/narabox_telebot`. Bring the existing state/session/spool volume forward; do not replace it with a new empty volume during upgrade. Standalone Compose uses a named `teletyde-data` volume; migrate the existing files into it before switching an existing deployment. It binds HTTP only to localhost for a reverse proxy. Use the existing authenticated ingress for the dashboard and bearer-protected `/api/worker` routes.

Do **not** run `python main.py` against the same Telethon session as the web service. `main.py` remains a separate legacy watcher/NBX mode and does not run the Telescope scheduler. A watcher deployment, if needed, requires its own Telegram session and state; it is not required for Portal imports.

New environment controls are listed in `.env.example`: `TELESCOPE_MAX_DOWNLOADS`, `TELESCOPE_MAX_MEDIA_JOBS`, `TELESCOPE_MAX_REMUX_JOBS`, `TELESCOPE_MAX_INSPECTIONS`, `TELESCOPE_MAX_UPLOADS`, `TELESCOPE_DISK_RESERVE_MB`, `TELESCOPE_DISK_EXPANSION_FACTOR`, `TELESCOPE_FAILED_RETENTION_HOURS`, `TELESCOPE_PROGRESS_INTERVAL_SECONDS`, `HANDBRAKE_BINARY`, `MEDIA_ENCODE_THREADS`, `MEDIA_PROCESS_NICE`. Existing `MAX_CONCURRENT_TRANSCODES`, `TELESCOPE_MAX_ACTIVE_JOBS`, per-channel, multipart, callback, timeout, retry and storage settings remain. Compose accepts shell/Compose-env `TELETYDE_CPU_LIMIT` and `TELETYDE_MEMORY_LIMIT` overrides.

Portal files are uploaded manually, not pushed with telebot. After uploading the Laravel changes:

```bash
cd /path/to/naraboxt-lara
php artisan migrate:status
php artisan migrate --path=database/migrations/2026_09_07_120000_create_teletyde_storage_events_table.php --force
php artisan config:cache
php artisan view:clear
php artisan queue:restart
php artisan telescope:sync-video-sources --limit=100
```

No new Laravel processing migration was needed: source metadata and existing columns suffice. The **existing** callback-table migration above was present in the worktree but unapplied locally, and is required if absent in production. SQLite receives three additive JSON columns (`processing_json`, `checkpoint_json`, `processing_result_json`) automatically during initialization; no existing job rows are rewritten destructively.

Keep the existing Laravel scheduler cron:

```cron
* * * * * cd /path/to/naraboxt-lara && php artisan schedule:run >> /dev/null 2>&1
```

`routes/console.php` already polls Telescope every minute and runs the existing `contabo-imports`, `default`, and `communications` queues. Telescope itself does not require a Laravel encoding worker. Existing scheduled queue commands are unchanged, including `php artisan queue:work database --queue=contabo-imports --timeout=21600 --tries=1 --sleep=3 --stop-when-empty` for legacy imports.

## Test and benchmark evidence

Verification: the worker suite passed 75 tests; the Portal suites passed 19 tests / 92 assertions.

See `docs/testing/media-benchmark.json` for per-input/output containers, codecs, dimensions, duration, bytes, copied/encoded decisions, wall/processing time, savings and validation. The source is a real existing 127,165,314-byte 106.92-second 1920×1080 H.264 Main/AAC local clip; variants were generated in isolated temporary directories. Synthetic portrait and raw-video fixtures are explicitly labeled. No original local media was overwritten.

| Test | Method | Wall seconds | Input bytes | Output bytes |
|---|---|---:|---:|---:|
| Full local clip as compatible MKV | Remux, video/audio copy | 2.229 | 127,163,961 | 127,176,406 |
| Same MKV, 720p Compact RF26 | HandBrake video transcode, AAC copy | 30.566 | 127,163,961 | 14,162,465 |
| Already optimized MP4 | Passthrough | 2.109 | 127,165,314 | 127,165,314 |
| H.264 + AC3 excerpt | Audio transcode, video copy | 2.757 | 14,758,700 | 14,657,456 |
| HEVC 10-bit excerpt | Video transcode, AAC copy | 2.240 | 188,097 | 197,081 |
| Explicit VJ audio, multitrack MKV | Remux selected track | 2.413 | 15,428,308 | 14,949,539 |
| Synthetic portrait, silent | Remux | 2.374 | 948,299 | 948,675 |
| Synthetic 8-second raw 1080p MKV, >1 GB | HandBrake 720p Compact | 6.337 | 1,194,403,215 | 78,266 |

The full-clip remux was **13.7× faster** than its intentional 720p encode, including inspection/validation overhead. Remux grew the container slightly and preserved encoded streams. The clip's Compact saving was 88.86%; this is a measured clip result, not a movie guarantee. The raw synthetic file's extreme compression ratio is **not representative** of Telegram movies. HEVC normalization grew its small fixture 4.78%, illustrating compatibility taking precedence over size.

On the same real 12-second excerpt with RF26, `veryfast` took 7.524s / 1,562,743 bytes; `faster` 9.326s / 1,771,503 bytes; `fast` 10.517s / 1,822,657 bytes. This narrow benchmark supports `veryfast` as the ingestion default; it does not establish equal perceptual quality across speeds or predict production VPS throughput. Only VideoToolbox was compiled into this Mac's FFmpeg; no hardware encoder was used.

Ambiguous two-track input failed with indices/titles as intended; explicit VJ index 2 succeeded. Tests also cover subtitle omission, sampled timestamp validation, output duration rejection, safe profiles, download failure, disk reservations, encoder failure, output reuse on upload retry, remote-object restart recovery, job ownership locks, cancellation, callback attempts, profile parity, inactive/primary gating and legacy NBX behavior.

Reproduce without production services:

```bash
HANDBRAKE_BINARY=/path/to/HandBrakeCLI .venv/bin/python -m unittest discover -s tests -v
HANDBRAKE_BINARY=/path/to/HandBrakeCLI .venv/bin/python scripts/benchmark_media.py --source /path/to/local/clip.mp4 --report docs/testing/media-benchmark.json --large
cd /path/to/naraboxt-lara
DB_CONNECTION=sqlite DB_DATABASE=:memory: php artisan test --compact --filter='TelescopeImportTest|TelegramProcessingContractTest|NbxVideoSourceEditContractTest'
```

Known validation boundaries: no live Telegram download/Contabo/R2 upload was performed; network failures and S3 verification use isolated doubles, while media operations use real binaries. No iPhone/TV device playback session or production-container benchmark was available. ffprobe packet health is sampled, not a full movie decode; mid-file corruption outside the sample can require a later stricter audit. Ambiguous tracks need an explicit administrator choice. Keep Original may remain unplayable on some clients. These limits are not hidden by reporting a green file-size check.

Technical references: [FFmpeg stream copy](https://www.ffmpeg.org/ffmpeg.html), [MP4 Fast Start](https://ffmpeg.org/ffmpeg-formats.html), [HandBrakeCLI options](https://handbrake.fr/docs/en/latest/cli/command-line-reference.html). Local binary help and actual output probes were used to verify command behavior.

## Telebot files changed

- `.env.example`
- `Dockerfile`
- `README.md`
- `app/config.py`
- `app/db.py`
- `app/media_probe.py`
- `app/media_processing.py`
- `app/media_tools.py`
- `app/object_storage.py`
- `app/processing_profiles.py`
- `app/telegram_worker.py`
- `app/telescope.py`
- `app/video_prepare.py`
- `app/web.py`
- `contracts/processing-v1.json`
- `deploy/compose.yaml`
- `docs/deployment/03-coolify-docker.md`
- `docs/telegram-processing.md`
- `docs/testing/media-benchmark.json`
- `scripts/benchmark_media.py`
- `tests/test_download_safety.py`
- `tests/test_media_processing.py`
- `tests/test_staged_pipeline.py`
- `tests/test_telescope.py`
- `tests/test_web_and_video.py`

The Portal manual-upload manifest is `naraboxt-lara/docs/telegram-processing-deployment.md`. No new Laravel migration was created; only additive local SQLite schema evolution is included.
