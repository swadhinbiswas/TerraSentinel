import { useCallback, useEffect, useState } from "react";

/**
 * Paginated browse of one registered table.
 *
 * The table list comes from the server's catalog, so the client cannot request a name
 * outside it — the allowlist is enforced where the SQL is built, not here.
 */
interface TableOption {
  name: string;
  title: string;
  present: boolean;
  rows: number | null;
  hasRegion: boolean;
}

interface Page {
  table: string;
  columns: string[];
  rows: Record<string, unknown>[];
  total: number;
  limit: number;
  offset: number;
  error?: string;
}

const PAGE_SIZE = 50;

export default function ExplorerTable({ tables }: { tables: TableOption[] }) {
  const available = tables.filter((table) => table.present);
  const [table, setTable] = useState(available[0]?.name ?? "");
  const [offset, setOffset] = useState(0);
  const [region, setRegion] = useState("");
  const [sinceDays, setSinceDays] = useState("");
  const [page, setPage] = useState<Page | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!table) return;
    setBusy(true);
    try {
      const params = new URLSearchParams({ table, limit: String(PAGE_SIZE), offset: String(offset) });
      if (region) params.set("region", region);
      if (sinceDays) params.set("since", sinceDays);
      setPage((await (await fetch(`/api/explorer?${params}`)).json()) as Page);
    } finally {
      setBusy(false);
    }
  }, [table, offset, region, sinceDays]);

  useEffect(() => {
    void load();
  }, [load]);

  const selected = available.find((option) => option.name === table);
  const pageCount = page ? Math.max(1, Math.ceil(page.total / PAGE_SIZE)) : 1;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs">
          <span className="block text-[var(--color-muted)]">Table</span>
          <select
            value={table}
            onChange={(event) => {
              setTable(event.target.value);
              setOffset(0);
            }}
            className="mt-1 rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 font-mono text-xs"
          >
            {available.map((option) => (
              <option key={option.name} value={option.name}>
                {option.name} ({option.rows?.toLocaleString() ?? 0})
              </option>
            ))}
          </select>
        </label>

        {selected?.hasRegion && (
          <label className="text-xs">
            <span className="block text-[var(--color-muted)]">Region</span>
            <input
              value={region}
              onChange={(event) => {
                setRegion(event.target.value);
                setOffset(0);
              }}
              placeholder="e.g. iberia_fire"
              className="mt-1 w-40 rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 font-mono text-xs"
            />
          </label>
        )}

        <label className="text-xs">
          <span className="block text-[var(--color-muted)]">Since (days)</span>
          <input
            value={sinceDays}
            onChange={(event) => {
              setSinceDays(event.target.value);
              setOffset(0);
            }}
            placeholder="all"
            className="mt-1 w-24 rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 font-mono text-xs"
          />
        </label>

        <div className="ml-auto flex items-center gap-2 text-xs text-[var(--color-muted)]">
          <button
            type="button"
            disabled={offset === 0 || busy}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            className="rounded border border-[var(--color-border)] px-2 py-1 disabled:opacity-40"
          >
            prev
          </button>
          <span className="tabular-nums">
            page {currentPage} / {pageCount} · {page?.total.toLocaleString() ?? "—"} rows
          </span>
          <button
            type="button"
            disabled={currentPage >= pageCount || busy}
            onClick={() => setOffset(offset + PAGE_SIZE)}
            className="rounded border border-[var(--color-border)] px-2 py-1 disabled:opacity-40"
          >
            next
          </button>
        </div>
      </div>

      {page?.error && (
        <p className="mt-3 rounded border border-[var(--color-extreme)]/60 px-3 py-2 text-xs text-[var(--color-extreme)]">
          {page.error}
        </p>
      )}

      {page?.columns && (
        <div className="mt-3 max-h-[520px] overflow-auto rounded border border-[var(--color-border)]">
          <table className="w-full border-collapse text-xs">
            <thead className="sticky top-0 bg-[var(--color-surface)]">
              <tr>
                {page.columns.map((column) => (
                  <th key={column} className="whitespace-nowrap border-b border-[var(--color-border)] px-2 py-1.5 text-left font-mono font-medium text-[var(--color-muted)]">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {page.rows.map((row, index) => (
                <tr key={index} className="odd:bg-[color-mix(in_oklab,var(--color-surface)_60%,transparent)] hover:bg-[var(--color-surface)]">
                  {page.columns.map((column) => (
                    <td key={column} className="whitespace-nowrap border-b border-[var(--color-border)]/50 px-2 py-1 font-mono tabular-nums">
                      {row[column] === null ? <span className="text-[var(--color-muted)]">null</span> : String(row[column])}
                    </td>
                  ))}
                </tr>
              ))}
              {page.rows.length === 0 && (
                <tr>
                  <td colSpan={page.columns.length} className="px-2 py-6 text-center text-[var(--color-muted)]">
                    No rows match these filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
