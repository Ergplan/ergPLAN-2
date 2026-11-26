import { Calculator } from "@/components/calculator/Calculator";

// The landed-cost calculator (Stage 19): open to a signed-out visitor only when the server allows it
// (ERGPLAN_PUBLIC_CALCULATOR); otherwise the API answers 401 and the screen asks to sign in.
export default function CalculatorPage() {
  return <Calculator />;
}
