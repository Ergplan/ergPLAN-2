"use client";

import { useCallback, useEffect, useState } from "react";
import { analystFetch } from "@/lib/analyst";
import type { BookDetail } from "@/lib/statebook";
import { BookView } from "./BookView";
import { ReviewBar } from "./ReviewBar";

// One rule book for review: the decision bar, then the book exactly as it will be issued.
export function BookScreen({ id }: { id: string }) {
  const [book, setBook] = useState<BookDetail | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try {
      setBook(await analystFetch<BookDetail>(`/statebooks/${id}`));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);
  useEffect(() => void load(), [load]);
  if (error) return <p role="alert" className="ep-empty">This rule book could not be opened ({error}). Drafts are visible to analysts only.</p>;
  if (!book) return <p className="ep-empty">Loading…</p>;
  return (
    <>
      <ReviewBar book={book} onChange={() => void load()} />
      <BookView book={book} />
    </>
  );
}
