import { useEffect, useState } from "react";
import { Home } from "./pages/Home";
import { ReviewResults } from "./pages/ReviewResults";
import type { Report } from "./types";

type Page = "home" | "results";

export function App() {
  const [page, setPage] = useState<Page>("home");
  const [report, setReport] = useState<Report | null>(null);
  const [batch, setBatch] = useState(0);
  function resetBatch() {
    setReport(null);
    setPage("home");
    setBatch(current => current + 1);
  }
  useEffect(() => { window.scrollTo(0, 0); }, [page]);

  return <>
    {page === "home" && <Home key={batch} report={report} onReset={resetBatch} onComplete={(nextReport) => { setReport(nextReport); setPage("results"); }} />}
    {report && <div hidden={page !== "results"}>
      <ReviewResults report={report} onBack={() => setPage("home")} />
    </div>}
  </>;
}
