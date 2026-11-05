import { useEffect, useMemo, useRef, useState } from "react";
import { BrandHeader } from "../components/BrandHeader";
import type { ImageRecord, PhotoGroup, Report } from "../types";
import { ArrowLeft, Check, Copy, Star, X } from "../components/Icons";
import { chooseKeeper, initialReview, isDuplicateGroup, qualityWarnings, selectionManifest } from "../services/review";

type Filter = "all" | "duplicates" | "similar" | "quality";
const bytes = (value = 0) => value < 1024 * 1024 ? `${Math.round(value / 1024)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`;
const warningLabels: Record<string, string> = {
  possibly_blurry: "Possibly blurry", possibly_underexposed: "Underexposed",
  possibly_overexposed: "Overexposed", low_resolution: "Low resolution",
};
const groupLabel = (group: PhotoGroup) => group.type === "exact_duplicate" ? "Exact duplicates"
  : group.type === "near_duplicate_candidate" ? "Near duplicates" : group.type === "possible_burst" ? "Burst shots" : "Similar shots";

export function ReviewResults({ report, onBack }: { report: Report; onBack: () => void }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [review, setReview] = useState(() => initialReview(report));
  const [comparison, setComparison] = useState<{ groupId: string; ids: [string, string] } | null>(null);
  const [zoom, setZoom] = useState(1);
  const [notice, setNotice] = useState("");
  const actionBarRef = useRef<HTMLElement | null>(null);
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [actionBarHeight, setActionBarHeight] = useState(0);
  useEffect(() => { setReview(initialReview(report)); setComparison(null); setNotice(""); }, [report]);
  useEffect(() => {
    const bar = actionBarRef.current;
    if (!bar) return;
    const update = () => setActionBarHeight(bar.getBoundingClientRect().height);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(bar);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (comparison && !dialogRef.current?.open) dialogRef.current?.showModal();
    if (!comparison) dialogRef.current?.close();
  }, [comparison]);

  const imageMap = useMemo(() => new Map(report.images.map(image => [image.id, image])), [report]);
  const groupedIds = new Set(report.groups.flatMap(group => group.member_photo_ids));
  const duplicateGroups = report.groups.filter(isDuplicateGroup);
  const similarGroups = report.groups.filter(group => !isDuplicateGroup(group));
  const qualityImages = report.images.filter(image => qualityWarnings(report, image).length || image.error);
  const shownGroups = filter === "all" ? report.groups : filter === "duplicates" ? duplicateGroups : filter === "similar" ? similarGroups : [];
  const standaloneImages = filter === "quality" ? qualityImages : filter === "all" ? report.images.filter(image => !groupedIds.has(image.id)) : [];
  const renderedCount = shownGroups.reduce((count, group) => count + group.member_photo_ids.length, 0) + standaloneImages.length;
  const compareGroup = report.groups.find(group => group.id === comparison?.groupId);

  function toggle(image: ImageRecord) {
    const group = report.groups.find(item => item.member_photo_ids.includes(image.id));
    if (group && review.keepers.get(group.id) === image.id) return;
    setReview(current => {
      const selected = new Set(current.selected);
      if (selected.has(image.id)) selected.delete(image.id); else selected.add(image.id);
      return { ...current, selected };
    });
    setNotice("");
  }
  function keep(group: PhotoGroup, id: string) {
    setReview(current => chooseKeeper(current, group, id));
    setNotice("");
  }
  function orderedImages(group: PhotoGroup) {
    const preferred = review.keepers.get(group.id) ?? group.recommended_keeper_id;
    return group.member_photo_ids.map(id => imageMap.get(id)).filter((image): image is ImageRecord => !!image)
      .sort((a, b) => Number(b.id === preferred) - Number(a.id === preferred));
  }
  function openCompare(group: PhotoGroup) {
    const images = orderedImages(group);
    if (images.length < 2) return;
    setZoom(1);
    setComparison({ groupId: group.id, ids: [images[0].id, images[1].id] });
  }
  function exportSelection() {
    const blob = new Blob([JSON.stringify(selectionManifest(report, review), null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = "pixsort-removal-selection.json";
    document.body.appendChild(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    setNotice("Selection exported. Your original photos have not been changed.");
  }
  function imageCard(image: ImageRecord) {
    const group = report.groups.find(item => item.member_photo_ids.includes(image.id));
    const keeper = !!group && review.keepers.get(group.id) === image.id;
    const suggested = !!group && !review.keepers.has(group.id) && group.recommended_keeper_id === image.id;
    const chosen = review.selected.has(image.id);
    const warnings = qualityWarnings(report, image);
    return <article className={`result-photo ${chosen ? "selected" : ""} ${keeper || suggested ? "keeper-photo" : ""}`} key={image.id}>
      <button className="check-button" disabled={keeper} aria-pressed={chosen} aria-label={keeper ? `Keeping ${image.filename}` : `${chosen ? "Deselect" : "Select"} ${image.filename} for removal`} onClick={() => toggle(image)}>{keeper ? <Star size={14} /> : chosen ? <Check size={14} /> : ""}</button>
      {image.thumbnail ? <img src={image.thumbnail} alt={image.filename} loading="lazy" /> : <div className="image-placeholder">No preview available</div>}
      <div className="photo-details"><strong title={image.filename}>{image.filename}</strong><span>{image.width && image.height ? `${image.width} × ${image.height}` : "Dimensions unavailable"} · {bytes(image.size_bytes)}</span>
        <div className="quality-tags">{keeper ? <em className="keep">Keeper</em> : suggested ? <em className="suggested">Recommended keeper</em> : group?.needs_review ? <em>Review this moment</em> : null}{chosen && <em className="removal-tag">Selected for removal</em>}{warnings.map(warning => <em key={warning}>{warningLabels[warning] ?? warning}</em>)}{image.error && <em>Could not analyze</em>}</div>
        {group && <button className="keeper-button" disabled={keeper} onClick={() => keep(group, image.id)}><Star size={13} />{keeper ? "Keeping this photo" : "Keep this photo"}</button>}
      </div>
    </article>;
  }

  return <main className="results-page" style={{ paddingBottom: `calc(${actionBarHeight}px + 24px)` }}>
    <BrandHeader />
    <button className="back-button" onClick={onBack}><ArrowLeft size={16} /> Back to library</button>
    <section className="results-intro"><span className="eyebrow">YOUR PHOTO REVIEW</span><h1>Keep the moments that matter.</h1><p>Compare related shots, choose your keepers, and review the extra copies.</p></section>
    <section className="review-summary" aria-label="Analysis summary"><div><strong>{report.images.length}</strong><span>Photos analyzed</span></div><div><strong>{duplicateGroups.length}</strong><span>Duplicate groups</span></div><div><strong>{similarGroups.length}</strong><span>Similar groups</span></div><div><strong>{qualityImages.length}</strong><span>Quality issues</span></div></section>
    <p className="review-guidance"><Star size={16} /> Extra copies are preselected only in confident duplicate groups. Different poses and moments stay yours to choose.</p>
    <nav className="filter-row" aria-label="Photo filters">{([
      ["all", `All photos (${report.images.length})`], ["duplicates", `Duplicates (${duplicateGroups.length} ${duplicateGroups.length === 1 ? "group" : "groups"})`],
      ["similar", `Similar shots (${similarGroups.length} ${similarGroups.length === 1 ? "group" : "groups"})`], ["quality", `Quality issues (${qualityImages.length})`],
    ] as [Filter, string][]).map(([value, label]) => <button aria-pressed={filter === value} className={filter === value ? "filter active" : "filter"} key={value} onClick={() => setFilter(value)}>{label}</button>)}<span className="shown-count">{renderedCount} photos shown</span></nav>
    {shownGroups.map((group, index) => <section className="duplicate-group" key={group.id}>
      <header><div><span className="group-icon"><Copy size={20} /></span><strong>{groupLabel(group)} · {index + 1}</strong><small>{group.member_photo_ids.length} photos · {group.needs_review ? "Your review needed" : "Suggested selection ready"}</small></div><button className="compare-button" onClick={() => openCompare(group)}>Compare photos</button></header>
      <p className="recommendation-note">{group.type === "exact_duplicate" ? "These files are identical, so their quality is the same. Keep whichever copy you prefer." : group.recommendation_reason || "Compare sharpness, resolution, and the moment before choosing a keeper."}</p>
      <div className="group-photos">{orderedImages(group).map(imageCard)}</div>
    </section>)}
    {standaloneImages.length > 0 && <section className="photo-section"><h2>{filter === "quality" ? "Photos to review" : "Other photos"} <span>{standaloneImages.length}</span></h2><div className="photo-grid">{standaloneImages.map(imageCard)}</div></section>}
    {!renderedCount && <p className="empty-state">No photos match this filter.</p>}
    <footer ref={actionBarRef} className="action-bar"><div><strong>{review.selected.size} photo{review.selected.size === 1 ? "" : "s"} selected for removal</strong><span>{bytes(report.images.filter(image => review.selected.has(image.id)).reduce((sum, image) => sum + (image.size_bytes ?? 0), 0))} in selected copies · Originals stay unchanged</span><span role="status">{notice}</span></div><button className="export-button" disabled={!review.selected.size} onClick={exportSelection}>Export removal selection ↓</button></footer>
    <dialog ref={dialogRef} className="compare-dialog" aria-labelledby="compare-title" onCancel={() => setComparison(null)} onClose={() => setComparison(null)}>
      <header><div><h2 id="compare-title">Compare your photos</h2><p>Reduced-resolution previews · Original dimensions shown below</p></div><button className="modal-close-button" aria-label="Close comparison" onClick={() => setComparison(null)}><X size={20} /></button></header>
      <label className="zoom-control">Preview zoom <input type="range" min="1" max="3" step=".25" value={zoom} onChange={event => setZoom(Number(event.target.value))} /> {Math.round(zoom * 100)}%</label>
      {comparison && compareGroup && <div className="comparison-pair">{comparison.ids.map((id, index) => {
        const image = imageMap.get(id);
        if (!image) return null;
        const keeper = review.keepers.get(compareGroup.id) === id;
        return <section key={index} className="comparison-photo"><label>Photo {index + 1}<select value={id} onChange={event => setComparison(current => {
          if (!current) return current;
          const ids: [string, string] = [...current.ids]; ids[index] = event.target.value;
          return { ...current, ids };
        })}>{compareGroup.member_photo_ids.map(photoId => <option key={photoId} value={photoId} disabled={photoId === comparison.ids[1 - index]}>{imageMap.get(photoId)?.filename ?? photoId}</option>)}</select></label>
          <div className="comparison-viewport">{image.preview || image.thumbnail ? <img src={image.preview || image.thumbnail!} alt={image.filename} style={{ width: `${zoom * 100}%`, height: `${zoom * 100}%`, maxWidth: "none" }} /> : <p>No preview available</p>}</div>
          <p>{image.width} × {image.height} · {bytes(image.size_bytes)}</p><div className="quality-tags">{qualityWarnings(report, image).map(warning => <em key={warning}>{warningLabels[warning] ?? warning}</em>)}</div>
          <button className="keeper-button" disabled={keeper} onClick={() => keep(compareGroup, id)}><Star size={14} /> {keeper ? "Keeping this photo" : "Keep this photo"}</button>
        </section>;
      })}</div>}
    </dialog>
  </main>;
}
