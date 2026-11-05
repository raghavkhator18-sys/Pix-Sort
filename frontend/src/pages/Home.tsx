import { useEffect, useRef, useState } from "react";
import { BrandHeader } from "../components/BrandHeader";
import { GoogleDriveModal } from "../components/GoogleDriveModal";
import { cancelAnalysisJob, getAnalysisJob, startAnalysisJob } from "../services/api";
import type { Report } from "../types";
import { ArrowRight, FolderOpen, Images, Copy, GoogleDrive } from "../components/Icons";

type Props = { report: Report | null; onComplete: (report: Report) => void; onReset: () => void };

export function Home({ report, onComplete, onReset }: Props) {
  const [files, setFiles] = useState<FileList | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState({ percent: 0, stage: "Loading images", processed: 0, total: 0 });
  const [status, setStatus] = useState("Your photos stay on this computer.");
  const [driveModalOpen, setDriveModalOpen] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const jobId = useRef<string | null>(null);
  const pollTimer = useRef<number | null>(null);
  const generation = useRef(0);
  const selectedCount = files?.length ?? 0;

  useEffect(() => () => {
    generation.current += 1;
    controller.current?.abort();
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
  }, []);

  function stopPolling() {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }

  async function pollJob(currentGeneration: number) {
    if (!jobId.current || !controller.current || currentGeneration !== generation.current) return;
    try {
      const update = await getAnalysisJob(jobId.current, controller.current.signal);
      if (currentGeneration !== generation.current) return;
      console.debug("[PixSort] Analysis job update", update);
      setProgress((current) => ({
        percent: Math.max(current.percent, Math.min(100, update.progress_percent)),
        stage: update.current_stage,
        processed: update.processed_images,
        total: update.total_images,
      }));
      if (update.status === "completed" && update.results) {
        onComplete(update.results);
        setStatus("Analysis complete.");
        setBusy(false);
        return;
      }
      if (update.status === "failed" || update.status === "cancelled") {
        setStatus(update.error ?? `Analysis ${update.status}.`);
        setBusy(false);
        return;
      }
      pollTimer.current = window.setTimeout(() => {
        pollTimer.current = null;
        void pollJob(currentGeneration);
      }, 750);
    } catch (error) {
      if (currentGeneration !== generation.current) return;
      console.error("[PixSort] Polling failed", error);
      if (error instanceof DOMException && error.name === "AbortError") {
        setStatus("Analysis cancelled.");
      } else {
        setStatus(error instanceof Error ? error.message : "Analysis failed.");
      }
      setBusy(false);
    }
  }

  async function analyze() {
    if (!files?.length) return;
    const currentGeneration = ++generation.current;
    controller.current = new AbortController();
    setBusy(true);
    setProgress({ percent: 0, stage: "Loading images", processed: 0, total: files.length });
    setStatus("Analyzing photos locally...");
    console.info("[PixSort] Starting analysis job", { fileCount: files.length });
    try {
      // Retain the response so cancellation can also stop a job created during upload.
      const started = await startAnalysisJob(files);
      if (currentGeneration !== generation.current) {
        await cancelAnalysisJob(started.job_id);
        return;
      }
      jobId.current = started.job_id;
      console.info("[PixSort] Analysis job started", started);
      await pollJob(currentGeneration);
    } catch (error) {
      if (currentGeneration !== generation.current) return;
      console.error("[PixSort] Analysis failed", error);
      if (error instanceof DOMException && error.name === "AbortError") {
        setStatus("Analysis cancelled.");
      } else {
        setStatus(error instanceof Error ? error.message : "Analysis failed.");
      }
      setBusy(false);
    }
  }

  function startDriveJob(driveJobId: string) {
    const currentGeneration = ++generation.current;
    controller.current = new AbortController();
    jobId.current = driveJobId;
    setBusy(true);
    setProgress({ percent: 0, stage: "Scanning Google Drive folder", processed: 0, total: 0 });
    setStatus("Importing photos from Google Drive...");
    pollJob(currentGeneration);
  }

  async function cancel() {
    const cancelledJobId = jobId.current;
    generation.current += 1;
    controller.current?.abort();
    stopPolling();
    controller.current = null;
    jobId.current = null;
    setBusy(false);
    setStatus("Analysis cancelled.");
    if (cancelledJobId) {
      try {
        await cancelAnalysisJob(cancelledJobId);
      } catch {
        // The local request is already cancelled; the next status request is no longer needed.
      }
    }
  }

  return (
    <main className="home-page">
      <BrandHeader />
      <section className="home-intro library-heading"><div>
        <h1>Your photo library</h1>
        <p>Preserve the moments.</p></div>
        <button className="reset-batch-button" disabled={!selectedCount && !report && !busy} onClick={() => { void cancel(); onReset(); }}>↺ Reset batch</button>
      </section>
      <section className="upload-card">
        <div className="upload-symbol"><Images size={42} strokeWidth={1.7} /></div>
        <h2>Select a folder or add photos</h2>
        <p>Start with photos from your computer or Google Drive.</p>
        <span className="format-note">JPG · PNG · WebP · BMP and other supported formats</span>
        <div className="upload-actions">
          <label className="primary-button"><Images size={18} /> Choose Photos<input type="file" accept="image/*" multiple onChange={(event) => setFiles(event.target.files)} /></label>
          <label className="secondary-button"><FolderOpen size={18} /> Choose Folder<input type="file" accept="image/*" multiple onChange={(event) => setFiles(event.target.files)} {...({ webkitdirectory: "", directory: "" } as Record<string, string>)} /></label>
        </div>
        <div className="upload-divider">
          <span>or</span>
        </div>
        <div className="upload-cloud-action">
          <button
            type="button"
            className="gdrive-button"
            disabled={busy}
            onClick={() => setDriveModalOpen(true)}
          >
            <GoogleDrive size={18} /> Import from Google Drive
          </button>
        </div>
        <div className="selection-line">{selectedCount ? `${selectedCount} photo${selectedCount === 1 ? "" : "s"} selected` : "No photos selected"}<span>Nothing is deleted automatically</span></div>
      </section>
      {busy && (
        <div className="progress-card">
          <div>
            <strong>{progress.stage}</strong>
            <span>
              {progress.stage.toLowerCase().includes("download")
                ? `Downloaded ${progress.processed} of ${progress.total} photos · ${progress.percent}% complete.`
                : progress.stage.toLowerCase().includes("scan") || progress.stage.toLowerCase().includes("connect")
                ? `${progress.percent}% complete.`
                : progress.stage.toLowerCase().includes("prepar")
                ? `Preparing images · ${progress.percent}% complete.`
                : progress.total > 0
                ? `Analyzed ${progress.processed} of ${progress.total} photos · ${progress.percent}% complete.`
                : `${progress.percent}% complete.`}
            </span>
          </div>
          <button onClick={cancel}>Cancel</button>
          <div className="progress-track"><i style={{ width: `${progress.percent}%` }} /></div>
        </div>
      )}
      {!busy && <button className="analyze-button" disabled={!selectedCount} onClick={analyze}>Analyze photos <ArrowRight size={17} /></button>}
      <section className="home-stats">
        <div><span className="stat-icon green"><Images size={22} /></span><div><strong>{report?.summary.processed ?? 0}</strong><span>Photos analyzed</span></div></div>
        <div><span className="stat-icon purple"><Copy size={22} /></span><div><strong>{report?.groups.filter((group) => group.type === "exact_duplicate" || group.type === "near_duplicate_candidate").length ?? 0}</strong><span>Duplicates detected</span></div></div>
      </section>
      <div className="view-results-wrap"><button className="view-results-button" disabled={busy || !report} onClick={() => report && onComplete(report)}>View results <ArrowRight size={17} /></button></div>
      <p className="privacy-note">{report ? "Counts reflect your latest local analysis." : "Displayed figures are placeholders until you analyze a photo collection."}</p>
      <p className="status" aria-live="polite">{status}</p>

      <GoogleDriveModal
        isOpen={driveModalOpen}
        onClose={() => setDriveModalOpen(false)}
        onImportStarted={startDriveJob}
      />
    </main>
  );
}
