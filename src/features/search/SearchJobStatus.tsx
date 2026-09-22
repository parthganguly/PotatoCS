import type { JobRecord } from "../../tauri";
import { canCancelSearch, searchJobLabel } from "./searchModel";

export function SearchJobStatus(props: { job: JobRecord; onCancel: () => void }) {
  const label = searchJobLabel(props.job.state);
  return (
    <div className="max-w-[82%] rounded-md border border-tide/20 bg-white px-3 py-2 text-xs text-ink/65">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="font-semibold text-tide">{label}</p>
          <p className="mt-1">Bounded fetch, extraction, evidence selection, and exact-quote verification.</p>
        </div>
        {canCancelSearch(props.job.state) && (
          <button className="rounded border border-clay/25 px-2.5 py-1.5 font-medium text-clay hover:bg-[#fff3ee]" onClick={props.onCancel} type="button">
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
