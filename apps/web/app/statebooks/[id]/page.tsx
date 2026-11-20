import Link from "next/link";
import { BookScreen } from "@/components/statebook/BookScreen";

export const dynamic = "force-dynamic";

export default async function StateBookPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <>
      <div className="eg-crumbs">
        <Link href="/statebooks" className="eg-muted">Monthly rule books</Link>
        <span data-icon="chevron-right" data-size="14"></span> <b>Review</b>
      </div>
      <BookScreen id={id} />
    </>
  );
}
