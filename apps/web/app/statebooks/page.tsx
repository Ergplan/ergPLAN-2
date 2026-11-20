import { BookList } from "@/components/statebook/BookList";

export const dynamic = "force-dynamic";

export default async function StateBooksPage({ searchParams }: { searchParams: Promise<{ month?: string; status?: string }> }) {
  const q = await searchParams;
  return <BookList month={q.month} status={q.status} />;
}
