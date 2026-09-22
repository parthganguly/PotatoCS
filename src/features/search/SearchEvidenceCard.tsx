import type { SearchEvidence } from "../../tauri";

export function SearchEvidenceCard({ evidence }: { evidence: SearchEvidence[] }) {
  if (!evidence.length) return null;
  return (
    <details className="max-w-[82%] rounded-md border border-tide/20 bg-white px-3 py-2 text-xs text-ink/70">
      <summary className="cursor-pointer font-semibold text-tide">
        {evidence.length} verified evidence quote{evidence.length === 1 ? "" : "s"}
      </summary>
      <p className="mt-2 text-[11px] leading-5 text-ink/55">
        “Verified” means the quote exists in the retained source text. It does not by itself prove that the answer’s interpretation is correct.
      </p>
      <div className="mt-3 grid gap-3">
        {evidence.map((item) => (
          <article className="rounded border border-ink/10 bg-paper/50 p-3" key={`${item.evidence_id}-${item.passage_id}`}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="font-semibold text-ink">[{item.citation_number}] {item.title}</p>
              <p className="text-[11px] uppercase tracking-wide text-ink/45">
                {originLabel(item.source_origin)} · {item.provenance_kind.replace(/_/g, " ")}
              </p>
            </div>
            <blockquote className="mt-2 border-l-2 border-gold/60 pl-3 leading-5 text-ink/75">
              “{item.exact_quote}”
            </blockquote>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink/50">
              {item.page_number && <span>Page {item.page_number}</span>}
              {item.fetched_at > 0 && <span>Fetched {new Date(item.fetched_at).toLocaleString()}</span>}
              {item.canonical_url && (
                <a className="break-all text-tide underline decoration-tide/30 underline-offset-2" href={item.canonical_url} rel="noreferrer" target="_blank">
                  Open source
                </a>
              )}
            </div>
          </article>
        ))}
      </div>
    </details>
  );
}

function originLabel(origin: string): string {
  if (origin === "cached_web") return "cached web";
  if (origin === "web") return "web";
  return "local Source";
}
