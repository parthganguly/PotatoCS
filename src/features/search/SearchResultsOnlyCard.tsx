import { resultsOnlyPassages } from "./searchModel";

// Deliberately not a SearchEvidenceCard: no citation numbers, no quote styling and no
// "verified" language, so retrieval output can never be read as answer support.
// The payload is durable persisted metadata, so it is normalized at runtime rather
// than trusted: an unusable payload renders nothing and bad rows are dropped.
export function SearchResultsOnlyCard({ results }: { results: unknown }) {
  const passages = resultsOnlyPassages(results);
  if (!passages.length) return null;
  return (
    <section
      aria-label="Retrieved passages, not verified answer support"
      className="max-w-[82%] rounded-md border border-dashed border-ink/25 bg-paper/40 px-3 py-2 text-xs text-ink/70"
    >
      <h3 className="font-semibold text-ink/80">Retrieved passages — not verified answer support</h3>
      <p className="mt-2 text-[11px] leading-5 text-ink/55">
        These passages were retrieved for your question. They were not selected or verified as support for an
        answer, so they carry no citation numbers.
      </p>
      <div className="mt-3 grid gap-3">
        {passages.map((item) => (
          <article className="rounded border border-ink/10 bg-white p-3" key={item.passage_id}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="font-semibold text-ink">{item.title}</p>
              <p className="text-[11px] uppercase tracking-wide text-ink/45">
                {originLabel(item.source_origin)} · not verified
              </p>
            </div>
            <p className="mt-2 whitespace-pre-wrap leading-5 text-ink/75">{item.text}</p>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink/50">
              {item.page_number ? <span>Page {item.page_number}</span> : null}
              {item.fetched_at > 0 && <span>Fetched {new Date(item.fetched_at).toLocaleString()}</span>}
              {item.canonical_url && (
                <a
                  className="break-all text-tide underline decoration-tide/30 underline-offset-2"
                  href={item.canonical_url}
                  rel="noreferrer"
                  target="_blank"
                >
                  Open source
                </a>
              )}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function originLabel(origin: string): string {
  if (origin === "cached_web") return "cached web";
  if (origin === "web") return "web";
  return "local Source";
}
