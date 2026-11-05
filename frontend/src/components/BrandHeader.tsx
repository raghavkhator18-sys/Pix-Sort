import { useEffect, useRef, useState } from "react";
import { Info } from "./Icons";

export function BrandHeader() {
  const [open, setOpen] = useState(false);
  const infoRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function closeOnOutsideClick(event: MouseEvent) {
      if (!infoRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        (document.activeElement as HTMLElement | null)?.blur();
      }
    }
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, []);

  return (
    <header className="brand-header">
      <div className="brand-lockup">
        <img className="brand-logo" src="/pixsort-logo.svg" alt="" />
        <div>
          <div className="brand-name">PixSort</div>
          <p>Your best photos, thoughtfully selected.</p>
        </div>
      </div>
      <div className="info-control" ref={infoRef}>
        <button
          className="settings-button"
          aria-label="About PixSort"
          aria-expanded={open}
          aria-describedby="pixsort-info-tooltip"
          onClick={() => setOpen((current) => !current)}
        >
          <Info size={19} />
        </button>
        <div
          id="pixsort-info-tooltip"
          className={`info-tooltip${open ? " open" : ""}`}
          role="tooltip"
        >
          <strong>About PixSort</strong>
          <span>
            PixSort helps you identify duplicate and similar photos, review image
            quality, and choose which photos to keep. Your photos are analyzed
            locally. Nothing is deleted automatically.
          </span>
        </div>
      </div>
    </header>
  );
}
