import { Footer } from "@/components/landing/footer";
import { Header } from "@/components/landing/header";
import { Hero } from "@/components/landing/hero";
import { CapabilityBentoSection } from "@/components/landing/sections/capability-bento-section";
import { EnterpriseCtaSection } from "@/components/landing/sections/enterprise-cta-section";
import { ExecutionLoopSection } from "@/components/landing/sections/execution-loop-section";
import { ScenariosSection } from "@/components/landing/sections/scenarios-section";
import { TechnologyArchitectureSection } from "@/components/landing/sections/technology-architecture-section";
import { TrendsSection } from "@/components/landing/sections/trends-section";
import { TrustSection } from "@/components/landing/sections/trust-section";
import { VisionSection } from "@/components/landing/sections/vision-section";

export default function LandingPage() {
  return (
    <div className="neumo-surface min-h-screen w-full overflow-x-clip text-[color:var(--neumo-text)]">
      <Header />
      <main className="flex w-full flex-col">
        <Hero />
        <VisionSection />
        <TrendsSection />
        <ScenariosSection />
        <TechnologyArchitectureSection />
        <ExecutionLoopSection />
        <CapabilityBentoSection />
        <TrustSection />
        <EnterpriseCtaSection />
      </main>
      <Footer />
    </div>
  );
}
