import { useEffect, useRef, useState } from "react";
import { AlertTriangle, FolderOpen, GoogleDrive, X } from "./Icons";
import {
  getGoogleAuthUrl,
  getGoogleDriveConfig,
  importFromGoogleDrive,
  listGoogleDriveFolders,
} from "../services/api";
import type { GoogleDriveConfig } from "../types";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  onImportStarted: (jobId: string) => void;
};

const DRIVE_URL_REGEX = /^https?:\/\/(?:drive|docs)\.google\.com\/(?:drive\/(?:u\/\d+\/)?folders\/|file\/d\/|open\?|folderview\?)[a-zA-Z0-9_-]+/;
const GENERAL_DRIVE_REGEX = /^https?:\/\/drive\.google\.com\/(?:drive\/(?:u\/\d+\/)?(?:my-drive|home|shared-with-me|recent|starred|trash|priority)|drive\/?$|$)/;
const RAW_ID_REGEX = /^[a-zA-Z0-9_-]{10,}$/;

export function GoogleDriveModal({ isOpen, onClose, onImportStarted }: Props) {
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState("Checking access...");
  const [error, setError] = useState<string | null>(null);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [config, setConfig] = useState<GoogleDriveConfig | null>(null);
  const [userFolders, setUserFolders] = useState<Array<{ id: string; name: string }>>([]);
  const [loadingFolders, setLoadingFolders] = useState(false);
  const authPopup = useRef<Window | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (authPopup.current?.closed) {
        authPopup.current = null;
        setLoading(false);
      }
    }, 500);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setNeedsAuth(false);
    getGoogleDriveConfig()
      .then((cfg) => {
        setConfig(cfg);
        if (cfg.authorized) {
          fetchUserFolders();
        }
      })
      .catch(() => {
        // Soft fail; will surface full message on submit if needed
      });
  }, [isOpen]);

  function fetchUserFolders() {
    setLoadingFolders(true);
    listGoogleDriveFolders()
      .then((folders) => {
        setUserFolders(folders);
        setLoadingFolders(false);
      })
      .catch(() => {
        setLoadingFolders(false);
      });
  }

  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      if (!isOpen || !authPopup.current || event.source !== authPopup.current) return;
      if (event.data?.type === "GOOGLE_AUTH_SUCCESS") {
        authPopup.current = null;
        setNeedsAuth(false);
        setError(null);
        setLoading(true);
        setLoadingMessage("Authorized! Connecting & importing...");
        setConfig((prev) => (prev ? { ...prev, authorized: true } : null));
        fetchUserFolders();
        // Automatically proceed with import if URL is present and not general
        if (url.trim() && !GENERAL_DRIVE_REGEX.test(url.trim())) {
          startImport(url.trim());
        } else {
          setLoading(false);
        }
      } else if (event.data?.type === "GOOGLE_AUTH_ERROR") {
        authPopup.current = null;
        setLoading(false);
        setError(
          event.data.error === "access_denied"
            ? "Google Drive authorization was cancelled. Please try again."
            : `Authorization failed: ${event.data.error || "Unknown error"}`
        );
      }
    }
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [url, isOpen]);

  if (!isOpen) return null;

  function validateUrl(input: string): string | null {
    const trimmed = input.trim();
    if (!trimmed) {
      return "Please enter a valid Google Drive link.";
    }
    if (GENERAL_DRIVE_REGEX.test(trimmed)) {
      return "This link points to your general Google Drive page and cannot identify which photos should be analyzed. Please open the specific folder or photo in Google Drive and copy its link, or select an existing folder below.";
    }
    if (!DRIVE_URL_REGEX.test(trimmed) && !RAW_ID_REGEX.test(trimmed)) {
      return "Unsupported Google Drive link format. Please provide a link to a file or folder.";
    }
    return null;
  }

  async function startImport(targetUrl: string) {
    setLoading(true);
    setLoadingMessage("Checking access and starting import...");
    setError(null);
    setNeedsAuth(false);

    try {
      const response = await importFromGoogleDrive(targetUrl);
      setLoading(false);
      onImportStarted(response.job_id);
      onClose();
    } catch (err: unknown) {
      setLoading(false);
      const apiErr = err as Error & { status?: number; code?: string };
      const status = apiErr.status;
      const code = apiErr.code;
      const message = apiErr.message || "Failed to import from Google Drive.";

      if (code === "GENERAL_DRIVE_LINK") {
        setNeedsAuth(true);
        setError(
          "This link points to your general Google Drive page rather than a specific folder or file. Please open the specific folder or photo in Google Drive and copy its link, or choose from your folders below."
        );
      } else if (status === 403 || code === "AUTH_REQUIRED") {
        setNeedsAuth(true);
        setError(
          "PixSort couldn't access this Google Drive folder. The folder may be private, or authorization may be required. Connect your Google Drive account or check your existing access permissions, then try again. You do not need to make your photos public."
        );
      } else if (status === 503 || code === "CONFIG_MISSING") {
        setError(
          "Google Drive API credentials are not configured on this server. Please configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (or GOOGLE_API_KEY) in the backend environment."
        );
      } else if (status === 404 || code === "NOT_FOUND") {
        setError("The requested Google Drive file or folder could not be found.");
      } else if (status === 429 || code === "RATE_LIMITED") {
        setError("Google Drive API rate limit reached or service temporarily unavailable. Please try again later.");
      } else {
        setError(message);
      }
    }
  }

  async function handleImportClick() {
    const validationError = validateUrl(url);
    if (validationError) {
      setError(validationError);
      if (GENERAL_DRIVE_REGEX.test(url.trim())) {
        setNeedsAuth(true);
      }
      return;
    }
    await startImport(url.trim());
  }

  async function handleAuthorizeOAuth() {
    setLoading(true);
    setLoadingMessage("Opening Google authorization...");
    setError(null);

    try {
      const auth = await getGoogleAuthUrl();
      const popup = window.open(
        auth.url,
        "pixsort_google_oauth",
        "width=520,height=640,menubar=no,toolbar=no,status=no"
      );
      if (!popup) {
        setError("Pop-up blocked. Please allow pop-ups for this site to connect your Google account.");
        setLoading(false);
        return;
      }
      authPopup.current = popup;
      setLoadingMessage("Waiting for Google authorization...");
    } catch (err: unknown) {
      setLoading(false);
      const message = err instanceof Error ? err.message : "Failed to start Google authorization.";
      setError(message);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose} role="dialog" aria-modal="true" aria-labelledby="gdrive-modal-title">
      <div className="gdrive-modal" onClick={(e) => e.stopPropagation()}>
        <div className="gdrive-modal-header">
          <div className="gdrive-modal-title-lockup">
            <span className="gdrive-icon-badge">
              <GoogleDrive size={22} color="#2E7D32" strokeWidth={1.8} />
            </span>
            <h2 id="gdrive-modal-title">Import from Google Drive</h2>
          </div>
          <button type="button" className="modal-close-button" onClick={onClose} aria-label="Close modal">
            <X size={18} />
          </button>
        </div>

        <p className="gdrive-modal-description">
          Import photos from a Google Drive file or folder.
        </p>

        <div className="gdrive-modal-body">
          <label htmlFor="gdrive-url-input" className="gdrive-input-label">
            Google Drive link
          </label>
          <input
            id="gdrive-url-input"
            type="url"
            className={`gdrive-input ${error ? "has-error" : ""}`}
            placeholder="Paste your Google Drive link"
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              if (error) setError(null);
            }}
            disabled={loading}
            autoFocus
          />

          {config?.authorized && (userFolders.length > 0 || loadingFolders) && (
            <div className="gdrive-folder-picker">
              <label htmlFor="gdrive-folder-dropdown">
                <FolderOpen size={14} /> Or select an existing folder from your Drive:
              </label>
              {loadingFolders ? (
                <span className="gdrive-picker-loading">Loading folders...</span>
              ) : (
                <select
                  id="gdrive-folder-dropdown"
                  className="gdrive-folder-select"
                  defaultValue=""
                  onChange={(e) => {
                    if (e.target.value) {
                      setUrl(`https://drive.google.com/drive/folders/${e.target.value}`);
                      setError(null);
                    }
                  }}
                >
                  <option value="" disabled>Choose an existing folder...</option>
                  {userFolders.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.name}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          {loading && (
            <div className="gdrive-loading-status" aria-live="polite">
              <span className="spinner" />
              <span>{loadingMessage}</span>
            </div>
          )}

          {error && (
            <div className="gdrive-modal-error" role="alert">
              <AlertTriangle size={18} />
              <div className="gdrive-modal-error-content">
                <span>{error}</span>
                {needsAuth && config?.oauth_available && !config.authorized && (
                  <div className="gdrive-auth-action">
                    <button
                      type="button"
                      className="primary-button oauth-connect-button"
                      onClick={handleAuthorizeOAuth}
                    >
                      <GoogleDrive size={16} /> Connect Google Account
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="gdrive-modal-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={onClose}
            disabled={loading}
          >
            Cancel
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={handleImportClick}
            disabled={loading || !url.trim()}
          >
            {loading ? "Connecting..." : "Import Photos"}
          </button>
        </div>
      </div>
    </div>
  );
}
