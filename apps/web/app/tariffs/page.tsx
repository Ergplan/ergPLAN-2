import { TariffScreen } from "@/components/tariffs/TariffScreen";

export const dynamic = "force-dynamic";

// The DISCOM tariffs tab (Stage 19): a DISCOM's tariff as the approved rule books hold it; drafts
// beside it for an analyst. ?d=<discom>&on=<date>&drafts=1 opens a given view.
export default async function TariffsPage({ searchParams }: { searchParams: Promise<{ d?: string; on?: string; drafts?: string }> }) {
  const q = await searchParams;
  return <TariffScreen initial={{ discom: q.d, on: q.on, drafts: q.drafts === "1" }} />;
}
